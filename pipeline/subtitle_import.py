"""Import bundled .vtt / .srt subtitles that ship inside some releases (esp. official
DLsite EN packages) directly as transcript runs - so we skip Whisper on audio that
already has a timed script.

Design notes / safety:
  * Pure-additive. The extraction job calls import_bundled_subtitles() inside a
    try/except, so anything in here failing can never break extraction.
  * Language is detected from the TEXT (CJK -> ja, else en), because EN releases name
    their subs '{audio}.vtt' with no language tag.
  * We only ever create TRANSCRIPT runs and only set one active when the track has NONE
    yet - existing Whisper/imported transcripts are never overridden.
  * We do NOT create translation runs here: translation segments must index-align to a
    transcript run, and an independently-authored EN sub won't align with a JP Whisper
    transcript. That stays a deliberate, separate decision.
  * After importing, we replicate to sibling tracks (FLAC/MP3 variants) like Whisper does.
"""

import logging
import re
from pathlib import Path

import database as db

logger = logging.getLogger(__name__)

SUBTITLE_EXTS = (".vtt", ".srt")
# HH:MM:SS,mmm  /  HH:MM:SS.mmm  /  MM:SS.mmm  (hours optional)
_TS_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_TAG_RE = re.compile(r"<[^>]+>")           # VTT inline tags <c>, <00:00:01.000>, etc.
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]")


def _to_seconds(m):
    h = int(m.group(1) or 0)
    return h * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4).ljust(3, "0")) / 1000.0


def parse_subtitle(text):
    """VTT or SRT text -> [{start, end, text}], timestamps preserved. Cue blocks are
    separated by blank lines; the line containing '-->' holds the timing."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        timing_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing_idx is None:
            continue
        ts = list(_TS_RE.finditer(lines[timing_idx]))
        if len(ts) < 2:
            continue
        body = " ".join(lines[timing_idx + 1:]).strip()
        body = _TAG_RE.sub("", body).strip()
        if body:
            cues.append({"start": _to_seconds(ts[0]), "end": _to_seconds(ts[1]), "text": body})
    return cues


def _read_text(path: Path):
    for enc in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _detect_lang(cues):
    sample = " ".join(c["text"] for c in cues[:40])
    return "ja" if _CJK_RE.search(sample) else "en"


def find_sibling_subtitles(track_path):
    """Subtitle files next to an audio track. Matches '{audio.name}.vtt' (e.g.
    'foo.mp3.vtt') and '{stem}*.vtt' (e.g. 'foo.vtt', 'foo.en.vtt')."""
    p = Path(track_path)
    if not p.parent.is_dir():
        return []
    audio_name, stem = p.name.lower(), p.stem.lower()
    out = []
    for f in p.parent.iterdir():
        if f.is_file() and f.suffix.lower() in SUBTITLE_EXTS:
            n = f.name.lower()
            if n.startswith(audio_name) or n.startswith(stem + ".") or f.stem.lower() == stem:
                out.append(f)
    return out


async def import_bundled_subtitles(item_id: int) -> dict:
    """For each track of item_id with a sibling .vtt/.srt and no active transcript yet,
    import the subtitle as a transcript run. Returns a summary; never raises."""
    summary = {"imported": 0, "skipped_existing": 0, "tracks_with_subs": 0, "errors": []}
    try:
        tracks = await db.get_pipeline_tracks(item_id)
    except Exception as e:  # pragma: no cover - defensive
        summary["errors"].append(f"get_pipeline_tracks: {e}")
        return summary

    for tr in tracks:
        track_id = tr.get("id")
        track_path = tr.get("track_path")
        if not track_id or not track_path:
            continue
        try:
            subs = find_sibling_subtitles(track_path)
            if not subs:
                continue
            summary["tracks_with_subs"] += 1

            active = await db.get_track_active_outputs(track_id)
            if active.get("active_transcript_run_id"):
                summary["skipped_existing"] += 1     # never override existing transcripts
                continue

            # parse all sibling subs; prefer the Japanese script as the transcript
            parsed = []
            for sub in subs:
                cues = parse_subtitle(_read_text(sub))
                if cues:
                    parsed.append((sub, cues, _detect_lang(cues)))
            if not parsed:
                continue
            parsed.sort(key=lambda x: 0 if x[2] == "ja" else 1)
            sub, cues, lang = parsed[0]

            segments = [
                {"segment_index": i, "start_seconds": round(c["start"], 3),
                 "end_seconds": round(c["end"], 3), "text": c["text"],
                 "confidence": None, "meta": {"source_file": sub.name}}
                for i, c in enumerate(cues)
            ]
            run_id = await db.create_transcript_run(
                track_id=track_id, language=lang, source="bundled_subtitle",
                engine="subtitle", model=None, prompt=None, segments=segments,
                metadata={"imported": True, "source_file": sub.name, "format": sub.suffix.lstrip(".")},
            )
            await db.set_track_active_transcript(track_id, run_id)
            try:
                await db.replicate_transcript_run_to_siblings(run_id)
            except Exception as e:
                summary["errors"].append(f"replicate(track {track_id}): {e}")
            summary["imported"] += 1
            logger.info("[subtitle_import] track %s <- %s (%s, %d cues)",
                        track_id, sub.name, lang, len(segments))
        except Exception as e:
            summary["errors"].append(f"track {track_id}: {e}")
    return summary
