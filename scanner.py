import re
import time
from collections import defaultdict
from pathlib import Path

from config import ARCHIVE_EXTENSIONS, SCAN_PATH

# Regex patterns for extracting product codes from filenames
# Order matters: more specific patterns first
CODE_PATTERNS = [
    # Standard DLsite codes: RJ01234567, BJ01234567, VJ01234567
    re.compile(r"(RJ|BJ|VJ)(\d{6,8})", re.IGNORECASE),
    # DLJ-01234567, DLB-01234567, DMJ-01234567 (download tool naming)
    re.compile(r"(DLJ|DLB|DMJ)-(\d{6,8})", re.IGNORECASE),
    # RE-ESC-1234567 pattern
    re.compile(r"RE-ESC-(\d{6,8})", re.IGNORECASE),
    # vst####### pattern
    re.compile(r"(vst)(\d{6,8})", re.IGNORECASE),
]

# Audio format detection from filename
FORMAT_PATTERN = re.compile(r"-(mp3|wav|flac|aac)", re.IGNORECASE)

# Multi-part archive detection
PART_PATTERN = re.compile(r"\.part\d+\.", re.IGNORECASE)


def extract_product_code(filename: str) -> tuple[str | None, str | None, str]:
    """Extract and normalize a product code from a filename.

    Returns (normalized_code, original_code, confidence) or (None, None, "none").
    """
    for pattern in CODE_PATTERNS:
        match = pattern.search(filename)
        if not match:
            continue

        groups = match.groups()

        if len(groups) == 1:
            number = groups[0]
            original = f"RE-ESC-{number}"
            return f"RJ{number}", original, "low"

        prefix, number = groups[0].upper(), groups[1]

        if prefix in ("RJ", "BJ", "VJ"):
            return f"{prefix}{number}", f"{prefix}{number}", "high"
        if prefix == "DLJ":
            return f"RJ{number}", f"DLJ-{number}", "low"
        if prefix == "DLB":
            return f"BJ{number}", f"DLB-{number}", "low"
        if prefix == "DMJ":
            return f"RJ{number}", f"DMJ-{number}", "low"
        if prefix == "VST":
            return f"RJ{number}", f"vst{number}", "low"

    return None, None, "none"


def extract_audio_format(filename: str) -> str | None:
    """Extract audio format indicator from filename."""
    match = FORMAT_PATTERN.search(filename)
    if match:
        return match.group(1).upper()
    if "-all." in filename.lower() or "-all.part" in filename.lower():
        return "ALL"
    return None


def is_part_archive(filename: str) -> bool:
    return bool(PART_PATTERN.search(filename))


def get_part_number(filename: str) -> int | None:
    match = re.search(r"\.part(\d+)\.", filename, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _iter_archive_files(paths: list[Path], recursive: bool):
    for root in paths:
        if recursive:
            iterator = (p for p in root.rglob("*") if p.is_file())
        else:
            iterator = (p for p in root.iterdir() if p.is_file())

        for entry in iterator:
            if entry.suffix.lower() in ARCHIVE_EXTENSIONS:
                yield entry


def _normalize_paths(scan_path: str | None = None, scan_paths: list[str] | None = None) -> list[Path]:
    raw_paths = []
    if scan_paths:
        raw_paths.extend(scan_paths)
    elif scan_path:
        raw_paths.append(scan_path)
    else:
        raw_paths.append(SCAN_PATH)

    normalized = []
    seen = set()
    for raw in raw_paths:
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(candidate)
    return normalized


def scan_folder_with_progress(
    scan_path: str | None = None,
    scan_paths: list[str] | None = None,
    recursive: bool = True,
    on_progress=None,
    pause_event=None,
    stop_event=None,
) -> dict:
    """Scan one or more folders with optional progress callbacks and pause/stop control."""
    folders = _normalize_paths(scan_path=scan_path, scan_paths=scan_paths)
    if not folders:
        raise FileNotFoundError("No scan paths configured")

    missing_paths = [str(folder) for folder in folders if not folder.exists()]
    valid_folders = [folder for folder in folders if folder.exists() and folder.is_dir()]

    if not valid_folders:
        raise FileNotFoundError(f"Scan paths do not exist: {', '.join(missing_paths)}")

    entries = list(_iter_archive_files(valid_folders, recursive=recursive))
    total_files = len(entries)

    items = defaultdict(
        lambda: {
            "product_code": None,
            "original_code": None,
            "confidence": "low",
            "files": [],
            "formats": set(),
            "total_size": 0,
            "file_count": 0,
        }
    )
    unmatched = []
    processed_files = 0
    stopped = False

    for entry in entries:
        if stop_event is not None and stop_event.is_set():
            stopped = True
            break

        while pause_event is not None and pause_event.is_set():
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break
            time.sleep(0.2)

        if stopped:
            break

        processed_files += 1

        filename = entry.name
        file_size = entry.stat().st_size

        code, original, confidence = extract_product_code(filename)
        if code is None:
            unmatched.append({"filename": filename, "filepath": str(entry), "size": file_size})
        else:
            item = items[code]
            item["product_code"] = code
            if item["original_code"] is None:
                item["original_code"] = original
            if confidence == "high":
                item["confidence"] = "high"
            item["files"].append(filename)
            item["total_size"] += file_size
            item["file_count"] += 1

            fmt = extract_audio_format(filename)
            if fmt:
                item["formats"].add(fmt)

        if on_progress:
            on_progress(
                {
                    "processed_files": processed_files,
                    "total_files": total_files,
                    "current": filename,
                    "matched": processed_files - len(unmatched),
                    "unmatched": len(unmatched),
                }
            )

    for item in items.values():
        item["files"] = sorted(item["files"])
        item["formats"] = sorted(item["formats"])

    return {
        "items": dict(items),
        "unmatched": unmatched,
        "stats": {
            "total_files": total_files,
            "processed_files": processed_files,
            "matched": processed_files - len(unmatched),
            "unmatched": len(unmatched),
            "unique_codes": len(items),
            "recursive": recursive,
            "scanned_paths": [str(folder) for folder in valid_folders],
            "missing_paths": missing_paths,
            "stopped": stopped,
        },
    }


def scan_folder(scan_path: str | None = None, scan_paths: list[str] | None = None, recursive: bool = True) -> dict:
    return scan_folder_with_progress(scan_path=scan_path, scan_paths=scan_paths, recursive=recursive)
