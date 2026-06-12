import asyncio
import hashlib
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends

import database as db
from database import _basename_lower
from auth import require_api_key
from config import ARCHIVE_EXTENSIONS, AUDIO_EXTENSIONS
from models import FetchMetadataRequest, ScanPathsUpdateRequest, ScanRequest
from scanner import clean_title, scan_folder_with_progress
from scraper import fetch_all_metadata, scrape_progress

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _now_iso():
    return datetime.utcnow().isoformat() + "Z"


async def _auto_index_loose_item(item_id: int, files: list) -> int:
    """If a scanned item's sources are already-extracted loose audio (absolute
    paths to existing audio files, no archive), index them in place at scan
    time — so the item is immediately playable in Atelier without a manual
    'extract' step that would only re-discover files already sitting on disk.

    Returns the number of tracks indexed, or 0 if the item isn't a pure loose-
    audio folder, or its tracks are already indexed AND still resolve on disk.
    If existing tracks are ALL missing on disk (folder renamed / moved / files
    re-encoded) while the item's current files DO resolve, the stale rows are
    re-indexed from the valid paths — self-healing after a rename. Best-effort:
    never raises into the scan loop. Relies on the same in-place indexer the
    extract job uses, so nothing is copied into the pipeline workspace."""
    audio_paths = []
    for f in files or []:
        try:
            p = Path(str(f))
        except Exception:
            return 0
        # Any member that isn't an existing absolute audio file means this is
        # an archive (or filename-only) item — leave it to the normal flow.
        if not p.is_absolute() or p.suffix.lower() not in AUDIO_EXTENSIONS or not p.is_file():
            return 0
        audio_paths.append(p)
    if not audio_paths:
        return 0
    try:
        existing = await db.get_pipeline_tracks(item_id)
        if existing:
            # Leave indexed items alone — UNLESS every track is now missing on
            # disk. Then the stored paths are stale (the item was renamed/moved
            # after indexing) and we re-index from the current, valid files.
            any_on_disk = any(
                bool((t.get("track_path") or "").strip())
                and Path(t["track_path"]).is_file()
                for t in existing
            )
            if any_on_disk:
                return 0
            logger.info(
                f"Auto-index: item {item_id} has {len(existing)} stale track(s) "
                "(none on disk) — re-indexing from current files"
            )
        from pipeline.extractor import _index_loose_tracks
        tracks = _index_loose_tracks(item_id, audio_paths)
        return await db.replace_pipeline_tracks_for_item(item_id, tracks)
    except Exception as exc:  # never break ingestion over an optional convenience
        logger.warning(f"Auto-index of loose item {item_id} failed: {exc}")
        return 0


async def _ingest_bundled_package_entry(payload: dict, archive_path: str, size: int) -> str | None:
    """Turn a scanner-unmatched archive that carries a bundled ``*.package.json`` into a
    non-DLsite custom (is_manual) item: adopt the package's product_code, register the
    archive as the item's file, and apply the bundled item-level metadata. Tracks come
    later from normal extraction (the bundled-package hook re-applies metadata then).

    Returns the product_code it matched, or None if the package declared no usable code."""
    items = payload.get("items") or []
    item_payload = next((it for it in items if (it.get("product_code") or "").strip()), None)
    if not item_payload:
        return None
    code = item_payload["product_code"].strip().upper()
    existing = await db.get_item_by_product_code(code)
    await db.upsert_item({                            # upsert_item never touches metadata fields
        "product_code": code,
        "original_code": None,
        "confidence": "verified",
        "files": json.dumps([Path(archive_path).name]),
        "file_count": 1,
        "total_size": size,
        "file_format": json.dumps([]),
    })
    await db.set_item_is_manual(code, True)          # never auto-fetch DLsite for this one
    # Apply the bundled metadata only on FIRST discovery - a re-scan must not clobber
    # metadata the user has since hand-edited in-app (same "never override" stance as the
    # subtitle/package import hooks).
    meta = item_payload.get("metadata")
    already_has_meta = bool(existing and (existing.get("title") or existing.get("metadata_date")))
    if meta and not already_has_meta:
        try:
            await db.update_item_metadata(code, meta)
        except Exception as exc:
            logger.warning(f"bundled metadata apply failed for {code}: {exc}")
    return code


def _stable_manual_code(path_key: str) -> str:
    """Deterministic MAN- code from a filesystem path so rescans upsert into
    the same row instead of stacking duplicates. (Moving the folder changes
    the hash — that's a new entry, the old one points at the dead path.)"""
    digest = hashlib.sha1(path_key.strip().lower().encode("utf-8")).hexdigest()
    return "MAN-" + digest[:12].upper()


async def _ingest_codeless_entry(
    code: str, title: str, files: list[str], total_size: int,
    formats: list[str], ignored_codes: set,
) -> bool:
    """Shared ingest for codeless folders / loose archives → manual item.
    Files are stored as ABSOLUTE paths (the extractor anchors on those).
    Title is only written while the entry still looks untouched, so a
    rescan never clobbers a hand-edited or metadata-applied title."""
    if code in ignored_codes:
        return False
    existing = await db.get_item_by_product_code(code)
    await db.upsert_item({
        "product_code": code,
        "original_code": None,
        "confidence": "verified",
        "files": json.dumps(files, ensure_ascii=False),
        "file_count": len(files),
        "total_size": total_size,
        "file_format": json.dumps(formats, ensure_ascii=False),
    })
    await db.set_item_is_manual(code, True)
    already_titled = bool(existing and (existing.get("title") or "").strip())
    if not already_titled and title:
        row = await db.get_item_by_product_code(code)
        if row:
            await db.update_item_user_data(row["id"], {"title": title})
    return existing is None


# Track scan progress
scan_state = {
    "job_id": None,
    "running": False,
    "paused": False,
    "stopping": False,
    "result": None,
    "error": None,
    "current": None,
    "total_files": 0,
    "processed_files": 0,
    "matched": 0,
    "unmatched": 0,
    "started_at": None,
    "finished_at": None,
}

_scan_pause_event = threading.Event()
_scan_stop_event = threading.Event()
_fetch_pause_event = threading.Event()
_fetch_stop_event = threading.Event()


async def _safe_latest_job(job_type: str) -> dict | None:
    try:
        return await db.get_latest_job(job_type)
    except Exception:
        return None


async def _safe_update_latest_job(job_type: str, allowed_statuses: set[str], **fields):
    latest = await _safe_latest_job(job_type)
    if latest and latest.get("status") in allowed_statuses:
        try:
            await db.update_job(latest["id"], **fields)
        except Exception:
            pass


def _log_job_event(job_type: str, event: str, job_id: int | None = None, **fields):
    payload = {"job_type": job_type, "event": event, "job_id": job_id}
    payload.update(fields)
    logger.info(json.dumps(payload, ensure_ascii=False))


def _scan_state_from_job(job: dict | None) -> dict:
    if not job:
        return {
            "running": False,
            "paused": False,
            "stopping": False,
            "result": None,
            "error": None,
            "current": None,
            "total_files": 0,
            "processed_files": 0,
            "matched": 0,
            "unmatched": 0,
            "started_at": None,
            "finished_at": None,
        }

    status = job.get("status") or ""
    return {
        "job_id": job.get("id"),
        "status": status,
        "running": status == "running",
        "paused": bool(job.get("paused")),
        "stopping": bool(job.get("stopping")),
        "result": job.get("result_json"),
        "error": job.get("error"),
        "current": job.get("current"),
        "total_files": job.get("total_files") or 0,
        "processed_files": job.get("processed_files") or 0,
        "matched": job.get("matched") or 0,
        "unmatched": job.get("unmatched") or 0,
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


def _fetch_state_from_job(job: dict | None) -> dict:
    if not job:
        return dict(scrape_progress)

    status = job.get("status") or ""
    return {
        "job_id": job.get("id"),
        "status": status,
        "running": status == "running",
        "paused": bool(job.get("paused")),
        "stopping": bool(job.get("stopping")),
        "stopped": bool(job.get("stopped")),
        "total": job.get("total") or 0,
        "completed": job.get("completed") or 0,
        "current": job.get("current"),
        "errors": job.get("errors_json") or [],
        "success": job.get("success") or 0,
        "failed": job.get("failed") or 0,
        "skipped": job.get("skipped") or 0,
        "error_summary": job.get("error_summary_json") or {},
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


async def run_scan(scan_paths: list[str] | None = None, recursive: bool = True):
    global scan_state

    scan_state.update(
        {
            "running": True,
            "paused": False,
            "stopping": False,
            "result": None,
            "error": None,
            "current": None,
            "total_files": 0,
            "processed_files": 0,
            "matched": 0,
            "unmatched": 0,
            "started_at": _now_iso(),
            "finished_at": None,
        }
    )
    _scan_pause_event.clear()
    _scan_stop_event.clear()
    job_id = None
    try:
        job_id = await db.create_job("scan", status="running", metadata={"paths": scan_paths, "recursive": recursive})
    except Exception:
        job_id = None
    scan_state["job_id"] = job_id
    if job_id:
        await db.append_job_event(job_id, "info", "Scan started", {"paths": scan_paths, "recursive": recursive})
    _log_job_event("scan", "started", job_id=job_id, paths=scan_paths, recursive=recursive)

    try:
        # Snapshot BEFORE clearing: which files already belong to an item, and which were
        # already shelved as unmatched last scan. Both let the bundled-package peek below
        # skip everything except genuinely new archives.
        claimed_basenames = await db.get_all_claimed_basenames()
        claimed_local_paths = await db.get_claimed_local_paths()
        prev_unmatched_keys = await db.get_unmatched_file_keys()
        await db.clear_unmatched_files()
        ignored_codes = await db.get_ignored_codes()

        if scan_paths is None:
            scan_paths = await db.get_scan_paths()

        last_scan_update = 0.0
        loop = asyncio.get_running_loop()

        def on_progress(progress: dict):
            scan_state["current"] = progress.get("current")
            scan_state["total_files"] = progress.get("total_files", 0)
            scan_state["processed_files"] = progress.get("processed_files", 0)
            scan_state["matched"] = progress.get("matched", 0)
            scan_state["unmatched"] = progress.get("unmatched", 0)

            nonlocal last_scan_update
            now = datetime.utcnow().timestamp()
            if now - last_scan_update < 0.5:
                return
            last_scan_update = now
            if job_id:
                loop.call_soon_threadsafe(
                    asyncio.create_task,
                    db.update_job(
                        job_id,
                        status="running",
                        paused=1 if scan_state["paused"] else 0,
                        stopping=1 if scan_state["stopping"] else 0,
                        current=scan_state["current"],
                        total_files=scan_state["total_files"],
                        processed_files=scan_state["processed_files"],
                        matched=scan_state["matched"],
                        unmatched=scan_state["unmatched"],
                    )
                )

        result = await asyncio.to_thread(
            scan_folder_with_progress,
            scan_paths=scan_paths,
            recursive=recursive,
            on_progress=on_progress,
            pause_event=_scan_pause_event,
            stop_event=_scan_stop_event,
        )

        # Auto-indexing already-extracted loose folders only matters when the
        # pipeline is on (Atelier is where tracks are used). Check once.
        pipeline_on = await db.get_pipeline_enabled()
        auto_indexed_items = 0

        for item_data in result["items"].values():
            product_code = (item_data.get("product_code") or "").strip().upper()
            original_code = (item_data.get("original_code") or "").strip().upper()
            if product_code in ignored_codes or (original_code and original_code in ignored_codes):
                continue

            item_id = await db.upsert_item(
                {
                    "product_code": item_data["product_code"],
                    "original_code": item_data["original_code"],
                    "confidence": item_data.get("confidence", "low"),
                    "files": json.dumps(item_data["files"]),
                    "file_count": item_data["file_count"],
                    "total_size": item_data["total_size"],
                    "file_format": json.dumps(item_data["formats"]),
                }
            )
            if pipeline_on and item_id:
                if await _auto_index_loose_item(item_id, item_data["files"]):
                    auto_indexed_items += 1

        # Codeless folder imports: each top-level folder of audio/archives
        # without any DLsite-coded member becomes one manual entry (stable
        # MAN-<hash> code so rescans upsert, never duplicate). Files inside
        # an imported folder are claimed — they must stay out of unmatched.
        folder_claimed: set[str] = set()
        folders_imported = 0
        folders_skipped_owned = 0
        for fi in result.get("folder_imports") or []:
            if _scan_stop_event.is_set():
                break
            code = _stable_manual_code(fi["folder"])
            # A member already owned by an existing entry (e.g. a manual item
            # whose archive_path points inside this folder) means this folder
            # is that entry's home — importing it would duplicate the CD.
            # Exception: the owner being this same stable code (a rescan).
            # Folders that ARE a tokuten (tokutens.local_path) are skipped
            # the same way — they're already in the library.
            existing_self = await db.get_item_by_product_code(code)
            self_owned = set()
            if existing_self:
                self_owned = {
                    _basename_lower(f)
                    for f in json.loads(existing_self.get("files") or "[]")
                }
            try:
                folder_path_key = str(Path(fi["folder"]).resolve()).lower()
            except OSError:
                folder_path_key = str(fi["folder"]).strip().lower()
            if folder_path_key in claimed_local_paths or any(
                _basename_lower(f) in claimed_basenames
                and _basename_lower(f) not in self_owned
                for f in fi["files"]
            ):
                folders_skipped_owned += 1
                # Don't blanket-claim the folder: unowned siblings (say, a
                # Vol.2 the entry doesn't know about) should still surface
                # in the unmatched list instead of vanishing silently.
                logger.info(
                    f"Scan: folder {Path(fi['folder']).name} skipped — member file "
                    "already owned by an existing entry"
                )
                continue
            try:
                created = await _ingest_codeless_entry(
                    code, fi["title"], fi["files"], fi["total_size"],
                    fi["formats"], ignored_codes,
                )
            except Exception as exc:
                logger.warning(f"folder import failed for {fi['folder']}: {exc}")
                continue
            if code in ignored_codes:
                continue
            folder_claimed.update(str(f).lower() for f in fi["files"])
            if created:
                folders_imported += 1
                logger.info(f"Scan: folder {Path(fi['folder']).name} -> {code} (manual entry)")
            if pipeline_on:
                imported_item = await db.get_item_by_product_code(code)
                if imported_item and await _auto_index_loose_item(
                    imported_item["id"], fi["files"]
                ):
                    auto_indexed_items += 1
        if folders_imported:
            logger.info(f"Scan: {folders_imported} codeless folder(s) imported as manual entries")
        if auto_indexed_items:
            logger.info(
                f"Scan: {auto_indexed_items} already-extracted folder(s) indexed in place "
                "(no extraction needed)"
            )
        if folders_skipped_owned:
            logger.info(
                f"Scan: {folders_skipped_owned} folder(s) skipped — contents already owned by existing entries"
            )

        # Non-DLsite fallthrough: an unmatched archive (no DLsite code in its name) may
        # still declare its own identity + metadata via a bundled *.package.json. If so,
        # ingest it as a non-DLsite custom entry; otherwise shelve it as unmatched as before.
        # The filename scan is done by now, so move the progress UI off its (throttled) last
        # value while this post-pass runs - and check the stop flag so it stays cancellable.
        from pipeline.package_import import find_package_in_archive
        if job_id:
            await db.update_job(job_id, current="checking new archives…",
                                processed_files=result["stats"].get("total_files", 0))
        scan_root_keys = set()
        for raw_root in scan_paths or []:
            try:
                scan_root_keys.add(str(Path(raw_root).expanduser().resolve()).lower())
            except OSError:
                pass
        package_matched = 0
        loose_imported = 0
        skipped_owned = 0
        for uf in result["unmatched"]:
            if _scan_stop_event.is_set():
                break
            fp = uf["filepath"]
            # Claimed by a folder import above — not unmatched.
            if str(fp).lower() in folder_claimed:
                continue
            # Already owned by an existing entry (e.g. a manual/custom item)? Then it isn't
            # unmatched at all - keep it out of the list and don't peek it.
            if _basename_lower(fp) in claimed_basenames:
                skipped_owned += 1
                continue
            # Already seen as unmatched on a prior scan (same path+size)? It has no bundled
            # package - keep it unmatched but DON'T re-peek. Only genuinely new archives are
            # peeked, so a steady re-scan does ~zero archive reads here.
            seen_before = (str(fp).lower(), int(uf.get("size") or 0)) in prev_unmatched_keys
            code = None
            is_archive = Path(fp).suffix.lower() in ARCHIVE_EXTENSIONS
            if not seen_before and is_archive:
                try:
                    payload = await asyncio.to_thread(find_package_in_archive, fp)
                    if payload:
                        code = await _ingest_bundled_package_entry(payload, fp, uf["size"])
                except Exception as exc:
                    logger.warning(f"bundled-package fallthrough failed for {fp}: {exc}")
            if code:
                package_matched += 1
                logger.info(f"Scan: non-DLsite archive {Path(fp).name} -> {code} (bundled package metadata)")
                continue
            # Codeless TOP-LEVEL archive (sits directly in a scan root):
            # import as a manual entry from the cleaned filename. Multi-part
            # sets share one entry — the part marker is stripped from the
            # grouping key so every .partN upserts into the same code.
            try:
                parent_key = str(Path(fp).parent.resolve()).lower()
            except OSError:
                parent_key = ""
            if is_archive and parent_key in scan_root_keys:
                p = Path(fp)
                series_stem = re.sub(r"\.part\d+$", "", p.stem, flags=re.IGNORECASE)
                group_key = str(p.with_name(series_stem + p.suffix))
                loose_code = _stable_manual_code(group_key)
                try:
                    created = await _ingest_codeless_entry(
                        loose_code, clean_title(series_stem), [str(p)],
                        int(uf.get("size") or 0), [], ignored_codes,
                    )
                    if loose_code not in ignored_codes:
                        if created:
                            loose_imported += 1
                            logger.info(f"Scan: loose archive {p.name} -> {loose_code} (manual entry)")
                        continue
                except Exception as exc:
                    logger.warning(f"loose-archive import failed for {fp}: {exc}")
            await db.add_unmatched_file(uf["filename"], uf["filepath"], uf["size"])
        if package_matched:
            logger.info(f"Scan: {package_matched} non-DLsite archive(s) matched via bundled package")
        if loose_imported:
            logger.info(f"Scan: {loose_imported} loose codeless archive(s) imported as manual entries")
        if skipped_owned:
            logger.info(f"Scan: {skipped_owned} archive(s) already owned by an entry - kept out of unmatched")
        result["stats"]["folders_imported"] = folders_imported
        result["stats"]["loose_archives_imported"] = loose_imported

        stats = result["stats"]
        scan_state["result"] = stats
        scan_state["total_files"] = stats.get("total_files", 0)
        scan_state["processed_files"] = stats.get("processed_files", 0)
        scan_state["matched"] = stats.get("matched", 0)
        scan_state["unmatched"] = stats.get("unmatched", 0)
        logger.info(f"Scan complete: {stats}")
        if job_id:
            await db.update_job(
                job_id,
                status="completed",
                paused=0,
                stopping=0,
                stopped=1 if stats.get("stopped") else 0,
                current=None,
                total_files=scan_state["total_files"],
                processed_files=scan_state["processed_files"],
                matched=scan_state["matched"],
                unmatched=scan_state["unmatched"],
                result_json=stats,
                finished_at=_now_iso(),
            )
            await db.append_job_event(job_id, "info", "Scan completed", {"stats": stats})
        _log_job_event(
            "scan",
            "completed",
            job_id=job_id,
            total_files=scan_state["total_files"],
            processed_files=scan_state["processed_files"],
            matched=scan_state["matched"],
            unmatched=scan_state["unmatched"],
            stopped=bool(stats.get("stopped")),
        )

    except Exception as e:
        scan_state["error"] = str(e)
        logger.error(f"Scan failed: {e}")
        if job_id:
            await db.update_job(
                job_id,
                status="failed",
                paused=0,
                stopping=0,
                stopped=1,
                error=str(e),
                current=None,
                total_files=scan_state["total_files"],
                processed_files=scan_state["processed_files"],
                matched=scan_state["matched"],
                unmatched=scan_state["unmatched"],
                finished_at=_now_iso(),
            )
            await db.append_job_event(job_id, "error", "Scan failed", {"error": str(e)})
        _log_job_event("scan", "failed", job_id=job_id, error=str(e))
    finally:
        scan_state["running"] = False
        scan_state["paused"] = False
        scan_state["stopping"] = False
        scan_state["current"] = None
        scan_state["finished_at"] = _now_iso()
        _scan_pause_event.clear()
        _scan_stop_event.clear()


@router.post("/scan")
async def trigger_scan(request: ScanRequest, background_tasks: BackgroundTasks, _auth=Depends(require_api_key)):
    if scan_state["running"]:
        return {"status": "already_running"}

    scan_paths = request.paths or ([request.path] if request.path else None)
    background_tasks.add_task(run_scan, scan_paths, request.recursive)
    return {"status": "started"}


@router.get("/scan/status")
async def get_scan_status():
    latest = await _safe_latest_job("scan")
    state = _scan_state_from_job(latest)
    if not latest:
        state = dict(scan_state)
        state.setdefault("status", "running" if state.get("running") else "idle")
        state.setdefault("job_id", state.get("job_id"))
        state["recent_events"] = []
    else:
        state["recent_events"] = await db.get_job_events(latest["id"], limit=25)
    total = state.get("total_files") or 0
    processed = state.get("processed_files") or 0
    state["percent"] = int((processed / total) * 100) if total > 0 else 0
    return state


@router.post("/scan/pause")
async def pause_scan(_auth=Depends(require_api_key)):
    if not scan_state["running"]:
        return {"status": "idle"}
    _scan_pause_event.set()
    scan_state["paused"] = True
    _log_job_event("scan", "pause_requested", job_id=scan_state.get("job_id"))
    if scan_state.get("job_id"):
        await db.append_job_event(scan_state["job_id"], "info", "Pause requested")
    await _safe_update_latest_job("scan", {"running"}, status="paused", paused=1, stopping=0)
    return {"status": "paused"}


@router.post("/scan/resume")
async def resume_scan(_auth=Depends(require_api_key)):
    if not scan_state["running"]:
        return {"status": "idle"}
    _scan_pause_event.clear()
    scan_state["paused"] = False
    _log_job_event("scan", "resume_requested", job_id=scan_state.get("job_id"))
    if scan_state.get("job_id"):
        await db.append_job_event(scan_state["job_id"], "info", "Resume requested")
    await _safe_update_latest_job("scan", {"running", "paused"}, status="running", paused=0, stopping=0)
    return {"status": "resumed"}


@router.post("/scan/stop")
async def stop_scan(_auth=Depends(require_api_key)):
    if not scan_state["running"]:
        return {"status": "idle"}
    scan_state["stopping"] = True
    scan_state["paused"] = False
    _scan_pause_event.clear()
    _scan_stop_event.set()
    _log_job_event("scan", "stop_requested", job_id=scan_state.get("job_id"))
    if scan_state.get("job_id"):
        await db.append_job_event(scan_state["job_id"], "info", "Stop requested")
    await _safe_update_latest_job("scan", {"running", "paused", "stopping"}, status="stopping", paused=0, stopping=1)
    return {"status": "stopping"}


@router.get("/scan/paths")
async def get_scan_paths():
    paths = await db.get_scan_paths()
    return {"paths": paths}


@router.put("/scan/paths")
async def update_scan_paths(request: ScanPathsUpdateRequest, _auth=Depends(require_api_key)):
    try:
        paths = await db.set_scan_paths(request.paths)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "updated", "paths": paths}


async def run_fetch_metadata(product_codes: list[str] | None = None, force: bool = False):
    job_id = None
    try:
        job_id = await db.create_job("fetch_metadata", status="running", metadata={"force": force, "product_codes": product_codes})
    except Exception:
        job_id = None
    if job_id:
        await db.append_job_event(job_id, "info", "Metadata fetch started", {"force": force, "requested_count": len(product_codes or [])})
    scrape_progress["job_id"] = job_id
    _log_job_event("fetch_metadata", "started", job_id=job_id, force=force, requested_count=len(product_codes or []))

    if product_codes is None:
        result = await db.get_all_items(limit=10000)
        all_items = result["items"]
        # Manual/custom entries (MAN-*/TKT-*) have no DLsite page — never auto-fetch them.
        scanned_items = [item for item in all_items if not item.get("is_manual")]
        if force:
            product_codes = [item["product_code"] for item in scanned_items if item.get("product_code")]
        else:
            # Backfill mode: include items missing JP metadata or EN title/tag caches.
            product_codes = [
                item["product_code"]
                for item in scanned_items
                if item.get("product_code")
                and (
                    not item.get("title")
                    or not item.get("title_en")
                    or item.get("tags_en") in (None, "", "[]")
                )
            ]

    if not product_codes:
        scrape_progress.update(
            {
                "running": False,
                "paused": False,
                "stopping": False,
                "stopped": False,
                "total": 0,
                "completed": 0,
                "current": None,
                "errors": [],
                "success": 0,
                "failed": 0,
                "skipped": 0,
                "error_summary": {},
                "started_at": None,
                "finished_at": None,
            }
        )
        if job_id:
            await db.update_job(
                job_id,
                status="completed",
                stopped=1,
                total=0,
                completed=0,
                current=None,
                success=0,
                failed=0,
                skipped=0,
                errors_json=[],
                error_summary_json={},
                finished_at=_now_iso(),
            )
        _log_job_event("fetch_metadata", "completed_empty", job_id=job_id)
        return

    _fetch_pause_event.clear()
    _fetch_stop_event.clear()
    scrape_progress["paused"] = False
    scrape_progress["stopping"] = False
    scrape_progress["stopped"] = False

    if job_id:
        await db.update_job(job_id, total=len(product_codes), completed=0, paused=0, stopping=0, stopped=0, current=None)

    async def progress_cb(progress: dict):
        status = "running" if progress.get("running") else "completed"
        if progress.get("stopped"):
            status = "stopped"
        if job_id:
            await db.update_job(
                job_id,
                status=status,
                paused=1 if progress.get("paused") else 0,
                stopping=1 if progress.get("stopping") else 0,
                stopped=1 if progress.get("stopped") else 0,
                total=progress.get("total", 0),
                completed=progress.get("completed", 0),
                current=progress.get("current"),
                success=progress.get("success", 0),
                failed=progress.get("failed", 0),
                skipped=progress.get("skipped", 0),
                errors_json=progress.get("errors", []),
                error_summary_json=progress.get("error_summary", {}),
                finished_at=progress.get("finished_at"),
            )
        completed = progress.get("completed", 0) or 0
        total = progress.get("total", 0) or 0
        if completed and (completed % 25 == 0 or completed == total):
            _log_job_event(
                "fetch_metadata",
                "progress",
                job_id=job_id,
                completed=completed,
                total=total,
                success=progress.get("success", 0),
                failed=progress.get("failed", 0),
                skipped=progress.get("skipped", 0),
            )

    await fetch_all_metadata(
        product_codes,
        force=force,
        pause_event=_fetch_pause_event,
        stop_event=_fetch_stop_event,
        progress_callback=progress_cb,
    )
    if job_id:
        await db.append_job_event(job_id, "info", "Metadata fetch finished", {"total": len(product_codes)})
    _log_job_event("fetch_metadata", "finished", job_id=job_id, total=len(product_codes))

    _fetch_pause_event.clear()
    _fetch_stop_event.clear()


@router.post("/fetch-metadata")
async def trigger_fetch_metadata(request: FetchMetadataRequest, background_tasks: BackgroundTasks, _auth=Depends(require_api_key)):
    if scrape_progress.get("running"):
        return {"status": "already_running"}

    background_tasks.add_task(run_fetch_metadata, request.product_codes, request.force)
    return {"status": "started", "message": "Metadata fetching started in background"}




@router.post("/fetch-metadata/pause")
async def pause_fetch_metadata(_auth=Depends(require_api_key)):
    if not scrape_progress.get("running"):
        return {"status": "idle"}
    _fetch_pause_event.set()
    scrape_progress["paused"] = True
    _log_job_event("fetch_metadata", "pause_requested", job_id=scrape_progress.get("job_id"))
    if scrape_progress.get("job_id"):
        await db.append_job_event(scrape_progress["job_id"], "info", "Pause requested")
    await _safe_update_latest_job("fetch_metadata", {"running"}, status="paused", paused=1, stopping=0)
    return {"status": "paused"}


@router.post("/fetch-metadata/resume")
async def resume_fetch_metadata(_auth=Depends(require_api_key)):
    if not scrape_progress.get("running"):
        return {"status": "idle"}
    _fetch_pause_event.clear()
    scrape_progress["paused"] = False
    _log_job_event("fetch_metadata", "resume_requested", job_id=scrape_progress.get("job_id"))
    if scrape_progress.get("job_id"):
        await db.append_job_event(scrape_progress["job_id"], "info", "Resume requested")
    await _safe_update_latest_job("fetch_metadata", {"running", "paused"}, status="running", paused=0, stopping=0)
    return {"status": "resumed"}


@router.post("/fetch-metadata/stop")
async def stop_fetch_metadata(_auth=Depends(require_api_key)):
    if not scrape_progress.get("running"):
        return {"status": "idle"}
    scrape_progress["stopping"] = True
    scrape_progress["paused"] = False
    _fetch_pause_event.clear()
    _fetch_stop_event.set()
    _log_job_event("fetch_metadata", "stop_requested", job_id=scrape_progress.get("job_id"))
    if scrape_progress.get("job_id"):
        await db.append_job_event(scrape_progress["job_id"], "info", "Stop requested")
    await _safe_update_latest_job("fetch_metadata", {"running", "paused", "stopping"}, status="stopping", paused=0, stopping=1)
    return {"status": "stopping"}

@router.get("/fetch-metadata/status")
async def get_fetch_status():
    latest = await _safe_latest_job("fetch_metadata")
    if latest:
        state = _fetch_state_from_job(latest)
        state["recent_events"] = await db.get_job_events(latest["id"], limit=25)
        return state
    scrape_progress["recent_events"] = []
    return scrape_progress
