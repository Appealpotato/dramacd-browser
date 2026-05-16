"""
Autopilot orchestrator: chains the full per-item pipeline in order.

Stages, in order:
  1. Translate metadata (JA -> EN title/description/seiyuu)
  2. Extract archives
  3. Translate track titles
  4. Transcribe all tracks needing transcription
  5. Auto-translate every track that has an active transcript

Each stage logs its own progress events on the autopilot job. Sub-jobs
(extract / transcribe / per-track translate) keep their own job rows
so existing UIs continue to work; the autopilot only sequences them.
"""

import asyncio
import logging
from datetime import datetime

import database as db

logger = logging.getLogger(__name__)

# Cap how many autopilot jobs do real work at once. Jobs over the cap stay in
# "queued" until a slot frees up — they don't fail, just wait. Bumped here if
# the host can take more concurrent Whisper loads.
MAX_CONCURRENT_AUTOPILOTS = 2
_autopilot_semaphore = asyncio.Semaphore(MAX_CONCURRENT_AUTOPILOTS)


# Internal stage IDs (used for skip_stages API + summary keys) mapped to the
# human-readable label that appears in the UI, the job's `current` field,
# and every job_event message. UI never has to translate these.
STAGE_LABELS: dict[str, str] = {
    "metadata_translate": "Translating metadata",
    "extract": "Unpacking audio",
    "track_titles_translate": "Translating track titles",
    "transcribe": "Transcribing",
    "track_translate": "Translating",
}
STAGE_NAMES: list[str] = list(STAGE_LABELS.keys())


def stage_label(stage_id: str) -> str:
    return STAGE_LABELS.get(stage_id, stage_id)


async def _stop_requested(job_id: int) -> bool:
    job = await db.get_job(job_id)
    if not job:
        return True
    return str(job.get("status") or "").lower() == "stopping"


async def _finalize(job_id: int, status: str, completed: int, error: str | None = None, result: dict | None = None):
    payload = {
        "status": status,
        "stopped": 1,
        "completed": completed,
        "total": len(STAGE_NAMES),
        "finished_at": datetime.now().isoformat(),
    }
    if error:
        payload["error"] = error
    if result is not None:
        payload["result_json"] = result
    await db.update_job(job_id, **payload)


async def run_autopilot_job(job_id: int):
    job = await db.get_job(job_id)
    if not job or job.get("job_type") != "pipeline_autopilot":
        return

    metadata = job.get("metadata_json") or {}
    item_id = int(metadata.get("item_id") or 0)
    if item_id <= 0:
        await db.update_job(job_id, status="failed", error="Missing item_id in autopilot metadata", stopped=1)
        return

    item = await db.get_item(item_id)
    if not item:
        await db.update_job(job_id, status="failed", error="Item not found", stopped=1)
        return

    # Cap concurrent autopilot jobs. If the cap is reached, this job waits its
    # turn while staying in the "queued" status it was created with.
    if _autopilot_semaphore.locked():
        await db.append_job_event(
            job_id,
            "info",
            f"Waiting for a slot (running {MAX_CONCURRENT_AUTOPILOTS} at a time)",
            {"item_id": item_id},
        )
    async with _autopilot_semaphore:
        await _execute_autopilot(job_id, item_id, metadata)


async def _execute_autopilot(job_id: int, item_id: int, metadata: dict):
    # Re-check that the job wasn't stopped while waiting for a slot.
    job = await db.get_job(job_id)
    if not job:
        return
    pre_status = str(job.get("status") or "").lower()
    if pre_status in {"stopping", "stopped", "completed", "failed"}:
        if pre_status == "stopping":
            await db.update_job(job_id, status="stopped", stopped=1, finished_at=datetime.now().isoformat())
            await db.append_job_event(job_id, "info", "Stopped before starting", {})
        return

    skip = set(metadata.get("skip_stages") or [])
    force_extract = bool(metadata.get("force_extract", False))
    force_transcribe = bool(metadata.get("force_transcribe", False))
    force_translate = bool(metadata.get("force_translate", False))
    transcribe_language = str(metadata.get("transcribe_language") or "ja")
    transcribe_model = metadata.get("transcribe_model") or None

    target_language = str(metadata.get("target_language") or "en")
    provider = str(metadata.get("provider") or await db.get_runtime_translation_provider() or "gemini")
    model = metadata.get("model") or None
    max_tokens = int(metadata.get("max_tokens_per_chunk") or 1000)
    max_lines = int(metadata.get("max_lines_per_chunk") or 20)
    max_retries = int(metadata.get("max_retries_per_chunk") or 2)
    retry_backoff = float(metadata.get("retry_backoff_seconds") or 1.0)
    glossary = metadata.get("glossary") or None
    character_memory = metadata.get("character_memory") or None

    await db.update_job(
        job_id,
        status="running",
        started_at=job.get("started_at") or datetime.now().isoformat(),
        total=len(STAGE_NAMES),
        completed=0,
        stopped=0,
    )
    await db.append_job_event(
        job_id,
        "info",
        "Starting workflow",
        {
            "item_id": item_id,
            "skip": list(skip),
            "force_extract": force_extract,
            "force_transcribe": force_transcribe,
            "force_translate": force_translate,
        },
    )

    completed = 0
    summary: dict = {"item_id": item_id, "stages": {}}

    def _record_stage(stage_id: str, **fields):
        # Always include the human-readable label so the UI never has to
        # translate snake_case ids back into prose.
        entry = {"label": stage_label(stage_id), **fields}
        summary["stages"][stage_id] = entry
        return entry

    # ---------------------------------------------------------------- stage 1
    stage = "metadata_translate"
    label = stage_label(stage)
    if stage in skip:
        await db.append_job_event(job_id, "info", f"{label} — skipped", {})
        _record_stage(stage, status="skipped")
    else:
        item_now = await db.get_item(item_id) or {}
        already_translated = bool(
            str(item_now.get("title_en") or "").strip()
            and str(item_now.get("description_en") or "").strip()
        )
        if already_translated and not metadata.get("force_metadata", False):
            _record_stage(stage, status="skipped", reason="already_translated")
            await db.append_job_event(job_id, "info", f"{label} — skipped (already done)", {})
        else:
            await db.update_job(job_id, current=label)
            try:
                from routers.api import translate_item_metadata  # lazy: avoid import cycle
                result = await translate_item_metadata(item_id)
                _record_stage(stage, status="ok", provider_used=result.get("provider_used"))
                await db.append_job_event(
                    job_id,
                    "info",
                    f"{label} — done",
                    {"provider_used": result.get("provider_used"), "fallback_used": result.get("fallback_used")},
                )
            except Exception as e:
                _record_stage(stage, status="warn", error=str(e)[:300])
                await db.append_job_event(job_id, "warn", f"{label} — failed", {"error": str(e)[:300]})
    completed += 1
    await db.update_job(job_id, completed=completed)
    if await _stop_requested(job_id):
        await _finalize(job_id, "stopped", completed, result=summary)
        await db.append_job_event(job_id, "info", "Workflow stopped", {})
        return

    # ---------------------------------------------------------------- stage 2
    stage = "extract"
    label = stage_label(stage)
    if stage in skip:
        await db.append_job_event(job_id, "info", f"{label} — skipped", {})
        _record_stage(stage, status="skipped")
    else:
        await db.update_job(job_id, current=label)
        try:
            from pipeline.service import queue_extraction
            from pipeline.extractor import run_extraction_job
            ext_job_id = await queue_extraction(item_id=item_id, force=force_extract)
            await run_extraction_job(ext_job_id)
            ext_job = await db.get_job(ext_job_id) or {}
            ext_status = str(ext_job.get("status") or "").lower()
            _record_stage(stage, status=ext_status, job_id=ext_job_id)
            if ext_status not in {"completed"}:
                await _finalize(
                    job_id,
                    "failed",
                    completed,
                    error=f"{label} job {ext_job_id} ended with status '{ext_status}'",
                    result=summary,
                )
                await db.append_job_event(
                    job_id,
                    "error",
                    f"Workflow aborted — {label.lower()} failed",
                    {"job_id": ext_job_id, "status": ext_status},
                )
                return
            await db.append_job_event(job_id, "info", f"{label} — done", {"job_id": ext_job_id})
        except Exception as e:
            _record_stage(stage, status="error", error=str(e)[:300])
            await _finalize(job_id, "failed", completed, error=f"{label} crashed: {str(e)[:200]}", result=summary)
            await db.append_job_event(job_id, "error", f"Workflow aborted — {label.lower()} crashed", {"error": str(e)[:300]})
            return
    completed += 1
    await db.update_job(job_id, completed=completed)
    if await _stop_requested(job_id):
        await _finalize(job_id, "stopped", completed, result=summary)
        await db.append_job_event(job_id, "info", "Workflow stopped", {})
        return

    # ---------------------------------------------------------------- stage 3
    stage = "track_titles_translate"
    label = stage_label(stage)
    if stage in skip:
        await db.append_job_event(job_id, "info", f"{label} — skipped", {})
        _record_stage(stage, status="skipped")
    else:
        existing_tracks = await db.get_pipeline_tracks(item_id)
        all_titles_translated = bool(existing_tracks) and all(
            str(t.get("title_en") or "").strip() for t in existing_tracks
        )
        if all_titles_translated and not metadata.get("force_track_titles", False):
            _record_stage(stage, status="skipped", reason="all_titles_translated")
            await db.append_job_event(
                job_id,
                "info",
                f"{label} — skipped (already done)",
                {"track_count": len(existing_tracks)},
            )
        else:
            await db.update_job(job_id, current=label)
            try:
                from pipeline.router import translate_track_names  # lazy
                result = await translate_track_names(item_id)
                _record_stage(
                    stage,
                    status="ok",
                    translated_count=result.get("translated_count"),
                    total=result.get("total"),
                )
                await db.append_job_event(
                    job_id,
                    "info",
                    f"{label} — done",
                    {"translated_count": result.get("translated_count"), "total": result.get("total")},
                )
            except Exception as e:
                _record_stage(stage, status="warn", error=str(e)[:300])
                await db.append_job_event(job_id, "warn", f"{label} — failed", {"error": str(e)[:300]})
    completed += 1
    await db.update_job(job_id, completed=completed)
    if await _stop_requested(job_id):
        await _finalize(job_id, "stopped", completed, result=summary)
        await db.append_job_event(job_id, "info", "Workflow stopped", {})
        return

    # ---------------------------------------------------------------- stages 4 + 5
    # Transcribe and translate are now overlapped per-track: as soon as a
    # track has an active transcript, its translation is queued. The two
    # nominal stages still appear in the UI ("Transcribing", "Translating")
    # and progress increments by 1 for each — but their *work* runs at the
    # same time on a single autopilot, halving wall-clock for long CDs.
    transcribe_stage = "transcribe"
    translate_stage = "track_translate"
    transcribe_label = stage_label(transcribe_stage)
    translate_label = stage_label(translate_stage)
    do_transcribe = transcribe_stage not in skip
    do_translate = translate_stage not in skip

    transcribe_task = None
    tjob_id: int | None = None
    transcribe_skipped_reason: str | None = None  # for record_stage when no work
    track_ids_needing: list[int] = []

    # ---- Decide whether to actually start transcription ----
    # Queue the *preferred* track per sibling group only. A CD that ships as
    # FLAC + MP3 (and/or SFX + no-SFX) has the same audio multiple times; if
    # we queue every variant, Whisper runs N times on the same recording and
    # each run produces a slightly different segment count (VAD/split drift).
    # Sibling replication then overwrites the active transcript with each
    # successive run, which (combined with translation snapshotting an earlier
    # run_id) causes JP/EN segment mismatch in the player. The translate stage
    # already iterates groups; transcribe now matches.
    if do_transcribe:
        # Prefer no-SFX variant for Whisper: cleaner audio, fewer SFX-induced
        # hallucinations. With `get_pipeline_track_groups` now duration-bucketing,
        # a trimmed no-SFX mix won't share a group with the longer SFX file —
        # both will be transcribed independently when their durations diverge.
        groups = await db.get_pipeline_track_groups(item_id, preferred_variant="no-sfx")
        if not groups:
            do_transcribe = False
            transcribe_skipped_reason = "no_tracks"
            await db.append_job_event(job_id, "warn", f"{transcribe_label} — skipped (no audio tracks)", {})
        else:
            for g in groups:
                pref_tid = g.get("preferred_track_id")
                if pref_tid is None:
                    continue
                if force_transcribe:
                    track_ids_needing.append(int(pref_tid))
                    continue
                # If *any* track in the group already has an active transcript,
                # the group is covered (sibling replication will fill the rest).
                group_has_active = False
                for t in (g.get("tracks") or []):
                    active = await db.get_track_active_outputs(int(t["id"]))
                    if active.get("active_transcript_run_id"):
                        group_has_active = True
                        break
                if not group_has_active:
                    track_ids_needing.append(int(pref_tid))
            if not track_ids_needing:
                do_transcribe = False
                transcribe_skipped_reason = "all_have_transcripts"
                await db.append_job_event(
                    job_id,
                    "info",
                    f"{transcribe_label} — skipped (already done)",
                    {"group_count": len(groups)},
                )

    # ---- Set the autopilot's `current` field for the UI ----
    if do_transcribe and do_translate:
        await db.update_job(job_id, current=f"{transcribe_label} + {translate_label.lower()}")
    elif do_transcribe:
        await db.update_job(job_id, current=transcribe_label)
    elif do_translate:
        await db.update_job(job_id, current=translate_label)

    # ---- Kick off transcription in the background (if anything to do) ----
    if do_transcribe:
        try:
            from pipeline.service import queue_transcription
            from pipeline.whisper_job import run_transcription_job
            resolved_model = transcribe_model or await db.get_runtime_whisper_model()
            tjob_id = await queue_transcription(
                item_id=item_id,
                language=transcribe_language,
                model=resolved_model,
                force=force_transcribe,
                track_ids=track_ids_needing,
            )
            transcribe_task = asyncio.create_task(run_transcription_job(tjob_id))
        except Exception as e:
            _record_stage(transcribe_stage, status="error", error=str(e)[:300])
            await _finalize(job_id, "failed", completed, error=f"{transcribe_label} crashed: {str(e)[:200]}", result=summary)
            await db.append_job_event(job_id, "error", f"Workflow aborted — {transcribe_label.lower()} crashed", {"error": str(e)[:300]})
            return

    # ---- Translation worker: drains tracks as they become ready ----
    translated_ok = 0
    translated_skipped = 0
    translated_failed = 0
    queued_for_translation: set[int] = set()

    async def _translate_one_track(t: dict) -> None:
        nonlocal translated_ok, translated_skipped, translated_failed
        tid = int(t["id"])
        if tid in queued_for_translation:
            return
        active = await db.get_track_active_outputs(tid)
        transcript_run_id = active.get("active_transcript_run_id")
        if not transcript_run_id:
            return  # not yet transcribed; will retry on next loop tick
        queued_for_translation.add(tid)
        if active.get("active_translation_run_id") and not force_translate:
            translated_skipped += 1
            return
        try:
            from pipeline.service import queue_translation
            from pipeline.translation_job import run_translation_job
            ltj = await queue_translation(
                item_id=item_id,
                track_id=tid,
                transcript_run_id=int(transcript_run_id),
                target_language=target_language,
                provider=provider,
                model=model or "",
                max_tokens_per_chunk=max_tokens,
                max_lines_per_chunk=max_lines,
                max_retries_per_chunk=max_retries,
                retry_backoff_seconds=retry_backoff,
                set_active=True,
                glossary=glossary,
                character_memory=character_memory,
            )
            await run_translation_job(ltj)
            sub = await db.get_job(ltj) or {}
            if str(sub.get("status") or "").lower() == "completed":
                translated_ok += 1
            else:
                translated_failed += 1
        except Exception as inner:
            translated_failed += 1
            await db.append_job_event(
                job_id,
                "warn",
                "Couldn't translate one track",
                {"track_id": tid, "error": str(inner)[:300]},
            )

    # ---- Main loop: kick off track translations as soon as each track's
    # transcript is ready, in track-index order. Translations run as
    # asyncio tasks so they overlap (capped globally at MAX_CONCURRENT_TRANSLATIONS
    # in run_translation_job). The summary-context chain is preserved
    # because whisper_job writes each track's summary to the DB before the
    # next track finishes transcribing — so by the time translation starts,
    # all prior summaries are already on disk. ----
    translation_tasks: list[asyncio.Task] = []

    if do_translate:
        while True:
            if await _stop_requested(job_id):
                if transcribe_task and not transcribe_task.done():
                    transcribe_task.cancel()
                    try:
                        await transcribe_task
                    except (asyncio.CancelledError, Exception):
                        pass
                for tt in translation_tasks:
                    if not tt.done():
                        tt.cancel()
                await asyncio.gather(*translation_tasks, return_exceptions=True)
                _record_stage(
                    translate_stage,
                    status="stopped",
                    ok=translated_ok,
                    skipped=translated_skipped,
                    failed=translated_failed,
                )
                await _finalize(job_id, "stopped", completed, result=summary)
                await db.append_job_event(job_id, "info", f"Stopped during {translate_label.lower()}", {})
                return

            # Iterate sibling-track *groups*, not raw tracks: a CD that ships
            # in FLAC + MP3 has the same audio twice, and translating each
            # copy is wasted LLM cost. We translate the preferred track per
            # group; translation_job's `replicate_translation_run_to_siblings`
            # propagates the result to the other variants.
            groups = await db.get_pipeline_track_groups(item_id)
            groups.sort(key=lambda g: min(
                (int(t.get("track_index") or 0) for t in (g.get("tracks") or [])),
                default=0,
            ))
            for g in groups:
                pref_tid = g.get("preferred_track_id")
                if pref_tid is None or pref_tid in queued_for_translation:
                    continue
                pref_track = next((t for t in (g.get("tracks") or []) if int(t.get("id") or 0) == int(pref_tid)), None)
                if not pref_track:
                    continue
                active = await db.get_track_active_outputs(int(pref_tid))
                if not active.get("active_transcript_run_id"):
                    continue  # not yet transcribed; pick it up on a later tick
                # Spawn translation as a task so the loop keeps scanning.
                # _translate_one_track records its own success/skip/fail counts.
                translation_tasks.append(asyncio.create_task(_translate_one_track(pref_track)))

            transcribe_done = transcribe_task is None or transcribe_task.done()
            tasks_done = all(tt.done() for tt in translation_tasks)
            if transcribe_done and tasks_done:
                # Final pass to catch any group whose preferred-track transcript
                # landed between scans. Same group-based logic as above.
                final_groups = await db.get_pipeline_track_groups(item_id)
                spawned_more = False
                for g in final_groups:
                    pref_tid = g.get("preferred_track_id")
                    if pref_tid is None or pref_tid in queued_for_translation:
                        continue
                    pref_track = next((t for t in (g.get("tracks") or []) if int(t.get("id") or 0) == int(pref_tid)), None)
                    if not pref_track:
                        continue
                    active = await db.get_track_active_outputs(int(pref_tid))
                    if active.get("active_transcript_run_id"):
                        translation_tasks.append(asyncio.create_task(_translate_one_track(pref_track)))
                        spawned_more = True
                if not spawned_more:
                    break
            await asyncio.sleep(1.5)

        # Drain any remaining translation tasks before recording the stage.
        if translation_tasks:
            await asyncio.gather(*translation_tasks, return_exceptions=True)
    elif transcribe_task is not None:
        # Translate stage is disabled; just wait for transcription to finish.
        try:
            await transcribe_task
        except Exception:
            pass  # status check below picks up the failure

    # ---- Resolve transcription outcome and emit stage 4 record ----
    if do_transcribe and tjob_id is not None:
        # transcribe_task may have ended naturally above; raise its exception if any
        if transcribe_task is not None and transcribe_task.done():
            exc = transcribe_task.exception()
            if exc is not None:
                _record_stage(transcribe_stage, status="error", error=str(exc)[:300])
                await _finalize(
                    job_id,
                    "failed",
                    completed,
                    error=f"{transcribe_label} crashed: {str(exc)[:200]}",
                    result=summary,
                )
                await db.append_job_event(
                    job_id,
                    "error",
                    f"Workflow aborted — {transcribe_label.lower()} crashed",
                    {"error": str(exc)[:300]},
                )
                return
        tjob = await db.get_job(tjob_id) or {}
        tstatus = str(tjob.get("status") or "").lower()
        _record_stage(
            transcribe_stage,
            status=tstatus,
            job_id=tjob_id,
            tracks_targeted=len(track_ids_needing),
        )
        if tstatus != "completed":
            await _finalize(
                job_id,
                "failed",
                completed,
                error=f"{transcribe_label} job {tjob_id} ended with status '{tstatus}'",
                result=summary,
            )
            await db.append_job_event(
                job_id,
                "error",
                f"Workflow aborted — {transcribe_label.lower()} failed",
                {"job_id": tjob_id, "status": tstatus},
            )
            return
        await db.append_job_event(
            job_id,
            "info",
            f"{transcribe_label} — done",
            {"job_id": tjob_id, "tracks_targeted": len(track_ids_needing)},
        )
    elif transcribe_skipped_reason:
        _record_stage(transcribe_stage, status="skipped", reason=transcribe_skipped_reason)
    elif transcribe_stage in skip:
        _record_stage(transcribe_stage, status="skipped")
        await db.append_job_event(job_id, "info", f"{transcribe_label} — skipped", {})

    completed += 1
    await db.update_job(job_id, completed=completed)
    if await _stop_requested(job_id):
        await _finalize(job_id, "stopped", completed, result=summary)
        await db.append_job_event(job_id, "info", "Workflow stopped", {})
        return

    # ---- Stage 5 record (translation results captured during the loop above) ----
    if do_translate:
        _record_stage(
            translate_stage,
            status="ok",
            ok=translated_ok,
            skipped=translated_skipped,
            failed=translated_failed,
        )
        await db.append_job_event(
            job_id,
            "info",
            f"{translate_label} — done",
            {"ok": translated_ok, "skipped": translated_skipped, "failed": translated_failed},
        )
    else:
        _record_stage(translate_stage, status="skipped")
        await db.append_job_event(job_id, "info", f"{translate_label} — skipped", {})
    completed += 1
    await db.update_job(job_id, completed=completed)

    await _finalize(job_id, "completed", completed, result=summary)
    await db.append_job_event(job_id, "info", "Workflow finished", summary)
