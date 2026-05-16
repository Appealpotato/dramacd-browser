from datetime import datetime

import database as db


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


async def queue_extraction(item_id: int, force: bool = False) -> int:
    metadata = {
        "item_id": item_id,
        "force": bool(force),
        "mode": "on_demand",
        "queued_at": _now_iso(),
        "auto_extract": False,
    }
    job_id = await db.create_job("pipeline_extract", status="queued", metadata=metadata)
    await db.append_job_event(
        job_id,
        "info",
        "On-demand extraction queued",
        {"item_id": item_id, "force": bool(force)},
    )
    return job_id


async def queue_transcription(item_id: int, language: str = "ja", model: str = "small", force: bool = False, track_ids: list = None) -> int:
    metadata = {
        "item_id": item_id,
        "language": language,
        "model": model,
        "force": bool(force),
        "mode": "on_demand",
        "queued_at": _now_iso(),
    }
    if track_ids:
        metadata["track_ids"] = track_ids
    job_id = await db.create_job("pipeline_transcribe", status="queued", metadata=metadata)
    await db.append_job_event(
        job_id,
        "info",
        "On-demand transcription queued",
        {"item_id": item_id, "language": language, "model": model, "track_count": len(track_ids) if track_ids else "all"},
    )
    return job_id


async def queue_autopilot(
    *,
    item_id: int,
    target_language: str = "en",
    provider: str | None = None,
    model: str | None = None,
    max_tokens_per_chunk: int = 1000,
    max_lines_per_chunk: int = 20,
    max_retries_per_chunk: int = 2,
    retry_backoff_seconds: float = 1.0,
    glossary: str | None = None,
    character_memory: str | None = None,
    transcribe_language: str = "ja",
    transcribe_model: str | None = None,
    skip_stages: list[str] | None = None,
    force_extract: bool = False,
    force_transcribe: bool = False,
    force_translate: bool = False,
) -> int:
    metadata = {
        "item_id": int(item_id),
        "target_language": target_language,
        "provider": provider,
        "model": model,
        "max_tokens_per_chunk": int(max_tokens_per_chunk),
        "max_lines_per_chunk": int(max_lines_per_chunk),
        "max_retries_per_chunk": int(max_retries_per_chunk),
        "retry_backoff_seconds": float(retry_backoff_seconds),
        "glossary": glossary,
        "character_memory": character_memory,
        "transcribe_language": transcribe_language,
        "transcribe_model": transcribe_model,
        "skip_stages": list(skip_stages or []),
        "force_extract": bool(force_extract),
        "force_transcribe": bool(force_transcribe),
        "force_translate": bool(force_translate),
        "queued_at": _now_iso(),
    }
    job_id = await db.create_job("pipeline_autopilot", status="queued", metadata=metadata)
    # The autopilot job emits a "Starting pipeline" event when the worker
    # actually begins, so the queue moment doesn't need its own event row —
    # it's just noise in the activity log.
    return job_id


async def queue_translation(
    *,
    item_id: int,
    track_id: int,
    transcript_run_id: int,
    target_language: str = "en",
    provider: str = "gemini",
    model: str = "gemini-2.0-flash",
    max_tokens_per_chunk: int = 1000,  # Cost optimized: 100 segments = ~5 API calls
    max_lines_per_chunk: int = 20,  # 20 lines per chunk (cheaper, but partial results OK now)
    max_retries_per_chunk: int = 2,
    retry_backoff_seconds: float = 1.0,
    set_active: bool = True,
    glossary: str | None = None,
    character_memory: str | None = None,
) -> int:
    metadata = {
        "item_id": int(item_id),
        "track_id": int(track_id),
        "transcript_run_id": int(transcript_run_id),
        "target_language": target_language,
        "provider": provider,
        "model": model,
        "max_tokens_per_chunk": int(max_tokens_per_chunk),
        "max_lines_per_chunk": int(max_lines_per_chunk),
        "max_retries_per_chunk": int(max_retries_per_chunk),
        "retry_backoff_seconds": float(retry_backoff_seconds),
        "set_active": bool(set_active),
        "mode": "on_demand",
        "queued_at": _now_iso(),
    }
    if glossary:
        metadata["glossary"] = str(glossary)
    if character_memory:
        metadata["character_memory"] = str(character_memory)
    job_id = await db.create_job("pipeline_translate", status="queued", metadata=metadata)
    await db.append_job_event(
        job_id,
        "info",
        "On-demand translation queued",
        {
            "item_id": int(item_id),
            "track_id": int(track_id),
            "transcript_run_id": int(transcript_run_id),
            "target_language": target_language,
            "provider": provider,
            "model": model,
        },
    )
    return job_id
