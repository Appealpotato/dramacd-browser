"""Games scanner.

Walks each configured `games_scan_paths` root and registers each top-level
folder or supported archive as a row in the `games` table. Catalog-only —
we never unpack generic archives or descend into folders past the top
level. A "game" is one folder OR one archive at the root of a configured
library path.

Idempotency: rows are upserted by `library_path` via
`db.upsert_game_from_scan()`. The default title is the folder / archive
stem; VNDB prefill replaces it with the real title.

Platform detection is best-effort: console-cartridge filenames (.nsp,
.xci, .cia, .3ds, .nds, .gba, .gb/.gbc) and obvious PC/Mac markers
(.exe, .app) are mapped to VNDB-compatible platform codes. Detected
platforms only fill the row when its platforms_json is currently empty —
so a later VNDB match (or the user's manual edit) is never overwritten.
"""
from __future__ import annotations

from pathlib import Path

from config import ARCHIVE_EXTENSIONS

# Maps a file extension to a VNDB platform code (used in the games.platforms
# JSON array and in the games-card platform pill). Codes mirror VNDB's enum
# values so a row populated by the scanner and a row populated by VNDB use
# the same vocabulary.
#
# Generic archive containers (.zip/.rar/.7z) intentionally don't map to a
# platform — they could hold anything, and guessing 'win' would mask the
# real platform when the user later runs VNDB match. Better to leave empty
# and let the match step fill it.
PLATFORM_BY_EXTENSION = {
    # Nintendo Switch — .nsp / .xci are the dump formats, .nsz / .xcz the
    # compressed variants (nstool / nsz).
    ".nsp": "swi",
    ".xci": "swi",
    ".nsz": "swi",
    ".xcz": "swi",
    # Nintendo 3DS
    ".cia": "n3d",
    ".3ds": "n3d",
    ".3dsx": "n3d",
    ".cci": "n3d",
    # Nintendo DS
    ".nds": "nds",
    # GBA / GBC / GB
    ".gba": "gba",
    ".gb": "gbc",
    ".gbc": "gbc",
    # PSP — .pbp / .cso are PSP-specific; .iso / .pkg are too ambiguous
    # (PS1/PS2/Gamecube/Wii/various) to auto-classify, leave manual.
    ".pbp": "psp",
    ".cso": "psp",
    # PS Vita — homebrew/install package format.
    ".vpk": "psv",
    # PC
    ".exe": "win",
    ".app": "mac",
}


_DETECT_MAX_DEPTH = 3  # game-folder → version-folder → optional disc-folder


def _walk_for_platforms(root: Path, found: set[str], depth: int) -> None:
    """Bounded recursive walk: descend up to `_DETECT_MAX_DEPTH` levels to
    pick up multi-disc / multi-version game layouts like
    `Hana Awase / Disc 1 / game.nsp`. Returns early as soon as we hit a
    platform-mapped file at any level — finding one Switch ROM is enough
    to label the parent folder."""
    if depth < 0:
        return
    try:
        children = list(root.iterdir())
    except (OSError, PermissionError):
        return
    # Files first — usually the answer lives one folder up.
    for child in children:
        if child.is_file():
            code = PLATFORM_BY_EXTENSION.get(child.suffix.lower())
            if code:
                found.add(code)
    if found or depth == 0:
        return
    for child in children:
        if child.is_dir():
            _walk_for_platforms(child, found, depth - 1)
            if found:
                return


def detect_platforms(entry: Path, is_archive: bool) -> list[str]:
    """Sniff VNDB-compatible platform codes from a scanner entry. Archives
    use their own suffix only. Folders are walked up to a few levels deep so
    common multi-disc layouts (Game / Disc 1 / game.nsp) still auto-detect.
    Returns a sorted unique list, possibly empty."""
    found: set[str] = set()
    if is_archive:
        code = PLATFORM_BY_EXTENSION.get(entry.suffix.lower())
        if code:
            found.add(code)
    else:
        _walk_for_platforms(entry, found, _DETECT_MAX_DEPTH)
    return sorted(found)


async def scan_games_paths(scan_paths: list[str]) -> dict:
    """Run the scanner across the supplied paths. Returns a summary dict
    with counts and per-path detail."""
    # Local import keeps this module a leaf — database imports the world.
    import database as db

    # Skip set: paths the user explicitly "Removed from library". Matched
    # case-insensitive on the absolute string (lowercased + trailing-slash
    # stripped).
    ignored_keys = await db.get_ignored_game_path_keys()

    def _is_ignored(p: Path) -> bool:
        return str(p).strip().rstrip("/\\").lower() in ignored_keys

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
    updated = 0  # reserved for future use (when we add field merges)
    per_path: list[dict] = []

    for root in existing:
        local_discovered = 0
        local_created = 0
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            is_folder = entry.is_dir()
            is_archive = (
                entry.is_file() and entry.suffix.lower() in ARCHIVE_EXTENSIONS
            )
            if not (is_folder or is_archive):
                # Also accept console-cartridge files even when their suffix
                # isn't in ARCHIVE_EXTENSIONS — .nsp/.xci/.cia etc. ARE the
                # game, not containers around it.
                if entry.is_file() and entry.suffix.lower() in PLATFORM_BY_EXTENSION:
                    is_archive = True
                else:
                    continue
            if _is_ignored(entry):
                # User "Removed from library" this path earlier — skip on
                # rescan. They can un-ignore it from Settings.
                continue
            title = entry.stem if is_archive else entry.name
            platforms = detect_platforms(entry, is_archive=is_archive)
            _, was_created = await db.upsert_game_from_scan(
                library_path=str(entry),
                title=title,
                is_archive=is_archive,
                platforms=platforms,
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
        "updated": updated,
        "missing_paths": missing,
        "scanned_paths": [str(p) for p in existing],
        "per_path": per_path,
    }
