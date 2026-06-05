"""CRUD + folder-scan endpoints for tokutens (game-bonus / community-shared
CDs and other side material). Every tokuten gets a paired items row with
kind='tokuten_audio' so it surfaces in the Library grid alongside drama CDs;
the dedicated /api/tokutens endpoints back the detail view + future game M:N
links."""
import json
import logging
import uuid
from datetime import datetime
from typing import Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

import database as db
from auth import require_api_key
from models import (
    TOKUTEN_KINDS,
    TOKUTEN_SHOPS,
    TokutenCreate,
    TokutenScanPathsUpdateRequest,
    TokutenScanRequest,
    TokutenUpdate,
)
from tokuten_scanner import register_tokuten_from_folder, scan_tokuten_paths

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tokutens")


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _validate_kind_shop(kind: Optional[str], shop: Optional[str]):
    if kind is not None and kind not in TOKUTEN_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {sorted(TOKUTEN_KINDS)}",
        )
    if shop is not None and shop not in TOKUTEN_SHOPS:
        raise HTTPException(
            status_code=400,
            detail=f"shop must be one of {sorted(TOKUTEN_SHOPS)}",
        )


async def _row_to_tokuten(conn: aiosqlite.Connection, row: aiosqlite.Row) -> dict:
    out = dict(row)
    cur = await conn.execute(
        "SELECT id FROM items WHERE tokuten_id = ? LIMIT 1",
        (row["id"],),
    )
    item_row = await cur.fetchone()
    out["item_id"] = item_row["id"] if item_row else None
    return out


@router.get("")
async def list_tokutens(
    kind: Optional[str] = Query(None),
    shop: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    vndb_id: Optional[str] = Query(None),
    sort: str = Query("created_at", pattern="^(created_at|updated_at|title|release_date)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    _validate_kind_shop(kind, shop)
    conn = await db.get_db()
    try:
        where = "WHERE 1=1"
        where_params: list = []
        if kind:
            where += " AND kind = ?"
            where_params.append(kind)
        if shop:
            where += " AND shop = ?"
            where_params.append(shop)
        if vndb_id:
            where += " AND vndb_id = ?"
            where_params.append(vndb_id)
        if search:
            where += " AND (title LIKE ? OR title_en LIKE ? OR notes LIKE ?)"
            like = f"%{search}%"
            where_params.extend([like, like, like])

        order_dir = "DESC" if order == "desc" else "ASC"
        if sort == "title":
            order_clause = f"ORDER BY title COLLATE NOCASE {order_dir}, id DESC"
        else:
            order_clause = f"ORDER BY {sort} IS NULL, {sort} {order_dir}, id DESC"

        cur = await conn.execute(
            f"SELECT * FROM tokutens {where} {order_clause} LIMIT ? OFFSET ?",
            where_params + [limit, offset],
        )
        rows = await cur.fetchall()
        items = [await _row_to_tokuten(conn, r) for r in rows]

        cur = await conn.execute(
            f"SELECT COUNT(*) FROM tokutens {where}",
            where_params,
        )
        total = (await cur.fetchone())[0]

        return {"tokutens": items, "total": total, "limit": limit, "offset": offset}
    finally:
        await conn.close()


@router.get("/stats")
async def tokutens_stats():
    """Sidebar stats panel for the Tokutens subtab: total, favorited, by_kind."""
    return await db.get_tokuten_stats()


@router.get("/{tokuten_id}")
async def get_tokuten(tokuten_id: int):
    conn = await db.get_db()
    try:
        cur = await conn.execute("SELECT * FROM tokutens WHERE id = ?", (tokuten_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Tokuten not found")
        out = await _row_to_tokuten(conn, row)
        cur = await conn.execute(
            """SELECT id, path, role, sort_order
               FROM media_assets
               WHERE parent_kind = 'tokuten' AND parent_id = ?
               ORDER BY sort_order ASC, id ASC""",
            (tokuten_id,),
        )
        out["media"] = [dict(r) for r in await cur.fetchall()]
        return out
    finally:
        await conn.close()


@router.post("", dependencies=[Depends(require_api_key)])
async def create_tokuten(payload: TokutenCreate):
    """Creates a tokuten and a paired items row so the new entry appears as a
    card in the Library grid immediately. The items row gets a synthetic
    TKT-<hex> product_code (no DLsite scan would ever produce this) and
    inherits the title from the tokuten."""
    _validate_kind_shop(payload.kind, payload.shop)
    now = _now_iso()
    conn = await db.get_db()
    try:
        cur = await conn.execute(
            """INSERT INTO tokutens (kind, title, title_en, shop, shop_other_name,
                                     release_date, notes, source_url, local_path,
                                     vndb_id, seiyuu, seiyuu_en,
                                     description, description_en,
                                     created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload.kind,
                payload.title,
                payload.title_en,
                payload.shop,
                payload.shop_other_name,
                payload.release_date,
                payload.notes or "",
                payload.source_url,
                payload.local_path,
                (payload.vndb_id or None),
                json.dumps(payload.seiyuu or [], ensure_ascii=False),
                json.dumps(payload.seiyuu_en or [], ensure_ascii=False),
                payload.description,
                payload.description_en,
                now, now,
            ),
        )
        new_id = cur.lastrowid

        synthetic_code = f"TKT-{uuid.uuid4().hex[:12].upper()}"
        await conn.execute(
            """INSERT INTO items (
                   product_code, title, title_en, kind, tokuten_id,
                   confidence, is_manual, scan_date, created_at, updated_at
               ) VALUES (?, ?, ?, 'tokuten_audio', ?, 'verified', 1, ?, ?, ?)""",
            (synthetic_code, payload.title, payload.title_en, new_id,
             now, now, now),
        )

        await conn.commit()
        cur = await conn.execute("SELECT * FROM tokutens WHERE id = ?", (new_id,))
        row = await cur.fetchone()
        return await _row_to_tokuten(conn, row)
    finally:
        await conn.close()


@router.patch("/{tokuten_id}", dependencies=[Depends(require_api_key)])
async def update_tokuten(tokuten_id: int, payload: TokutenUpdate):
    _validate_kind_shop(payload.kind, payload.shop)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    # List-valued fields are stored as JSON text (mirrors items.seiyuu).
    for json_field in ("seiyuu", "seiyuu_en"):
        if json_field in fields:
            fields[json_field] = json.dumps(fields[json_field] or [], ensure_ascii=False)
    fields["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in fields.keys())
    params = list(fields.values()) + [tokuten_id]
    conn = await db.get_db()
    try:
        cur = await conn.execute(
            f"UPDATE tokutens SET {set_clause} WHERE id = ?",
            params,
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Tokuten not found")
        # Mirror title changes onto the linked items row so the Drama CDs
        # filtered view stays consistent with the tokuten record.
        if "title" in fields or "title_en" in fields:
            mirror_fields = []
            mirror_params = []
            if "title" in fields:
                mirror_fields.append("title = ?")
                mirror_params.append(fields["title"])
            if "title_en" in fields:
                mirror_fields.append("title_en = ?")
                mirror_params.append(fields["title_en"])
            mirror_fields.append("updated_at = ?")
            mirror_params.append(fields["updated_at"])
            mirror_params.append(tokuten_id)
            await conn.execute(
                f"UPDATE items SET {', '.join(mirror_fields)} WHERE tokuten_id = ?",
                mirror_params,
            )
        await conn.commit()
        cur = await conn.execute("SELECT * FROM tokutens WHERE id = ?", (tokuten_id,))
        row = await cur.fetchone()
        return await _row_to_tokuten(conn, row)
    finally:
        await conn.close()


@router.delete("/{tokuten_id}", dependencies=[Depends(require_api_key)])
async def delete_tokuten(tokuten_id: int):
    """Removes the tokuten and any linked items row (which cascades pipeline
    tracks/transcripts/translations). media_assets entries are removed too."""
    conn = await db.get_db()
    try:
        cur = await conn.execute("SELECT id FROM tokutens WHERE id = ?", (tokuten_id,))
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="Tokuten not found")
        await conn.execute("DELETE FROM items WHERE tokuten_id = ?", (tokuten_id,))
        await conn.execute(
            "DELETE FROM media_assets WHERE parent_kind = 'tokuten' AND parent_id = ?",
            (tokuten_id,),
        )
        await conn.execute("DELETE FROM tokutens WHERE id = ?", (tokuten_id,))
        await conn.commit()
        return {"deleted": tokuten_id}
    finally:
        await conn.close()


@router.get("/scan/paths")
async def get_tokuten_scan_paths_endpoint():
    paths = await db.get_tokuten_scan_paths()
    return {"paths": paths}


@router.put("/scan/paths", dependencies=[Depends(require_api_key)])
async def update_tokuten_scan_paths(request: TokutenScanPathsUpdateRequest):
    paths = await db.set_tokuten_scan_paths(request.paths)
    return {"status": "updated", "paths": paths}


@router.post("/scan", dependencies=[Depends(require_api_key)])
async def trigger_tokuten_scan():
    """Walk `tokuten_scan_paths` and register stub tokutens for each
    top-level folder / archive. Catalog-only — no unpacking, no audio
    indexing. The user runs the existing folder-scan-once flow on a single
    row if they want tracks pulled out."""
    paths = await db.get_tokuten_scan_paths()
    if not paths:
        raise HTTPException(
            status_code=400,
            detail="No tokuten library paths configured. Set them in Settings first.",
        )
    summary = await scan_tokuten_paths(paths)
    return {"status": "ok", **summary}


@router.post("/scan-folder", dependencies=[Depends(require_api_key)])
async def scan_folder_endpoint(payload: TokutenScanRequest):
    """One-shot: walk a folder of audio + images and create the tokuten +
    items + tracks. Use this when the user already has the bonus CD files
    on disk and wants them registered with one click rather than typing the
    tracklist by hand."""
    _validate_kind_shop("audio", payload.shop)
    try:
        result = await register_tokuten_from_folder(
            folder_path=payload.folder_path,
            title=payload.title,
            title_en=payload.title_en,
            kind="audio",
            shop=payload.shop,
            shop_other_name=payload.shop_other_name,
            release_date=payload.release_date,
            notes=payload.notes or "",
            source_url=payload.source_url,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to register tokuten folder")
        raise HTTPException(status_code=500, detail=f"Scan failed: {exc}")
    return result
