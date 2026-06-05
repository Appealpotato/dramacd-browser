"""Scrape anime-sharing.com thread listings and diff DLsite codes against the local DramaCD DB.

Usage:
    python scrape_anime_sharing.py --config as_config.json
    python scrape_anime_sharing.py --forum-url "https://www.anime-sharing.com/forum/hentai-audio.123/" --max-pages 42

Auth:
    Easiest: export your browser cookie header (Network tab -> any request to anime-sharing.com ->
    copy the entire `Cookie:` header value) and put it in config.json under "cookie", or pass via
    AS_COOKIE env var.

Output:
    CSV with columns: code, in_db, in_ignored, thread_title, thread_url, last_post, age_days, likely_dead
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import httpx
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
# Script lives at dramacd-browser/scripts/dead link scraper/, so app dir is two levels up.
APP_DIR = SCRIPT_DIR.parent.parent
DEFAULT_DB_PATH = APP_DIR / "data" / "library.db"
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "as_config.json"
DEFAULT_OUTPUT_PATH = SCRIPT_DIR / "as_missing.csv"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Mirror dramacd-browser/scanner.py code patterns so DLJ-/DLB-/DMJ-/vst variants normalize the same.
CODE_PATTERNS = [
    re.compile(r"(RJ|BJ|VJ)\s*[-_]?\s*(\d{6,10})", re.IGNORECASE),
    re.compile(r"(DLJ|DLB|DMJ)\s*[-_]?\s*(\d{6,10})", re.IGNORECASE),
    re.compile(r"RE-ESC[-_]?(\d{6,10})", re.IGNORECASE),
    re.compile(r"(vst)\s*[-_]?\s*(\d{6,10})", re.IGNORECASE),
]
# Bare-number fallback only triggers if no prefixed code matched. Looks for 6-8 digit runs near
# bracket/paren boundaries to avoid grabbing year/episode numbers from titles.
BARE_PATTERN = re.compile(r"(?<!\d)(\d{6,8})(?!\d)")


def normalize_code(prefix: str | None, number: str) -> str:
    if prefix is None:
        return f"RJ{number}"
    p = prefix.upper()
    if p in ("RJ", "BJ", "VJ"):
        return f"{p}{number}"
    if p == "DLB":
        return f"BJ{number}"
    return f"RJ{number}"  # DLJ, DMJ, VST -> RJ


def extract_codes(text: str) -> list[str]:
    """Return a deduplicated list of normalized codes found in text."""
    seen: list[str] = []
    seen_set: set[str] = set()
    matched_prefixed = False
    for pat in CODE_PATTERNS:
        for m in pat.finditer(text):
            matched_prefixed = True
            groups = m.groups()
            if len(groups) == 1:
                code = normalize_code(None, groups[0])
            else:
                code = normalize_code(groups[0], groups[1])
            if code not in seen_set:
                seen_set.add(code)
                seen.append(code)
    if not matched_prefixed:
        for m in BARE_PATTERN.finditer(text):
            code = f"RJ{m.group(1)}"
            if code not in seen_set:
                seen_set.add(code)
                seen.append(code)
    return seen


@dataclass
class Thread:
    title: str
    url: str
    last_post: str | None = None  # ISO 8601 if parseable
    raw_codes: list[str] = field(default_factory=list)


def load_db_codes(db_path: Path) -> tuple[set[str], set[str]]:
    if not db_path.exists():
        print(f"[warn] DB not found at {db_path}; proceeding with empty sets", file=sys.stderr)
        return set(), set()
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("SELECT product_code FROM items WHERE product_code IS NOT NULL")
        in_db = {row[0].upper() for row in cur.fetchall() if row[0]}
        try:
            cur = conn.execute("SELECT code FROM ignored_codes")
            ignored = {row[0].upper() for row in cur.fetchall() if row[0]}
        except sqlite3.OperationalError:
            ignored = set()
    finally:
        conn.close()
    return in_db, ignored


def page_url(base_url: str, page: int) -> str:
    """XenForo accepts both /forum/foo.123/page-N and ?page=N. Prefer path style."""
    if page <= 1:
        return base_url
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/")
    # Strip any existing page-N segment
    path = re.sub(r"/page-\d+$", "", path)
    new_path = f"{path}/page-{page}"
    return urlunparse(parsed._replace(path=new_path))


def parse_threads(html: str, base_url: str) -> list[Thread]:
    soup = BeautifulSoup(html, "lxml")
    threads: list[Thread] = []

    # XenForo 2.x: div.structItem--thread
    for item in soup.select("div.structItem--thread"):
        title_a = item.select_one(".structItem-title a[data-tp-primary], .structItem-title a")
        if not title_a:
            continue
        title = title_a.get_text(" ", strip=True)
        href = title_a.get("href", "")
        url = urljoin(base_url, href)

        last_post_iso: str | None = None
        time_el = item.select_one(".structItem-cell--latest time")
        if time_el:
            dt = time_el.get("datetime") or time_el.get("data-time-string")
            if dt:
                last_post_iso = dt

        codes = extract_codes(title)
        threads.append(Thread(title=title, url=url, last_post=last_post_iso, raw_codes=codes))

    # Fallback for older XenForo 1.x markup
    if not threads:
        for li in soup.select("li.discussionListItem, li.structItem"):
            a = li.select_one("h3.title a, .structItem-title a")
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            url = urljoin(base_url, a.get("href", ""))
            codes = extract_codes(title)
            threads.append(Thread(title=title, url=url, raw_codes=codes))

    return threads


def jittered(delay: float, jitter: float) -> float:
    """Return delay +/- jitter fraction. jitter=0.5 means ±50% around delay, floor 0.25s."""
    if jitter <= 0:
        return max(0.0, delay)
    spread = delay * jitter
    return max(0.25, random.uniform(delay - spread, delay + spread))


def fetch_pages(
    client: httpx.Client,
    forum_url: str,
    max_pages: int,
    delay: float,
    jitter: float,
) -> Iterable[Thread]:
    for page in range(1, max_pages + 1):
        url = page_url(forum_url, page)
        sleep_for = jittered(delay, jitter)
        print(f"[fetch] page {page}/{max_pages} (next sleep ~{sleep_for:.2f}s): {url}", file=sys.stderr)
        try:
            r = client.get(url, timeout=30.0)
        except httpx.HTTPError as e:
            print(f"[error] {url} -> {e}", file=sys.stderr)
            time.sleep(sleep_for)
            continue
        if r.status_code != 200:
            print(f"[warn] HTTP {r.status_code} for {url}", file=sys.stderr)
            if r.status_code in (403, 429):
                print("[warn] auth/rate-limit issue — check cookie or slow down", file=sys.stderr)
            time.sleep(sleep_for)
            continue
        threads = parse_threads(r.text, url)
        if not threads:
            print(f"[warn] no threads parsed on page {page} — selectors may be stale or auth required", file=sys.stderr)
        for t in threads:
            yield t
        time.sleep(sleep_for)


def age_days(last_post_iso: str | None) -> int | None:
    if not last_post_iso:
        return None
    # XenForo emits ISO 8601 with timezone, e.g. "2024-08-12T03:14:11+0000"
    s = last_post_iso.strip()
    # Normalize tz so fromisoformat works
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(last_post_iso, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (now - dt).days)


def likely_dead(age: int | None) -> bool:
    return age is not None and age >= 365


def load_config(path: Path | None) -> dict:
    if path and path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="JSON config path")
    ap.add_argument("--forum-url", action="append", help="Forum section URL (repeatable). Overrides config.")
    ap.add_argument("--max-pages", type=int, help="Pages per forum section (default 42)")
    ap.add_argument("--delay", type=float, help="Base seconds between requests (default 1.5)")
    ap.add_argument("--jitter", type=float, help="Random jitter as fraction of delay, default 0.5 (= ±50%%). 0 disables.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="library.db path")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH, help="CSV output path")
    ap.add_argument("--cookie", help="Cookie header string. Also reads AS_COOKIE env var.")
    ap.add_argument("--include-known", action="store_true", help="Include codes already in DB in output")
    args = ap.parse_args()

    config = load_config(args.config)
    forum_urls: list[str] = args.forum_url or config.get("forum_urls") or []
    max_pages: int = args.max_pages or config.get("max_pages", 42)
    delay: float = args.delay if args.delay is not None else config.get("delay", 1.5)
    jitter: float = args.jitter if args.jitter is not None else config.get("jitter", 0.5)
    cookie: str = args.cookie or os.environ.get("AS_COOKIE", "") or config.get("cookie", "")

    if not forum_urls:
        print("error: no forum URLs provided. Use --forum-url or set forum_urls in config.", file=sys.stderr)
        return 2
    if not cookie:
        print("[warn] no cookie provided — public-only thread visibility, role-gated content will be missing", file=sys.stderr)

    in_db, ignored = load_db_codes(args.db)
    print(f"[db] {len(in_db)} codes in items, {len(ignored)} in ignored_codes", file=sys.stderr)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie

    # code -> earliest/best Thread (prefer most-recent last_post for "alive" indication)
    code_to_thread: dict[str, Thread] = {}

    with httpx.Client(headers=headers, follow_redirects=True) as client:
        for forum_url in forum_urls:
            print(f"\n[section] {forum_url}", file=sys.stderr)
            for thread in fetch_pages(client, forum_url, max_pages, delay, jitter):
                for code in thread.raw_codes:
                    existing = code_to_thread.get(code)
                    # Prefer the thread with the most recent last_post (= more likely live link)
                    if existing is None:
                        code_to_thread[code] = thread
                    else:
                        new_age = age_days(thread.last_post)
                        old_age = age_days(existing.last_post)
                        if new_age is not None and (old_age is None or new_age < old_age):
                            code_to_thread[code] = thread

    # Build rows
    rows = []
    for code, thread in sorted(code_to_thread.items()):
        in_db_flag = code.upper() in in_db
        in_ignored_flag = code.upper() in ignored
        if in_db_flag and not args.include_known:
            continue
        a = age_days(thread.last_post)
        rows.append({
            "code": code,
            "in_db": int(in_db_flag),
            "in_ignored": int(in_ignored_flag),
            "thread_title": thread.title,
            "thread_url": thread.url,
            "last_post": thread.last_post or "",
            "age_days": a if a is not None else "",
            "likely_dead": int(likely_dead(a)),
        })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "code", "in_db", "in_ignored", "thread_title", "thread_url",
            "last_post", "age_days", "likely_dead",
        ])
        writer.writeheader()
        writer.writerows(rows)

    total = len(code_to_thread)
    missing = sum(1 for c in code_to_thread if c.upper() not in in_db)
    print(f"\n[done] {total} unique codes seen, {missing} missing from DB -> {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
