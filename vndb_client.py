"""Thin async wrapper around the VNDB Kana HTTPS API for the Games wing.

Endpoint: POST https://api.vndb.org/kana/vn (no auth needed for read queries).
Rate limit: 200 req / 5 min, 1 sec exec time / minute. We don't bother with
caching here — search calls only fire on debounced user input and direct
ID lookups happen at most once per save.

Docs reference: see memory/vndb_api.md.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

VNDB_API_BASE = "https://api.vndb.org/kana"
DEFAULT_TIMEOUT = 8.0

# Fields we ever need anywhere in the app. Selecting just these keeps the
# response under VNDB's "Too much data selected" threshold even for 10
# search hits.
_VN_FIELDS = (
    "title, alttitle, "
    "titles{lang,title,latin,official,main}, "
    "olang, released, "
    "image{url,thumbnail,dims,thumbnail_dims}, "
    "description, "
    "developers{name,original}, "
    "platforms, languages"
)


async def _vndb_post(path: str, body: dict) -> Any:
    """POST to a VNDB endpoint and return the parsed JSON. Raises for HTTP
    errors with the server's error body folded into the exception message,
    since VNDB's 4xx responses include a useful text/plain reason."""
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.post(
            f"{VNDB_API_BASE}{path}",
            json=body,
            headers={"Content-Type": "application/json"},
        )
    if resp.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"VNDB {resp.status_code}: {resp.text[:200]}",
            request=resp.request,
            response=resp,
        )
    return resp.json()


async def search_vn(query: str, limit: int = 10) -> list[dict]:
    """Title search. Returns up to `limit` candidates (max 100 per the API).
    Empty/whitespace query short-circuits to []."""
    q = (query or "").strip()
    if not q:
        return []
    body = {
        "filters": ["search", "=", q],
        "fields": _VN_FIELDS,
        "results": max(1, min(int(limit), 25)),
        "sort": "searchrank",
    }
    data = await _vndb_post("/vn", body)
    return list(data.get("results") or [])


async def get_vn(vndb_id: str) -> dict | None:
    """Direct ID lookup. `vndb_id` may be passed with or without the 'v'
    prefix; the API accepts a bare integer when the prefix is unambiguous."""
    vid = (vndb_id or "").strip()
    if not vid:
        return None
    body = {
        "filters": ["id", "=", vid],
        "fields": _VN_FIELDS,
        "results": 1,
    }
    data = await _vndb_post("/vn", body)
    results = data.get("results") or []
    return results[0] if results else None


async def download_cover(image_url: str, target_path: Path) -> Path:
    """Stream the cover image to disk. Caller is responsible for picking the
    target path (and its parent dir's existence). Returns the path written."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(image_url)
        resp.raise_for_status()
        target_path.write_bytes(resp.content)
    return target_path


def candidate_summary(vn: dict) -> dict:
    """Trim a full VN object to just the fields the search dropdown renders.
    Frontend code uses these keys directly."""
    image = vn.get("image") or {}
    developers = vn.get("developers") or []
    dev_names = [d.get("name") for d in developers if d.get("name")]
    return {
        "id": vn.get("id"),
        "title": vn.get("title"),
        "alttitle": vn.get("alttitle"),
        "released": vn.get("released"),
        "thumbnail": image.get("thumbnail"),
        "developer": ", ".join(dev_names) if dev_names else None,
    }


def vn_to_game_fields(vn: dict) -> dict:
    """Map a full VNDB response into the dict shape our games table expects.
    Caller can merge this into a PATCH /api/games/{id} body."""
    image = vn.get("image") or {}
    developers = vn.get("developers") or []
    dev_names = [d.get("name") for d in developers if d.get("name")]
    titles = vn.get("titles") or []
    # title_jp = the original-script title (titles entry with main=True OR
    # alttitle when olang is non-latin). Prefer the main-title row's `title`.
    main_title = next((t for t in titles if t.get("main")), None)
    title_jp = (main_title or {}).get("title") or vn.get("alttitle")
    # title_en = English-language title if available; fall back to romanized.
    en_title_row = next((t for t in titles if t.get("lang") == "en"), None)
    if en_title_row:
        title_en = en_title_row.get("title") or en_title_row.get("latin")
    else:
        # No native English entry — use the romanized form from any row that
        # has one (the display 'title' field is already romanized in most
        # cases, but we already use that as the primary title).
        title_en = None
    return {
        "vndb_id": vn.get("id"),
        "title": vn.get("title"),
        "title_jp": title_jp,
        "title_en": title_en,
        "developer": ", ".join(dev_names) if dev_names else None,
        "developers": developers,
        "release_date": vn.get("released") if vn.get("released") not in ("TBA", "unknown") else None,
        "cover_url": image.get("url"),
        "description": vn.get("description"),
        # VNDB returns the full list of platforms a VN was released on —
        # that's "available", not "owned". The owned list is set by the
        # scanner from local file extensions or by the user via the edit
        # panel. Keeping them in separate columns preserves both signals.
        "platforms_available": vn.get("platforms") or [],
        "languages": vn.get("languages") or [],
    }
