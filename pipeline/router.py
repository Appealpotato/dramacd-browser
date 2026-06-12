import asyncio
import hashlib
import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

import database as db
from auth import require_api_key
from config import FFMPEG_PATH, WHISPER_MODEL
from models import (
    AutopilotRequest,
    AutoTranslateRequest,
    AutoTranscribeRequest,
    PipelineExtractRequest,
    PipelineToggleRequest,
    SegmentTextUpdateRequest,
    TranscriptRunCreateRequest,
    TranslationRunCreateRequest,
)
from pipeline.autopilot_job import run_autopilot_job
from pipeline.extractor import _resolve_archives_for_item, get_runtime_archive_support, list_archive_contents, run_extraction_job, stream_archive_file
from pipeline.service import queue_autopilot, queue_extraction, queue_transcription, queue_translation
from pipeline.package_io import (
    build_package_zip,
    build_tracklist_data,
    build_tracklist_text,
    extract_transcripts_json_from_zip,
    looks_like_zip,
)
from pipeline.transcript_io import build_export_payload, import_payload
from pipeline.translation_job import run_translation_job
from pipeline.whisper_job import run_transcription_job
from text_cleaning import build_clean_translation_source

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


def _whisper_runtime_ready() -> bool:
    if FFMPEG_PATH:
        ffmpeg_path = Path(FFMPEG_PATH)
        if ffmpeg_path.is_file():
            return True
    return shutil.which("ffmpeg") is not None


async def _ensure_pipeline_enabled():
    enabled = await db.get_pipeline_enabled()
    if not enabled:
        raise HTTPException(status_code=403, detail="Pipeline is disabled")


@router.get("/status")
async def get_pipeline_status():
    return {
        "enabled": await db.get_pipeline_enabled(),
        "extraction_mode": "on_demand",
        "auto_extract_on_scan": False,
        "archive_support": get_runtime_archive_support(),
    }


@router.put("/enabled")
async def set_pipeline_enabled(request: PipelineToggleRequest, _auth=Depends(require_api_key)):
    enabled = await db.set_pipeline_enabled(bool(request.enabled))
    return {"status": "updated", "enabled": enabled}


@router.post("/items/{item_id}/extract")
async def queue_item_extraction(
    item_id: int,
    request: PipelineExtractRequest,
    background_tasks: BackgroundTasks,
    _auth=Depends(require_api_key),
):
    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    job_id = await queue_extraction(item_id=item_id, force=request.force)
    background_tasks.add_task(run_extraction_job, job_id)
    return {
        "status": "queued",
        "job_id": job_id,
        "item_id": item_id,
        "force": bool(request.force),
        "mode": "on_demand",
    }


@router.post("/items/{item_id}/import-subtitles")
async def import_item_subtitles(item_id: int, _auth=Depends(require_api_key)):
    """Scan this item's extracted tracks for sibling .vtt/.srt scripts and import them as
    transcript runs (tracks that already have an active transcript are left untouched).
    Fast - no Whisper. Used to backfill already-extracted works."""
    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    from pipeline.subtitle_import import import_bundled_subtitles
    summary = await import_bundled_subtitles(item_id)
    return {"status": "ok", "item_id": item_id, **summary}


@router.post("/items/{item_id}/autopilot")
async def queue_item_autopilot(
    item_id: int,
    request: AutopilotRequest,
    background_tasks: BackgroundTasks,
    _auth=Depends(require_api_key),
):
    """Queue the full per-item pipeline (metadata -> extract -> track titles -> transcribe -> translate)."""
    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    # Validate skip_stages values up front so callers get an immediate error.
    valid_stages = {
        "metadata_translate",
        "glossary_build",
        "extract",
        "track_titles_translate",
        "transcribe",
        "track_translate",
    }
    if request.skip_stages:
        unknown = [s for s in request.skip_stages if s not in valid_stages]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown skip_stages: {unknown}. Valid: {sorted(valid_stages)}",
            )

    job_id = await queue_autopilot(
        item_id=item_id,
        target_language=request.target_language,
        provider=(request.provider or None),
        model=(request.model or None),
        max_tokens_per_chunk=request.max_tokens_per_chunk,
        max_lines_per_chunk=request.max_lines_per_chunk,
        max_retries_per_chunk=request.max_retries_per_chunk,
        retry_backoff_seconds=request.retry_backoff_seconds,
        glossary=request.glossary,
        character_memory=request.character_memory,
        review_pass=request.review_pass,
        glossary_feedback=request.glossary_feedback,
        transcribe_language=request.transcribe_language,
        transcribe_model=request.transcribe_model,
        skip_stages=request.skip_stages,
        force_extract=request.force_extract,
        force_transcribe=request.force_transcribe,
        force_translate=request.force_translate,
    )
    background_tasks.add_task(run_autopilot_job, job_id)
    return {
        "status": "queued",
        "job_id": job_id,
        "item_id": item_id,
    }


@router.delete("/items/{item_id}/transcripts/redundant")
async def delete_redundant_transcript_runs_for_item(item_id: int, _auth=Depends(require_api_key)):
    """Per-track cleanup, fanned out across every track in the item. For each
    track, keeps the active transcript run + the transcript anchor of the
    active translation; deletes the rest (with FK cascade). Use this to clean
    up the duplicate sibling runs the auto workflow spawned before the
    deduplication fix landed."""
    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    tracks = await db.get_pipeline_tracks(item_id)
    total_deleted = 0
    total_kept = 0
    per_track: list[dict] = []
    for t in tracks:
        track_id = int(t["id"])
        active = await db.get_track_active_outputs(track_id)
        keep_ids: set[int] = set()
        active_transcript_id = active.get("active_transcript_run_id")
        if active_transcript_id:
            keep_ids.add(int(active_transcript_id))
        active_translation_id = active.get("active_translation_run_id")
        if active_translation_id:
            tl_run = await db.get_translation_run(int(active_translation_id))
            if tl_run and tl_run.get("transcript_run_id"):
                keep_ids.add(int(tl_run["transcript_run_id"]))
        runs = await db.list_transcript_runs(track_id)
        deleted = []
        for r in runs:
            rid = int(r["id"])
            if rid in keep_ids:
                continue
            await db.delete_transcript_segments(rid)
            await db.delete_transcript_run(rid)
            deleted.append(rid)
        total_deleted += len(deleted)
        total_kept += len(keep_ids)
        if deleted:
            per_track.append({
                "track_id": track_id,
                "deleted_run_ids": deleted,
                "kept_run_ids": sorted(keep_ids),
            })
    return {
        "item_id": item_id,
        "deleted_count": total_deleted,
        "kept_count": total_kept,
        "per_track": per_track,
    }


@router.get("/items/{item_id}/archive-contents")
async def list_item_archive_contents(item_id: int):
    """List the files inside the item's source archive(s) — read-only, no
    extraction. Used by the Workshop Archive panel's inline viewer so the
    user can see what's in the archive without opening Explorer or 7-Zip.
    Multi-archive items return the union; multi-volume RARs are handled by
    ``_resolve_archives_for_item`` (first part only)."""
    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    scan_paths = await db.get_scan_paths()
    archives = _resolve_archives_for_item(item, scan_paths)
    if not archives:
        return {"item_id": item_id, "archives": [], "files": [], "warning": "No source archive found in scan paths"}
    out_archives: list[dict] = []
    all_files: list[dict] = []
    for archive in archives:
        try:
            files = list_archive_contents(archive)
        except Exception as exc:
            out_archives.append({"name": archive.name, "error": str(exc)[:300], "file_count": 0})
            continue
        out_archives.append({"name": archive.name, "file_count": len(files)})
        for f in files:
            all_files.append({**f, "archive": archive.name})
    all_files.sort(key=lambda f: (f["archive"], f["path"].lower()))
    return {"item_id": item_id, "archives": out_archives, "files": all_files}


@router.get("/items/{item_id}/archive-thumb")
async def get_archive_thumb(item_id: int, path: str = Query(..., min_length=1)):
    """Extract a single image from the item's source archive(s), thumbnail it
    via Pillow, and serve the JPEG. Results cache on disk under
    ``data/pipeline/archive-thumbs/{item_id}/{sha1(path)}.jpg`` so the second
    visit is a static-file read instead of a 7z stream + image resize."""
    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    # Sanity: only thumbnail recognized image extensions.
    ext = Path(path).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        raise HTTPException(status_code=400, detail="Not an image extension")

    from config import PIPELINE_WORK_DIR
    cache_dir = PIPELINE_WORK_DIR / "archive-thumbs" / str(item_id)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_name = hashlib.sha1(path.encode("utf-8")).hexdigest() + ".jpg"
    cache_path = cache_dir / cache_name

    if cache_path.exists():
        return Response(content=cache_path.read_bytes(), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})

    scan_paths = await db.get_scan_paths()
    archives = _resolve_archives_for_item(item, scan_paths)
    if not archives:
        raise HTTPException(status_code=404, detail="No source archive found")

    # 7z accepts a relative inner path. Try each archive in turn — for items
    # with multiple archives (rare) the first one to yield bytes wins.
    raw = None
    last_err = None
    for archive in archives:
        try:
            raw = stream_archive_file(archive, path)
            if raw:
                break
        except Exception as exc:
            last_err = exc
    if not raw:
        raise HTTPException(status_code=404, detail=f"Couldn't extract {path}: {last_err}")

    try:
        from PIL import Image
        from io import BytesIO
        img = Image.open(BytesIO(raw))
        img.thumbnail((240, 240), Image.LANCZOS)
        # Convert RGBA/P → RGB before JPEG-encoding.
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        out = BytesIO()
        img.save(out, "JPEG", quality=82, optimize=True)
        thumb_bytes = out.getvalue()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Thumbnail render failed: {exc}")

    cache_path.write_bytes(thumb_bytes)
    return Response(content=thumb_bytes, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/items/{item_id}/extract/status")
async def get_item_extraction_status(item_id: int):
    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    latest = await db.get_latest_job_for_item("pipeline_extract", item_id)
    if not latest:
        return {"status": "idle", "item_id": item_id, "job": None}
    return {"status": latest.get("status") or "unknown", "item_id": item_id, "job": latest}


@router.post("/items/{item_id}/backfill-summaries")
async def backfill_track_summaries(item_id: int, force: bool = False, _auth=Depends(require_api_key)):
    """Generate per-track context summaries in track-index order so each track's
    summary sees its predecessors (last 2). Skips tracks that already have a
    summary unless ``force=True`` is passed."""
    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    tracks = await db.get_pipeline_tracks(item_id)
    if not tracks:
        raise HTTPException(status_code=400, detail="No tracks for this item")

    # Resolve provider + key now so we fail fast.
    provider = await db.get_runtime_translation_provider()
    base_url = None
    request_format = "openai"
    if provider == "openrouter":
        api_key = await db.get_runtime_openrouter_api_key()
        model = await db.get_runtime_openrouter_model()
    elif provider == "chutes":
        api_key = await db.get_runtime_chutes_api_key()
        model = await db.get_runtime_chutes_model()
    elif provider == "openai_compat":
        api_key = await db.get_runtime_openai_compat_api_key()
        model = await db.get_runtime_openai_compat_model()
        base_url = await db.get_runtime_openai_compat_base_url()
        request_format = await db.get_runtime_openai_compat_request_format()
        if not (base_url and model):
            raise HTTPException(status_code=400, detail="openai_compat provider needs base URL + model")
    else:
        api_key = await db.get_runtime_gemini_api_key()
        model = await db.get_runtime_gemini_model()
        provider = "gemini"
    # openai_compat servers are typically local/keyless (LM Studio, Ollama…);
    # base URL + model is a complete configuration for them.
    if not api_key and provider != "openai_compat":
        raise HTTPException(status_code=400, detail=f"Active provider '{provider}' has no API key")

    drama_description = str(item.get("description_en") or item.get("description") or "").strip()

    from pipeline.track_summarizer import TrackSummarizer
    summarizer = TrackSummarizer(
        api_key=api_key,
        model=model,
        provider=provider,
        base_url=base_url,
        request_format=request_format,
    )

    sorted_tracks = sorted(tracks, key=lambda t: int(t.get("track_index") or 0))
    generated = 0
    skipped = 0
    failed: list[dict] = []
    for track in sorted_tracks:
        track_id = int(track["id"])
        if track.get("track_summary_json") and not force:
            skipped += 1
            continue
        active = await db.get_track_active_outputs(track_id)
        active_run_id = active.get("active_transcript_run_id")
        if not active_run_id:
            runs = await db.list_transcript_runs(track_id)
            if not runs:
                failed.append({"track_id": track_id, "reason": "no transcript run"})
                continue
            active_run_id = int(runs[0]["id"])
        segments = await db.get_transcript_segments(int(active_run_id))
        if not segments:
            failed.append({"track_id": track_id, "reason": "transcript has no segments"})
            continue

        previous_raw = await db.get_previous_track_summaries(
            item_id=item_id,
            current_track_index=int(track.get("track_index") or 0),
            limit=2,
        )
        previous_summaries = []
        for row in previous_raw:
            js = row.get("summary_json")
            if js:
                try:
                    previous_summaries.append(json.loads(js))
                except Exception:
                    pass

        try:
            summary = await summarizer.generate_summary(
                track_number=int(track.get("track_index") or 0),
                segments=segments,
                drama_description=drama_description,
                previous_summaries=previous_summaries,
            )
            if summary:
                await db.set_track_summary(track_id, json.dumps(summary, ensure_ascii=False))
                generated += 1
            else:
                failed.append({"track_id": track_id, "reason": "summary returned empty"})
        except Exception as exc:  # noqa: BLE001
            failed.append({"track_id": track_id, "reason": str(exc)[:200]})

    return {
        "status": "ok",
        "item_id": item_id,
        "provider": provider,
        "generated": generated,
        "skipped_existing": skipped,
        "failed": failed,
        "total": len(sorted_tracks),
    }


@router.post("/maintenance/fix-mojibake")
async def fix_mojibake_titles(dry_run: bool = True, _auth=Depends(require_api_key)):
    """Scan every track's title for cp437-presented CP932 mojibake (e.g.
    ``âiâCâgâIâuâcâCâôâY`` → ``ナイトオブツインズ``) and rewrite the DB title
    when recovery succeeds. Pass ``dry_run=false`` to apply changes."""
    await _ensure_pipeline_enabled()
    from pipeline.extractor import try_recover_mojibake

    items_page = await db.get_all_items(limit=100000, offset=0)
    items = items_page.get("items") or []

    fixes: list[dict] = []
    for item in items:
        item_id = int(item["id"])
        tracks = await db.get_pipeline_tracks(item_id)
        for track in tracks:
            title = (track.get("title") or "").strip()
            recovered = try_recover_mojibake(title)
            if recovered:
                fixes.append({
                    "item_id": item_id,
                    "product_code": item.get("product_code"),
                    "track_id": int(track["id"]),
                    "track_index": track.get("track_index"),
                    "before": title,
                    "after": recovered,
                })

    applied = 0
    if not dry_run:
        for fix in fixes:
            inst = await db.get_db()
            try:
                await inst.execute(
                    "UPDATE pipeline_tracks SET title = ?, updated_at = ? WHERE id = ?",
                    (fix["after"], datetime.now().isoformat(), fix["track_id"]),
                )
                await inst.commit()
                applied += 1
            finally:
                await inst.close()

    return {
        "status": "ok",
        "dry_run": dry_run,
        "candidates": len(fixes),
        "applied": applied,
        "items_scanned": len(items),
        "preview": fixes[:20],
        "total_fixes": fixes if not dry_run else [],
    }


@router.post("/maintenance/fix-mojibake-paths")
async def fix_mojibake_paths(dry_run: bool = True, _auth=Depends(require_api_key)):
    """For each track whose track_path contains cp437-presented CP932 mojibake
    in any path segment, rename the on-disk folder/file to its recovered form
    and update track_path + extract_root in the DB.

    Algorithm: collect every unique (old_segment_path, new_segment_path) pair
    across all tracks, then rename deepest-first so children get renamed before
    their parents (otherwise the parent rename invalidates child paths).

    Pass ``dry_run=false`` to actually move files."""
    await _ensure_pipeline_enabled()
    from config import PIPELINE_EXTRACT_DIR
    from pipeline.extractor import try_recover_mojibake

    try:
        workspace_root = PIPELINE_EXTRACT_DIR.resolve()
    except OSError:
        workspace_root = PIPELINE_EXTRACT_DIR

    page = await db.get_all_items(limit=100000, offset=0)
    items = page.get("items") or []

    # Per-track plans: which tracks would have their track_path/extract_root
    # rewritten, plus the cumulative rename ladder for each.
    track_plans: list[dict] = []
    rename_pairs: dict[str, tuple[Path, Path]] = {}  # key: str(old) → (old, new)
    skipped_outside_workspace = 0

    for it in items:
        tracks = await db.get_pipeline_tracks(int(it["id"]))
        for t in tracks:
            old = (t.get("track_path") or "").strip()
            if not old:
                continue
            old_path = Path(old)
            # Loose in-place tracks point at the user's ORIGINAL library files.
            # Mojibake recovery is strictly for workspace extractions (where WE
            # produced the mangled names) — never rename anything outside the
            # workspace, and never trust the heuristic on the user's own naming.
            try:
                old_path.resolve().relative_to(workspace_root)
            except (ValueError, OSError):
                skipped_outside_workspace += 1
                continue
            old_parts = list(old_path.parts)
            new_parts: list[str] = []
            cum_old = None
            cum_new = None
            for i, seg in enumerate(old_parts):
                rec = try_recover_mojibake(seg)
                new_seg = rec if rec else seg
                new_parts.append(new_seg)
                cum_old = Path(*old_parts[: i + 1])
                cum_new = Path(*new_parts[: i + 1])
                if rec and rec != seg:
                    key = str(cum_old)
                    if key not in rename_pairs:
                        rename_pairs[key] = (cum_old, cum_new)

            if new_parts == old_parts:
                continue

            new_path = str(Path(*new_parts))
            new_extract_root = None
            old_extract_root = (t.get("extract_root") or "").strip()
            if old_extract_root:
                er_parts = list(Path(old_extract_root).parts)
                new_er = []
                for seg in er_parts:
                    rec = try_recover_mojibake(seg)
                    new_er.append(rec if rec else seg)
                if new_er != er_parts:
                    new_extract_root = str(Path(*new_er))

            track_plans.append({
                "track_id": int(t["id"]),
                "item_id": int(it["id"]),
                "product_code": it.get("product_code"),
                "old_path": old,
                "new_path": new_path,
                "old_extract_root": old_extract_root or None,
                "new_extract_root": new_extract_root,
            })

    # Sort renames deepest-first so a leaf gets renamed before its parent dir
    # (renaming the parent first would relocate the child and break its old path).
    pairs = list(rename_pairs.values())
    pairs.sort(key=lambda x: len(x[0].parts), reverse=True)

    if dry_run:
        return {
            "status": "ok",
            "dry_run": True,
            "track_candidates": len(track_plans),
            "rename_steps": len(pairs),
            "skipped_outside_workspace": skipped_outside_workspace,
            "preview_renames": [{"old": str(o), "new": str(n)} for (o, n) in pairs[:20]],
            "preview_tracks": track_plans[:20],
        }

    applied: list[dict] = []
    failed: list[dict] = []
    for old_p, new_p in pairs:
        try:
            if not old_p.exists():
                # Already moved (e.g., via an ancestor rename in another item)
                applied.append({"old": str(old_p), "new": str(new_p), "skipped": "already-renamed"})
                continue
            new_p.parent.mkdir(parents=True, exist_ok=True)
            old_p.rename(new_p)
            applied.append({"old": str(old_p), "new": str(new_p)})
        except OSError as exc:
            failed.append({"old": str(old_p), "new": str(new_p), "error": str(exc)})

    # Update DB. Even if some renames failed, we update the rows whose new_path
    # exists on disk (best-effort).
    db_updated = 0
    db_skipped: list[dict] = []
    for plan in track_plans:
        new_path = plan["new_path"]
        if Path(new_path).exists():
            inst = await db.get_db()
            try:
                await inst.execute(
                    "UPDATE pipeline_tracks SET track_path = ?, extract_root = COALESCE(?, extract_root), updated_at = ? WHERE id = ?",
                    (new_path, plan.get("new_extract_root"), datetime.now().isoformat(), plan["track_id"]),
                )
                await inst.commit()
                db_updated += 1
            finally:
                await inst.close()
        else:
            db_skipped.append({"track_id": plan["track_id"], "new_path": new_path, "reason": "new path missing on disk"})

    return {
        "status": "ok",
        "dry_run": False,
        "renames_attempted": len(pairs),
        "renames_applied": len(applied),
        "renames_failed": failed,
        "tracks_updated": db_updated,
        "tracks_skipped": db_skipped,
        "skipped_outside_workspace": skipped_outside_workspace,
    }


@router.post("/items/{item_id}/translate-track-names")
async def translate_track_names(item_id: int, _auth=Depends(require_api_key)):
    """Translate every track's JA title to English in one batch using the active provider."""
    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    tracks = await db.get_pipeline_tracks(item_id)
    if not tracks:
        raise HTTPException(status_code=400, detail="No tracks for this item")

    pairs = [(int(t["id"]), int(t.get("track_index") or 0), str(t.get("title") or "").strip()) for t in tracks]
    pairs = [p for p in pairs if p[2]]
    if not pairs:
        raise HTTPException(status_code=400, detail="No track titles to translate")

    drama_title = str(item.get("title_en") or item.get("title") or "").strip()
    drama_description = str(item.get("description_en") or item.get("description") or "").strip()
    payload = [{"i": idx, "title": title} for (_tid, idx, title) in pairs]
    prompt = (
        "Translate Japanese drama CD track titles to natural, idiomatic English.\n\n"
        "REQUIREMENTS:\n"
        "• Return ONLY a JSON object with key 'titles': an array of {\"i\": <int>, \"title_en\": <string>}.\n"
        "• Preserve every 'i' value exactly as provided. Return one entry per input.\n"
        "• Keep titles short and natural. Do not add commentary, just the translation.\n"
        "• If a title is already English or untranslatable, return it unchanged.\n"
        "• Use the drama description below to disambiguate tone, setting, character names, "
        "and recurring terms — title-only translation often loses these.\n\n"
        f"Drama title: {drama_title or '(none)'}\n"
        f"Drama description: {drama_description or '(none)'}\n\n"
        f"Track titles to translate (JSON):\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Respond with the JSON object only:"
    )

    from routers.api import _llm_one_shot_json, _parse_json_payload, _coerce_translation_payload  # local import avoids cycles
    raw, provider_used = await _llm_one_shot_json(prompt)
    try:
        parsed = _parse_json_payload(raw)
    except Exception:
        raise HTTPException(status_code=502, detail=f"Provider returned invalid JSON: {raw[:200]}")

    rows = parsed.get("titles") if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail=f"Provider response missing 'titles' array: {str(parsed)[:200]}")

    by_index: dict[int, str] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            i = int(r.get("i"))
        except (TypeError, ValueError):
            continue
        en = str(r.get("title_en") or r.get("text") or "").strip()
        if en:
            by_index[i] = en

    updates: list[tuple[int, str]] = []
    result_rows = []
    for (track_id, idx, title) in pairs:
        en = by_index.get(idx)
        if en:
            updates.append((track_id, en))
            result_rows.append({"track_id": track_id, "track_index": idx, "title": title, "title_en": en})
        else:
            result_rows.append({"track_id": track_id, "track_index": idx, "title": title, "title_en": None})

    await db.bulk_set_track_titles_en(updates)
    return {
        "status": "ok",
        "item_id": item_id,
        "provider": provider_used,
        "translated_count": len(updates),
        "total": len(pairs),
        "tracks": result_rows,
    }


@router.get("/items/{item_id}/tracks")
async def list_item_tracks(item_id: int):
    # Read-only — used by both Workshop and Player. The Player tab needs to
    # work even when the pipeline (write-side) is disabled.
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    tracks = await db.get_pipeline_tracks(item_id)
    # Track rows outlive the extracted files (workspace cleanup, manual
    # deletes), so report per-track on-disk presence. Player/Atelier use it
    # to prompt "re-extract" instead of silently failing at play/export time.
    audio_missing = 0
    for t in tracks:
        tp = (t.get("track_path") or "").strip()
        exists = bool(tp) and Path(tp).is_file()
        t["file_exists"] = exists
        if not exists:
            audio_missing += 1
    return {
        "item_id": item_id,
        "tracks": tracks,
        "total": len(tracks),
        "audio_missing": audio_missing,
    }


@router.get("/items/{item_id}/track-groups")
async def list_item_track_groups(item_id: int):
    """
    Tracks grouped by filename + duration so FLAC/MP3 of the same audio appear as
    one row. Used by the Workshop transcription/transcribed lists.
    """
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    preferred_variant = await db.get_runtime_whisper_preferred_variant()
    groups = await db.get_pipeline_track_groups(item_id, preferred_variant=preferred_variant)
    return {"item_id": item_id, "groups": groups, "total": len(groups)}


@router.post("/items/{item_id}/auto-transcribe")
async def queue_item_transcription(
    item_id: int,
    request: AutoTranscribeRequest,
    background_tasks: BackgroundTasks,
    _auth=Depends(require_api_key),
):
    import logging
    logger = logging.getLogger(__name__)

    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    tracks = await db.get_pipeline_tracks(item_id)
    if not tracks:
        raise HTTPException(status_code=400, detail="No extracted tracks found for this item")

    model = request.model or await db.get_runtime_whisper_model()

    if not _whisper_runtime_ready():
        raise HTTPException(
            status_code=400,
            detail="Whisper transcription requires ffmpeg. Install ffmpeg or set DRAMACD_FFMPEG_PATH to ffmpeg.exe.",
        )

    logger.info(f"Queue transcribe: item={item_id}, track_ids={request.track_ids}, total_tracks={len(tracks)}")

    # Filter tracks if specific track_ids provided
    if request.track_ids:
        selected_track_ids = set(request.track_ids)
        tracks = [t for t in tracks if t.get("id") in selected_track_ids]
        logger.info(f"After filtering: {len(tracks)} tracks selected from {selected_track_ids}")

    if not tracks:
        raise HTTPException(status_code=400, detail="No valid tracks selected for transcription")

    job_id = await queue_transcription(item_id=item_id, language=request.language, model=model, force=request.force, track_ids=request.track_ids or [])
    background_tasks.add_task(run_transcription_job, job_id)
    return {
        "status": "queued",
        "job_id": job_id,
        "item_id": item_id,
        "language": request.language,
        "model": model,
        "tracks_queued": len(tracks),
        "debug_track_ids": request.track_ids,
        "debug_filtered": bool(request.track_ids),
    }


@router.post("/tracks/{track_id}/auto-translate")
async def queue_track_auto_translation(
    track_id: int,
    request: AutoTranslateRequest,
    background_tasks: BackgroundTasks,
    _auth=Depends(require_api_key),
):
    await _ensure_pipeline_enabled()
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    active_outputs = await db.get_track_active_outputs(track_id)
    if request.only_if_missing and active_outputs.get("active_translation_run_id"):
        return {
            "status": "skipped",
            "reason": "already_translated",
            "track_id": track_id,
            "active_translation_run_id": active_outputs.get("active_translation_run_id"),
        }
    transcript_run_id = request.transcript_run_id or active_outputs.get("active_transcript_run_id")
    if not transcript_run_id:
        raise HTTPException(status_code=400, detail="Set transcript run id or active transcript first")

    transcript_run = await db.get_transcript_run(int(transcript_run_id))
    if not transcript_run or int(transcript_run.get("track_id", -1)) != int(track_id):
        raise HTTPException(status_code=404, detail="Transcript run not found for this track")

    transcript_segments = await db.get_transcript_segments(int(transcript_run_id))
    if not transcript_segments:
        raise HTTPException(status_code=400, detail="Transcript run has no segments")

    provider = str(request.provider or "gemini").strip().lower()
    if provider not in db.SUPPORTED_TRANSLATION_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail="provider must be one of: " + ", ".join(sorted(db.SUPPORTED_TRANSLATION_PROVIDERS)),
        )

    if provider == "gemini":
        runtime_model = await db.get_runtime_gemini_model()
    elif provider == "openrouter":
        runtime_model = await db.get_runtime_openrouter_model()
    elif provider == "openai_compat":
        runtime_model = await db.get_runtime_openai_compat_model()
    else:
        runtime_model = await db.get_runtime_chutes_model()
    item_id = int(track.get("item_id") or 0)
    job_id = await queue_translation(
        item_id=item_id,
        track_id=track_id,
        transcript_run_id=int(transcript_run_id),
        target_language=request.target_language,
        provider=provider,
        model=request.model or runtime_model,
        max_tokens_per_chunk=request.max_tokens_per_chunk,
        max_lines_per_chunk=request.max_lines_per_chunk,
        max_retries_per_chunk=max(0, min(6, int(request.max_retries_per_chunk))),
        retry_backoff_seconds=max(0.2, min(10.0, float(request.retry_backoff_seconds))),
        set_active=request.set_active,
        glossary=request.glossary,
        character_memory=request.character_memory,
        review_pass=request.review_pass,
        glossary_feedback=request.glossary_feedback,
    )
    background_tasks.add_task(run_translation_job, job_id)
    return {
        "status": "queued",
        "job_id": job_id,
        "track_id": track_id,
        "transcript_run_id": int(transcript_run_id),
        "provider": provider,
        "target_language": request.target_language,
        "segments_queued": len(transcript_segments),
        "max_tokens_per_chunk": request.max_tokens_per_chunk,
        "max_lines_per_chunk": request.max_lines_per_chunk,
        "max_retries_per_chunk": max(0, min(6, int(request.max_retries_per_chunk))),
        "retry_backoff_seconds": max(0.2, min(10.0, float(request.retry_backoff_seconds))),
    }


@router.get("/jobs")
async def get_pipeline_jobs(job_id: int | None = None, limit: int = 50, status: str | None = None):
    await _ensure_pipeline_enabled()
    if job_id:
        job = await db.get_job(job_id)
        if not job or not str(job.get("job_type", "")).startswith("pipeline_"):
            return {"jobs": []}
        if status and str(job.get("status", "")).lower() != status.lower():
            return {"jobs": []}
        return {"jobs": [job]}

    jobs = await db.get_recent_jobs(limit=max(1, min(limit, 200)))
    pipeline_jobs = [j for j in jobs if str(j.get("job_type", "")).startswith("pipeline_")]
    if status:
        pipeline_jobs = [j for j in pipeline_jobs if str(j.get("status", "")).lower() == status.lower()]
    return {"jobs": pipeline_jobs}


@router.get("/jobs/{job_id}/events")
async def get_pipeline_job_events(job_id: int, limit: int = 60):
    await _ensure_pipeline_enabled()
    job = await db.get_job(job_id)
    if not job or not str(job.get("job_type", "")).startswith("pipeline_"):
        raise HTTPException(status_code=404, detail="Pipeline job not found")
    events = await db.get_job_events(job_id, limit=max(1, min(limit, 200)))
    return {"job_id": job_id, "events": events}


@router.post("/jobs/{job_id}/pause")
async def pause_pipeline_job(job_id: int, _auth=Depends(require_api_key)):
    await _ensure_pipeline_enabled()
    job = await db.get_job(job_id)
    if not job or not str(job.get("job_type", "")).startswith("pipeline_"):
        raise HTTPException(status_code=404, detail="Pipeline job not found")
    status = str(job.get("status") or "").lower()
    if status not in {"running"}:
        raise HTTPException(status_code=400, detail=f"Cannot pause job in status '{status or 'unknown'}'")
    await db.update_job(job_id, status="paused", paused=1, stopping=0)
    await db.append_job_event(job_id, "info", "Pause requested", {})
    return {"status": "paused", "job_id": job_id}


@router.post("/jobs/{job_id}/resume")
async def resume_pipeline_job(job_id: int, _auth=Depends(require_api_key)):
    await _ensure_pipeline_enabled()
    job = await db.get_job(job_id)
    if not job or not str(job.get("job_type", "")).startswith("pipeline_"):
        raise HTTPException(status_code=404, detail="Pipeline job not found")
    status = str(job.get("status") or "").lower()
    if status not in {"paused"}:
        raise HTTPException(status_code=400, detail=f"Cannot resume job in status '{status or 'unknown'}'")
    await db.update_job(job_id, status="running", paused=0, stopping=0)
    await db.append_job_event(job_id, "info", "Resume requested", {})
    return {"status": "running", "job_id": job_id}


@router.post("/jobs/{job_id}/restart")
async def restart_pipeline_autopilot_job(
    job_id: int,
    background_tasks: BackgroundTasks,
    _auth=Depends(require_api_key),
):
    """Resume a stopped/failed autopilot by queueing a fresh autopilot job
    with the same metadata. Stage idempotency means already-done work
    (translated metadata, transcribed tracks, etc.) is skipped automatically."""
    await _ensure_pipeline_enabled()
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("job_type") != "pipeline_autopilot":
        raise HTTPException(status_code=400, detail="Restart only supported for autopilot jobs")
    metadata = job.get("metadata_json") or {}
    item_id = int(metadata.get("item_id") or 0)
    if item_id <= 0:
        raise HTTPException(status_code=400, detail="Original job has no item_id")

    new_job_id = await queue_autopilot(
        item_id=item_id,
        target_language=metadata.get("target_language") or "en",
        provider=metadata.get("provider"),
        model=metadata.get("model"),
        max_tokens_per_chunk=metadata.get("max_tokens_per_chunk"),
        max_lines_per_chunk=metadata.get("max_lines_per_chunk"),
        max_retries_per_chunk=int(metadata.get("max_retries_per_chunk") or 2),
        retry_backoff_seconds=float(metadata.get("retry_backoff_seconds") or 1.0),
        glossary=metadata.get("glossary"),
        character_memory=metadata.get("character_memory"),
        review_pass=bool(metadata.get("review_pass", False)),
        glossary_feedback=bool(metadata.get("glossary_feedback", True)),
        transcribe_language=metadata.get("transcribe_language") or "ja",
        transcribe_model=metadata.get("transcribe_model"),
        skip_stages=list(metadata.get("skip_stages") or []),
        force_extract=bool(metadata.get("force_extract", False)),
        force_transcribe=bool(metadata.get("force_transcribe", False)),
        force_translate=bool(metadata.get("force_translate", False)),
    )
    background_tasks.add_task(run_autopilot_job, new_job_id)
    return {
        "status": "queued",
        "job_id": new_job_id,
        "previous_job_id": job_id,
        "item_id": item_id,
    }


@router.post("/jobs/{job_id}/stop")
async def stop_pipeline_job(job_id: int):
    await _ensure_pipeline_enabled()
    job = await db.get_job(job_id)
    if not job or not str(job.get("job_type", "")).startswith("pipeline_"):
        raise HTTPException(status_code=404, detail="Pipeline job not found")
    status = str(job.get("status") or "").lower()
    if status not in {"running", "paused", "stopping"}:
        raise HTTPException(status_code=400, detail=f"Cannot stop job in status '{status or 'unknown'}'")
    await db.update_job(job_id, status="stopping", paused=0, stopping=1)
    await db.append_job_event(job_id, "info", "Stop requested", {})
    return {"status": "stopping", "job_id": job_id}


@router.post("/tracks/{track_id}/transcripts")
async def create_track_transcript_run(
    track_id: int,
    request: TranscriptRunCreateRequest,
    _auth=Depends(require_api_key),
):
    await _ensure_pipeline_enabled()
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if not request.segments:
        raise HTTPException(status_code=400, detail="At least one segment is required")

    run_id = await db.create_transcript_run(
        track_id=track_id,
        language=request.language,
        source=request.source,
        engine=request.engine,
        model=request.model,
        prompt=request.prompt,
        segments=[seg.model_dump() for seg in request.segments],
        metadata={"created_via": "api"},
    )
    if request.set_active:
        await db.set_track_active_transcript(track_id, run_id)
    run = await db.get_transcript_run(run_id)
    return {"status": "created", "run": run, "segments": len(request.segments)}


@router.get("/tracks/{track_id}/transcripts")
async def list_track_transcript_runs(track_id: int):
    # Read-only: needed by the Player tab; not gated on pipeline_enabled.
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    runs = await db.list_transcript_runs(track_id)
    active = await db.get_track_active_outputs(track_id)
    return {"track_id": track_id, "active": active, "runs": runs}


@router.get("/tracks/{track_id}/transcripts/{run_id}")
async def get_track_transcript_run(track_id: int, run_id: int):
    # Read-only: needed by the Player tab; not gated on pipeline_enabled.
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    run = await db.get_transcript_run(run_id)
    if not run or int(run.get("track_id")) != track_id:
        raise HTTPException(status_code=404, detail="Transcript run not found")
    segments = await db.get_transcript_segments(run_id)
    clean_source = build_clean_translation_source(segments)
    return {"track_id": track_id, "run": run, "segments": segments, "clean_source": clean_source}


@router.patch("/tracks/{track_id}/transcripts/{run_id}/segments/{segment_index}")
async def update_transcript_segment(
    track_id: int,
    run_id: int,
    segment_index: int,
    request: SegmentTextUpdateRequest,
    _auth=Depends(require_api_key),
):
    """Edit a single transcript segment's text. Used by the inline editor in
    the Player and Workshop views."""
    await _ensure_pipeline_enabled()
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    run = await db.get_transcript_run(run_id)
    if not run or int(run.get("track_id")) != track_id:
        raise HTTPException(status_code=404, detail="Transcript run not found for this track")
    updated = await db.update_transcript_segment_text(run_id, segment_index, request.text)
    if not updated:
        raise HTTPException(status_code=404, detail="Segment not found")
    return {"status": "updated", "segment": updated}


@router.put("/tracks/{track_id}/active-transcript/{run_id}")
async def set_track_active_transcript(track_id: int, run_id: int, _auth=Depends(require_api_key)):
    await _ensure_pipeline_enabled()
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    ok = await db.set_track_active_transcript(track_id, run_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Transcript run not found for this track")
    return {"status": "updated", "track_id": track_id, "active_transcript_run_id": run_id}


@router.post("/tracks/{track_id}/translations")
async def create_track_translation_run(
    track_id: int,
    request: TranslationRunCreateRequest,
    _auth=Depends(require_api_key),
):
    await _ensure_pipeline_enabled()
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    transcript_run = await db.get_transcript_run(request.transcript_run_id)
    if not transcript_run or int(transcript_run.get("track_id")) != track_id:
        raise HTTPException(status_code=404, detail="Transcript run not found for this track")
    if not request.segments:
        raise HTTPException(status_code=400, detail="At least one segment is required")

    run_id = await db.create_translation_run(
        track_id=track_id,
        transcript_run_id=request.transcript_run_id,
        target_language=request.target_language,
        source=request.source,
        engine=request.engine,
        model=request.model,
        prompt=request.prompt,
        segments=[seg.model_dump() for seg in request.segments],
        metadata={"created_via": "api"},
    )
    if request.set_active:
        await db.set_track_active_translation(track_id, run_id)
    run = await db.get_translation_run(run_id)
    return {"status": "created", "run": run, "segments": len(request.segments)}


@router.get("/tracks/{track_id}/translations")
async def list_track_translation_runs(track_id: int, target_language: str | None = None):
    # Read-only: needed by the Player tab; not gated on pipeline_enabled.
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    runs = await db.list_translation_runs(track_id, target_language=target_language)
    active = await db.get_track_active_outputs(track_id)
    return {"track_id": track_id, "active": active, "runs": runs}


@router.get("/tracks/{track_id}/translations/{run_id}")
async def get_track_translation_run(track_id: int, run_id: int):
    # Read-only: needed by the Player tab; not gated on pipeline_enabled.
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    run = await db.get_translation_run(run_id)
    if not run or int(run.get("track_id")) != track_id:
        raise HTTPException(status_code=404, detail="Translation run not found")
    segments = await db.get_translation_segments(run_id)
    return {"track_id": track_id, "run": run, "segments": segments}


@router.patch("/tracks/{track_id}/translations/{run_id}/segments/{segment_index}")
async def update_translation_segment(
    track_id: int,
    run_id: int,
    segment_index: int,
    request: SegmentTextUpdateRequest,
    _auth=Depends(require_api_key),
):
    """Edit a single translation segment's text."""
    await _ensure_pipeline_enabled()
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    run = await db.get_translation_run(run_id)
    if not run or int(run.get("track_id")) != track_id:
        raise HTTPException(status_code=404, detail="Translation run not found for this track")
    updated = await db.update_translation_segment_text(run_id, segment_index, request.text)
    if not updated:
        raise HTTPException(status_code=404, detail="Segment not found")
    return {"status": "updated", "segment": updated}


@router.put("/tracks/{track_id}/active-translation/{run_id}")
async def set_track_active_translation(track_id: int, run_id: int, _auth=Depends(require_api_key)):
    await _ensure_pipeline_enabled()
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    ok = await db.set_track_active_translation(track_id, run_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Translation run not found for this track")
    run = await db.get_translation_run(run_id)
    return {
        "status": "updated",
        "track_id": track_id,
        "active_translation_run_id": run_id,
        "target_language": run.get("target_language") if run else None,
    }


@router.delete("/tracks/{track_id}/transcripts/redundant")
async def delete_redundant_transcript_runs(track_id: int, _auth=Depends(require_api_key)):
    """Delete every transcript run on this track that isn't (a) the active
    transcript or (b) the transcript_run the active translation is anchored on.
    Cleans up the duplicate runs the auto workflow used to spawn before the
    sibling-dedup fix. FK CASCADE drops dependent translations + segments.

    NOTE: This must be declared before the parameterized `/{run_id}` route
    below — FastAPI matches routes in registration order, and the int parser
    on `{run_id}` would otherwise reject the literal string 'redundant' with
    a 422 instead of letting this handler fire."""
    await _ensure_pipeline_enabled()
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    active = await db.get_track_active_outputs(track_id)
    keep_ids: set[int] = set()
    active_transcript_id = active.get("active_transcript_run_id")
    if active_transcript_id:
        keep_ids.add(int(active_transcript_id))
    active_translation_id = active.get("active_translation_run_id")
    if active_translation_id:
        tl_run = await db.get_translation_run(int(active_translation_id))
        if tl_run and tl_run.get("transcript_run_id"):
            keep_ids.add(int(tl_run["transcript_run_id"]))
    runs = await db.list_transcript_runs(track_id)
    deleted = []
    for r in runs:
        rid = int(r["id"])
        if rid in keep_ids:
            continue
        await db.delete_transcript_segments(rid)
        await db.delete_transcript_run(rid)
        deleted.append(rid)
    return {
        "track_id": track_id,
        "deleted_run_ids": deleted,
        "kept_run_ids": sorted(keep_ids),
        "deleted_count": len(deleted),
    }


@router.delete("/tracks/{track_id}/transcripts/{run_id}")
async def delete_transcript_run(track_id: int, run_id: int, _auth=Depends(require_api_key)):
    """Delete a single transcript run. FK ON DELETE SET NULL clears the active
    pointer; FK ON DELETE CASCADE drops the run's segments and any dependent
    translation runs (plus their segments)."""
    await _ensure_pipeline_enabled()
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    run = await db.get_transcript_run(run_id)
    if not run or int(run.get("track_id")) != track_id:
        raise HTTPException(status_code=404, detail="Transcript run not found")
    await db.delete_transcript_segments(run_id)
    await db.delete_transcript_run(run_id)
    return {"status": "deleted", "run_id": run_id, "track_id": track_id}


@router.delete("/tracks/{track_id}/translations/{run_id}")
async def delete_translation_run(track_id: int, run_id: int, _auth=Depends(require_api_key)):
    """Delete a translation run and clear it from active if needed"""
    await _ensure_pipeline_enabled()
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    run = await db.get_translation_run(run_id)
    if not run or int(run.get("track_id")) != track_id:
        raise HTTPException(status_code=404, detail="Translation run not found")

    # Clear from active if this is the active run
    if int(track.get("active_translation_run_id") or 0) == run_id:
        await db.set_track_active_translation(track_id, None)

    # Delete all segments for this run
    await db.delete_translation_segments(run_id)
    # Delete the run itself
    await db.delete_translation_run(run_id)

    return {"status": "deleted", "run_id": run_id, "track_id": track_id}


def _resolve_ffmpeg() -> str | None:
    if FFMPEG_PATH:
        if Path(FFMPEG_PATH).is_file():
            return FFMPEG_PATH
    from pipeline.extractor import _find_binary
    return _find_binary(["ffmpeg"], env_var="DRAMACD_FFMPEG_PATH")


# One transcode per track at a time: concurrent cold requests for the same
# FLAC (two devices, a prewarm racing the real request, mobile <audio>
# aborting and retrying) would otherwise each run a full `-threads 0` ffmpeg
# pass. Losers block on the winner's lock, then hit the warm cache re-check.
_transcode_locks: dict[int, "threading.Lock"] = {}
_transcode_locks_guard = threading.Lock()
_transcode_tmp_swept = False


def _ensure_aac_cache(src: Path, track_id: int) -> Path:
    """Transcode `src` to AAC-in-MP4 (.m4a) at 128k/48kHz, cached at
    data/pipeline/transcoded/{track_id}.m4a. MP4 container is required
    instead of raw ADTS: ADTS has no duration header, so mobile browsers
    estimate duration from average bitrate and advance currentTime against
    that wrong estimate -> linear subtitle drift even though playback speed
    is correct. MP4 with +faststart puts the moov atom up front so duration
    is known before the first byte of audio plays."""
    from config import PIPELINE_WORK_DIR
    cache_dir = PIPELINE_WORK_DIR / "transcoded"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{track_id}.m4a"

    src_mtime = src.stat().st_mtime
    if cache_file.exists() and cache_file.stat().st_mtime >= src_mtime:
        return cache_file

    global _transcode_tmp_swept
    with _transcode_locks_guard:
        # First cold transcode since startup: sweep tmp files orphaned by a
        # crashed/killed previous run. Safe here — no per-track lock has been
        # handed out yet, so nothing in this process is mid-transcode.
        if not _transcode_tmp_swept:
            _transcode_tmp_swept = True
            for orphan in cache_dir.glob("*.tmp.m4a"):
                try:
                    orphan.unlink()
                except OSError:
                    pass
        lock = _transcode_locks.setdefault(track_id, threading.Lock())

    with lock:
        # Re-check: the winner of a concurrent race already built the cache
        # while we waited on the lock.
        if cache_file.exists() and cache_file.stat().st_mtime >= src_mtime:
            return cache_file

        ffmpeg = _resolve_ffmpeg()
        if not ffmpeg:
            raise HTTPException(
                status_code=503,
                detail="AAC transcode requires ffmpeg. Install ffmpeg or set DRAMACD_FFMPEG_PATH.",
            )

        import subprocess
        import uuid
        # Unique temp name so a tmp left by a crashed run can never be confused
        # with ours; the finally-unlink below keeps the cache dir free of them.
        tmp_file = cache_file.with_suffix(f".{uuid.uuid4().hex}.tmp.m4a")
        # 128k AAC: transparent for speech (drama CDs are voice), and noticeably
        # faster to encode + smaller to download than 192k. `-threads 0` lets
        # ffmpeg use all cores. `+faststart` is load-bearing for mobile sync —
        # it puts the moov atom (with real duration) up front; do NOT remove it.
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-threads", "0",
            "-i", str(src),
            "-vn",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            "-movflags", "+faststart",
            "-f", "ipod",
            str(tmp_file),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)
            try:
                tmp_file.replace(cache_file)
            except PermissionError:
                # Windows: another client is mid-stream on the existing (stale
                # but valid) cache file, so it can't be replaced right now.
                # Serve that one; the next cold request retries the swap.
                if cache_file.exists():
                    return cache_file
                raise
            return cache_file
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode(errors="replace")[:500]
            raise HTTPException(status_code=500, detail=f"ffmpeg transcode failed: {stderr}")
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="ffmpeg transcode timed out")
        finally:
            # No-op after a successful replace; cleans up after every failure
            # path (ffmpeg error, timeout, missing binary, failed swap).
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass


@router.get("/player/audio/{track_id}")
async def serve_track_audio(track_id: int, format: str | None = Query(None)):
    """Serve audio file for playback in the player. Read-only — not gated on
    pipeline_enabled so the Player tab keeps working when Workshop is off.

    `format=aac` triggers an on-the-fly transcode (cached on disk) for
    mobile clients where source-rate FLAC drifts against the device's
    output clock."""
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    track_path = track.get("track_path")
    if not track_path:
        raise HTTPException(status_code=404, detail="Track path not available")

    audio_file = Path(track_path)
    # Threaded stat: loose tracks can live on a sleeping external HDD where a
    # cold stat blocks for seconds — keep it off the event loop.
    if not await asyncio.to_thread(audio_file.is_file):
        raise HTTPException(status_code=404, detail="Audio file not found on disk")

    if format == "aac":
        # Run the (potentially multi-second) transcode off the event loop so a
        # cold FLAC load doesn't freeze every other request — including the
        # player's own subtitle-sync polling.
        cached = await asyncio.to_thread(_ensure_aac_cache, audio_file, track_id)
        return FileResponse(
            path=str(cached),
            media_type="audio/mp4",
            filename=f"{audio_file.stem}.m4a",
        )

    codec = (track.get("codec") or "").lower()
    ext = audio_file.suffix.lower()

    media_type_map = {
        "mp3": "audio/mpeg",
        ".mp3": "audio/mpeg",
        "wav": "audio/wav",
        ".wav": "audio/wav",
        "flac": "audio/flac",
        ".flac": "audio/flac",
        "m4a": "audio/mp4",
        ".m4a": "audio/mp4",
        "ogg": "audio/ogg",
        ".ogg": "audio/ogg",
    }

    media_type = media_type_map.get(codec) or media_type_map.get(ext) or "audio/mpeg"

    return FileResponse(
        path=str(audio_file),
        media_type=media_type,
        filename=audio_file.name
    )


@router.get("/player/audio/{track_id}/prewarm")
async def prewarm_track_audio(track_id: int):
    """Build the AAC cache for a track ahead of playback without streaming it.
    The player calls this fire-and-forget when it selects a track that needs
    the transcode on mobile: unlike the <audio> element's own load (which
    Safari freely aborts), this request runs the transcode to completion, and
    the per-track lock dedups it against the real `?format=aac` request."""
    track = await db.get_pipeline_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    track_path = track.get("track_path")
    if not track_path:
        raise HTTPException(status_code=404, detail="Track path not available")
    audio_file = Path(track_path)
    if not await asyncio.to_thread(audio_file.is_file):
        raise HTTPException(status_code=404, detail="Audio file not found on disk")
    try:
        cached = await asyncio.to_thread(_ensure_aac_cache, audio_file, track_id)
    except HTTPException:
        raise
    except Exception as exc:  # transcode is best-effort; don't 500 the UI
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=200)
    return {"ok": True, "cached": cached.exists()}


_EXPORT_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _export_filename(prefix: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = _EXPORT_FILENAME_RE.sub("-", prefix).strip("-") or "export"
    return f"dramacd-{safe}-{stamp}.json"


def _json_export_response(payload: dict, filename: str) -> JSONResponse:
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export")
async def export_all_transcripts():
    """Export every item's transcripts and translations as a JSON backup."""
    await _ensure_pipeline_enabled()
    payload = await build_export_payload(item_ids=None)
    return _json_export_response(payload, _export_filename("library"))


@router.get("/items/{item_id}/export")
async def export_item_transcripts(item_id: int):
    """Export a single item's transcripts and translations."""
    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    payload = await build_export_payload(item_ids=[item_id])
    code = item.get("product_code") or f"item-{item_id}"
    return _json_export_response(payload, _export_filename(str(code)))


@router.post("/import")
async def import_transcripts(
    request: Request,
    replace_existing: bool = False,
    _auth=Depends(require_api_key),
):
    """Apply a previously exported JSON to the current database.

    Send the export file as the raw request body (Content-Type: application/json).
    """
    await _ensure_pipeline_enabled()
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="Request body is empty")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Body is not valid UTF-8 JSON")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    try:
        summary = await import_payload(payload, replace_existing=bool(replace_existing))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "replace_existing": bool(replace_existing), "summary": summary}


@router.post("/import-package")
async def import_transcripts_package(
    request: Request,
    replace_existing: bool = False,
    _auth=Depends(require_api_key),
):
    """Apply an exported package ZIP (or raw JSON) to the database.

    Detects ZIP magic bytes and pulls the embedded ``dramacd-transcripts.json``;
    otherwise treats the body as raw JSON. Audio inside the package is ignored
    here — this only restores transcripts/translations.
    """
    await _ensure_pipeline_enabled()
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="Request body is empty")

    if looks_like_zip(raw):
        try:
            payload = extract_transcripts_json_from_zip(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid package ZIP: {exc}")
    else:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Body is not valid UTF-8 JSON")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    try:
        summary = await import_payload(payload, replace_existing=bool(replace_existing))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "replace_existing": bool(replace_existing), "summary": summary}


@router.get("/items/{item_id}/package.zip")
async def export_item_package(
    item_id: int,
    runs: str = Query("active", pattern="^(active|all)$"),
    audio: int = Query(0, ge=0, le=1),
    preserve_paths: int = Query(0, ge=0, le=1),
    srt: int = Query(1, ge=0, le=1),
    txt: int = Query(1, ge=0, le=1),
    tracklist: int = Query(1, ge=0, le=1),
    all_files: int = Query(0, ge=0, le=1),
):
    """Build a ZIP package for a single item. Always carries ``manifest.json``
    and ``dramacd-transcripts.json`` (re-import payload). Sidecar formats
    toggled via ``srt`` / ``txt``. ``audio=1`` adds audio files; ``all_files=1``
    mirrors the entire original archive (forces ``preserve_paths`` and
    ``audio``). ``tracklist=1`` adds tracklist.txt + tracklist.json at the
    package root."""
    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    try:
        data, filename, skipped_audio = await build_package_zip(
            item_id,
            runs=runs,
            include_audio=bool(audio),
            preserve_paths=bool(preserve_paths),
            include_srt=bool(srt),
            include_txt=bool(txt),
            include_tracklist=bool(tracklist),
            include_all_archive_files=bool(all_files),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    # Audio was requested but some files weren't on disk — tell the UI so it
    # can warn instead of shipping a silently audio-less "full" package.
    if skipped_audio:
        headers["X-DramaCD-Audio-Skipped"] = str(len(skipped_audio))
    return Response(
        content=data,
        media_type="application/zip",
        headers=headers,
    )


def _safe_filename_for_disposition(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip().rstrip(".") or "tracklist"


@router.post("/items/{item_id}/open-folder")
async def open_item_extract_folder(item_id: int, _auth=Depends(require_api_key)):
    """Open the item's extracted-audio folder in the host OS file browser.
    Only works when the app is running locally — there's no remote shell
    invocation here, just a request to the local OS to open a directory."""
    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    target: Path | None = None
    # Prefer the actual extract_root recorded on a track (handles items whose
    # archives were extracted somewhere non-standard), fall back to the
    # canonical PIPELINE_EXTRACT_DIR/{CODE}.
    tracks = await db.get_pipeline_tracks(item_id)
    for t in tracks:
        er = (t.get("extract_root") or "").strip()
        if er and Path(er).is_dir():
            target = Path(er)
            break
    if target is None:
        from config import PIPELINE_EXTRACT_DIR
        code = (item.get("product_code") or "").strip().upper()
        if code:
            candidate = PIPELINE_EXTRACT_DIR / code
            if candidate.is_dir():
                target = candidate

    if target is None:
        raise HTTPException(
            status_code=404,
            detail="No extracted folder found for this item. Run extraction first.",
        )

    try:
        from os_utils import open_folder_focused
        open_folder_focused(target)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to open folder: {exc}")

    return {"status": "opened", "path": str(target)}


@router.get("/items/{item_id}/tracklist.txt")
async def get_item_tracklist_txt(item_id: int):
    """Standalone human-readable tracklist for a single item. One line per
    canonical track (sibling FLAC/MP3 variants are collapsed)."""
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    groups = await db.get_pipeline_track_groups(item_id)
    text = build_tracklist_text(item, groups)
    code = _safe_filename_for_disposition(item.get("product_code") or f"item{item_id}")
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="tracklist-{code}.txt"'},
    )


@router.get("/items/{item_id}/tracklist.json")
async def get_item_tracklist_json(item_id: int):
    """Structured tracklist for a single item, one entry per canonical
    track (sibling variants collapsed)."""
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    groups = await db.get_pipeline_track_groups(item_id)
    return build_tracklist_data(item, groups, preserve_paths=False)


# --- workspace maintenance --------------------------------------------------


def _workspace_subdirs() -> list[Path]:
    """Top-level directories that look like extraction workspaces."""
    from config import PIPELINE_EXTRACT_DIR
    root = PIPELINE_EXTRACT_DIR
    if not root.exists():
        return []
    return [p for p in root.iterdir() if p.is_dir()]


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for sub in path.rglob("*"):
            try:
                if sub.is_file():
                    total += sub.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


async def _referenced_extract_paths() -> set[str]:
    """Lower-cased absolute paths every track currently points into."""
    refs: set[str] = set()
    # One query over ALL pipeline_tracks rows. Deliberately NOT routed
    # through get_all_items(): its default kind filter (drama_cd only)
    # hid tokuten/manual items from this scan, so their extraction
    # folders were flagged orphaned — and purge deleted live audio.
    for t in await db.get_all_pipeline_track_paths():
        for key in ("extract_root", "track_path"):
            v = t.get(key)
            if v:
                try:
                    refs.add(str(Path(v).resolve()).lower())
                except OSError:
                    refs.add(str(v).lower())
    return refs


@router.get("/workspace/orphans")
async def list_workspace_orphans():
    """List workspace directories that are no longer referenced by any track."""
    await _ensure_pipeline_enabled()
    refs = await _referenced_extract_paths()
    orphans = []
    in_use = []
    for top in _workspace_subdirs():
        try:
            top_resolved = str(top.resolve()).lower()
        except OSError:
            top_resolved = str(top).lower()
        # Treat as referenced if any track points into this top-level dir.
        is_used = any(r == top_resolved or r.startswith(top_resolved + os.sep.lower()) for r in refs)
        info = {
            "path": str(top),
            "name": top.name,
            "size_bytes": _dir_size_bytes(top),
        }
        (in_use if is_used else orphans).append(info)
    return {
        "orphans": orphans,
        "in_use_count": len(in_use),
        "total_orphan_bytes": sum(o["size_bytes"] for o in orphans),
    }


@router.post("/workspace/purge-orphans")
async def purge_workspace_orphans(_auth=Depends(require_api_key)):
    """Delete workspace directories that no track row references."""
    await _ensure_pipeline_enabled()
    refs = await _referenced_extract_paths()
    deleted: list[dict] = []
    failed: list[dict] = []
    bytes_freed = 0
    for top in _workspace_subdirs():
        try:
            top_resolved = str(top.resolve()).lower()
        except OSError:
            top_resolved = str(top).lower()
        if any(r == top_resolved or r.startswith(top_resolved + os.sep.lower()) for r in refs):
            continue
        size = _dir_size_bytes(top)
        try:
            shutil.rmtree(top)
            deleted.append({"path": str(top), "size_bytes": size})
            bytes_freed += size
        except OSError as exc:
            failed.append({"path": str(top), "error": str(exc)})
    return {
        "status": "ok",
        "deleted_count": len(deleted),
        "deleted": deleted,
        "failed": failed,
        "bytes_freed": bytes_freed,
    }


@router.post("/items/{item_id}/purge-workspace")
async def purge_item_workspace(item_id: int, _auth=Depends(require_api_key)):
    """Delete the extracted audio for one item. Track rows for workspace
    extractions are cleared with it (their transcripts/translations CASCADE
    away — re-extracting starts fresh). Loose in-place tracks, whose audio
    lives OUTSIDE the workspace and isn't deleted here, keep their rows —
    clearing them would destroy transcripts for files still on disk."""
    await _ensure_pipeline_enabled()
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    tracks = await db.get_pipeline_tracks(item_id)
    extract_dirs: set[Path] = set()
    for t in tracks:
        root = t.get("extract_root")
        if root:
            extract_dirs.add(Path(root))
        track_path = t.get("track_path")
        if track_path:
            extract_dirs.add(Path(track_path).parent)

    code = (item.get("product_code") or f"item{item_id}").strip().upper() or f"item{item_id}"
    from config import PIPELINE_EXTRACT_DIR
    candidate = PIPELINE_EXTRACT_DIR / code
    if candidate.exists():
        extract_dirs.add(candidate)

    try:
        workspace_root = PIPELINE_EXTRACT_DIR.resolve()
    except OSError:
        workspace_root = PIPELINE_EXTRACT_DIR

    deleted: list[dict] = []
    failed: list[dict] = []
    bytes_freed = 0
    seen: set[str] = set()
    for d in extract_dirs:
        try:
            resolved = d.resolve()
        except OSError:
            resolved = d
        # Don't delete files outside the workspace root
        try:
            resolved.relative_to(workspace_root)
        except (ValueError, OSError):
            continue
        if not resolved.exists() or not resolved.is_dir():
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        size = _dir_size_bytes(resolved)
        try:
            shutil.rmtree(resolved)
            deleted.append({"path": str(resolved), "size_bytes": size})
            bytes_freed += size
        except OSError as exc:
            failed.append({"path": str(resolved), "error": str(exc)})

    # Clear track rows so the item shows as needing re-extraction — but KEEP
    # rows for loose in-place tracks (audio outside the workspace): their
    # files weren't deleted above, and dropping the rows would CASCADE away
    # transcripts/translations for audio that's still on disk.
    kept_loose: list[dict] = []
    for t in tracks:
        tp = (t.get("track_path") or "").strip()
        if not tp:
            continue
        try:
            Path(tp).resolve().relative_to(workspace_root)
        except (ValueError, OSError):
            kept_loose.append({
                "archive_path": t.get("archive_path"),
                "extract_root": t.get("extract_root"),
                "track_path": tp,
                "track_index": t.get("track_index", 0),
                "title": t.get("title"),
                "duration_seconds": t.get("duration_seconds"),
                "codec": t.get("codec"),
                "sample_rate": t.get("sample_rate"),
                "channels": t.get("channels"),
                "status": t.get("status", "indexed"),
                "error": t.get("error"),
            })
    tracks_now = await db.replace_pipeline_tracks_for_item(item_id, kept_loose)

    return {
        "status": "ok",
        "item_id": item_id,
        "deleted": deleted,
        "failed": failed,
        "bytes_freed": bytes_freed,
        "tracks_cleared_was": len(tracks),
        "tracks_kept_loose": len(kept_loose),
        "tracks_now": tracks_now,
    }
