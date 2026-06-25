import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import wave
import zipfile
from datetime import datetime
from pathlib import Path

import database as db
from config import ARCHIVE_EXTENSIONS, PIPELINE_EXTRACT_DIR, PIPELINE_WORK_DIR
from scanner import extract_product_code

logger = logging.getLogger(__name__)


# Modern multi-volume RAR style: ``name.part1.rar`` / ``name.part2.rar``…
# Only ``.partN.rar`` matters because ARCHIVE_EXTENSIONS limits scanning to
# .zip / .rar / .7z / .tar — old-style .r00/.r01 continuation files and
# .7z.001 splits are never picked up by the scanner in the first place.
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
    support = ["zip", "tar"]
    if _find_binary(["7z", "7za", "7zz"], env_var="DRAMACD_7Z_PATH"):
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
    # Fall back to well-known install locations. This matters on macOS/Linux
    # too: a server launched from Finder/launchd inherits a minimal PATH that
    # often omits /opt/homebrew/bin, so shutil.which() can miss a brew-installed
    # binary that is in fact present.
    first = candidates[0].lower()
    if first in {"7z", "7za", "7zz"}:
        common_paths = [
            Path(r"C:\Program Files\7-Zip\7z.exe"),
            Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
            Path("/opt/homebrew/bin/7z"), Path("/opt/homebrew/bin/7zz"),
            Path("/usr/local/bin/7z"), Path("/usr/local/bin/7zz"),
            Path("/usr/bin/7z"), Path("/usr/bin/7zz"),
        ]
    elif first == "ffprobe":
        common_paths = [
            Path(r"C:\ffmpeg\bin\ffprobe.exe"),
            Path(r"C:\Program Files\ffmpeg\bin\ffprobe.exe"),
            Path("/opt/homebrew/bin/ffprobe"),
            Path("/usr/local/bin/ffprobe"),
            Path("/usr/bin/ffprobe"),
        ]
    elif first == "ffmpeg":
        common_paths = [
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
            Path("/opt/homebrew/bin/ffmpeg"),
            Path("/usr/local/bin/ffmpeg"),
            Path("/usr/bin/ffmpeg"),
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


def _safe_extract_tar(archive_path: Path, target_dir: Path):
    """Extract a .tar (or auto-detected tar.gz/bz2/xz) with path-traversal
    guards. Skips symlinks, hardlinks, and device entries — drama-CD tars
    only ever contain regular files and directories, and refusing the
    rest avoids the classic tar-slip footguns."""
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved_root = str(target_dir.resolve())
    with tarfile.open(archive_path, "r:*") as tf:
        for member in tf.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute():
                continue
            candidate = (target_dir / member_path).resolve()
            if not str(candidate).startswith(resolved_root):
                continue
            if member.isdir():
                candidate.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            candidate.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, candidate.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _extract_with_7z(archive_path: Path, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    exe = _find_binary(["7z", "7za", "7zz"], env_var="DRAMACD_7Z_PATH")
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
    if ext == ".tar":
        _safe_extract_tar(archive_path, target_dir)
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

    scannable = ARCHIVE_EXTENSIONS | AUDIO_EXTENSIONS
    for raw_root in scan_paths:
        root = Path(raw_root).expanduser()
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in scannable:
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


def _list_tar_contents(archive_path: Path) -> list[dict]:
    """tarfile-based equivalent of the 7z listing path. Mirrors the
    ``[{path, size}]`` shape returned by ``list_archive_contents`` so the
    caller doesn't care which backend produced it."""
    files: list[dict] = []
    with tarfile.open(archive_path, "r:*") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            files.append({"path": member.name, "size": int(member.size or 0)})
    return files


def _list_with_7z(archive_path: Path) -> list[dict]:
    """Raw ``7z l -slt`` listing for .zip/.rar/.7z. Returns ``[{path, size}]``
    with directories and the archive's own self-record filtered out — but
    *not* sorted or __MACOSX-filtered (those happen in the caller)."""
    exe = _find_binary(["7z", "7za", "7zz"], env_var="DRAMACD_7Z_PATH")
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
    return files


def _find_single_inner_tar(files: list[dict]) -> str | None:
    """If a wrapper archive contains exactly one top-level ``.tar`` file
    (and nothing else), return that tar's in-archive name. This is the
    common DLsite layout where the work is packed as ``RJ123.tar`` inside
    a ``RJ123.7z`` for compression — the user wants to see the tar's
    contents, not the useless single-entry wrapper listing."""
    if len(files) != 1:
        return None
    only = files[0]["path"]
    if not only.lower().endswith(".tar"):
        return None
    # Reject anything nested in a subdirectory — only top-level wrappers
    # qualify. A ``data/foo.tar`` arrangement could legitimately sit
    # alongside other content the user wants to see.
    if "/" in only or "\\" in only:
        return None
    return only


@contextlib.contextmanager
def _open_nested_tar(outer: Path, inner_tar_name: str):
    """Yield a streaming ``tarfile.TarFile`` reading the nested tar named
    ``inner_tar_name`` inside the wrapper archive ``outer``.

    Uses ``zipfile.open`` for zip wrappers (no external dep) and
    ``7z e -so`` piping for rar/7z. Stream mode (``r|*``) means callers
    can only iterate members forward — they can't seek back."""
    outer_ext = outer.suffix.lower()
    if outer_ext == ".zip":
        zf = zipfile.ZipFile(outer, "r")
        try:
            target = next(
                (info for info in zf.infolist() if _decode_zip_filename(info) == inner_tar_name),
                None,
            )
            if target is None:
                raise RuntimeError(f"Nested tar {inner_tar_name} not found in {outer.name}")
            inner = zf.open(target, "r")
            try:
                with tarfile.open(fileobj=inner, mode="r|*") as tf:
                    yield tf
            finally:
                inner.close()
        finally:
            zf.close()
        return

    exe = _find_binary(["7z", "7za", "7zz"], env_var="DRAMACD_7Z_PATH")
    if not exe:
        raise RuntimeError("7z executable not found. Install 7-Zip or set DRAMACD_7Z_PATH.")
    proc = subprocess.Popen(
        [exe, "e", "-so", "-sccUTF-8", str(outer), inner_tar_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|*") as tf:
            yield tf
    finally:
        # When callers break early (e.g. found their member) 7z will block
        # writing to the pipe; terminate it so we don't leak the process.
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        try:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass


def _list_inner_tar(outer: Path, inner_tar_name: str) -> list[dict]:
    files: list[dict] = []
    with _open_nested_tar(outer, inner_tar_name) as tf:
        for member in tf:
            if not member.isfile():
                continue
            files.append({"path": member.name, "size": int(member.size or 0)})
    return files


_LISTING_CACHE_VERSION = 1


def _archive_listing_cache_path(archive_path: Path) -> Path:
    try:
        key_input = str(archive_path.resolve())
    except OSError:
        key_input = str(archive_path)
    key = hashlib.sha1(key_input.encode("utf-8")).hexdigest()
    return PIPELINE_WORK_DIR / "archive-listings" / f"{key}.json"


def _read_cached_listing(archive_path: Path) -> dict | None:
    """Return the cached listing dict for ``archive_path`` if it's still
    valid (same size + mtime as the file on disk). Otherwise ``None``."""
    cache = _archive_listing_cache_path(archive_path)
    if not cache.exists():
        return None
    try:
        st = archive_path.stat()
        meta = json.loads(cache.read_text("utf-8"))
    except Exception:
        return None
    if meta.get("v") != _LISTING_CACHE_VERSION:
        return None
    if meta.get("size") != st.st_size:
        return None
    if int(meta.get("mtime", 0)) != int(st.st_mtime):
        return None
    return meta


def _write_cached_listing(
    archive_path: Path, files: list[dict], *, wrapper_tar: str | None
) -> None:
    cache = _archive_listing_cache_path(archive_path)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        st = archive_path.stat()
        cache.write_text(
            json.dumps({
                "v": _LISTING_CACHE_VERSION,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "wrapper_tar": wrapper_tar,
                "files": files,
            }, ensure_ascii=False),
            "utf-8",
        )
    except Exception as exc:
        logger.warning("Failed to write listing cache for %s: %s", archive_path.name, exc)


def _filter_macosx(files: list[dict]) -> list[dict]:
    """Hide the AppleDouble resource-fork entries macOS zipping leaves in
    ``__MACOSX/`` — they're never useful content, just ``._`` duplicates of
    the real files. Keep them only if the archive contains nothing else,
    so a __MACOSX-only archive still shows something instead of looking empty."""
    def _under_macosx(p: str) -> bool:
        return "__MACOSX" in p.replace("\\", "/").split("/")
    non_macosx = [f for f in files if not _under_macosx(f["path"])]
    return non_macosx if non_macosx else files


def list_archive_contents(archive_path: Path) -> list[dict]:
    """Enumerate the files inside an archive without extracting. Returns
    ``[{path, size}]`` sorted by path, directories filtered out. Used by
    the Workshop Archive panel's inline viewer.

    ``.tar`` is handled in-process via :mod:`tarfile`. Other archives go
    through ``7z l -slt`` — with a transparent unwrap step for the common
    DLsite layout of a single ``.tar`` packed inside ``.zip/.rar/.7z``."""
    ext = archive_path.suffix.lower()

    if ext == ".tar":
        files = _list_tar_contents(archive_path)
        files.sort(key=lambda f: f["path"].lower())
        return _filter_macosx(files)

    cached = _read_cached_listing(archive_path)
    if cached is not None:
        return cached.get("files") or []

    outer_files = _list_with_7z(archive_path)
    wrapper_tar = _find_single_inner_tar(outer_files)
    if wrapper_tar is not None:
        try:
            files = _list_inner_tar(archive_path, wrapper_tar)
        except Exception as exc:
            logger.warning(
                "Failed to drill into nested tar %s in %s: %s",
                wrapper_tar, archive_path.name, exc,
            )
            files = outer_files
            wrapper_tar = None
    else:
        files = outer_files

    files.sort(key=lambda f: f["path"].lower())
    files = _filter_macosx(files)

    if wrapper_tar is not None:
        # Cache only the expensive case: streaming a tar through a 7z/rar
        # wrapper decompresses the whole thing. Plain top-level listings
        # via ``7z l`` are already fast and don't need disk caching.
        _write_cached_listing(archive_path, files, wrapper_tar=wrapper_tar)

    return files


def _stream_member_from_nested_tar(
    archive_path: Path, wrapper_tar: str, inner_path: str
) -> bytes:
    """Pull a single member out of a tar wrapped inside ``archive_path``.
    Streams via :func:`_open_nested_tar` so the 1.9 GB decompression happens
    only over the bytes leading up to the target member."""
    normalized = inner_path.replace("\\", "/").lstrip("/")
    target_base = Path(normalized).name
    with _open_nested_tar(archive_path, wrapper_tar) as tf:
        for member in tf:
            if not member.isfile():
                continue
            if member.name == normalized or Path(member.name).name == target_base:
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src:
                    return src.read()
    raise RuntimeError(
        f"Member {inner_path} not found inside nested tar {wrapper_tar}"
    )


def stream_archive_file(archive_path: Path, inner_path: str) -> bytes:
    """Extract a single file from an archive to memory. Used by the
    archive-thumb endpoint to pull one image out without unpacking the
    whole archive.

    ``.tar`` reads in-process via :mod:`tarfile`. ``.zip/.rar/.7z`` go
    through ``7z e -so`` — unless the wrapper just contains a single
    nested ``.tar`` (common DLsite layout), in which case the requested
    member is streamed out of that nested tar instead."""
    ext = archive_path.suffix.lower()
    if ext == ".tar":
        normalized = inner_path.replace("\\", "/").lstrip("/")
        with tarfile.open(archive_path, "r:*") as tf:
            try:
                member = tf.getmember(normalized)
            except KeyError:
                # Fall back to a basename match — 7z accepts a bare
                # filename and the thumb endpoint sometimes passes one.
                target_base = Path(normalized).name
                member = next(
                    (m for m in tf.getmembers() if m.isfile() and Path(m.name).name == target_base),
                    None,
                )
                if member is None:
                    raise RuntimeError(f"tar stream failed for {inner_path}: member not found")
            if not member.isfile():
                raise RuntimeError(f"tar stream failed for {inner_path}: not a regular file")
            src = tf.extractfile(member)
            if src is None:
                raise RuntimeError(f"tar stream failed for {inner_path}: unreadable")
            with src:
                return src.read()

    cached = _read_cached_listing(archive_path)
    wrapper_tar = cached.get("wrapper_tar") if cached else None
    if wrapper_tar is None and cached is None:
        # No cache yet — do a cheap top-level listing to detect a wrapper.
        # ``7z l`` reads the central directory only, so this is fast even
        # for multi-GB archives.
        try:
            wrapper_tar = _find_single_inner_tar(_list_with_7z(archive_path))
        except Exception:
            wrapper_tar = None

    if wrapper_tar:
        return _stream_member_from_nested_tar(archive_path, wrapper_tar, inner_path)

    exe = _find_binary(["7z", "7za", "7zz"], env_var="DRAMACD_7Z_PATH")
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
    archive_map = {p.name: str(p) for p in archives}

    # macOS zipping drops AppleDouble resource forks into __MACOSX/, so every
    # real track gets a phantom "._track" sibling that would index as a
    # duplicate. Drop __MACOSX entries — but only when there's real audio
    # elsewhere, so an archive that somehow contains __MACOSX-only audio still
    # produces tracks instead of going empty.
    all_audio = [
        p for p in sorted(extract_root.rglob("*"))
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    non_macosx = [
        p for p in all_audio
        if "__MACOSX" not in p.relative_to(extract_root).parts
    ]
    audio_paths = non_macosx if non_macosx else all_audio

    tracks = []
    for idx, path in enumerate(audio_paths, start=1):
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


def _index_loose_tracks(item_id: int, audio_files: list[Path], start_index: int = 1) -> list[dict]:
    """Index already-extracted loose audio IN PLACE (no copy into the pipeline
    workspace), for a folder the user dropped in pre-unzipped. ``track_path``
    points at the original file and ``extract_root`` at its real folder, so the
    audio is never duplicated on disk. Both deletion paths (item delete +
    purge-workspace) only remove dirs inside PIPELINE_EXTRACT_DIR, so the user's
    source files are safe from those operations."""
    # Same AppleDouble filtering as _index_tracks_from_dir: a folder unzipped
    # on/from a Mac carries __MACOSX/ and ._* resource-fork phantoms with audio
    # suffixes that would index as broken duplicate tracks. Keep the fallback
    # so a (pathological) junk-only folder still indexes rather than going empty.
    candidates = sorted(audio_files)
    non_junk = [
        p for p in candidates
        if "__MACOSX" not in p.parts and not p.name.startswith("._")
    ]
    audio_files = non_junk if non_junk else candidates
    tracks = []
    for idx, path in enumerate(audio_files, start=start_index):
        probe, probe_error = _probe_audio_metadata(path)
        tracks.append(
            {
                "item_id": item_id,
                "archive_path": None,
                "extract_root": str(path.parent),
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

    scan_paths = await db.get_scan_paths()
    archives = _resolve_archives_for_item(item, scan_paths)
    if not archives:
        await db.update_job(
            job_id,
            status="failed",
            error="No source files resolved for item from scan paths",
            stopped=1,
            finished_at=datetime.now().isoformat(),
        )
        await db.append_job_event(job_id, "error", "Extraction failed", {"error": "source_files_not_found"})
        return

    # Already-extracted loose audio (a folder the user dropped in pre-unzipped)
    # is indexed IN PLACE — never copied into the workspace, so it isn't
    # duplicated on disk. Only real archives go through extraction.
    loose_audio = [a for a in archives if a.suffix.lower() in AUDIO_EXTENSIONS]
    real_archives = [a for a in archives if a.suffix.lower() not in AUDIO_EXTENSIONS]
    if real_archives and loose_audio:
        # An item with a real archive treats the archive as canonical. Stray
        # loose audio matching the same product code under the scan roots would
        # otherwise index in place ALONGSIDE the extracted tracks — a doubled
        # tracklist.
        await db.append_job_event(
            job_id, "info",
            f"Ignoring {len(loose_audio)} loose audio file(s) — item has real archive(s)",
            {"ignored": [str(a) for a in loose_audio[:10]]},
        )
        loose_audio = []
        archives = real_archives

    # A pure loose-audio item never touches the workspace — don't create (or
    # keep recreating) an empty PIPELINE_EXTRACT_DIR/<CODE> dir for it that
    # would only show up as an orphan.
    if real_archives:
        extract_root.mkdir(parents=True, exist_ok=True)

    # Reuse existing extraction unless --force: skip re-extracting archives
    # that already produced an audio-bearing subfolder.
    has_existing_audio = extract_root.exists() and any(
        p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
        for p in extract_root.rglob("*")
    )
    reused_existing = has_existing_audio and not force

    # If a source archive is NEWER than what we extracted, the archive changed since the
    # last run (e.g. the merged blob was replaced with proper split tracks). Reusing the
    # old extraction would keep the stale tracks forever, so invalidate the reuse and
    # re-extract from scratch - no --force needed.
    if reused_existing:
        try:
            # Compare against extracted DIRECTORY mtimes (= when we last extracted), NOT
            # file mtimes: 7z restores each file's archive-internal date, which is usually
            # OLDER than the .7z container, so file mtimes would trip a re-extract every
            # run. Directory mtimes are stamped at extraction time, so they're the right
            # "last extracted" signal.
            extracted_when = max(
                (p.stat().st_mtime for p in [extract_root, *extract_root.rglob("*")] if p.is_dir()),
                default=0.0,
            )
            # Only REAL archives count: a recently-touched loose audio file
            # isn't a changed archive and must not force a workspace re-extract.
            newest_archive = max(
                (a.stat().st_mtime for a in real_archives if a.exists()),
                default=0.0,
            )
            if newest_archive > extracted_when:
                reused_existing = False
                shutil.rmtree(extract_root, ignore_errors=True)
                extract_root.mkdir(parents=True, exist_ok=True)
                await db.append_job_event(
                    job_id,
                    "info",
                    "Source archive is newer than the extraction - re-extracting",
                    {"extract_root": str(extract_root)},
                )
        except Exception:
            pass

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
        for archive in real_archives:
            try:
                archive_target = extract_root / archive.name
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
        # Loose audio needs no extraction — already on disk, indexed in place.
        if loose_audio:
            completed += len(loose_audio)
            await db.update_job(job_id, completed=completed, current=None)

    tracks = (
        _index_tracks_from_dir(item_id=item_id, extract_root=extract_root, archives=real_archives)
        if extract_root.exists()
        else []
    )
    if loose_audio:
        tracks += _index_loose_tracks(item_id, loose_audio, start_index=len(tracks) + 1)
    track_count = await db.replace_pipeline_tracks_for_item(item_id, tracks)

    # If the release shipped .vtt/.srt scripts next to the audio, import them as transcript
    # runs so Whisper can be skipped. Fully guarded - must never affect extraction outcome.
    try:
        from pipeline.subtitle_import import import_bundled_subtitles
        sub_summary = await import_bundled_subtitles(item_id)
        if sub_summary.get("imported"):
            await db.append_job_event(
                job_id, "info", f"Imported {sub_summary['imported']} bundled subtitle(s)",
                {"item_id": item_id, **sub_summary},
            )
    except Exception as sub_err:  # pragma: no cover - defensive
        try:
            await db.append_job_event(
                job_id, "info", "Bundled-subtitle import skipped (error)",
                {"item_id": item_id, "error": str(sub_err)},
            )
        except Exception:
            pass

    # If the release shipped a producer-preprocessed <CODE>.package.json (split tracks +
    # per-track JA transcript + prefetched metadata), ingest it so we skip DLsite + Whisper.
    # Idempotent and fully guarded - must never affect extraction outcome.
    try:
        from pipeline.package_import import import_bundled_package
        pkg_summary = await import_bundled_package(item_id, extract_root)
        if pkg_summary.get("imported"):
            await db.append_job_event(
                job_id, "info",
                f"Imported bundled package ({pkg_summary['transcript_runs_created']} transcript run(s))",
                {"item_id": item_id, **pkg_summary},
            )
    except Exception as pkg_err:  # pragma: no cover - defensive
        try:
            await db.append_job_event(
                job_id, "info", "Bundled-package import skipped (error)",
                {"item_id": item_id, "error": str(pkg_err)},
            )
        except Exception:
            pass

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
