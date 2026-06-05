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
from models import (
    MetadataApplyRequest,
    MetadataFetchRequest,
    MetadataSearchRequest,
)
from scraper import download_cover

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


def _build_source_note(meta: dict) -> str:
    """One-line provenance stamp: price/catalog/JAN/maker + URL + fetch date.
    Goes into notes so the description stays clean prose."""
    parts = [f"[{meta.get('source', '?')}]"]
    if meta.get("price"):
        parts.append(meta["price"])
    if meta.get("catalog_number"):
        parts.append(f"品番 {meta['catalog_number']}")
    if meta.get("jan"):
        parts.append(f"JAN {meta['jan']}")
    if meta.get("maker"):
        parts.append(meta["maker"])
    if meta.get("series"):
        parts.append(f"シリーズ: {meta['series']}")
    if meta.get("source_url"):
        parts.append(meta["source_url"])
    parts.append(f"fetched {datetime.now().strftime('%Y-%m-%d')}")
    return " ・ ".join(parts)


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
        data["seiyuu"] = list(meta["seiyuu"])
    if "description" in fields and meta.get("description"):
        data["description"] = meta["description"]
    if "source_note" in fields:
        data["notes"] = _append_note(item.get("notes"), _build_source_note(meta))

    applied = sorted(fields & set(data) | ({"source_note"} if "notes" in data else set()))
    if data:
        await db.update_item_user_data(item_id, data)

    if "cover" in fields and meta.get("cover_url"):
        cover_local = await _download_cover_for(meta, item["product_code"])
        await db.set_item_cover(item_id, cover_local, meta["cover_url"])
        applied.append("cover")

    return {"applied": applied, "item": await db.get_item(item_id)}


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
            _set("seiyuu", _json.dumps(list(meta["seiyuu"]), ensure_ascii=False))
            mirror["seiyuu"] = list(meta["seiyuu"])
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
    return {"applied": applied, "tokuten": out}
