"""Rejet (rejet.jp) metadata source — otome/drama CD maker's own works DB.

Unlike Gamers/Chil-Chil there are NO per-work detail pages: works render as
inline <article class="item"> blocks on paginated WordPress listing pages
(/works/?cat=N, /works/?s=query). Each block carries title (with CV credit),
cover (filename embeds the catalog number, e.g. REC-861_500.jpg), 発売日,
price, series category, and the blurb.

To give every work an addressable URL anyway, hits are keyed with a
"#rejet=<catalog-or-title-hash>" fragment appended to the page URL;
fetch_by_url re-fetches that page and picks the matching block. A pasted
listing URL without a fragment only resolves when the page holds exactly
one work.

Search does NOT use WordPress `?s=` (it's literal: it tokenizes on spaces
and misses "日野聡" vs "日野 聡", abbreviations like "ディアラバ", and truncates
to one page of 10). Instead the whole maker catalog — a bounded ~1k works
across paginated /works/ listing pages — is fetched once (concurrently),
cached, and filtered locally with normalize_text/loose_match, so matching
is space/width/case-insensitive and complete. A small alias map bridges
fan-abbreviations that share no substring with the official title."""
import asyncio
import hashlib
import logging
import re
import time
from urllib.parse import quote, urldefrag, urljoin

import httpx
from bs4 import BeautifulSoup

from .base import (
    CV_RE,
    MetadataSource,
    SourceError,
    empty_metadata,
    loose_match,
    normalize_date,
)

logger = logging.getLogger(__name__)

BASE = "https://rejet.jp"

# Catalog number from cover filenames: .../2019/09/REC-861_500.jpg
_CATALOG_RE = re.compile(r"/([A-Z]{2,5}-\d+)[_.]")

# Total-works count printed on listing pages: "全968件 1〜10件目を表示"
_TOTAL_RE = re.compile(r"全\s*([\d,]+)\s*件")

# Fan-abbreviations that share no substring with the official title, so
# normalization alone can't bridge them. Key (as typed) -> official phrase
# fed to the matcher. Extend as needed.
SEARCH_ALIASES = {
    "ディアラバ": "DIABOLIK LOVERS",
}

# In-process catalog cache (the maker's release history changes rarely).
# The full ~1k-work scan is slow (~25s — rejet.jp serves ~1.3s/page), so a
# cold search serves a fast WordPress `?s=` result and primes the catalog in
# the background; once primed, searches are local, instant, and fully fuzzy.
_CATALOG_TTL = 6 * 3600          # seconds
_CATALOG_MAX_PAGES = 150         # safety cap (~1,500 works)
_CATALOG_CONCURRENCY = 8         # polite parallel page fetches
_catalog_cache: dict = {"works": None, "at": 0.0}
_prime_task: asyncio.Task | None = None


class RejetSource(MetadataSource):
    name = "rejet"
    label = "Rejet"
    url_example = "https://rejet.jp/works/?cat=41"
    supports_search = True
    _url_re = re.compile(r"rejet\.jp/works/?", re.I)

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        page_url, fragment = urldefrag(url or "")
        if not self._url_re.search(page_url):
            raise SourceError(f"{self.label}: not a works URL: {url}")
        key = None
        m = re.match(r"rejet=(.+)", fragment or "")
        if m:
            key = m.group(1)
        html = await self._get(client, page_url)
        works = self.parse_page(html, page_url)
        if not works:
            raise SourceError(f"{self.label}: no works found on {page_url}")
        if key:
            for work_key, meta in works:
                if work_key == key:
                    return meta
            raise SourceError(f"{self.label}: work '{key}' not found on {page_url}")
        if len(works) == 1:
            return works[0][1]
        raise SourceError(
            f"{self.label}: listing holds {len(works)} works — search by title "
            "and pick one instead of pasting the listing URL"
        )

    async def search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        works = self._cached_catalog()
        if works is not None:
            return self._filter_works(works, query)
        # Cold/stale cache: prime the full catalog in the background (so the
        # next search is instant + fully fuzzy) and serve a fast WordPress
        # result right now instead of blocking ~25s on the scan.
        self._schedule_prime()
        return await self._wp_search(client, query)

    # ------------------------------------------------------------- catalog
    @staticmethod
    def _cached_catalog() -> list[tuple[str, dict]] | None:
        works = _catalog_cache["works"]
        if works is not None and time.monotonic() - _catalog_cache["at"] < _CATALOG_TTL:
            return works
        return None

    def _schedule_prime(self) -> None:
        """Kick off a one-shot background catalog load (idempotent: a prime
        already in flight is left to finish)."""
        global _prime_task
        if _prime_task is not None and not _prime_task.done():
            return
        try:
            _prime_task = asyncio.create_task(self._prime_catalog())
        except RuntimeError:
            _prime_task = None  # no running loop (e.g. unit test context)

    async def _prime_catalog(self) -> None:
        try:
            async with httpx.AsyncClient() as client:
                await self._load_catalog(client)
        except Exception as exc:  # never let a background task crash the loop
            logger.warning("[rejet] catalog prime failed: %s", exc)

    async def _load_catalog(
        self, client: httpx.AsyncClient
    ) -> list[tuple[str, dict]]:
        """All works across the paginated /works/ listing, cached in-process
        for _CATALOG_TTL. Page count comes from the printed 全N件 total so the
        remaining pages can be fetched concurrently in one fan-out."""
        cached = self._cached_catalog()
        if cached is not None:
            return cached

        first_url = f"{BASE}/works/"
        first_html = await self._get(client, first_url)
        works = list(self.parse_page(first_html, first_url))
        per_page = len(works) or 10

        m = _TOTAL_RE.search(first_html)
        if m:
            total = int(m.group(1).replace(",", ""))
            pages = min((total + per_page - 1) // per_page, _CATALOG_MAX_PAGES)
            if pages > 1:
                works.extend(await self._fetch_pages(client, range(2, pages + 1)))
        else:
            # No printed total: walk pages until one comes back empty.
            page = 2
            while page <= _CATALOG_MAX_PAGES:
                chunk = await self._fetch_page(client, page)
                if not chunk:
                    break
                works.extend(chunk)
                page += 1

        _catalog_cache["works"] = works
        _catalog_cache["at"] = time.monotonic()
        logger.info("[rejet] catalog cached: %d works", len(works))
        return works

    async def _wp_search(
        self, client: httpx.AsyncClient, query: str
    ) -> list[dict]:
        """Fast cold-path: WordPress `?s=` (literal, one page of ~10). Alias
        expansion still applies, so abbreviations resolve even before the
        full catalog finishes priming; spacing/partial misses are healed on
        the next search once the catalog is cached."""
        wp_query = self._expand_aliases(query).strip() or query
        page_url = f"{BASE}/works/?s={quote(wp_query)}"
        try:
            html = await self._get(client, page_url)
        except SourceError:
            return []
        return [self._to_hit(meta) for _key, meta in self.parse_page(html, page_url)]

    async def _fetch_pages(self, client, pages) -> list[tuple[str, dict]]:
        sem = asyncio.Semaphore(_CATALOG_CONCURRENCY)

        async def one(page: int):
            async with sem:
                return await self._fetch_page(client, page)

        results = await asyncio.gather(*(one(p) for p in pages))
        out: list[tuple[str, dict]] = []
        for chunk in results:
            out.extend(chunk)
        return out

    async def _fetch_page(self, client, page: int) -> list[tuple[str, dict]]:
        url = f"{BASE}/works/?paged={page}"
        try:
            return self.parse_page(await self._get(client, url), url)
        except SourceError as exc:
            logger.debug("[rejet] page %d skipped: %s", page, exc)
            return []

    # ------------------------------------------------------------- matching
    def _filter_works(
        self, works: list[tuple[str, dict]], query: str
    ) -> list[dict]:
        """Local loose match over title + series + cast + blurb. Pure, so it
        is unit-tested against the listing fixture without network."""
        expanded = self._expand_aliases(query)
        hits = []
        for _key, meta in works:
            haystacks = (
                meta.get("title") or "",
                meta.get("series") or "",
                " ".join(meta.get("seiyuu") or []),
                meta.get("description") or "",
            )
            if loose_match(expanded, *haystacks):
                hits.append(self._to_hit(meta))
        return hits

    @staticmethod
    def _expand_aliases(query: str) -> str:
        """Substitute known fan-abbreviations with their official phrase so
        the matcher can find them (replace, not append — appending would AND
        the un-findable abbreviation into the requirement)."""
        out = query or ""
        for alias, official in SEARCH_ALIASES.items():
            if alias in out:
                out = out.replace(alias, f" {official} ")
        return out

    def _to_hit(self, meta: dict) -> dict:
        return {
            "source": self.name,
            "title": meta["title"],
            "url": meta["source_url"],
            "thumbnail": meta["cover_url"],
            "release_date": meta["release_date"],
            "price": meta["price"],
            "category": meta["series"] or meta["extra"].get("type"),
        }

    # ------------------------------------------------------------- parse
    def parse_page(self, html: str, page_url: str) -> list[tuple[str, dict]]:
        """All works on a listing/search page as (key, normalized-meta)."""
        soup = BeautifulSoup(html, "html.parser")
        works: list[tuple[str, dict]] = []
        for article in soup.select("article.item"):
            h2 = article.select_one(".information h2")
            if not h2 or not h2.get_text(strip=True):
                continue
            meta = empty_metadata(self.name, page_url)
            meta["title"] = h2.get_text(strip=True)
            meta["maker"] = "Rejet"

            img = article.select_one("img.worksimg[src]")
            if img:
                # Site still serves http:// asset URLs; normalize.
                meta["cover_url"] = urljoin(BASE, img["src"]).replace(
                    "http://rejet.jp", "https://rejet.jp"
                )
                cm = _CATALOG_RE.search(meta["cover_url"])
                if cm:
                    meta["catalog_number"] = cm.group(1)

            date = article.select_one(".information p.date")
            if date:
                meta["release_date"] = normalize_date(date.get_text(strip=True))

            # Yes, the site's CSS class really is "plice".
            price = article.select_one(".information p.plice")
            if price:
                text = price.get_text(strip=True)
                text = re.sub(r"^価格\s*[:：]\s*", "", text)
                if text and "円" not in text:
                    text = re.sub(r"^([\d,]+)", r"\1円", text)
                meta["price"] = text or None

            cate = article.select_one(".information p.cate a[rel='category']") \
                or article.select_one(".information p.cate a")
            if cate and cate.get_text(strip=True):
                meta["series"] = cate.get_text(strip=True)

            text_div = article.select_one(".information .text")
            if text_div:
                desc = text_div.get_text("\n", strip=True).replace("\xa0", "").strip()
                meta["description"] = desc or None

            for cv in CV_RE.findall(meta["title"]):
                cv = cv.strip()
                if cv and cv not in meta["seiyuu"]:
                    meta["seiyuu"].append(cv)

            type_el = article.select_one(".information p.type")
            if type_el and type_el.get_text(strip=True):
                meta["extra"]["type"] = type_el.get_text(strip=True)
            official = article.select_one(".images .links a[href]")
            if official:
                meta["extra"]["official_site"] = official["href"]

            key = meta["catalog_number"] or "t-" + hashlib.md5(
                meta["title"].encode("utf-8")
            ).hexdigest()[:10]
            meta["source_url"] = f"{page_url}#rejet={key}"
            works.append((key, meta))
        return works
