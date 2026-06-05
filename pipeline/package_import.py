"""Import a bundled ``*.package.json`` (dramacd-transcripts-export) that ships inside a
producer-preprocessed release.

Some releases are pre-processed outside the library: a merged blob is split into proper
tracks and a ``<CODE>.package.json`` is dropped next to the audio carrying the per-track
JA transcript (and prefetched DLsite metadata). When such a release is scanned + extracted,
this hook ingests that package so the library skips DLsite AND Whisper.

Design notes / safety (mirrors subtitle_import.py):
  * Pure-additive and IDEMPOTENT. The extraction job calls this inside a try/except, so
    anything failing here can never break extraction.
  * It runs at most once per item: if any track already carries a ``preprocess`` transcript
    run, the package was already imported on a prior extraction and we skip - no duplicates.
  * It never clobbers other transcripts (import_payload is called replace_existing=False).
  * After importing it sets the JA transcript active on tracks that have none yet and
    replicates to sibling formats, exactly like the subtitle hook.
"""

import json
import logging
import re
from pathlib import Path

import database as db
from pipeline.transcript_io import EXPORT_FORMAT, import_payload

logger = logging.getLogger(__name__)

PACKAGE_SOURCE = "preprocess"   # the `source` build_package stamps on its transcript runs


# ── tolerant package normalization ──────────────────────────────────────────
# Producers (esp. Hermes scraping arbitrary non-DLsite sites) don't always emit the strict
# {items:[{product_code, metadata}]} shape - some ship a flat single-work object with their
# own field names (name/publisher/voice_actors/…). We normalize any *.package.json to the
# canonical shape the importer + refresh expect, mapping common aliases to the metadata keys
# update_item_metadata() actually stores. Deliberately does NOT trust the `format` field for
# detection (a stray duplicate "format":"mp3" key clobbers the export marker) - the
# *.package.json filename is the intent signal; a usable title is the acceptance bar.

_AGE_MAP = {"all ages": "ALL", "all": "ALL", "general": "ALL", "全年齢": "ALL",
            "r15": "R15", "r-15": "R15", "r18": "R18", "r-18": "R18", "18+": "R18", "adult": "R18"}
_JP_EN_RE = re.compile(r"^\s*(.+?)\s*[\(（]\s*([^（）()]+?)\s*[\)）]\s*$")


def _pkg_first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _pkg_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, dict):
        return [str(x).strip() for x in v.values() if str(x).strip()]
    return [str(v).strip()]


def _split_jp_en(names):
    """['森久保祥太郎 (Showtaro Morikubo)', …] -> (['森久保祥太郎', …], ['Showtaro Morikubo', …]).
    EN list dropped if no entry carried a parenthetical romanization."""
    jp, en = [], []
    for n in names:
        m = _JP_EN_RE.match(str(n))
        if m:
            jp.append(m.group(1).strip())
            en.append(m.group(2).strip())
        else:
            jp.append(str(n).strip())
            en.append("")
    return jp, (en if any(en) else [])


def _normalize_metadata(flat: dict) -> dict:
    """A flat producer object -> the metadata dict update_item_metadata() understands."""
    md = {}
    for dst, srcs in (
        ("title", ("title", "name", "work_name")),
        ("title_en", ("title_en", "name_en")),
        ("circle", ("circle", "publisher", "maker", "maker_name", "label", "brand")),
        ("description", ("description", "intro", "synopsis", "summary")),
        ("description_en", ("description_en",)),
        ("series", ("series", "series_name")),
        ("release_date", ("release_date", "regist_date", "date")),
        ("cover_url", ("cover_url", "cover", "image", "work_image")),
    ):
        v = _pkg_first(flat, *srcs)
        if v:
            md[dst] = str(v)
    age = flat.get("age_rating")
    if age:
        md["age_rating"] = _AGE_MAP.get(str(age).strip().lower(), str(age))
    va = _pkg_first(flat, "seiyuu", "voice_actors", "cast", "cv")
    if va:
        jp, en = _split_jp_en(_pkg_list(va))
        if jp:
            md["seiyuu"] = jp
        if en:
            md["seiyuu_en"] = en
    tags = _pkg_first(flat, "tags", "genres")
    if tags:
        md["tags"] = _pkg_list(tags)
    md["raw"] = flat                      # keep the full original blob for provenance
    return md


def _normalize_one_item(it: dict) -> dict | None:
    """One item entry (already-canonical {metadata} OR a flat object) -> canonical entry."""
    if not isinstance(it, dict):
        return None
    md = it.get("metadata")
    if not isinstance(md, dict):
        md = _normalize_metadata(it)
    if not md.get("title"):
        return None
    entry = {"metadata": md}
    code = it.get("product_code")
    if code:
        entry["product_code"] = str(code).strip().upper()
    if it.get("tracks"):
        entry["tracks"] = it["tracks"]
    if it.get("title_en") and "title_en" not in md:
        md["title_en"] = it["title_en"]
    return entry


def normalize_package_payload(data) -> dict | None:
    """Any parsed *.package.json -> canonical {format, version, items:[{metadata, …}]} or None.
    Handles both the strict items[] shape and a flat single-work object."""
    if not isinstance(data, dict):
        return None
    items = data.get("items")
    if isinstance(items, list) and items:
        norm = [e for e in (_normalize_one_item(it) for it in items) if e]
    else:
        one = _normalize_one_item(data)         # flat single-work package
        norm = [one] if one else []
    if not norm:
        return None
    return {"format": EXPORT_FORMAT, "version": 1, "items": norm}


def find_bundled_package(extract_root) -> tuple[Path, dict] | None:
    """First ``*.package.json`` under extract_root that is a dramacd-transcripts-export."""
    root = Path(extract_root)
    if not root.is_dir():
        return None
    for p in sorted(root.rglob("*.package.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        norm = normalize_package_payload(data)
        if norm:
            return p, norm
    return None


def find_package_in_archive(archive_path) -> dict | None:
    """Peek INSIDE an archive (.zip/.rar/.7z/.tar) for a bundled
    ``*.package.json`` (dramacd-transcripts-export) WITHOUT extracting it.

    Used by the scanner's non-DLsite fallthrough: an archive whose filename
    carries no DLsite code can still declare its own identity + metadata via a
    bundled package. Lists the central directory (cheap) and streams out only
    the tiny package member. Returns the parsed payload dict, or None. Never raises.
    """
    # Lazy import: extractor imports THIS module for import_bundled_package, so a
    # top-level import here would be circular.
    # IMPORTANT: list the archive's TOP LEVEL only (central directory). We deliberately do
    # NOT use list_archive_contents() here - it transparently drills into a single nested
    # .tar (the common DLsite layout) by FULLY DECOMPRESSING it just to enumerate, which on
    # a scan over a whole library would decompress every unmatched rip. A bundled package
    # always sits at the archive's top level, so a header-only listing is enough and cheap.
    from pipeline.extractor import _list_with_7z, _list_tar_contents, stream_archive_file

    archive_path = Path(archive_path)
    try:
        if archive_path.suffix.lower() == ".tar":
            entries = _list_tar_contents(archive_path)        # tar header read, no data decompress
        else:
            entries = _list_with_7z(archive_path)             # 7z central directory, no decompress
    except Exception:
        return None
    member = next(
        (e for e in entries if str(e.get("path") or "").lower().endswith(".package.json")),
        None,
    )
    if not member:
        return None
    try:
        raw = stream_archive_file(archive_path, member["path"])
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return normalize_package_payload(payload)


def find_package_at_path(path) -> dict | None:
    """Find + normalize a bundled package given a source path that may be a DIRECTORY (look
    for a loose *.package.json, then peek any archives sitting in it) or an ARCHIVE file
    (peek it). Used by tokuten refresh, whose source lives on the tokuten's ``local_path``.
    Returns a normalized payload or None."""
    from config import ARCHIVE_EXTENSIONS
    p = Path(path)
    try:
        if p.is_file():
            if p.suffix.lower() in ARCHIVE_EXTENSIONS:
                return find_package_in_archive(p)
            return None
        if p.is_dir():
            found = find_bundled_package(p)              # loose *.package.json on disk
            if found:
                return found[1]
            for f in sorted(p.iterdir()):                # else an archive inside the folder
                if f.is_file() and f.suffix.lower() in ARCHIVE_EXTENSIONS:
                    payload = find_package_in_archive(f)
                    if payload:
                        return payload
    except Exception:
        return None
    return None


async def import_bundled_package(item_id: int, extract_root) -> dict:
    """Ingest a bundled package's transcripts/metadata for item_id. Returns a summary;
    never raises."""
    summary = {
        "found": False,
        "imported": False,
        "skipped_existing": False,
        "transcript_runs_created": 0,
        "tracks_activated": 0,
        "errors": [],
    }
    try:
        found = find_bundled_package(extract_root)
        if not found:
            return summary
        pkg_path, payload = found
        summary["found"] = True

        # Idempotency: if any track already carries a preprocess transcript, this package
        # was imported on an earlier extraction - don't import it again.
        tracks = await db.get_pipeline_tracks(item_id)
        for tr in tracks:
            for run in await db.list_transcript_runs(int(tr["id"])):
                if (run.get("source") or "") == PACKAGE_SOURCE:
                    summary["skipped_existing"] = True
                    return summary

        result = await import_payload(payload, replace_existing=False)
        summary["transcript_runs_created"] = int(result.get("transcript_runs_created", 0))
        summary["imported"] = summary["transcript_runs_created"] > 0
        if result.get("errors"):
            summary["errors"].extend(result["errors"])

        # Make the freshly-imported JA transcript active on tracks that have none yet, and
        # replicate to sibling formats (FLAC/MP3 variants) - same as the subtitle hook.
        for tr in await db.get_pipeline_tracks(item_id):
            tid = int(tr["id"])
            active = await db.get_track_active_outputs(tid)
            if active.get("active_transcript_run_id"):
                continue
            pre = next(
                (r for r in await db.list_transcript_runs(tid)
                 if (r.get("source") or "") == PACKAGE_SOURCE),
                None,
            )
            if not pre:
                continue
            try:
                await db.set_track_active_transcript(tid, int(pre["id"]))
                summary["tracks_activated"] += 1
                await db.replicate_transcript_run_to_siblings(int(pre["id"]))
            except Exception as e:
                summary["errors"].append(f"activate/replicate track {tid}: {e}")

        logger.info("[package_import] item %s <- %s (%d runs, %d activated)",
                    item_id, pkg_path.name, summary["transcript_runs_created"],
                    summary["tracks_activated"])
    except Exception as e:  # pragma: no cover - defensive
        summary["errors"].append(str(e))
    return summary
