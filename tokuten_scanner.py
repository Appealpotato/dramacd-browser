"""Folder -> tokuten import. No DLsite, no archive extraction; just walk a
folder of audio + images on disk and register it as a tokuten with its tracks
and cover. Mirrors what the DLsite scanner+pipeline produce for drama CDs,
but the source is the user pointing at a directory."""
import logging
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite

import database as db
from config import ARCHIVE_EXTENSIONS, COVERS_DIR
from pipeline.extractor import AUDIO_EXTENSIONS

logger = logging.getLogger(__name__)


async def scan_tokuten_paths(scan_paths: list[str]) -> dict:
    """Walk the configured `tokuten_scan_paths` and register each top-level
    folder or supported archive as a stub tokuten + items row. Catalog-only —
    we don't unpack audio, the user can run the existing folder-scan-once
    flow per row if they want tracks indexed. Idempotent via the stable
    `TKS-<sha1>` product_code derived from the absolute path."""
    normalized: list[Path] = []
    seen = set()
    for raw in scan_paths or []:
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(candidate)

    missing = [str(p) for p in normalized if not p.exists()]
    existing = [p for p in normalized if p.exists()]

    discovered = 0
    created = 0
    per_path: list[dict] = []

    for root in existing:
        local_discovered = 0
        local_created = 0
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except (OSError, PermissionError):
            entries = []
        for entry in entries:
            is_folder = entry.is_dir()
            is_archive = (
                entry.is_file() and entry.suffix.lower() in ARCHIVE_EXTENSIONS
            )
            if not (is_folder or is_archive):
                continue
            title = entry.stem if is_archive else entry.name
            total_size = 0
            file_format: list[str] = []
            if is_archive:
                try:
                    total_size = entry.stat().st_size
                except OSError:
                    total_size = 0
                suf = entry.suffix.lower().lstrip(".")
                if suf:
                    file_format = [suf]
            _, was_created = await db.upsert_tokuten_from_scan(
                library_path=str(entry),
                title=title,
                is_archive=is_archive,
                total_size=total_size,
                file_format=file_format,
            )
            local_discovered += 1
            if was_created:
                local_created += 1
        per_path.append({
            "path": str(root),
            "discovered": local_discovered,
            "created": local_created,
        })
        discovered += local_discovered
        created += local_created

    return {
        "discovered": discovered,
        "created": created,
        "missing_paths": missing,
        "scanned_paths": [str(p) for p in existing],
        "per_path": per_path,
    }

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
COVER_NAME_HINTS = {"cover", "folder", "front", "jacket", "ジャケット"}
TRACKLIST_NAME_HINTS = {"tracklist", "track list", "トラックリスト", "曲目"}

# Track filename patterns, tried in order. Each capture group is
# (track_number, title). A 4th fallback uses the whole stem as title with
# track_index = position in sorted listing.
_TRACK_PATTERNS = [
    re.compile(r"^Track\s*0*(\d+)\s*[-\.\s_]+\s*(.+)$", re.IGNORECASE),
    re.compile(r"^0*(\d+)\s*[-–—]\s*(.+)$"),
    re.compile(r"^0*(\d+)\s*[\.\)_]\s*(.+)$"),
    re.compile(r"^0*(\d+)\s+(.+)$"),
]


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def parse_track_filename(stem: str) -> tuple[Optional[int], str]:
    """Returns (track_index, title) parsed from a filename stem (no ext).
    track_index is None if no leading number could be extracted."""
    cleaned = stem.strip()
    for pat in _TRACK_PATTERNS:
        m = pat.match(cleaned)
        if m:
            try:
                return int(m.group(1)), m.group(2).strip()
            except (ValueError, IndexError):
                continue
    return None, cleaned


def _classify_image(path: Path) -> str:
    """Returns 'cover', 'tracklist', or 'gallery'."""
    name = path.stem.lower()
    if any(h in name for h in COVER_NAME_HINTS):
        return "cover"
    if any(h in name for h in TRACKLIST_NAME_HINTS):
        return "tracklist"
    return "gallery"


def scan_folder(folder: Path) -> dict:
    """Walks folder once. Returns:
        {
          'audio': [{'path': Path, 'track_index': int, 'title': str}],
          'cover': Path | None,
          'tracklist': Path | None,
          'gallery': [Path],
        }
    Audio files are sorted by track_index when parseable, else by filename.
    """
    if not folder.exists() or not folder.is_dir():
        raise FileNotFoundError(f"Folder not found: {folder}")

    audio_raw: list[tuple[Optional[int], str, Path]] = []
    cover: Optional[Path] = None
    tracklist: Optional[Path] = None
    gallery: list[Path] = []

    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file():
            continue
        ext = entry.suffix.lower()
        if ext in AUDIO_EXTENSIONS:
            track_index, title = parse_track_filename(entry.stem)
            audio_raw.append((track_index, title, entry))
        elif ext in IMAGE_EXTENSIONS:
            role = _classify_image(entry)
            if role == "cover" and cover is None:
                cover = entry
            elif role == "tracklist" and tracklist is None:
                tracklist = entry
            else:
                gallery.append(entry)

    # Renumber any missing track indices using positional order.
    fallback = 1
    for tup in audio_raw:
        if tup[0] is None:
            continue
        fallback = max(fallback, tup[0] + 1)
    audio: list[dict] = []
    for idx, (parsed_idx, title, path) in enumerate(audio_raw, start=1):
        track_index = parsed_idx if parsed_idx is not None else (fallback + idx)
        audio.append({"path": path, "track_index": track_index, "title": title})
    audio.sort(key=lambda t: (t["track_index"], t["path"].name.lower()))

    return {
        "audio": audio,
        "cover": cover,
        "tracklist": tracklist,
        "gallery": gallery,
    }


def _copy_cover(src: Path, tokuten_id: int) -> str:
    """Copy a cover image into COVERS_DIR/tokutens/<id>.<ext>. Returns the
    path relative to COVERS_DIR (matches how items.cover_local is shaped)."""
    dest_dir = COVERS_DIR / "tokutens"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{tokuten_id}{src.suffix.lower()}"
    shutil.copy2(str(src), str(dest))
    return str(dest.relative_to(COVERS_DIR)).replace("\\", "/")


def _copy_gallery(src: Path, tokuten_id: int) -> str:
    dest_dir = COVERS_DIR / "tokutens" / str(tokuten_id) / "gallery"
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Avoid overwriting if two files share a name
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.stem}-{uuid.uuid4().hex[:6]}{src.suffix}"
    shutil.copy2(str(src), str(dest))
    return str(dest.relative_to(COVERS_DIR)).replace("\\", "/")


async def register_tokuten_from_folder(
    folder_path: str,
    title: str,
    title_en: Optional[str] = None,
    kind: str = "audio",
    shop: str = "other",
    shop_other_name: Optional[str] = None,
    release_date: Optional[str] = None,
    notes: str = "",
    source_url: Optional[str] = None,
) -> dict:
    """Creates the tokuten row, copies cover + gallery into COVERS_DIR, and
    if any audio files are found also creates the items row (with synthetic
    product_code) and pipeline_tracks rows. Returns a summary dict with
    tokuten_id, item_id (or None for non-audio kinds), and counts."""
    folder = Path(folder_path)
    scan = scan_folder(folder)

    has_audio = bool(scan["audio"]) and kind == "audio"
    now = _now_iso()

    conn = await db.get_db()
    try:
        cur = await conn.execute(
            """INSERT INTO tokutens (kind, title, title_en, shop, shop_other_name,
                                     release_date, notes, source_url, local_path,
                                     created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (kind, title, title_en, shop, shop_other_name,
             release_date, notes or "", source_url, str(folder),
             now, now),
        )
        tokuten_id = cur.lastrowid

        cover_local = None
        if scan["cover"] is not None:
            cover_local = _copy_cover(scan["cover"], tokuten_id)
            await conn.execute(
                "UPDATE tokutens SET cover_local = ?, updated_at = ? WHERE id = ?",
                (cover_local, now, tokuten_id),
            )
            await conn.execute(
                """INSERT INTO media_assets (parent_kind, parent_id, path, role,
                                              sort_order, created_at)
                   VALUES ('tokuten', ?, ?, 'cover', 0, ?)""",
                (tokuten_id, cover_local, now),
            )
        if scan["tracklist"] is not None:
            asset_path = _copy_gallery(scan["tracklist"], tokuten_id)
            await conn.execute(
                """INSERT INTO media_assets (parent_kind, parent_id, path, role,
                                              sort_order, created_at)
                   VALUES ('tokuten', ?, ?, 'tracklist', 1, ?)""",
                (tokuten_id, asset_path, now),
            )
        for sort_idx, gallery_src in enumerate(scan["gallery"], start=2):
            asset_path = _copy_gallery(gallery_src, tokuten_id)
            await conn.execute(
                """INSERT INTO media_assets (parent_kind, parent_id, path, role,
                                              sort_order, created_at)
                   VALUES ('tokuten', ?, ?, 'gallery', ?, ?)""",
                (tokuten_id, asset_path, sort_idx, now),
            )

        item_id: Optional[int] = None
        if has_audio:
            synthetic_code = f"TKT-{uuid.uuid4().hex[:12].upper()}"
            file_format_json = _format_summary(scan["audio"])
            cur = await conn.execute(
                """INSERT INTO items (
                       product_code, title, title_en, kind, tokuten_id,
                       cover_local, file_format, files,
                       file_count, total_size, confidence,
                       scan_date, created_at, updated_at
                   ) VALUES (?, ?, ?, 'tokuten_audio', ?, ?, ?, ?, ?, ?, 'verified', ?, ?, ?)""",
                (
                    synthetic_code,
                    title,
                    title_en,
                    tokuten_id,
                    cover_local,
                    file_format_json,
                    _files_payload(folder, scan["audio"]),
                    len(scan["audio"]),
                    sum(t["path"].stat().st_size for t in scan["audio"]),
                    now, now, now,
                ),
            )
            item_id = cur.lastrowid

            for track in scan["audio"]:
                await conn.execute(
                    """INSERT INTO pipeline_tracks (
                           item_id, archive_path, extract_root, track_path,
                           track_index, title, status, created_at, updated_at
                       ) VALUES (?, NULL, ?, ?, ?, ?, 'indexed', ?, ?)""",
                    (
                        item_id,
                        str(folder),
                        str(track["path"]),
                        track["track_index"],
                        track["title"],
                        now, now,
                    ),
                )

        await conn.commit()
    finally:
        await conn.close()

    return {
        "tokuten_id": tokuten_id,
        "item_id": item_id,
        "audio_count": len(scan["audio"]) if has_audio else 0,
        "cover_imported": cover_local is not None,
        "gallery_count": len(scan["gallery"]) + (1 if scan["tracklist"] else 0),
    }


def _format_summary(audio: list[dict]) -> str:
    """JSON list of unique uppercase extensions, matching items.file_format."""
    import json as _json
    seen: list[str] = []
    for t in audio:
        ext = t["path"].suffix.lstrip(".").upper()
        if ext and ext not in seen:
            seen.append(ext)
    return _json.dumps(seen, ensure_ascii=False)


def _files_payload(folder: Path, audio: list[dict]) -> str:
    """Mimic items.files shape: list of {filename, path, size}."""
    import json as _json
    payload = []
    for t in audio:
        try:
            size = t["path"].stat().st_size
        except OSError:
            size = 0
        payload.append({
            "filename": t["path"].name,
            "path": str(t["path"]),
            "size": size,
        })
    return _json.dumps(payload, ensure_ascii=False)
