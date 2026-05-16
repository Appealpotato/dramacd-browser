import json
import logging
import os
import re
import shutil
import subprocess
import wave
import zipfile
from datetime import datetime
from pathlib import Path

import database as db
from config import ARCHIVE_EXTENSIONS, PIPELINE_EXTRACT_DIR
from scanner import extract_product_code

logger = logging.getLogger(__name__)


# Modern multi-volume RAR style: ``name.part1.rar`` / ``name.part2.rar``…
# Only ``.partN.rar`` matters because ARCHIVE_EXTENSIONS limits scanning to
# .zip / .rar / .7z — old-style .r00/.r01 continuation files and .7z.001
# splits are never picked up by the scanner in the first place.
_PART_RAR_RE = re.compile(r"\.part(\d+)\.rar$", re.IGNORECASE)


def _multivolume_series_key(path: Path) -> tuple[str, int | None]:
    """Return ``(series_key, part_number)``. If ``path`` is a multi-volume
    RAR part the key collapses every sibling part of the same series to a
    common bucket, so callers can dedupe to the lowest part. Standalone
    archives get a unique key (themselves) and ``part_number=None`` so they
    pass through untouched."""
    m = _PART_RAR_RE.search(path.name)
    if not m:
        return (str(path).lower(), None)
    base = path.name[: m.start()]
    return (str(path.parent / base).lower(), int(m.group(1)))

def try_recover_mojibake(s: str) -> str | None:
    """Detect and recover CP437-presented CP932 mojibake.

    Many old Japanese archives store filenames as raw CP932 bytes; when those
    bytes get interpreted as CP437 (zipfile's default) the result is mojibake
    like ``âiâCâg`` (which is ``ナイト``). The fix is a single round-trip:
    encode as CP437 to get the original bytes back, then decode as CP932.

    Returns the recovered string, or None if ``s`` doesn't look like mojibake
    or recovery wouldn't materially improve it. ``errors='ignore'`` is used
    on the lossy fallback so trailing half-characters (a frequent corruption
    in malformed archives) get dropped instead of leaving ``\\ufffd``."""
    if not s or s.isascii():
        return None
    try:
        raw = s.encode("cp437")
    except UnicodeEncodeError:
        return None
    try:
        recovered = raw.decode("cp932")
    except UnicodeDecodeError:
        try:
            recovered = raw.decode("cp932", errors="ignore")
        except Exception:
            return None
    if not recovered or recovered == s:
        return None

    # Heuristic: accept the recovery if it has actual CJK content OR if it
    # eliminated a meaningful number of high-byte mojibake characters
    # (covers segments like ``memorandum of theirü`` where the only mojibake
    # was a single corrupted trailing byte).
    has_cjk = any(
        "぀" <= c <= "ヿ" or "一" <= c <= "鿿" or "＀" <= c <= "￯"
        for c in recovered
    )

    def _high_byte_count(t: str) -> int:
        return sum(1 for c in t if 0x80 <= ord(c) <= 0xFFFF and not (
            "぀" <= c <= "ヿ" or "一" <= c <= "鿿" or "＀" <= c <= "￯"
        ))

    if has_cjk:
        return recovered
    if _high_byte_count(recovered) < _high_byte_count(s):
        return recovered  # recovery eliminated some mojibake bytes
    return None


AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".flac",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".wma",
}


def get_runtime_archive_support() -> list[str]:
    support = ["zip"]
    if _find_binary(["7z", "7za"], env_var="DRAMACD_7Z_PATH"):
        support.extend(["rar", "7z"])
    return support


def _find_binary(candidates: list[str], env_var: str | None = None) -> str | None:
    if env_var:
        env_path = os.environ.get(env_var, "").strip()
        if env_path and Path(env_path).exists():
            return env_path
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    first = candidates[0].lower()
    if first in {"7z", "7za"}:
        common_paths = [
            Path(r"C:\Program Files\7-Zip\7z.exe"),
            Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
        ]
    elif first == "ffprobe":
        common_paths = [
            Path(r"C:\ffmpeg\bin\ffprobe.exe"),
            Path(r"C:\Program Files\ffmpeg\bin\ffprobe.exe"),
        ]
    elif first == "ffmpeg":
        common_paths = [
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
        ]
    else:
        common_paths = []
    for path in common_paths:
        if path.exists():
            return str(path)
    return None


def _parse_json_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    try:
        loaded = json.loads(value)
    except Exception:
        return []
    if not isinstance(loaded, list):
        return []
    return [str(v) for v in loaded if str(v).strip()]


def _decode_zip_filename(info: zipfile.ZipInfo) -> str:
    """Re-decode ZIP filename as CP932 (Shift-JIS) when UTF-8 flag is absent.

    Some Japanese archives are malformed (e.g. a lead byte followed by '/'
    instead of a proper trailing byte). Strict CP932 fails on those, so we
    fall back to lossy CP932 decoding before giving up — most of the name
    is then still readable, at the cost of one or two replacement chars."""
    if info.flag_bits & 0x800:
        return info.filename  # already UTF-8, trust zipfile's decode
    raw = info.filename.encode("cp437")  # undo zipfile's default cp437 decode
    try:
        return raw.decode("cp932")
    except (UnicodeDecodeError, ValueError):
        pass
    try:
        return raw.decode("cp932", errors="replace")
    except (UnicodeDecodeError, ValueError):
        return info.filename  # last resort: raw cp437 mojibake


def _safe_extract_zip(archive_path: Path, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist():
            member = Path(_decode_zip_filename(info))
            if member.is_absolute():
                continue
            candidate = (target_dir / member).resolve()
            if not str(candidate).startswith(str(target_dir.resolve())):
                continue
            if info.is_dir():
                candidate.mkdir(parents=True, exist_ok=True)
                continue
            candidate.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, candidate.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _extract_with_7z(archive_path: Path, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    exe = _find_binary(["7z", "7za"], env_var="DRAMACD_7Z_PATH")
    if not exe:
        raise RuntimeError("7z executable not found. Install 7-Zip or set DRAMACD_7Z_PATH.")
    result = subprocess.run(
        [exe, "x", "-y", f"-o{target_dir}", str(archive_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",  # Replace invalid chars instead of crashing
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        message = stderr or stdout or f"7z failed with exit code {result.returncode}"
        raise RuntimeError(message)


def _extract_archive(archive_path: Path, target_dir: Path):
    ext = archive_path.suffix.lower()
    if ext == ".zip":
        _safe_extract_zip(archive_path, target_dir)
        return
    if ext in {".rar", ".7z"}:
        _extract_with_7z(archive_path, target_dir)
        return
    raise RuntimeError(f"Unsupported archive extension: {ext}")


def _resolve_archives_for_item(item: dict, scan_paths: list[str]) -> list[Path]:
    wanted_names = set(_parse_json_list(item.get("files")))
    wanted_codes = {
        str(item.get("product_code") or "").strip().upper(),
        str(item.get("original_code") or "").strip().upper(),
    }
    wanted_codes.discard("")

    resolved: list[Path] = []
    seen = set()

    # Manual items point items.files at an absolute path on disk (the user
    # picked the file via the items detail edit panel). Anchor on absolute
    # entries directly so the scan_paths walk below is unnecessary for them.
    absolute_only_names: set[str] = set()
    for entry in list(wanted_names):
        try:
            p = Path(entry)
            if p.is_absolute() and p.exists() and p.is_file():
                key = str(p.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    resolved.append(p)
                absolute_only_names.add(entry)
        except Exception:
            pass
    wanted_names.difference_update(absolute_only_names)

    for raw_root in scan_paths:
        root = Path(raw_root).expanduser()
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in ARCHIVE_EXTENSIONS:
                continue

            include = False
            if wanted_names and path.name in wanted_names:
                include = True
            else:
                code, original, _confidence = extract_product_code(path.name)
                normalized = {
                    str(code or "").strip().upper(),
                    str(original or "").strip().upper(),
                }
                if any(code in wanted_codes for code in normalized if code):
                    include = True

            if include:
                key = str(path.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                resolved.append(path)

    # Collapse multi-volume RARs to just the first part. Extracting every
    # ``.partN.rar`` produces N sibling output trees full of cross-volume
    # fragments; pointing the extractor at ``.part1.rar`` alone lets RAR's
    # built-in volume continuation stream all parts into one tree.
    min_part_per_series: dict[str, int] = {}
    for p in resolved:
        series, part = _multivolume_series_key(p)
        if part is None:
            continue
        if part < min_part_per_series.get(series, part + 1):
            min_part_per_series[series] = part

    filtered: list[Path] = []
    dropped: list[Path] = []
    for p in resolved:
        series, part = _multivolume_series_key(p)
        if part is None or part == min_part_per_series.get(series):
            filtered.append(p)
        else:
            dropped.append(p)
    if dropped:
        logger.info(
            "Multi-volume RAR: keeping first part only, skipping %d continuation file(s): %s",
            len(dropped),
            ", ".join(p.name for p in dropped),
        )
    return filtered


def list_archive_contents(archive_path: Path) -> list[dict]:
    """Run ``7z l -slt`` to enumerate the files inside an archive (without
    extracting). Returns ``[{path, size}]`` sorted by path, directories
    filtered out. Used by the Workshop Archive panel's inline viewer."""
    exe = _find_binary(["7z", "7za"], env_var="DRAMACD_7Z_PATH")
    if not exe:
        raise RuntimeError("7z executable not found. Install 7-Zip or set DRAMACD_7Z_PATH.")
    # ``-sccUTF-8`` forces 7z to emit UTF-8 on stdout regardless of the system
    # console code page. Without it, Japanese filenames get decoded as CP932
    # (or whatever the local OEM code page is) and Python's UTF-8 reader turns
    # the bytes into replacement chars.
    result = subprocess.run(
        [exe, "l", "-slt", "-sccUTF-8", str(archive_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"7z list failed for {archive_path.name}: {result.stderr[:300]}")
    entries: list[dict] = []
    record: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            if record.get("Path"):
                entries.append(record)
            record = {}
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            record[key.strip()] = value.strip()
    if record.get("Path"):
        entries.append(record)

    # 7z's first record in -slt mode is a header describing the archive
    # itself. The Path value there is sometimes the bare filename and
    # sometimes the absolute disk path (depends on how 7z was invoked).
    # Filter on both forms so the archive's own entry never leaks into the
    # listing.
    archive_name = archive_path.name
    archive_abspath = str(archive_path)
    files: list[dict] = []
    for e in entries:
        path = e.get("Path", "").strip()
        if not path or path == archive_name or path == archive_abspath:
            continue
        attrs = e.get("Attributes", "")
        if "D" in attrs:  # directory entry
            continue
        try:
            size = int(e.get("Size", "0") or "0")
        except ValueError:
            size = 0
        files.append({"path": path, "size": size})
    files.sort(key=lambda f: f["path"].lower())
    return files


def stream_archive_file(archive_path: Path, inner_path: str) -> bytes:
    """Extract a single file from an archive to memory via ``7z e -so``.
    Used by the archive-thumb endpoint to pull just one image out of the
    source archive without unpacking the whole thing."""
    exe = _find_binary(["7z", "7za"], env_var="DRAMACD_7Z_PATH")
    if not exe:
        raise RuntimeError("7z executable not found. Install 7-Zip or set DRAMACD_7Z_PATH.")
    # ``-so`` streams to stdout; the file we want is named on the command line.
    # 7z matches the inner path against archive contents. Capture as bytes,
    # not text, so we don't corrupt the binary stream.
    result = subprocess.run(
        [exe, "e", "-so", "-sccUTF-8", str(archive_path), inner_path],
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"7z stream failed for {inner_path}: {stderr}")
    return result.stdout


def _probe_with_ffprobe(path: Path) -> dict:
    exe = _find_binary(["ffprobe"], env_var="DRAMACD_FFPROBE_PATH")
    if not exe:
        return {}
    result = subprocess.run(
        [
            exe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "stream=codec_name,sample_rate,channels:format=duration",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return {}
    try:
        payload = json.loads(result.stdout)
    except Exception:
        return {}
    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    audio_stream = streams[0] if streams else {}
    duration = fmt.get("duration")
    sample_rate = audio_stream.get("sample_rate")
    channels = audio_stream.get("channels")
    codec = audio_stream.get("codec_name")
    return {
        "duration_seconds": float(duration) if duration not in (None, "") else None,
        "sample_rate": int(sample_rate) if sample_rate not in (None, "") else None,
        "channels": int(channels) if channels not in (None, "") else None,
        "codec": codec or None,
    }


def _probe_wave(path: Path) -> dict:
    if path.suffix.lower() != ".wav":
        return {}
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            channels = wf.getnchannels()
            duration = float(frames) / float(rate) if rate else None
            return {
                "duration_seconds": duration,
                "sample_rate": int(rate) if rate else None,
                "channels": int(channels) if channels else None,
                "codec": "pcm_s16le",
            }
    except Exception:
        return {}


def _probe_audio_metadata(path: Path) -> tuple[dict, str | None]:
    ffprobe_meta = _probe_with_ffprobe(path)
    if ffprobe_meta:
        return ffprobe_meta, None
    wave_meta = _probe_wave(path)
    if wave_meta:
        return wave_meta, None
    return {
        "duration_seconds": None,
        "sample_rate": None,
        "channels": None,
        "codec": path.suffix.lower().lstrip(".") or None,
    }, "audio_probe_unavailable"


def _index_tracks_from_dir(item_id: int, extract_root: Path, archives: list[Path]) -> list[dict]:
    tracks = []
    archive_map = {p.name: str(p) for p in archives}
    for idx, path in enumerate(sorted(extract_root.rglob("*")), start=1):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        rel_parts = path.relative_to(extract_root).parts
        archive_name = rel_parts[0] if rel_parts else ""
        probe, probe_error = _probe_audio_metadata(path)
        tracks.append(
            {
                "item_id": item_id,
                "archive_path": archive_map.get(archive_name),
                "extract_root": str(extract_root),
                "track_path": str(path),
                "track_index": idx,
                "title": path.stem,
                "duration_seconds": probe.get("duration_seconds"),
                "codec": probe.get("codec") or path.suffix.lower().lstrip("."),
                "sample_rate": probe.get("sample_rate"),
                "channels": probe.get("channels"),
                "status": "indexed",
                "error": probe_error,
            }
        )
    return tracks


async def run_extraction_job(job_id: int):
    job = await db.get_job(job_id)
    if not job:
        return
    if job.get("job_type") != "pipeline_extract":
        return

    metadata = job.get("metadata_json") or {}
    item_id = int(metadata.get("item_id", 0))
    force = bool(metadata.get("force", False))
    if item_id <= 0:
        await db.update_job(job_id, status="failed", error="Missing item_id in job metadata")
        await db.append_job_event(job_id, "error", "Extraction failed", {"error": "missing_item_id"})
        return

    item = await db.get_item(item_id)
    if not item:
        await db.update_job(job_id, status="failed", error="Item not found")
        await db.append_job_event(job_id, "error", "Extraction failed", {"error": "item_not_found"})
        return

    await db.update_job(
        job_id,
        status="running",
        started_at=job.get("started_at") or datetime.now().isoformat(),
        total=0,
        completed=0,
        stopped=0,
    )
    await db.append_job_event(job_id, "info", "Extraction started", {"item_id": item_id, "force": force})

    code = str(item.get("product_code") or f"item{item_id}").strip().upper()
    extract_root = PIPELINE_EXTRACT_DIR / code

    if force and extract_root.exists():
        shutil.rmtree(extract_root, ignore_errors=True)
    extract_root.mkdir(parents=True, exist_ok=True)

    # Reuse existing extraction unless --force: skip re-extracting archives
    # that already produced an audio-bearing subfolder.
    has_existing_audio = any(
        p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
        for p in extract_root.rglob("*")
    )
    reused_existing = has_existing_audio and not force

    scan_paths = await db.get_scan_paths()
    archives = _resolve_archives_for_item(item, scan_paths)
    if not archives:
        await db.update_job(
            job_id,
            status="failed",
            error="No source archives resolved for item from scan paths",
            stopped=1,
            finished_at=datetime.now().isoformat(),
        )
        await db.append_job_event(job_id, "error", "Extraction failed", {"error": "source_archive_not_found"})
        return

    await db.update_job(job_id, total=len(archives), completed=0, current=archives[0].name)

    completed = 0
    failures = []
    if reused_existing:
        # Treat all archives as already done; just re-index from disk.
        completed = len(archives)
        await db.append_job_event(
            job_id,
            "info",
            "Reusing existing extracted audio (no --force)",
            {"extract_root": str(extract_root)},
        )
        await db.update_job(job_id, completed=completed, current=None)
    else:
        for archive in archives:
            archive_target = extract_root / archive.name
            try:
                _extract_archive(archive, archive_target)
                completed += 1
                await db.update_job(job_id, completed=completed, current=archive.name)
            except Exception as exc:
                failures.append({"archive": str(archive), "error": str(exc)})
                await db.append_job_event(
                    job_id,
                    "error",
                    "Archive extraction failed",
                    {"archive": str(archive), "error": str(exc)},
                )

    tracks = _index_tracks_from_dir(item_id=item_id, extract_root=extract_root, archives=archives)
    track_count = await db.replace_pipeline_tracks_for_item(item_id, tracks)

    status = "completed" if completed > 0 else "failed"
    await db.update_job(
        job_id,
        status=status,
        stopped=1,
        total=len(archives),
        completed=completed,
        success=track_count,
        failed=len(failures),
        errors_json=failures,
        result_json={
            "item_id": item_id,
            "archives_total": len(archives),
            "archives_extracted": completed,
            "tracks_indexed": track_count,
            "extract_root": str(extract_root),
            "reused_existing": reused_existing,
        },
        finished_at=datetime.now().isoformat(),
        current=None,
    )
    await db.append_job_event(
        job_id,
        "info",
        "Extraction finished",
        {
            "item_id": item_id,
            "archives_total": len(archives),
            "archives_extracted": completed,
            "tracks_indexed": track_count,
            "failed_archives": len(failures),
        },
    )
