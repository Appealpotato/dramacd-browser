import asyncio
import json
import logging
import threading
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends

import database as db
from auth import require_api_key
from models import FetchMetadataRequest, ScanPathsUpdateRequest, ScanRequest
from scanner import scan_folder_with_progress
from scraper import fetch_all_metadata, scrape_progress

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _now_iso():
    return datetime.utcnow().isoformat() + "Z"


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

        for item_data in result["items"].values():
            product_code = (item_data.get("product_code") or "").strip().upper()
            original_code = (item_data.get("original_code") or "").strip().upper()
            if product_code in ignored_codes or (original_code and original_code in ignored_codes):
                continue

            await db.upsert_item(
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

        for uf in result["unmatched"]:
            await db.add_unmatched_file(uf["filename"], uf["filepath"], uf["size"])

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
        if force:
            product_codes = [item["product_code"] for item in all_items if item.get("product_code")]
        else:
            # Backfill mode: include items missing JP metadata or EN title/tag caches.
            product_codes = [
                item["product_code"]
                for item in all_items
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
