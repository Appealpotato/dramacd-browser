"""Build a per-item package ZIP containing transcript/translation sidecars
and (optionally) the audio files. Reuses the JSON export format so the
package round-trips through the existing import flow.

Layout::

    {product_code}/
      dramacd-transcripts.json
      tracks/
        {stem}.{lang}.srt
        {stem}.{lang}.txt
        {stem}.{tgt}.srt           (if active translation exists)
        {stem}.{tgt}.txt
        {stem}.runs.json           (only when runs="all")
        {stem}{ext}                (only when include_audio=True)
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

import database as db
from pipeline.transcript_io import build_export_payload

logger = logging.getLogger(__name__)


_FNAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(name: str) -> str:
    cleaned = _FNAME_RE.sub("_", name).strip().rstrip(".")
    return cleaned or "track"


def _format_srt_timestamp(seconds: float | int | None) -> str:
    if seconds is None:
        seconds = 0.0
    seconds = max(0.0, float(seconds))
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    s = (total_ms // 1000) % 60
    m = (total_ms // 60_000) % 60
    h = total_ms // 3_600_000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _build_srt(segments: list[dict], text_overrides: dict[int, str] | None = None) -> str:
    """Build SRT from transcript segments. ``text_overrides`` maps
    segment_index → translation text when generating a translated SRT."""
    lines: list[str] = []
    for n, seg in enumerate(segments, start=1):
        idx = int(seg.get("segment_index") or 0)
        text = (
            text_overrides.get(idx)
            if text_overrides is not None and idx in text_overrides
            else seg.get("text")
        ) or ""
        text = str(text).strip()
        if not text:
            continue
        start = _format_srt_timestamp(seg.get("start_seconds"))
        end = _format_srt_timestamp(seg.get("end_seconds"))
        lines.append(str(n))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_txt(segments: list[dict], text_overrides: dict[int, str] | None = None) -> str:
    out: list[str] = []
    for seg in segments:
        idx = int(seg.get("segment_index") or 0)
        if text_overrides is not None:
            text = text_overrides.get(idx) or ""
        else:
            text = seg.get("text") or ""
        text = str(text).strip()
        if text:
            out.append(text)
    return "\n".join(out) + ("\n" if out else "")


async def _pick_active_or_recent(track_id: int, *, kind: str) -> tuple[dict | None, list[dict]]:
    """Return (run, segments) for the active run, or fall back to the most
    recent run if no active is set. ``kind`` is 'transcript' or 'translation'."""
    active = await db.get_track_active_outputs(track_id)
    if kind == "transcript":
        run_id = active.get("active_transcript_run_id")
        if run_id:
            run = await db.get_transcript_run(int(run_id))
            if run:
                return run, await db.get_transcript_segments(int(run_id))
        runs = await db.list_transcript_runs(track_id)
        if runs:
            run = runs[0]
            return run, await db.get_transcript_segments(int(run["id"]))
        return None, []
    else:
        run_id = active.get("active_translation_run_id")
        if run_id:
            run = await db.get_translation_run(int(run_id))
            if run:
                return run, await db.get_translation_segments(int(run_id))
        runs = await db.list_translation_runs(track_id)
        if runs:
            run = runs[0]
            return run, await db.get_translation_segments(int(run["id"]))
        return None, []


def _sanitize_relative_components(relative: Path) -> list[str]:
    """Split a relative path into safe-for-zip components, dropping any
    drive/anchor and filtering '..' segments."""
    parts: list[str] = []
    for part in relative.parts:
        if part in ("", ".", os.sep, "/", "\\"):
            continue
        if part == "..":
            # Refuse to escape the package root; collapse to nothing.
            continue
        parts.append(_safe_filename(part))
    return parts


def _format_clock(seconds) -> str:
    """Render a duration as `M:SS` or `H:MM:SS`. Returns `--:--` for falsy."""
    try:
        s = float(seconds or 0)
    except (TypeError, ValueError):
        s = 0.0
    if s <= 0:
        return "--:--"
    total = int(round(s))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _archive_relative_for_track(track: dict, *, preserve_paths: bool) -> str | None:
    """Return the in-zip path the track's audio lives at, when known.
    Only meaningful for the preserve_paths layout."""
    if not preserve_paths:
        return None
    rel = _track_relative_path(track)
    if rel is None:
        return None
    parts = _sanitize_relative_components(rel)
    if not parts:
        return None
    return "audio/" + "/".join(parts)


_TRACK_PREFIX_RE = re.compile(
    r"^\s*(?:tr|track)\s*\d+\s*[_\-\.\s:]+|^\s*\d+\s*[_\-\.\s:]+",
    re.IGNORECASE,
)


def _strip_track_number_prefix(s: str) -> str:
    """Drop filename-style ``TR01_`` / ``track 12 - `` prefixes from titles
    so the displayed name reads like prose. The numeric position is shown
    by the tracklist's own line numbering."""
    if not s:
        return s
    return _TRACK_PREFIX_RE.sub("", s, count=1).strip()


def _sort_groups(groups: list[dict]) -> list[dict]:
    """Stable order: by the smallest track_index in each group's tracks."""
    def _min_idx(g: dict) -> int:
        tracks = g.get("tracks") or []
        if not tracks:
            return 1_000_000
        return min(int(t.get("track_index") or 0) for t in tracks)
    return sorted(groups, key=_min_idx)


def _preferred_track_in_group(group: dict) -> dict | None:
    pref_id = group.get("preferred_track_id")
    if pref_id is None:
        return None
    for t in group.get("tracks") or []:
        if int(t.get("id") or 0) == int(pref_id):
            return t
    # Fall back to the first track if the preferred id couldn't be matched.
    tracks = group.get("tracks") or []
    return tracks[0] if tracks else None


def build_tracklist_data(item: dict, groups: list[dict], *, preserve_paths: bool = False) -> dict:
    """Structured tracklist payload for tracklist.json + standalone API.

    Takes *groups* (sibling-clustered tracks) so FLAC + MP3 + SFX/no-SFX
    variants of the same audio collapse into a single tracklist row."""
    track_rows: list[dict] = []
    total_duration = 0.0
    for n, group in enumerate(_sort_groups(groups), start=1):
        pref = _preferred_track_in_group(group)
        if not pref:
            continue
        dur = pref.get("duration_seconds") or 0
        try:
            total_duration += float(dur or 0)
        except (TypeError, ValueError):
            pass
        track_rows.append({
            "n": n,
            "track_index": pref.get("track_index"),
            "title": _strip_track_number_prefix(pref.get("title") or ""),
            "title_raw": pref.get("title"),
            "title_en": _strip_track_number_prefix(pref.get("title_en") or ""),
            "title_en_raw": pref.get("title_en"),
            "duration_seconds": dur or None,
            "duration_clock": _format_clock(dur),
            "codecs": list(group.get("codecs") or []),
            "variants": list(group.get("variants") or []),
            "track_path": pref.get("track_path"),
            "archive_path": _archive_relative_for_track(pref, preserve_paths=preserve_paths),
        })
    return {
        "product_code": item.get("product_code"),
        "item_id": item.get("id"),
        "title": item.get("title"),
        "title_en": item.get("title_en"),
        "track_count": len(track_rows),
        "total_duration_seconds": round(total_duration, 3),
        "total_duration_clock": _format_clock(total_duration),
        "preserve_paths": bool(preserve_paths),
        "tracks": track_rows,
    }


def build_tracklist_text(item: dict, groups: list[dict]) -> str:
    """Render a human-readable tracklist (tracklist.txt). One line per
    canonical track (siblings collapsed), filename prefixes stripped."""
    code = (item.get("product_code") or "").strip()
    title_ja = (item.get("title") or "").strip()
    title_en = (item.get("title_en") or "").strip()

    header_lines: list[str] = []
    if code:
        header_lines.append(code)
    if title_ja:
        header_lines.append(title_ja)
    if title_en and title_en != title_ja:
        header_lines.append(title_en)
    header_lines.append("")

    body_lines: list[str] = []
    total = 0.0
    track_count = 0
    for n, group in enumerate(_sort_groups(groups), start=1):
        pref = _preferred_track_in_group(group)
        if not pref:
            continue
        track_count += 1
        ja = _strip_track_number_prefix(pref.get("title") or "")
        en = _strip_track_number_prefix(pref.get("title_en") or "")
        dur = pref.get("duration_seconds") or 0
        try:
            total += float(dur or 0)
        except (TypeError, ValueError):
            pass
        clock = _format_clock(dur) if dur else ""
        # Primary line: JA title (the original) + duration. EN translation
        # sits underneath with an arrow, mirroring the Workshop track row.
        primary = ja or en or "—"
        head = f"{n:02d}.  {primary}"
        if clock:
            pad = max(2, 56 - len(head))
            body_lines.append(f"{head}{' ' * pad}{clock}")
        else:
            body_lines.append(head)
        if en and ja and en != ja:
            body_lines.append(f"     ↳ {en}")

    body_lines.append("")
    body_lines.append(f"{track_count} track{'s' if track_count != 1 else ''} · {_format_clock(total)}")

    return "\n".join(header_lines + body_lines) + "\n"


def _track_relative_path(track: dict) -> Path | None:
    """Compute a track's path relative to its extract_root, when both exist
    on disk (or look like they could). Returns None when no useful
    relative path can be derived."""
    track_path = (track.get("track_path") or "").strip()
    extract_root = (track.get("extract_root") or "").strip()
    if not track_path:
        return None
    tp = Path(track_path)
    if extract_root:
        try:
            return tp.relative_to(Path(extract_root))
        except ValueError:
            pass  # extract_root doesn't prefix track_path; fall through
    # Fallback: use the last two path components (parent folder + filename) so
    # we still preserve at least the immediate disc/folder context when
    # extract_root isn't set.
    try:
        if len(tp.parts) >= 2:
            return Path(tp.parts[-2]) / tp.parts[-1]
    except Exception:
        pass
    return Path(tp.name)


async def build_package_zip(
    item_id: int,
    *,
    runs: str = "active",
    include_audio: bool = False,
    preserve_paths: bool = False,
    include_srt: bool = True,
    include_txt: bool = True,
    include_tracklist: bool = True,
    include_all_archive_files: bool = False,
) -> tuple[bytes, str, list[dict]]:
    """Build a single-item package as an in-memory ZIP.

    Returns (bytes, suggested_filename, skipped_audio) where skipped_audio
    lists tracks whose audio was requested but missing/unreadable on disk —
    callers surface it so a subtitles-only ZIP never masquerades as a full
    export."""
    item = await db.get_item(item_id)
    if not item:
        raise ValueError(f"Item {item_id} not found")
    include_all = (runs == "all")
    # Mirroring everything the archive shipped only makes sense in the
    # original-folder layout, and obviously implies audio.
    if include_all_archive_files:
        preserve_paths = True
        include_audio = True

    code = (item.get("product_code") or f"item{item_id}").strip() or f"item{item_id}"
    safe_code = _safe_filename(code)
    tracks = await db.get_pipeline_tracks(item_id)

    payload = await build_export_payload(item_ids=[item_id])

    # Resolve collision-free sidecar stems. Items often ship the same track
    # in both MP3 and FLAC, which produces duplicate basenames; disambiguate
    # by appending the codec/extension, then track_index as a final tiebreaker.
    raw_stems: list[str] = []
    for track in tracks:
        track_path = track.get("track_path") or ""
        raw = Path(track_path).stem if track_path else f"track-{int(track['id'])}"
        raw_stems.append(_safe_filename(raw))
    stem_counts: dict[str, int] = {}
    for s in raw_stems:
        stem_counts[s] = stem_counts.get(s, 0) + 1
    sidecar_stems: list[str] = []
    used: set[str] = set()
    for track, base in zip(tracks, raw_stems):
        candidate = base
        if stem_counts.get(base, 0) > 1:
            ext = Path(track.get("track_path") or "").suffix.lstrip(".").lower()
            if ext:
                candidate = f"{base}.{ext}"
        if candidate in used:
            candidate = f"{candidate}-{int(track.get('track_index') or track['id'])}"
        n = 2
        while candidate in used:
            candidate = f"{candidate}-{n}"
            n += 1
        used.add(candidate)
        sidecar_stems.append(candidate)

    buf = io.BytesIO()
    used_zip_names: set[str] = set()
    skipped_audio: list[dict] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr(
            f"{safe_code}/dramacd-transcripts.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        used_zip_names.add(f"{safe_code}/dramacd-transcripts.json")

        manifest_tracks: list[dict] = []
        for track, stem in zip(tracks, sidecar_stems):
            track_id = int(track["id"])
            track_path = track.get("track_path") or ""

            transcript_run, transcript_segments = await _pick_active_or_recent(track_id, kind="transcript")
            translation_run, translation_segments = await _pick_active_or_recent(track_id, kind="translation")

            # Pick the in-zip locations for this track's sidecars and audio.
            # When preserve_paths=True we mirror the original archive layout
            # under audio/; otherwise we use the flat tracks/{stem}/ layout.
            if preserve_paths:
                rel = _track_relative_path(track)
                if rel is not None:
                    rel_parts = _sanitize_relative_components(rel)
                else:
                    rel_parts = [_safe_filename(Path(track_path).name) if track_path else f"track-{track_id}"]
                if not rel_parts:
                    rel_parts = [f"track-{track_id}"]
                rel_dir_parts = rel_parts[:-1]
                rel_file = rel_parts[-1]
                rel_stem = Path(rel_file).stem or rel_file
                dir_prefix = "/".join([safe_code, "audio", *rel_dir_parts]).rstrip("/")
                sidecar_prefix = f"{dir_prefix}/{rel_stem}"
                audio_zip_path = f"{dir_prefix}/{rel_file}"
            else:
                sidecar_prefix = f"{safe_code}/tracks/{stem}"
                audio_zip_path = None  # computed at write time below

            track_entry: dict = {
                "track_id": track_id,
                "track_index": track.get("track_index"),
                "title": track.get("title"),
                "files": [],
            }

            if transcript_run and transcript_segments:
                lang = (transcript_run.get("language") or "ja").strip() or "ja"
                if include_srt:
                    srt_name = f"{sidecar_prefix}.{lang}.srt"
                    zf.writestr(srt_name, _build_srt(transcript_segments))
                    used_zip_names.add(srt_name)
                    track_entry["files"].append(srt_name)
                if include_txt:
                    txt_name = f"{sidecar_prefix}.{lang}.txt"
                    zf.writestr(txt_name, _build_txt(transcript_segments))
                    used_zip_names.add(txt_name)
                    track_entry["files"].append(txt_name)

                if translation_run and translation_segments:
                    overrides = {
                        int(s.get("segment_index") or 0): str(s.get("text") or "")
                        for s in translation_segments
                    }
                    tgt = (translation_run.get("target_language") or "en").strip() or "en"
                    if include_srt:
                        tsrt = f"{sidecar_prefix}.{tgt}.srt"
                        zf.writestr(tsrt, _build_srt(transcript_segments, text_overrides=overrides))
                        used_zip_names.add(tsrt)
                        track_entry["files"].append(tsrt)
                    if include_txt:
                        ttxt = f"{sidecar_prefix}.{tgt}.txt"
                        zf.writestr(ttxt, _build_txt(transcript_segments, text_overrides=overrides))
                        used_zip_names.add(ttxt)
                        track_entry["files"].append(ttxt)

            if include_all:
                all_runs = {
                    "track_id": track_id,
                    "track_path": track_path,
                    "transcript_runs": [],
                    "translation_runs": [],
                }
                for r in await db.list_transcript_runs(track_id):
                    segs = await db.get_transcript_segments(int(r["id"]))
                    all_runs["transcript_runs"].append({
                        "run_id": int(r["id"]),
                        "language": r.get("language"),
                        "engine": r.get("engine"),
                        "model": r.get("model"),
                        "created_at": r.get("created_at"),
                        "segments": [
                            {
                                "segment_index": s.get("segment_index"),
                                "start_seconds": s.get("start_seconds"),
                                "end_seconds": s.get("end_seconds"),
                                "text": s.get("text"),
                            }
                            for s in segs
                        ],
                    })
                for r in await db.list_translation_runs(track_id):
                    segs = await db.get_translation_segments(int(r["id"]))
                    all_runs["translation_runs"].append({
                        "run_id": int(r["id"]),
                        "transcript_run_id": r.get("transcript_run_id"),
                        "target_language": r.get("target_language"),
                        "engine": r.get("engine"),
                        "model": r.get("model"),
                        "created_at": r.get("created_at"),
                        "segments": [
                            {
                                "segment_index": s.get("segment_index"),
                                "text": s.get("text"),
                            }
                            for s in segs
                        ],
                    })
                runs_name = f"{sidecar_prefix}.runs.json"
                zf.writestr(runs_name, json.dumps(all_runs, ensure_ascii=False, indent=2))
                used_zip_names.add(runs_name)
                track_entry["files"].append(runs_name)

            if include_audio and track_path:
                src = Path(track_path)
                if src.is_file():
                    if preserve_paths and audio_zip_path:
                        audio_name = audio_zip_path
                    else:
                        # Audio keeps its original suffix; the disambiguated
                        # stem already contains the extension when needed (e.g.
                        # "01_xxx.flac"), so strip a trailing duplicate suffix.
                        audio_stem = stem
                        if audio_stem.lower().endswith(src.suffix.lower()):
                            audio_stem = audio_stem[: -len(src.suffix)]
                        audio_name = f"{safe_code}/tracks/{audio_stem}{src.suffix.lower()}"
                    if audio_name in used_zip_names:
                        # Disambiguate only as a last resort.
                        base, ext = os.path.splitext(audio_name)
                        audio_name = f"{base}-{track_id}{ext}"
                    used_zip_names.add(audio_name)
                    try:
                        zf.write(src, audio_name)
                        track_entry["files"].append(audio_name)
                    except OSError as exc:
                        logger.warning("Failed to add audio %s to package: %s", src, exc)
                        skipped_audio.append({
                            "track_id": track_id,
                            "title": track.get("title"),
                            "path": str(src),
                            "reason": "unreadable",
                        })
                else:
                    logger.info("Audio missing for track %s (%s); skipping in package", track_id, src)
                    skipped_audio.append({
                        "track_id": track_id,
                        "title": track.get("title"),
                        "path": str(src),
                        "reason": "missing",
                    })

            manifest_tracks.append(track_entry)

        # Tracklist (txt + json). Cheap and always useful; toggle off if
        # the user explicitly doesn't want it.
        if include_tracklist:
            try:
                # Group sibling tracks (FLAC/MP3 of the same audio) so the
                # tracklist lists each recording once instead of duplicating
                # entries per codec/variant.
                tl_groups = await db.get_pipeline_track_groups(item_id)
                tl_data = build_tracklist_data(item, tl_groups, preserve_paths=preserve_paths)
                tl_text = build_tracklist_text(item, tl_groups)
                tl_txt_name = f"{safe_code}/tracklist.txt"
                tl_json_name = f"{safe_code}/tracklist.json"
                zf.writestr(tl_txt_name, tl_text)
                zf.writestr(tl_json_name, json.dumps(tl_data, ensure_ascii=False, indent=2))
                used_zip_names.update({tl_txt_name, tl_json_name})
            except Exception as exc:
                logger.warning("Failed to write tracklist: %s", exc)

        # Mirror non-track files (covers, booklets, scripts, anything else
        # that shipped in the archive). Walks each track's extract_root once.
        archive_extras_added = 0
        if include_all_archive_files:
            seen_roots: set[str] = set()
            for t in tracks:
                er = (t.get("extract_root") or "").strip()
                if not er or er in seen_roots:
                    continue
                seen_roots.add(er)
                root = Path(er)
                if not root.is_dir():
                    continue
                for src in root.rglob("*"):
                    if not src.is_file():
                        continue
                    try:
                        rel = src.relative_to(root)
                    except ValueError:
                        rel = Path(src.name)
                    rel_parts = _sanitize_relative_components(rel)
                    if not rel_parts:
                        continue
                    zip_name = "/".join([safe_code, "audio", *rel_parts])
                    if zip_name in used_zip_names:
                        continue  # already emitted (audio file or sidecar)
                    try:
                        zf.write(src, zip_name)
                        used_zip_names.add(zip_name)
                        archive_extras_added += 1
                    except OSError as exc:
                        logger.warning("Failed to add archive file %s: %s", src, exc)

        manifest = {
            "product_code": item.get("product_code"),
            "item_id": item_id,
            "title": item.get("title"),
            "title_en": item.get("title_en"),
            "generated_at": datetime.now().isoformat(),
            "runs": "all" if include_all else "active",
            "include_audio": bool(include_audio),
            "preserve_paths": bool(preserve_paths),
            "include_srt": bool(include_srt),
            "include_txt": bool(include_txt),
            "include_tracklist": bool(include_tracklist),
            "include_all_archive_files": bool(include_all_archive_files),
            "archive_extras_added": archive_extras_added,
            "skipped_audio": skipped_audio,
            "tracks": manifest_tracks,
        }
        zf.writestr(
            f"{safe_code}/manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"dramacd-package-{safe_code}-{stamp}.zip"
    return buf.getvalue(), filename, skipped_audio


def extract_transcripts_json_from_zip(raw: bytes) -> dict:
    """Parse a package ZIP and return the embedded dramacd-transcripts.json payload."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        candidates = [n for n in zf.namelist() if n.endswith("dramacd-transcripts.json")]
        if not candidates:
            raise ValueError("Package ZIP does not contain dramacd-transcripts.json")
        candidates.sort(key=lambda n: (len(n), n))
        with zf.open(candidates[0]) as f:
            data = f.read()
    return json.loads(data.decode("utf-8"))


def looks_like_zip(raw: bytes) -> bool:
    return len(raw) >= 4 and raw[:4] == b"PK\x03\x04"
