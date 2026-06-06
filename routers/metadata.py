"""Metadata-fetch endpoints for custom entries (manual drama CDs + tokutens).

Flow is preview-then-apply: /fetch-url and /search never write anything;
the UI shows the normalized result with per-field checkboxes and posts the
(possibly user-edited) payload to /apply with the list of fields to write.
A fetch therefore never silently clobbers hand-edited data."""
import asyncio
import logging
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException

import database as db
import metadata_sources
from auth import require_api_key
from metadata_sources.base import SourceError
from metadata_sources.merge import merge_metadata
from models import (
    MetadataApplyRequest,
    MetadataFetchMultiRequest,
    MetadataFetchRequest,
    MetadataSearchRequest,
)
from scraper import download_cover

MAX_MULTI_FETCH = 20

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/metadata")

# Field names the UI may request; anything else in `fields` is ignored.
ITEM_FIELDS = {"title", "release_date", "seiyuu", "description", "cover", "source_note"}
TOKUTEN_FIELDS = ITEM_FIELDS | {"shop", "source_url"}


@router.get("/sources")
async def get_sources():
    return {"sources": metadata_sources.list_sources()}


@router.post("/fetch-url")
async def fetch_url(payload: MetadataFetchRequest):
    """Fetch + normalize a product page from a supported source. Preview
    only — nothing is written."""
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    source = metadata_sources.match_url(url)
    if not source:
        supported = ", ".join(s["label"] for s in metadata_sources.list_sources())
        raise HTTPException(
            status_code=400,
            detail=f"URL doesn't match a supported source ({supported})",
        )
    async with httpx.AsyncClient() as client:
        try:
            meta = await source.fetch_by_url(client, url)
        except SourceError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
    return {"metadata": meta}


@router.post("/fetch-multi")
async def fetch_multi(payload: MetadataFetchMultiRequest):
    """Fetch several volumes (any mix of supported sources) and merge them
    into one normalized payload. Preview only — nothing is written. Partial
    failures degrade to per-URL errors as long as at least one volume
    fetches."""
    urls = [u.strip() for u in (payload.urls or []) if u and u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="At least one URL is required")
    if len(urls) > MAX_MULTI_FETCH:
        raise HTTPException(
            status_code=400,
            detail=f"Too many URLs ({len(urls)}); max {MAX_MULTI_FETCH} per fetch",
        )
    pairs = []
    for url in urls:
        source = metadata_sources.match_url(url)
        if not source:
            raise HTTPException(
                status_code=400, detail=f"URL doesn't match a supported source: {url}"
            )
        pairs.append((source, url))

    errors: list[str] = []
    metas: list[dict] = []
    async with httpx.AsyncClient() as client:
        gathered = await asyncio.gather(
            *(source.fetch_by_url(client, url) for source, url in pairs),
            return_exceptions=True,
        )
    for (source, url), outcome in zip(pairs, gathered):
        if isinstance(outcome, Exception):
            logger.warning(f"[metadata] fetch-multi failed for {url}: {outcome}")
            errors.append(f"{url}: {outcome}")
        else:
            metas.append(outcome)
    if not metas:
        raise HTTPException(
            status_code=502, detail="All fetches failed: " + " / ".join(errors)
        )
    return {"metadata": merge_metadata(metas), "fetched": len(metas), "errors": errors}


@router.post("/search")
async def search(payload: MetadataSearchRequest):
    """Search one source (payload.source) or all searchable sources in
    parallel. Per-source failures degrade to a warning instead of failing
    the whole search."""
    query = (payload.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query is required")

    if payload.source:
        source = metadata_sources.get_source(payload.source)
        if not source:
            raise HTTPException(status_code=400, detail=f"Unknown source: {payload.source}")
        sources = [source]
    else:
        sources = [s for s in metadata_sources.SOURCES if s.supports_search]

    results: list[dict] = []
    errors: list[str] = []
    async with httpx.AsyncClient() as client:
        gathered = await asyncio.gather(
            *(s.search(client, query) for s in sources),
            return_exceptions=True,
        )
    for source, outcome in zip(sources, gathered):
        if isinstance(outcome, Exception):
            logger.warning(f"[metadata] search failed for {source.name}: {outcome}")
            errors.append(f"{source.label}: {outcome}")
        else:
            results.extend(outcome)
    return {"results": results, "errors": errors}


def _note_line(meta: dict, include_title: bool = False) -> str:
    parts = [f"[{meta.get('source', '?')}]"]
    if include_title and meta.get("title"):
        parts.append(meta["title"])
    if meta.get("price"):
        parts.append(meta["price"])
    if meta.get("catalog_number"):
        parts.append(f"品番 {meta['catalog_number']}")
    if meta.get("jan"):
        parts.append(f"JAN {meta['jan']}")
    if meta.get("maker") and not include_title:
        parts.append(meta["maker"])
    if meta.get("series") and not include_title:
        parts.append(f"シリーズ: {meta['series']}")
    if meta.get("release_date") and include_title:
        parts.append(meta["release_date"])
    if meta.get("source_url"):
        parts.append(meta["source_url"])
    return " ・ ".join(parts)


def _build_source_note(meta: dict) -> str:
    """Provenance stamp for notes. Single fetch: one line with
    price/catalog/JAN/maker + URL + fetch date. Multi-volume merge: one
    line per volume (title + identifiers) so nothing per-volume is lost."""
    volumes = (meta.get("extra") or {}).get("volumes") or []
    fetched = f"fetched {datetime.now().strftime('%Y-%m-%d')}"
    if volumes:
        lines = [_note_line(v, include_title=True) for v in volumes]
        lines.append(f"({len(volumes)} volumes ・ {fetched})")
        return "\n".join(lines)
    return _note_line(meta) + f" ・ {fetched}"


def _append_note(existing: str | None, line: str) -> str:
    existing = (existing or "").rstrip()
    return f"{existing}\n{line}".strip()


async def _download_cover_for(meta: dict, cover_key: str) -> str | None:
    """Download meta['cover_url'] into the shared data/covers cache."""
    if not meta.get("cover_url"):
        return None
    async with httpx.AsyncClient() as client:
        cover_local, reason = await download_cover(client, meta["cover_url"], cover_key)
    if not cover_local:
        raise HTTPException(status_code=502, detail=f"Cover download failed ({reason})")
    return cover_local


def _merge_seiyuu(existing_json: str | None, incoming: list) -> list[str]:
    """Union the entry's current cast with the fetched cast, existing order
    first — applying a fetch never drops hand-entered names."""
    import json as _json
    try:
        existing = _json.loads(existing_json or "[]")
    except Exception:
        existing = []
    out = [s for s in existing if isinstance(s, str) and s.strip()]
    for name in incoming or []:
        if name and name not in out:
            out.append(name)
    return out


async def _download_volume_gallery(
    meta: dict, cover_key: str, parent_kind: str, parent_id: int,
) -> int:
    """Multi-volume fetches carry per-volume covers in extra['volumes'].
    The first/primary cover lands on the entry itself; the rest download
    into data/covers and register as media_assets gallery rows (deduped by
    path so re-applying doesn't stack duplicates). Failed downloads are
    skipped, not fatal."""
    volumes = (meta.get("extra") or {}).get("volumes") or []
    primary_url = meta.get("cover_url")
    extra_covers = []
    seen_urls = {primary_url}
    for vol in volumes:
        url = vol.get("cover_url")
        if url and url not in seen_urls:
            seen_urls.add(url)
            extra_covers.append(url)
    if not extra_covers:
        return 0

    added = 0
    now = datetime.utcnow().isoformat() + "Z"
    async with httpx.AsyncClient() as client:
        downloaded: list[str] = []
        for i, url in enumerate(extra_covers, start=1):
            cover_local, reason = await download_cover(client, url, f"{cover_key}_vol{i}")
            if cover_local:
                downloaded.append(cover_local)
            else:
                logger.warning(f"[metadata] volume cover failed ({reason}): {url}")

    if not downloaded:
        return 0
    conn = await db.get_db()
    try:
        cur = await conn.execute(
            "SELECT path FROM media_assets WHERE parent_kind = ? AND parent_id = ?",
            (parent_kind, parent_id),
        )
        existing_paths = {r["path"] for r in await cur.fetchall()}
        for sort_idx, path in enumerate(downloaded, start=1):
            if path in existing_paths:
                continue
            await conn.execute(
                """INSERT INTO media_assets (parent_kind, parent_id, path, role,
                                             sort_order, created_at)
                   VALUES (?, ?, ?, 'gallery', ?, ?)""",
                (parent_kind, parent_id, path, sort_idx, now),
            )
            added += 1
        await conn.commit()
    finally:
        await conn.close()
    return added


@router.post("/apply", dependencies=[Depends(require_api_key)])
async def apply_metadata(payload: MetadataApplyRequest):
    meta = payload.metadata or {}
    fields = set(payload.fields or [])
    if payload.target == "item":
        return await _apply_to_item(payload.target_id, meta, fields & ITEM_FIELDS)
    if payload.target == "tokuten":
        return await _apply_to_tokuten(payload.target_id, meta, fields & TOKUTEN_FIELDS)
    raise HTTPException(status_code=400, detail="target must be 'item' or 'tokuten'")


async def _apply_to_item(item_id: int, meta: dict, fields: set[str]) -> dict:
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    data: dict = {}
    if "title" in fields and meta.get("title"):
        data["title"] = meta["title"]
    if "release_date" in fields and meta.get("release_date"):
        data["release_date"] = meta["release_date"]
    if "seiyuu" in fields and meta.get("seiyuu"):
        # Union, not replace — hand-entered names survive a fetch.
        data["seiyuu"] = _merge_seiyuu(item.get("seiyuu"), meta["seiyuu"])
    if "description" in fields and meta.get("description"):
        data["description"] = meta["description"]
    if "source_note" in fields:
        data["notes"] = _append_note(item.get("notes"), _build_source_note(meta))

    applied = sorted(fields & set(data) | ({"source_note"} if "notes" in data else set()))
    if data:
        await db.update_item_user_data(item_id, data)

    gallery_added = 0
    if "cover" in fields and meta.get("cover_url"):
        cover_local = await _download_cover_for(meta, item["product_code"])
        await db.set_item_cover(item_id, cover_local, meta["cover_url"])
        applied.append("cover")
        gallery_added = await _download_volume_gallery(
            meta, item["product_code"], "item", item_id
        )

    return {
        "applied": applied,
        "gallery_added": gallery_added,
        "item": await db.get_item(item_id),
    }


async def _apply_to_tokuten(tokuten_id: int, meta: dict, fields: set[str]) -> dict:
    import json as _json

    conn = await db.get_db()
    try:
        cur = await conn.execute("SELECT * FROM tokutens WHERE id = ?", (tokuten_id,))
        tokuten = await cur.fetchone()
        if not tokuten:
            raise HTTPException(status_code=404, detail="Tokuten not found")
        cur = await conn.execute(
            "SELECT id, product_code, notes FROM items WHERE tokuten_id = ? LIMIT 1",
            (tokuten_id,),
        )
        paired = await cur.fetchone()

        now = datetime.utcnow().isoformat() + "Z"
        sets: list[str] = []
        values: list = []
        mirror: dict = {}  # fields echoed onto the paired items row
        applied: list[str] = []

        def _set(column: str, value):
            sets.append(f"{column} = ?")
            values.append(value)

        if "title" in fields and meta.get("title"):
            _set("title", meta["title"])
            mirror["title"] = meta["title"]
            applied.append("title")
        if "release_date" in fields and meta.get("release_date"):
            _set("release_date", meta["release_date"])
            mirror["release_date"] = meta["release_date"]
            applied.append("release_date")
        if "seiyuu" in fields and meta.get("seiyuu"):
            merged_cast = _merge_seiyuu(tokuten["seiyuu"], meta["seiyuu"])
            _set("seiyuu", _json.dumps(merged_cast, ensure_ascii=False))
            mirror["seiyuu"] = merged_cast
            applied.append("seiyuu")
        if "description" in fields and meta.get("description"):
            _set("description", meta["description"])
            mirror["description"] = meta["description"]
            applied.append("description")
        if "shop" in fields and meta.get("source"):
            _set("shop", meta["source"])
            applied.append("shop")
        if "source_url" in fields and meta.get("source_url"):
            _set("source_url", meta["source_url"])
            applied.append("source_url")
        if "source_note" in fields:
            _set("notes", _append_note(tokuten["notes"], _build_source_note(meta)))
            applied.append("source_note")

        if sets:
            _set("updated_at", now)
            await conn.execute(
                f"UPDATE tokutens SET {', '.join(sets)} WHERE id = ?",
                values + [tokuten_id],
            )
            await conn.commit()
    finally:
        await conn.close()

    cover_local = None
    gallery_added = 0
    if "cover" in fields and meta.get("cover_url"):
        cover_key = paired["product_code"] if paired else f"tokuten_{tokuten_id}"
        cover_local = await _download_cover_for(meta, cover_key)
        conn = await db.get_db()
        try:
            await conn.execute(
                "UPDATE tokutens SET cover_local = ?, updated_at = ? WHERE id = ?",
                (cover_local, datetime.utcnow().isoformat() + "Z", tokuten_id),
            )
            await conn.commit()
        finally:
            await conn.close()
        applied.append("cover")
        gallery_added = await _download_volume_gallery(
            meta, cover_key, "tokuten", tokuten_id
        )

    # Mirror onto the paired Library card so filters/cards stay in sync.
    # update_item_user_data also refreshes the seiyuu/tags index tables.
    if paired and mirror:
        await db.update_item_user_data(paired["id"], mirror)
    if paired and cover_local:
        await db.set_item_cover(paired["id"], cover_local, meta.get("cover_url"))

    conn = await db.get_db()
    try:
        cur = await conn.execute("SELECT * FROM tokutens WHERE id = ?", (tokuten_id,))
        row = await cur.fetchone()
        out = dict(row)
        out["item_id"] = paired["id"] if paired else None
    finally:
        await conn.close()
    return {"applied": applied, "gallery_added": gallery_added, "tokuten": out}
