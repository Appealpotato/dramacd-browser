import asyncio
import time
from datetime import datetime
from pathlib import Path

import database as db
from pipeline.concurrency import DynamicSemaphore
from pipeline.transcriber import WhisperTranscriber

# Cap concurrent transcription jobs across the process. Whisper is GPU-bound and
# a single GPU decodes one stream at a time, so >1 concurrent job just thrashes
# VRAM and slows everything. Runtime-tunable (Settings → Concurrency); default 1.
_whisper_semaphore = DynamicSemaphore(db.get_runtime_max_whisper_jobs, name="Whisper", default=1)

# hotwords are resent with every ~30s decoding window and are NOT truncated by
# faster-whisper, so they compete with carried-over context for the decoder's
# fixed 448-token prompt budget. For Japanese (~1.5-2 tokens/char) an overlong
# bias string can eat the entire budget and crash the decode with "maximum
# decoding length must be > 0". Keep it well under that — the most important
# terms (title, series, then top glossary readings) come first, so truncation
# drops the least-important tail. (A retry-without-hotwords in the transcriber
# is the backstop if this ever still overflows.)
_HOTWORDS_MAX_CHARS = 128


def _build_hotwords(item: dict, glossary: str = "") -> str | None:
    """Vocabulary bias for Whisper: the item's JP title/series plus the
    Japanese side of its glossary rules (character name readings). Returns
    None when the item has nothing useful (e.g. English-only metadata)."""
    parts: list[str] = []
    for key in ("title", "series"):
        value = str(item.get(key) or "").strip()
        if value:
            parts.append(value)
    for line in str(glossary or "").splitlines():
        ja = line.split("=", 1)[0].strip()
        # ASCII-only left sides are romaji/English rules — useless to Whisper.
        if ja and not ja.isascii():
            parts.append(ja)
    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(part)
    text = "、".join(unique).strip()
    return text[:_HOTWORDS_MAX_CHARS] or None


async def run_transcription_job(job_id: int):
    """Public entry point — wraps the worker in a DynamicSemaphore so the
    max-concurrent-transcriptions cap is enforced no matter who queued the job
    (manual, autopilot, bulk). While waiting for a slot the job stays 'queued'
    with a human-readable reason so it doesn't look stuck."""
    job = await db.get_job(job_id)
    if not job or job.get("job_type") != "pipeline_transcribe":
        return
    if await _whisper_semaphore.would_wait():
        limit = await db.get_runtime_max_whisper_jobs()
        await db.update_job(job_id, current=f"Waiting for a free transcription slot (max {limit} at once)")
        await db.append_job_event(
            job_id,
            "info",
            f"Queued — waiting for a free transcription slot (max {limit} running at once)",
            {"max_whisper_jobs": limit},
        )
    async with _whisper_semaphore:
        # Re-fetch in case the job was stopped while it waited for a slot.
        job_now = await db.get_job(job_id)
        if not job_now:
            return
        st = str(job_now.get("status") or "").lower()
        if st in {"stopping", "stopped", "failed"}:
            if st == "stopping":
                await db.update_job(job_id, status="stopped", stopped=1, finished_at=datetime.now().isoformat())
            return
        await _run_transcription_job_inner(job_id)


async def _run_transcription_job_inner(job_id: int):
    """
    Background job to auto-transcribe all tracks for an item using Whisper.
    Mirrors the pattern of run_extraction_job.
    """
    import logging
    logger = logging.getLogger(__name__)

    logger.info(f"[JOB START] run_transcription_job started for job_id={job_id}")
    job = await db.get_job(job_id)
    if not job:
        logger.error(f"[JOB START] Job {job_id} not found")
        return
    if job.get("job_type") != "pipeline_transcribe":
        logger.error(f"[JOB START] Job {job_id} is not a transcribe job, type={job.get('job_type')}")
        return

    metadata = job.get("metadata_json") or {}
    item_id = int(metadata.get("item_id", 0))
    language = metadata.get("language", "ja")
    # Model from job metadata wins (per-request override); otherwise use the runtime DB setting.
    model_override = metadata.get("model")
    if model_override:
        model_name = str(model_override).strip()
    else:
        model_name = await db.get_runtime_whisper_model()
    vad_filter = await db.get_runtime_whisper_vad_filter()
    beam_size = await db.get_runtime_whisper_beam_size()
    condition_on_previous = await db.get_runtime_whisper_condition_on_previous()
    word_timestamps = await db.get_runtime_whisper_word_timestamps()
    force = bool(metadata.get("force", False))
    track_ids_filter = metadata.get("track_ids")  # None means all tracks

    if item_id <= 0:
        await db.update_job(job_id, status="failed", error="Missing item_id in job metadata")
        await db.append_job_event(job_id, "error", "Transcription failed", {"error": "missing_item_id"})
        return

    item = await db.get_item(item_id)
    if not item:
        await db.update_job(job_id, status="failed", error="Item not found")
        await db.append_job_event(job_id, "error", "Transcription failed", {"error": "item_not_found"})
        return

    # Get all tracks for this item
    all_tracks = await db.get_pipeline_tracks(item_id)
    if not all_tracks:
        await db.update_job(job_id, status="failed", error="No extracted tracks found for item")
        await db.append_job_event(job_id, "error", "Transcription failed", {"error": "no_tracks"})
        return

    # Filter tracks if specific track_ids provided
    if track_ids_filter:
        selected_ids = set(track_ids_filter) if isinstance(track_ids_filter, list) else {track_ids_filter}
        tracks = [t for t in all_tracks if t.get("id") in selected_ids]
    else:
        tracks = all_tracks

    if not tracks:
        await db.update_job(job_id, status="failed", error="No matching tracks found for the selected track IDs")
        await db.append_job_event(job_id, "error", "Transcription failed", {"error": "no_matching_tracks"})
        return

    await db.update_job(
        job_id,
        status="running",
        started_at=job.get("started_at") or datetime.now().isoformat(),
        total=len(tracks),
        completed=0,
        stopped=0,
    )
    await db.append_job_event(
        job_id,
        "info",
        "Transcription started",
        {"item_id": item_id, "track_count": len(tracks), "language": language, "model": model_name},
    )

    # Initialize Faster Whisper transcriber with progress tracking
    try:
        # Create a shared progress tracker for the transcriber
        current_track_progress = {"value": 0, "track_title": ""}

        def on_whisper_progress(progress_percent):
            """Callback from Faster Whisper's segment loop."""
            current_track_progress["value"] = progress_percent
            logger.debug(f"[PROGRESS CALLBACK] Updated to {progress_percent}%")

        # Bias decoding toward vocabulary Whisper can't guess: the CD's own
        # title/series (ero compounds like 睡眠姦 are usually IN the title)
        # plus the JP side of the item's glossary (character name readings).
        try:
            item_glossary = await db.get_item_glossary(item_id) or ""
        except Exception:
            item_glossary = ""
        hotwords = _build_hotwords(item, item_glossary)

        transcriber = WhisperTranscriber(
            model_name=model_name,
            progress_callback=on_whisper_progress,
            vad_filter=vad_filter,
            beam_size=beam_size,
            condition_on_previous_text=condition_on_previous,
            hotwords=hotwords,
            word_timestamps=word_timestamps,
        )
        device = transcriber.get_device()
        await db.append_job_event(
            job_id,
            "info",
            "Whisper model loaded",
            {
                "device": device,
                "model": model_name,
                "vad_filter": vad_filter,
                "beam_size": beam_size,
                "condition_on_previous_text": condition_on_previous,
                "hotwords": hotwords or None,
                "word_timestamps": word_timestamps,
                "batch_size": transcriber.batch_size if transcriber._uses_batching() else 0,
            },
        )
    except Exception as e:
        await db.update_job(job_id, status="failed", error=f"Failed to load Whisper: {str(e)}")
        await db.append_job_event(job_id, "error", "Whisper initialization failed", {"error": str(e)})
        return

    # Transcribe each track
    completed = 0
    failures = []
    for idx, track in enumerate(tracks, start=1):
        # Check if stop was requested
        job = await db.get_job(job_id)
        if job and job.get("status") == "stopping":
            await db.update_job(job_id, status="stopped", stopped=1)
            await db.append_job_event(job_id, "info", "Transcription stopped by user", {})
            return
        track_id = track.get("id")
        track_path = track.get("track_path")
        track_title = track.get("title", f"Track {idx}")

        # Sibling-replication safety: if a previous track in this job was a
        # sibling of this one, replicate_transcript_run_to_siblings has already
        # set an active transcript on this track. Re-running Whisper would
        # create a second run with a slightly different segment count and
        # overwrite the active, causing JP/EN drift in the player (translation
        # may have snapshotted the earlier run_id). Skip unless caller forced.
        if not force and track_id is not None:
            existing_active = await db.get_track_active_outputs(int(track_id))
            if existing_active.get("active_transcript_run_id"):
                completed += 1
                await db.update_job(
                    job_id,
                    completed=completed,
                    current=f"Skipped (sibling already transcribed): {track_title}",
                )
                await db.append_job_event(
                    job_id,
                    "info",
                    f"Skipped (active transcript from sibling): {track_title}",
                    {"track_id": track_id, "active_run_id": existing_active.get("active_transcript_run_id")},
                )
                continue

        if not track_path or not Path(track_path).exists():
            error_msg = f"Track file not found: {track_path}"
            failures.append({"track_id": track_id, "track_path": track_path, "error": error_msg})
            await db.append_job_event(
                job_id,
                "error",
                f"Track file not found: {track_title}",
                {"track_id": track_id, "track_path": track_path},
            )
            completed += 1
            # Update progress even for skipped tracks
            await db.update_job(job_id, completed=completed, current=f"Skipped: {track_title}")
            continue

        try:
            # Update progress BEFORE transcribing
            current_track_progress["track_title"] = track_title
            current_track_progress["value"] = 0
            await db.update_job(job_id, completed=completed, current=f"Transcribing: {track_title} (0%)")

            # Start transcription in background, poll for real progress from Whisper
            transcribe_task = asyncio.create_task(transcriber.transcribe(Path(track_path)))

            # Poll Whisper's actual progress every 500ms
            # Keep polling until task is done to allow frontend updates
            poll_count = 0
            max_polls = 1000  # 500 seconds max (safety limit)

            while True:
                poll_count += 1

                # Calculate progress: completed tracks + current track within-track progress
                # If callback has a value, use it; otherwise default to 0 for current track
                current_track_percent = min(95, current_track_progress["value"]) if current_track_progress["value"] > 0 else 0

                # Overall progress = (completed tracks / total tracks) * 100 + (current track % / total tracks)
                # This gives smooth progression across all tracks
                if len(tracks) > 0:
                    base_progress = (completed / len(tracks)) * 100
                    current_contribution = (current_track_percent / 100) * (100 / len(tracks))
                    overall_progress = int(base_progress + current_contribution)
                else:
                    overall_progress = 0

                # Update database with track-based progress
                await db.update_job(
                    job_id,
                    completed=completed,
                    current=f"Transcribing: {track_title} ({overall_progress}%)"
                )

                # Check if task is done
                if transcribe_task.done():
                    break

                # Safety limit
                if poll_count >= max_polls:
                    logger.warning(f"Transcription poll limit reached")
                    break

                # Sleep and continue polling
                try:
                    await asyncio.wait_for(asyncio.sleep(0.5), timeout=0.5)
                except asyncio.TimeoutError:
                    pass

            # Wait for transcription to complete (should be instant if we broke out of loop above)
            whisper_output = await transcribe_task

            # Parse segments
            segments = WhisperTranscriber.parse_segments(whisper_output)
            if not segments:
                raise ValueError("No segments produced by Whisper")

            # Update progress AFTER transcription completes (saving)
            await db.update_job(job_id, current=f"Saving: {track_title}")

            # Create transcript run
            run_id = await db.create_transcript_run(
                track_id=track_id,
                language=language,
                source="whisper",
                engine="openai",
                model=model_name,
                prompt=None,
                segments=segments,
                metadata={"created_via": "auto_transcription", "job_id": job_id, "device": device},
            )

            # Set as active
            await db.set_track_active_transcript(track_id, run_id)

            # Share this transcript run with sibling tracks (same filename + duration in
            # the same item). Spares the user from re-running whisper on FLAC vs MP3.
            try:
                shared_ids = await db.replicate_transcript_run_to_siblings(run_id)
                if shared_ids:
                    await db.append_job_event(
                        job_id,
                        "info",
                        f"Shared transcript with {len(shared_ids)} sibling track(s)",
                        {"source_run_id": run_id, "new_run_ids": shared_ids},
                    )
            except Exception as share_err:
                logger.warning(f"[whisper_job] Sibling-share failed for run {run_id}: {share_err}")

            # Generate track summary for context memory (optional, don't fail if it errors)
            try:
                from pipeline.track_summarizer import TrackSummarizer

                # Get API key and model for summary generation
                summary_provider = await db.get_runtime_translation_provider()
                summary_base_url = None
                summary_request_format = "openai"
                if summary_provider == "openrouter":
                    summary_api_key = await db.get_runtime_openrouter_api_key()
                    summary_model = await db.get_runtime_openrouter_model()
                elif summary_provider == "chutes":
                    summary_api_key = await db.get_runtime_chutes_api_key()
                    summary_model = await db.get_runtime_chutes_model()
                elif summary_provider == "openai_compat":
                    summary_api_key = await db.get_runtime_openai_compat_api_key()
                    summary_model = await db.get_runtime_openai_compat_model()
                    summary_base_url = await db.get_runtime_openai_compat_base_url()
                    summary_request_format = await db.get_runtime_openai_compat_request_format()
                else:
                    summary_api_key = await db.get_runtime_gemini_api_key()
                    summary_model = await db.get_runtime_gemini_model()
                    summary_provider = "gemini"

                # Ollama-format endpoints are local and unauthenticated, so a
                # missing key doesn't mean "unconfigured" in that mode.
                summary_configured = bool(summary_api_key) or (
                    summary_provider == "openai_compat"
                    and summary_request_format == "ollama"
                    and bool(summary_base_url)
                )
                if summary_configured:
                    # Get item for drama description context
                    item = await db.get_item(item_id)
                    drama_description = str(item.get("description_en") or item.get("description") or "") if item else ""

                    summarizer = TrackSummarizer(
                        api_key=summary_api_key,
                        model=summary_model,
                        provider=summary_provider,
                        base_url=summary_base_url,
                        request_format=summary_request_format,
                    )
                    # -----------------------------------------
                    # Fetch previous summaries for continuity
                    # -----------------------------------------
                    previous_raw = await db.get_previous_track_summaries(
                        item_id=item_id,
                        current_track_index=track.get("track_index", idx),
                        limit=2
                    )
                    previous_summaries = []
                    for row in previous_raw:
                        js = row.get("summary_json")
                        if js:
                            try:
                                previous_summaries.append(json.loads(js))
                            except Exception:
                                pass

                    # -----------------------------------------
                    # Generate new summary with continuity
                    # -----------------------------------------
                    summary = await summarizer.generate_summary(
                        track_number=track.get("track_index", idx),
                        segments=segments,
                        drama_description=drama_description,
                        previous_summaries=previous_summaries
                    )
                    if summary:
                        import json
                        summary_json_str = json.dumps(summary, ensure_ascii=False)
                        await db.set_track_summary(track_id, summary_json_str)
                        await db.append_job_event(
                            job_id,
                            "info",
                            f"Generated context summary for {track_title}",
                            {"track_id": track_id, "summary_length": len(summary_json_str)},
                        )
                    else:
                        logger.warning(f"[whisper_job] No summary generated for track {track_id}")
                else:
                    logger.debug(f"[whisper_job] No API key for summary generation, skipping")
            except Exception as summary_error:
                logger.warning(f"[whisper_job] Failed to generate track summary: {summary_error}")
                # Don't fail the job, just log and continue

            completed += 1
            # Update progress after successful completion
            await db.update_job(job_id, completed=completed, current=f"Completed: {track_title}")
            await db.append_job_event(
                job_id,
                "info",
                f"Transcribed: {track_title}",
                {"track_id": track_id, "transcript_run_id": run_id, "segment_count": len(segments)},
            )

        except Exception as e:
            failures.append({"track_id": track_id, "track_path": track_path, "error": str(e)})
            completed += 1
            # Update progress even on failure
            await db.update_job(job_id, completed=completed, current=f"Failed: {track_title}")
            await db.append_job_event(
                job_id,
                "error",
                f"Transcription failed: {track_title}",
                {"track_id": track_id, "error": str(e)},
            )

    # Finalize job
    status = "completed" if completed > 0 else "failed"
    logger.info(f"[JOB FINALIZE] Job {job_id} finalizing: status={status}, completed={completed}/{len(tracks)}, failures={len(failures)}")
    await db.update_job(
        job_id,
        status=status,
        stopped=1,
        total=len(tracks),
        completed=completed,
        success=completed,
        failed=len(failures),
        errors_json=failures,
        result_json={
            "item_id": item_id,
            "language": language,
            "model": model_name,
            "tracks_transcribed": completed,
            "tracks_failed": len(failures),
            "total_tracks": len(tracks),
        },
        finished_at=datetime.now().isoformat(),
    )
    logger.info(f"[JOB FINALIZE] Job {job_id} status set to '{status}'")
    await db.append_job_event(
        job_id,
        "info",
        "Transcription completed",
        {"completed": completed, "failed": len(failures), "total": len(tracks)},
    )
