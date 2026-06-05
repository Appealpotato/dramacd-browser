"""Export and import for transcripts/translations.

Backup format is portable across installs: items are keyed by ``product_code``
and tracks by ``track_path`` (with basename + track_index fallbacks for
re-resolution on a different machine where extraction roots differ).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Iterable

import database as db

logger = logging.getLogger(__name__)

EXPORT_FORMAT = "dramacd-transcripts-export"
EXPORT_VERSION = 1


def _run_signature(run: dict, kind: str) -> str:
    """Stable identifier for a transcript or translation run within an export.

    Used so a translation can reference the transcript it was built against
    without leaking database ids."""
    parts = [
        kind,
        str(run.get("language") or run.get("target_language") or ""),
        str(run.get("source") or ""),
        str(run.get("engine") or ""),
        str(run.get("model") or ""),
        str(run.get("created_at") or ""),
    ]
    return "|".join(parts)


async def build_export_payload(item_ids: Iterable[int] | None = None) -> dict:
    """Serialize transcripts + translations for the given items (or all)."""
    if item_ids is None:
        result = await db.get_all_items(limit=100000, offset=0)
        ids = [int(it["id"]) for it in (result.get("items") or [])]
    else:
        ids = [int(i) for i in item_ids]

    out_items: list[dict] = []
    for item_id in ids:
        item = await db.get_item(item_id)
        if not item:
            continue
        tracks = await db.get_pipeline_tracks(item_id)
        track_payloads: list[dict] = []
        any_runs = False
        for track in tracks:
            track_id = int(track["id"])
            transcript_runs = await db.list_transcript_runs(track_id)
            translation_runs = await db.list_translation_runs(track_id)
            active = await db.get_track_active_outputs(track_id)

            transcript_run_payloads: list[dict] = []
            transcript_sig_by_id: dict[int, str] = {}
            for run in transcript_runs:
                segments = await db.get_transcript_segments(int(run["id"]))
                sig = _run_signature(run, "transcript")
                transcript_sig_by_id[int(run["id"])] = sig
                transcript_run_payloads.append({
                    "signature": sig,
                    "language": run.get("language"),
                    "source": run.get("source"),
                    "status": run.get("status"),
                    "engine": run.get("engine"),
                    "model": run.get("model"),
                    "prompt": run.get("prompt"),
                    "metadata": run.get("metadata_json") or {},
                    "created_at": run.get("created_at"),
                    "segments": [
                        {
                            "segment_index": s["segment_index"],
                            "start_seconds": s["start_seconds"],
                            "end_seconds": s["end_seconds"],
                            "text": s["text"],
                            "confidence": s.get("confidence"),
                            "meta": s.get("meta_json") or {},
                        }
                        for s in segments
                    ],
                })

            translation_run_payloads: list[dict] = []
            translation_sig_by_id: dict[int, str] = {}
            for run in translation_runs:
                segments = await db.get_translation_segments(int(run["id"]))
                sig = _run_signature(run, "translation")
                translation_sig_by_id[int(run["id"])] = sig
                transcript_run_id = run.get("transcript_run_id")
                translation_run_payloads.append({
                    "signature": sig,
                    "transcript_run_signature": transcript_sig_by_id.get(int(transcript_run_id)) if transcript_run_id else None,
                    "target_language": run.get("target_language"),
                    "source": run.get("source"),
                    "status": run.get("status"),
                    "engine": run.get("engine"),
                    "model": run.get("model"),
                    "prompt": run.get("prompt"),
                    "metadata": run.get("metadata_json") or {},
                    "created_at": run.get("created_at"),
                    "segments": [
                        {
                            "segment_index": s["segment_index"],
                            "text": s["text"],
                            "meta": s.get("meta_json") or {},
                        }
                        for s in segments
                    ],
                })

            if not transcript_run_payloads and not translation_run_payloads:
                continue
            any_runs = True

            active_transcript_id = active.get("active_transcript_run_id")
            active_translation_id = active.get("active_translation_run_id")
            track_payloads.append({
                "track_path": track.get("track_path"),
                "track_basename": os.path.basename(track.get("track_path") or ""),
                "track_index": track.get("track_index"),
                "title": track.get("title"),
                "title_en": track.get("title_en"),
                "duration_seconds": track.get("duration_seconds"),
                "active_transcript_signature": transcript_sig_by_id.get(int(active_transcript_id)) if active_transcript_id else None,
                "active_translation_signature": translation_sig_by_id.get(int(active_translation_id)) if active_translation_id else None,
                "transcript_runs": transcript_run_payloads,
                "translation_runs": translation_run_payloads,
            })

        if not any_runs:
            continue

        out_items.append({
            "product_code": item.get("product_code"),
            "title": item.get("title"),
            "title_en": item.get("title_en"),
            "tracks": track_payloads,
        })

    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": datetime.now().isoformat(),
        "item_count": len(out_items),
        "items": out_items,
    }


def _match_track(payload_track: dict, db_tracks: list[dict]) -> dict | None:
    """Re-resolve a track from the export payload to a row in pipeline_tracks.

    Strategy: exact track_path → basename match → track_index match."""
    if not db_tracks:
        return None
    target_path = (payload_track.get("track_path") or "").strip()
    if target_path:
        for t in db_tracks:
            if (t.get("track_path") or "").strip() == target_path:
                return t
    target_basename = (payload_track.get("track_basename")
                       or os.path.basename(target_path)).strip().lower()
    if target_basename:
        for t in db_tracks:
            if os.path.basename(t.get("track_path") or "").strip().lower() == target_basename:
                return t
    target_index = payload_track.get("track_index")
    if target_index is not None:
        for t in db_tracks:
            if t.get("track_index") == target_index:
                return t
    return None


async def import_payload(payload: dict, replace_existing: bool = False) -> dict:
    """Apply an export payload to the database. Returns a per-item summary."""
    if not isinstance(payload, dict):
        raise ValueError("Import payload must be a JSON object")
    if payload.get("format") != EXPORT_FORMAT:
        raise ValueError(f"Unrecognized export format: {payload.get('format')!r}")
    if int(payload.get("version") or 0) > EXPORT_VERSION:
        raise ValueError(
            f"Export version {payload.get('version')} is newer than this app supports ({EXPORT_VERSION})"
        )

    items = payload.get("items") or []
    summary = {
        "items_seen": 0,
        "items_matched": 0,
        "items_created": 0,
        "metadata_applied": 0,
        "items_skipped_missing": [],
        "tracks_matched": 0,
        "tracks_skipped_missing": [],
        "transcript_runs_created": 0,
        "translation_runs_created": 0,
        "active_transcript_set": 0,
        "active_translation_set": 0,
        "errors": [],
    }

    for item_payload in items:
        summary["items_seen"] += 1
        product_code = (item_payload.get("product_code") or "").strip()
        if not product_code:
            summary["errors"].append("Item entry missing product_code; skipped")
            continue

        item_metadata = item_payload.get("metadata")
        item = await db.get_item_by_product_code(product_code)
        if not item and item_metadata:
            # A producer package brought prefetched DLsite metadata for a work the library
            # doesn't have yet: create the item so metadata (and, once its archive is
            # extracted, transcripts) have somewhere to land.
            try:
                await db.upsert_item({**item_metadata, "product_code": product_code})
                item = await db.get_item_by_product_code(product_code)
                summary["items_created"] = summary.get("items_created", 0) + 1
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"create item {product_code} failed: {exc}")
        if not item:
            summary["items_skipped_missing"].append(product_code)
            continue
        summary["items_matched"] += 1
        item_id = int(item["id"])

        if item_metadata:
            # Apply prefetched metadata so the library never has to hit DLsite itself.
            try:
                await db.update_item_metadata(product_code, item_metadata)
                summary["metadata_applied"] = summary.get("metadata_applied", 0) + 1
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"metadata for {product_code} failed: {exc}")

        db_tracks = await db.get_pipeline_tracks(item_id)
        tracks_payload = item_payload.get("tracks") or []

        for track_payload in tracks_payload:
            db_track = _match_track(track_payload, db_tracks)
            if not db_track:
                summary["tracks_skipped_missing"].append({
                    "product_code": product_code,
                    "track_path": track_payload.get("track_path"),
                    "track_basename": track_payload.get("track_basename"),
                    "track_index": track_payload.get("track_index"),
                })
                continue
            summary["tracks_matched"] += 1
            track_id = int(db_track["id"])

            if replace_existing:
                # FK cascades drop dependent translation runs + segments.
                for run in await db.list_translation_runs(track_id):
                    await db.delete_translation_run(int(run["id"]))
                for run in await db.list_transcript_runs(track_id):
                    await db.delete_transcript_run(int(run["id"]))

            # Restore translated track title if the export carried one and the
            # track currently has none (don't clobber a manually-edited title).
            payload_title_en = (track_payload.get("title_en") or "").strip()
            if payload_title_en and not (db_track.get("title_en") or "").strip():
                await db.set_track_title_en(track_id, payload_title_en)

            sig_to_new_transcript_id: dict[str, int] = {}

            for run_payload in track_payload.get("transcript_runs") or []:
                metadata = dict(run_payload.get("metadata") or {})
                metadata.setdefault("imported_at", datetime.now().isoformat())
                metadata.setdefault("imported_from_signature", run_payload.get("signature"))
                metadata.setdefault("imported_original_created_at", run_payload.get("created_at"))
                try:
                    new_id = await db.create_transcript_run(
                        track_id=track_id,
                        language=run_payload.get("language") or "ja",
                        source=run_payload.get("source") or "imported",
                        engine=run_payload.get("engine"),
                        model=run_payload.get("model"),
                        prompt=run_payload.get("prompt"),
                        segments=run_payload.get("segments") or [],
                        metadata=metadata,
                    )
                    summary["transcript_runs_created"] += 1
                    if run_payload.get("signature"):
                        sig_to_new_transcript_id[run_payload["signature"]] = int(new_id)
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(
                        f"transcript run failed for {product_code} track={track_id}: {exc}"
                    )

            sig_to_new_translation_id: dict[str, int] = {}
            for run_payload in track_payload.get("translation_runs") or []:
                transcript_sig = run_payload.get("transcript_run_signature")
                transcript_run_id = sig_to_new_transcript_id.get(transcript_sig) if transcript_sig else None
                if not transcript_run_id:
                    # Fall back: any transcript run on this track will satisfy FK; pick newest.
                    fallback_runs = await db.list_transcript_runs(track_id)
                    if fallback_runs:
                        transcript_run_id = int(fallback_runs[0]["id"])
                if not transcript_run_id:
                    summary["errors"].append(
                        f"translation run for {product_code} track={track_id} has no transcript to attach"
                    )
                    continue
                metadata = dict(run_payload.get("metadata") or {})
                metadata.setdefault("imported_at", datetime.now().isoformat())
                metadata.setdefault("imported_from_signature", run_payload.get("signature"))
                metadata.setdefault("imported_original_created_at", run_payload.get("created_at"))
                try:
                    new_id = await db.create_translation_run(
                        track_id=track_id,
                        transcript_run_id=transcript_run_id,
                        target_language=run_payload.get("target_language") or "en",
                        source=run_payload.get("source") or "imported",
                        engine=run_payload.get("engine"),
                        model=run_payload.get("model"),
                        prompt=run_payload.get("prompt"),
                        segments=run_payload.get("segments") or [],
                        metadata=metadata,
                    )
                    summary["translation_runs_created"] += 1
                    if run_payload.get("signature"):
                        sig_to_new_translation_id[run_payload["signature"]] = int(new_id)
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(
                        f"translation run failed for {product_code} track={track_id}: {exc}"
                    )

            active_transcript_sig = track_payload.get("active_transcript_signature")
            if active_transcript_sig and active_transcript_sig in sig_to_new_transcript_id:
                if await db.set_track_active_transcript(track_id, sig_to_new_transcript_id[active_transcript_sig]):
                    summary["active_transcript_set"] += 1
            active_translation_sig = track_payload.get("active_translation_signature")
            if active_translation_sig and active_translation_sig in sig_to_new_translation_id:
                if await db.set_track_active_translation(track_id, sig_to_new_translation_id[active_translation_sig]):
                    summary["active_translation_set"] += 1

    return summary
