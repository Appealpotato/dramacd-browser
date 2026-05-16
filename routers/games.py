"""Games wing router.

CRUD for the `games` table, scanner trigger, OS open-folder, VNDB metadata
prefill, and manual cover upload.
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query

import database as db
import vndb_client
from auth import require_api_key
from games_scanner import scan_games_paths
from models import (
    GameCoverUploadRequest,
    GameUpdateRequest,
    GamesScanPathsUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/games")

# Cover files live under data/games/covers/. Mirrors the existing COVERS_DIR
# layout for drama-CD items but kept separate so a game cover and a drama-CD
# cover with the same product code don't collide.
GAME_COVERS_DIR = Path("data/games/covers")
ALLOWED_COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_COVER_BYTES = 8 * 1024 * 1024


def _ext_from_url(url: str) -> str:
    """Best-effort extension sniff from a URL path. Defaults to .jpg since
    VNDB covers are almost always JPEGs."""
    if not url:
        return ".jpg"
    match = re.search(r"\.(jpg|jpeg|png|webp)(?:$|\?)", url, re.IGNORECASE)
    return f".{match.group(1).lower()}" if match else ".jpg"


async def _maybe_download_cover(game_id: int, vndb_id: str | None, cover_url: str | None) -> str | None:
    """Download a VNDB cover image to disk and return the relative-to-data
    path string suitable for storing in games.cover_local. Returns None on
    failure; failures are non-fatal (the row still saves, the user can
    re-trigger by manually entering the URL)."""
    if not cover_url:
        return None
    try:
        suffix = _ext_from_url(cover_url)
        stem = vndb_id or f"game{game_id}"
        target = GAME_COVERS_DIR / f"{stem}_{uuid4().hex[:8]}{suffix}"
        await vndb_client.download_cover(cover_url, target)
        # Same shape as items.cover_local: 'data/games/covers/foo.jpg'.
        return str(target).replace("\\", "/")
    except Exception as exc:
        logger.warning("Failed to fetch VNDB cover for game %s: %s", game_id, exc)
        return None


@router.get("")
async def list_games(
    sort: str = Query(
        "created_at",
        pattern="^(created_at|updated_at|title|release_date|play_status|personal_rating|favorite)$",
    ),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    search: Optional[str] = None,
    play_status: Optional[str] = Query(
        None, pattern="^(backlog|playing|completed|dropped|on_hold|wishlist|want_to_play)$"
    ),
    play_statuses: Optional[list[str]] = Query(None),
    favorite: Optional[bool] = None,
    is_manual: Optional[bool] = None,
    matched: Optional[bool] = None,
    platform: Optional[str] = None,
    developer: Optional[str] = None,
    custom_tag: Optional[str] = None,
    vndb_id: Optional[str] = Query(None),
    include_wishlist: bool = Query(False),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    """List games. Wishlist rows are hidden by default (`include_wishlist=False`)
    so the main grid doesn't fill up with TBD entries. The play-status pills,
    the cleanup queue, and the Wishlist filter pass include_wishlist=True or a
    matching play_status to bypass the default."""
    return await db.list_games(
        search=search,
        play_status=play_status,
        play_statuses=play_statuses,
        favorite=favorite,
        is_manual=is_manual,
        matched=matched,
        platform=platform,
        developer=developer,
        custom_tag=custom_tag,
        vndb_id=vndb_id,
        exclude_wishlist=not include_wishlist,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )


@router.get("/distinct")
async def games_distinct():
    """Sorted unique developers + platform codes + custom tags across the
    whole games table. Powers the sidebar filter dropdowns so the user can
    pick from real data rather than typing free-form. Cheap on a 160-row
    table; we just scan the JSON arrays once on the Python side."""
    from database import get_db
    import json as _json
    conn = await get_db()
    try:
        cursor = await conn.execute(
            "SELECT developer, platforms_json, custom_tags_json FROM games"
        )
        rows = await cursor.fetchall()
    finally:
        await conn.close()
    devs = set()
    platforms = set()
    tags = set()
    for r in rows:
        if r["developer"]:
            devs.add(r["developer"])
        try:
            for p in _json.loads(r["platforms_json"] or "[]"):
                if p:
                    platforms.add(str(p))
        except Exception:
            pass
        try:
            for t in _json.loads(r["custom_tags_json"] or "[]"):
                if t:
                    tags.add(str(t))
        except Exception:
            pass
    return {
        "developers": sorted(devs, key=str.casefold),
        "platforms": sorted(platforms),
        "custom_tags": sorted(tags, key=str.casefold),
    }


@router.get("/stats")
async def games_stats():
    """Aggregated counts powering the Games subtab sidebar stats panel.
    `total` excludes wishlist (the main grid hides it by default). The
    `by_status` map carries every status including wishlist so the
    individual stat pills can render their own counts."""
    from database import get_db
    conn = await get_db()
    try:
        cursor = await conn.execute(
            "SELECT play_status, COUNT(*) AS c FROM games GROUP BY play_status"
        )
        rows = await cursor.fetchall()
        by_status = {r["play_status"]: int(r["c"]) for r in rows}
        cursor = await conn.execute("SELECT COUNT(*) AS c FROM games")
        grand_total = int((await cursor.fetchone())["c"])
        cursor = await conn.execute(
            """SELECT COUNT(*) AS c FROM games
               WHERE (vndb_id IS NOT NULL AND vndb_id != '') OR vndb_searched = 1"""
        )
        matched = int((await cursor.fetchone())["c"])
        cursor = await conn.execute(
            "SELECT COUNT(*) AS c FROM games WHERE favorite = 1"
        )
        favorited = int((await cursor.fetchone())["c"])
        cursor = await conn.execute(
            "SELECT COUNT(*) AS c FROM games WHERE is_manual = 1"
        )
        manual = int((await cursor.fetchone())["c"])
    finally:
        await conn.close()
    wishlist = by_status.get("wishlist", 0)
    total_excl_wishlist = grand_total - wishlist
    return {
        "total": total_excl_wishlist,
        "total_with_wishlist": grand_total,
        "matched": matched,
        "unmatched": grand_total - matched,
        "favorited": favorited,
        "manual": manual,
        "by_status": by_status,
    }


@router.get("/duplicates")
async def list_duplicate_games():
    """Returns groups of game rows that share a vndb_id (and have >1
    member). Frontend uses this to render a 'Merge X duplicates' banner."""
    return {"groups": await db.find_duplicate_game_vndb_ids()}


@router.post("/merge-duplicates", dependencies=[Depends(require_api_key)])
async def merge_duplicates_endpoint():
    """One-shot: merges every vndb_id duplicate group into a single row.
    For each group, the highest-quality member (cover present > non-backlog
    status > lowest id) becomes primary; the rest fold their paths in and
    get deleted. Personal fields stay with primary."""
    groups = await db.find_duplicate_game_vndb_ids()
    summaries = []
    total_merged = 0
    for grp in groups:
        members = grp.get("members", [])
        if len(members) < 2:
            continue
        primary = members[0]
        others = [m["id"] for m in members[1:]]
        result = await db.merge_game_rows(int(primary["id"]), others)
        total_merged += int(result.get("merged", 0))
        summaries.append({
            "vndb_id": grp["vndb_id"],
            "primary_id": primary["id"],
            "merged_into_primary": result.get("merged", 0),
        })
    return {
        "groups_processed": len(summaries),
        "rows_merged": total_merged,
        "summaries": summaries,
    }


@router.get("/ignored-paths")
async def list_ignored_paths():
    """Returns every path the user has 'Removed from library' so Settings
    can display + remove individual entries."""
    return {"paths": await db.get_ignored_game_paths()}


@router.delete("/ignored-paths/{path_key:path}", dependencies=[Depends(require_api_key)])
async def remove_ignored_path(path_key: str):
    removed = await db.remove_ignored_game_path(path_key)
    if not removed:
        raise HTTPException(status_code=404, detail="Path not in ignored list")
    return {"status": "removed", "path_key": path_key}


@router.get("/{game_id}")
async def get_game(game_id: int):
    game = await db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    return game


@router.post("/blank")
async def create_blank_game(_auth=Depends(require_api_key)):
    """Insert a placeholder games row with no library_path so the partial
    UNIQUE index on library_path doesn't block multiple blank rows. The user
    fills in fields via the games detail panel."""
    from datetime import datetime
    from database import get_db
    now = datetime.now().isoformat()
    conn = await get_db()
    try:
        cursor = await conn.execute(
            """INSERT INTO games (title, is_manual, created_at, updated_at)
               VALUES (?, 1, ?, ?)""",
            ("[New Game]", now, now),
        )
        await conn.commit()
        new_id = cursor.lastrowid
    finally:
        await conn.close()
    return await db.get_game(new_id)


@router.patch("/{game_id}")
async def update_game(
    game_id: int,
    request: GameUpdateRequest,
    _auth=Depends(require_api_key),
):
    existing = await db.get_game(game_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Game not found")

    # Strip None values — pydantic emits them for unset fields, and we don't
    # want PATCH to clear scalars the caller didn't mention.
    fields = {k: v for k, v in request.model_dump().items() if v is not None}
    try:
        updated = await db.update_game(game_id, fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Cover auto-download: when the PATCH sets a cover_url and the row still
    # has no local cover, fetch the remote image and persist its path.
    # Non-fatal — a failure here just leaves cover_local empty, the user can
    # re-trigger via re-save or use the manual upload endpoint.
    cover_url = fields.get("cover_url") or (updated or {}).get("cover_url")
    if cover_url and not (updated or {}).get("cover_local"):
        local_path = await _maybe_download_cover(
            game_id,
            (updated or {}).get("vndb_id"),
            cover_url,
        )
        if local_path:
            updated = await db.set_game_cover(game_id, local_path, None)
    return updated


@router.delete("/{game_id}")
async def delete_game(
    game_id: int,
    ignore_path: bool = Query(False),
    _auth=Depends(require_api_key),
):
    """Delete a game from the library.

      - ignore_path=false (default) → plain delete; row reappears on next
        scan if the library_path still exists.
      - ignore_path=true → also add library_path to ignored_game_paths so
        the scanner skips it forever (until removed from Settings).
    """
    game = await db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    ignored_paths: list[str] = []
    if ignore_path and (game.get("library_path") or "").strip():
        ignored_paths = await db.add_ignored_game_paths(
            [game["library_path"]], reason="user_removed_from_library"
        )
    if not await db.delete_game(game_id):
        raise HTTPException(status_code=500, detail="Failed to delete game")
    return {"status": "deleted", "id": game_id, "ignored_paths": ignored_paths}


@router.post("/scan")
async def trigger_scan(_auth=Depends(require_api_key)):
    """Walks the configured `games_scan_paths` and upserts a stub row for
    each top-level folder / archive. Synchronous — games scans are cheap
    (no archive unpacking, no metadata fetch) so this returns once done."""
    paths = await db.get_games_scan_paths()
    if not paths:
        raise HTTPException(
            status_code=400,
            detail="No games library paths configured. Set them in Settings first.",
        )
    summary = await scan_games_paths(paths)
    return {"status": "ok", **summary}


@router.post("/{game_id}/open-folder")
async def open_game_folder(game_id: int, _auth=Depends(require_api_key)):
    """Open the game's library_path in the host OS file browser. For archive
    entries the parent directory is opened instead (you can't 'enter' an
    archive in Explorer the same way you do a folder)."""
    game = await db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    lib = (game.get("library_path") or "").strip()
    if not lib:
        raise HTTPException(
            status_code=404,
            detail="This game has no library_path set. Re-scan or edit the entry.",
        )
    target = Path(lib)
    if game.get("is_archive"):
        target = target.parent
    if not target.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Path no longer exists on disk: {target}",
        )
    try:
        from os_utils import open_folder_focused
        open_folder_focused(target)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to open folder: {exc}")
    return {"status": "opened", "path": str(target)}


@router.get("/scan/paths")
async def get_paths():
    paths = await db.get_games_scan_paths()
    return {"paths": paths}


@router.put("/scan/paths")
async def update_paths(
    request: GamesScanPathsUpdateRequest,
    _auth=Depends(require_api_key),
):
    paths = await db.set_games_scan_paths(request.paths)
    return {"status": "updated", "paths": paths}


@router.post("/match-vndb-all")
async def match_vndb_all(_auth=Depends(require_api_key)):
    """Bulk-match every unmatched game's title against VNDB and apply the top
    search hit. Sequential with a small pacing sleep — VNDB caps at 200 req
    per 5 minutes, so a 160-game library takes ~2 minutes here.

    Skips placeholder rows ('[New Game]') and rows with already-set vndb_id.
    Per-game errors are caught and counted so a single bad title doesn't
    abort the run."""
    import asyncio
    # Pull the full unmatched-games set up-front. Bulk update fits in memory
    # comfortably and skipping the per-iteration query keeps the API rate
    # clearly separated from DB latency.
    listing = await db.list_games(limit=2000, offset=0, sort="created_at", order="asc")
    candidates = [
        g for g in listing.get("items", [])
        if not g.get("vndb_id")
        and (g.get("title") or "").strip()
        and (g.get("title") or "").strip() != "[New Game]"
        and len(g.get("title", "").strip()) >= 2
    ]

    matched = 0
    skipped_no_hit = 0
    errors: list[dict] = []

    def _existing_is_empty(val) -> bool:
        """A field counts as 'empty' (and therefore safe to fill from VNDB)
        when it's None, an empty string, or an empty list. The scanner
        always populates `title` from the folder name, so the user's manual
        edits + folder-derived titles are both treated as set and skipped."""
        if val is None:
            return True
        if isinstance(val, str) and val.strip() == "":
            return True
        if isinstance(val, list) and not val:
            return True
        return False

    for idx, game in enumerate(candidates):
        title = (game.get("title") or "").strip()
        try:
            results = await vndb_client.search_vn(title, limit=1)
            if not results:
                skipped_no_hit += 1
            else:
                fields = vndb_client.vn_to_game_fields(results[0])
                # Bulk match is fill-empties-only — never clobber a folder-
                # derived title, a manually-typed developer, etc. The vndb_id
                # is always written (it's the whole point of the link). The
                # cover gets fetched only when the row has no local cover.
                patch = {}
                for key, value in fields.items():
                    if value in (None, "", []):
                        continue
                    if key == "vndb_id":
                        patch[key] = value
                        continue
                    if key == "cover_url":
                        continue  # handled separately below
                    if _existing_is_empty(game.get(key)):
                        patch[key] = value
                await db.update_game(game["id"], patch)

                cover_url = fields.get("cover_url")
                if cover_url and not game.get("cover_local"):
                    local_path = await _maybe_download_cover(
                        game["id"],
                        patch.get("vndb_id"),
                        cover_url,
                    )
                    if local_path:
                        await db.set_game_cover(game["id"], local_path, cover_url)
                    else:
                        # Keep cover_url for retry even if the download failed.
                        await db.set_game_cover(game["id"], None, cover_url)
                matched += 1
        except Exception as exc:
            errors.append({"id": game["id"], "title": title, "error": str(exc)})
        # VNDB rate limit headroom: 200 req / 5 min == 1 req per 1.5s.
        # 0.4s pacing + ~0.2-0.5s round-trip lands well under the cap.
        if idx < len(candidates) - 1:
            await asyncio.sleep(0.4)

    return {
        "status": "ok",
        "processed": len(candidates),
        "matched": matched,
        "skipped_no_hit": skipped_no_hit,
        "errors": errors,
    }


@router.get("/vndb/search")
async def vndb_search(q: str = Query(..., min_length=1)):
    """Proxy VNDB title search. Returns trimmed candidates the frontend
    dropdown can render directly — full VN payload would blow context and
    leak fields we don't use."""
    try:
        results = await vndb_client.search_vn(q, limit=10)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"VNDB search failed: {exc}") from exc
    return {
        "results": [vndb_client.candidate_summary(vn) for vn in results],
    }


@router.get("/vndb/{vndb_id}")
async def vndb_get(vndb_id: str):
    """Direct ID lookup. Returns the full game-fields dict so the user can
    review before committing via PATCH /api/games/{id}."""
    try:
        vn = await vndb_client.get_vn(vndb_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"VNDB lookup failed: {exc}") from exc
    if not vn:
        raise HTTPException(status_code=404, detail="No VNDB entry with that id")
    return vndb_client.vn_to_game_fields(vn)


@router.put("/{game_id}/cover")
async def upload_game_cover(
    game_id: int,
    request: GameCoverUploadRequest,
    _auth=Depends(require_api_key),
):
    """Mirrors PUT /api/items/{id}/cover. Accepts a {filename, data_url}
    JSON body (base64-encoded image), writes to data/games/covers/, and
    updates games.cover_local."""
    game = await db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    suffix = Path(request.filename or "cover").suffix.lower()
    if suffix not in ALLOWED_COVER_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported image type. Use JPG, PNG, or WEBP.")

    try:
        if "," not in request.data_url:
            raise ValueError("Invalid data URL")
        _prefix, encoded = request.data_url.split(",", 1)
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=400, detail="Invalid cover data.")

    if not content:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(content) > MAX_COVER_BYTES:
        raise HTTPException(status_code=400, detail="Cover is too large (max 8MB).")

    GAME_COVERS_DIR.mkdir(parents=True, exist_ok=True)
    stem = game.get("vndb_id") or f"game{game_id}"
    target = GAME_COVERS_DIR / f"{stem}_{uuid4().hex[:8]}{suffix}"
    target.write_bytes(content)

    old_cover = game.get("cover_local")
    if old_cover:
        try:
            old_path = Path(old_cover)
            if old_path.exists() and old_path.resolve() != target.resolve():
                old_path.unlink()
        except Exception:
            pass

    cover_local = str(target).replace("\\", "/")
    updated = await db.set_game_cover(game_id, cover_local, None)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to save cover.")
    return updated
