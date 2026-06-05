"""Render a DLsite product as a thread title + BBCode post block.

Usage:
    python scripts/dlsite_template.py RJ210477
    python scripts/dlsite_template.py RJ210477 --tag-lang both --stdout
    python scripts/dlsite_template.py RJ210477 --password mypw \
        --gofile https://gofile.io/... --buzzheavier https://buzzheavier.com/...
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

# Make the project root importable when running as a script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from scraper import fetch_metadata_for_code


_HTML_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _normalize_cover_url(url: str | None) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url


def _clean_description(text: str | None) -> str:
    if not text:
        return ""
    s = _HTML_BR_RE.sub("\n", str(text))
    s = _HTML_TAG_RE.sub("", s)
    # Collapse 3+ blank lines into 2.
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


def _tag_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    if isinstance(value, dict):
        return [str(t).strip() for t in value.values() if str(t).strip()]
    return []


def _dlsite_url(metadata: dict, fallback_code: str) -> str:
    raw = metadata.get("raw") or {}
    site_id = (raw.get("site_id") or "maniax").strip() or "maniax"
    code = (metadata.get("actual_code") or metadata.get("product_code") or fallback_code).strip()
    return f"https://www.dlsite.com/{site_id}/work/=/product_id/{code}.html"


def _split_code(code: str) -> tuple[str, str]:
    """Split 'RJ210477' into ('RJ', '210477'). Returns ('', code) if no match."""
    m = re.match(r"^(RJ|BJ|VJ)(\d+)$", code, re.IGNORECASE)
    if m:
        return m.group(1).upper(), m.group(2)
    return "", code


async def fetch(code: str) -> dict | None:
    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"User-Agent": "Mozilla/5.0 (compatible; dramacd-template/1.0)"},
        follow_redirects=True,
    ) as client:
        metadata, error = await fetch_metadata_for_code(client, code)
        if not metadata:
            print(f"[error] failed to fetch {code}: {error or 'unknown'}", file=sys.stderr)
            return None
        return metadata


def _local_db_item(code: str) -> dict | None:
    """Pull translated metadata fields from the running app's SQLite DB if a
    row for this product exists. Returns None if the DB or row is missing."""
    import json
    import sqlite3
    db_path = ROOT / "data" / "library.db"
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT id, title, title_en, description_en, seiyuu, seiyuu_en FROM items WHERE product_code = ? LIMIT 1",
                (code,),
            )
            row = cur.fetchone()
    except sqlite3.Error as exc:
        print(f"[warn] local DB lookup failed: {exc}", file=sys.stderr)
        return None
    if not row:
        return None

    def _parse_name_list(raw):
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return []

    return {
        "id": int(row["id"]),
        "title": (str(row["title"] or "").strip() or None),
        "title_en": (str(row["title_en"] or "").strip() or None),
        "description_en": (str(row["description_en"] or "").strip() or None),
        "seiyuu": _parse_name_list(row["seiyuu"]),
        "seiyuu_en": _parse_name_list(row["seiyuu_en"]),
    }


def _render_tracklist(item_id: int | None, *, code: str, title_ja: str, title_en: str) -> str:
    """Render the tracklist block via the app's existing grouping helpers.
    Returns "" silently if the item or its tracks aren't in the DB yet —
    the rest of the post still renders, just without a tracklist."""
    if not item_id:
        return ""
    try:
        import database as db
        from pipeline.package_io import build_tracklist_text
    except Exception as exc:
        print(f"[warn] tracklist imports failed: {exc}", file=sys.stderr)
        return ""

    async def _fetch():
        groups = await db.get_pipeline_track_groups(item_id)
        if not groups:
            return ""
        item_stub = {
            "id": item_id,
            "product_code": code,
            "title": title_ja,
            "title_en": title_en if title_en and title_en != title_ja else "",
        }
        return build_tracklist_text(item_stub, groups)

    try:
        text = asyncio.run(_fetch())
    except Exception as exc:
        print(f"[warn] tracklist render failed: {exc}", file=sys.stderr)
        return ""
    if not text:
        return ""

    # Strip the header lines (code/title/title_en/blank) — we already show
    # those in the metadata block above the tracklist. Keep just the entries
    # and the totals footer.
    lines = text.splitlines()
    body_start = 0
    blanks_seen = 0
    for i, line in enumerate(lines):
        if not line.strip():
            blanks_seen += 1
            if blanks_seen == 1:
                body_start = i + 1
                break
    body = "\n".join(lines[body_start:]).strip("\n")

    # BBCode formatting: keep each track's JA + ↳ EN line glued together,
    # but separate distinct numbered entries with one blank line so the
    # spoiler reads as a list rather than a paragraph wall.
    out_lines: list[str] = []
    for raw in body.splitlines():
        if not raw:
            continue
        # A new numbered entry (e.g. "01.  ..."): if there's already
        # content above, insert a blank line first.
        if re.match(r"^\d+\.\s", raw) and out_lines:
            out_lines.append("")
        out_lines.append(raw)
    # Totals footer ("N tracks · ...") gets its own blank-line separator
    # since it doesn't start with "NN." but is logically a new section.
    rendered_lines: list[str] = []
    for line in out_lines:
        if (re.match(r"^\d+\s+tracks?\b", line.lstrip()) or "tracks ·" in line) and rendered_lines:
            rendered_lines.append("")
        rendered_lines.append(line)
    return "\n".join(rendered_lines).rstrip()


def render_title(metadata: dict, *, code: str) -> str:
    """Thread title line, e.g. ``✦OWN BOUGHT✦ [RJ210477] ナイトオブツインズ...``.
    Uses the JA title verbatim — the thread title format expects the original
    so search/lookup matches the product code."""
    title_ja = (metadata.get("title") or "").strip()
    actual_code = (metadata.get("actual_code") or metadata.get("product_code") or code).strip().upper()
    return f"✦OWN BOUGHT✦ [{actual_code}] {title_ja}"


def render_post(
    metadata: dict,
    *,
    code: str,
    tag_lang: str = "en",
    password: str = "{PASSWORD}",
    gofile_url: str = "{GOFILE_URL}",
    buzzheavier_url: str = "{buzzheavier_URL}",
) -> str:
    cover = _normalize_cover_url(metadata.get("cover_url"))
    title_ja = (metadata.get("title") or "").strip()
    circle = (metadata.get("circle") or "").strip()
    release = (metadata.get("release_date") or "").strip()
    if " " in release:
        release = release.split(" ", 1)[0]
    dlsite_url = _dlsite_url(metadata, code)
    description_ja = _clean_description(metadata.get("description"))

    # Prefer translations the user has saved in the app DB (these are usually
    # the result of the metadata-translation feature). Fall back to whatever
    # the scraper returned, but only if it's actually different from the JA
    # text — DLsite's API often echoes the JA copy under the en-US locale
    # when no real translation exists.
    local = _local_db_item(metadata.get("product_code") or code) or {}

    title_en = local.get("title_en")
    if not title_en:
        scraper_title_en = (metadata.get("title_en") or "").strip()
        if scraper_title_en and scraper_title_en != title_ja:
            title_en = scraper_title_en
    if not title_en:
        title_en = title_ja  # mirror the original when no real translation

    if local.get("description_en"):
        description_en = _clean_description(local["description_en"])
    else:
        description_en = _clean_description(metadata.get("description_en"))
        if description_en == description_ja:
            description_en = ""

    # Voice actors: Japanese names only (DB > scraper).
    def _scraper_names(field: str) -> list[str]:
        raw = metadata.get(field)
        if isinstance(raw, list):
            return [str(v).strip() for v in raw if str(v).strip()]
        return []

    seiyuu_names = local.get("seiyuu") or _scraper_names("seiyuu")
    seiyuu_str = ", ".join(seiyuu_names)

    if tag_lang == "ja":
        tags = _tag_list(metadata.get("tags"))
    elif tag_lang == "both":
        tags = _tag_list(metadata.get("tags_en")) or _tag_list(metadata.get("tags"))
        ja_tags = _tag_list(metadata.get("tags"))
        if ja_tags and ja_tags != tags:
            tags = tags + ja_tags
    else:
        tags = _tag_list(metadata.get("tags_en")) or _tag_list(metadata.get("tags"))
    tags_str = ", ".join(tags)

    en_block = description_en or "(no English description available)"

    # Tracklist (auto-fetched from the local DB; empty string if the item
    # hasn't been extracted yet, in which case the spoiler is omitted).
    tracklist_body = _render_tracklist(
        local.get("id"),
        code=metadata.get("product_code") or code,
        title_ja=title_ja,
        title_en=title_en,
    )
    tracklist_block = (
        "[spoiler=Tracklist]\n"
        "\n"
        f"{tracklist_body}\n"
        "\n"
        "[/spoiler]\n"
        "\n"
    ) if tracklist_body else ""

    return (
        f"[center][img]{cover}[/img]\n"
        "\n"
        "[color=#E8486A]⸻⸻⸻⸻⸻⸻⸻⸻⸻⸻[/color]\n"
        "\n"
        f"[quote][color=#E8486A][b]English title:[/b][/color] {title_en}\n"
        f"[color=#E8486A][b]Original title:[/b][/color] {title_ja}\n"
        "[color=#E8486A][b]Format:[/b][/color] MP3 + FLAC\n"
        f"[color=#E8486A][b]Voice Actor(s):[/b][/color] {seiyuu_str}\n"
        f"[color=#E8486A][b]Release date:[/b][/color] {release}\n"
        f"[color=#E8486A][b]Developer/Publisher:[/b][/color] {circle}\n"
        f"[color=#E8486A][b]DLsite:[/b][/color] {dlsite_url}\n"
        f"[color=#E8486A][b]Tags:[/b][/color] {tags_str}[/quote]\n"
        "\n"
        "[color=#E8486A]⸻⸻⸻⸻⸻⸻⸻⸻⸻⸻[/color]\n"
        "\n"
        "[color=#E8486A][b]✦ DOWNLOAD ✦[/b][/color]\n"
        "\n"
        f"[quote][HIDE][HIDEGROUP=55][color=#E8486A][b]Password:[/b][/color] [icode]{password}[/icode]\n"
        f"[url={gofile_url}]gofile[/url] | [url={buzzheavier_url}]buzzheavier[/url][/HIDEGROUP][/HIDE][/quote]\n"
        "\n"
        f"{tracklist_block}"
        "[spoiler=Description (EN)]\n"
        f"[left]{en_block}[/left]\n"
        "[/spoiler]\n"
        "[spoiler=説明 (JA)]\n"
        f"[left]{description_ja}[/left]\n"
        "[/spoiler]\n"
        "[/center]"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("code", help="DLsite product code, e.g. RJ210477")
    parser.add_argument("--out-dir", default=".", help="Directory to write {CODE}.txt into (default: current directory)")
    parser.add_argument("--stdout", action="store_true", help="Also print the rendered text to stdout")
    parser.add_argument(
        "--tag-lang",
        choices=("en", "ja", "both"),
        default="en",
        help="Tags language: en (default, falls back to ja), ja, or both",
    )
    parser.add_argument(
        "--password",
        default="{PASSWORD}",
        help="Archive password (left as {PASSWORD} placeholder if omitted)",
    )
    parser.add_argument(
        "--gofile",
        default="{GOFILE_URL}",
        help="gofile download URL (left as placeholder if omitted)",
    )
    parser.add_argument(
        "--buzzheavier",
        default="{buzzheavier_URL}",
        help="buzzheavier download URL (left as placeholder if omitted)",
    )
    args = parser.parse_args()

    code = args.code.strip().upper()
    metadata = asyncio.run(fetch(code))
    if not metadata:
        return 1

    title = render_title(metadata, code=code)
    body = render_post(
        metadata,
        code=code,
        tag_lang=args.tag_lang,
        password=args.password,
        gofile_url=args.gofile,
        buzzheavier_url=args.buzzheavier,
    )
    output = (
        f"Thread title: {title}\n"
        "\n"
        "---BBCode post---\n"
        f"{body}\n"
    )

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{code}.txt"
    out_path.write_text(output, encoding="utf-8")
    print(f"wrote {out_path}")
    if args.stdout:
        print()
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
