#!/usr/bin/env python3
"""Cross-platform installer for DramaCD Browser.

This is the single source of truth for setup. The platform launchers
(install.bat on Windows, install.command on macOS) do nothing but guarantee a
Python interpreter exists, then hand off to this script. All OS-specific logic
lives here, so it never has to be duplicated across shell dialects.

What it does:
  1. Detects the OS (Windows / macOS / Linux).
  2. Lets you choose a lightweight "core" install (library, scanning, metadata,
     AI translation, web UI) or the full install that adds the local audio
     transcription pipeline (Whisper + torch, ~2-3 GB).
  3. Installs the matching requirements file into THIS interpreter (sys.executable).
  4. On Windows + full install, ensures the Visual C++ runtime torch needs.
  5. Auto-installs the optional external tools (7-Zip always; ffmpeg for the
     pipeline) via the platform package manager (winget / Homebrew), and checks
     that tkinter is present for the native file pickers.

Usage:
    python install.py            # interactive (asks core vs full)
    python install.py --core     # lightweight, no prompt
    python install.py --full     # everything, no prompt
    python install.py --check    # report what's installed; change nothing
    python install.py --yes      # assume "yes" to every prompt
"""
from __future__ import annotations

import argparse
import importlib.util
import platform
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYSTEM = platform.system()          # 'Windows' | 'Darwin' | 'Linux'
IS_WINDOWS = SYSTEM == "Windows"
IS_MAC = SYSTEM == "Darwin"
IS_LINUX = SYSTEM == "Linux"

CORE_REQ = HERE / "requirements-core.txt"
PIPELINE_REQ = HERE / "requirements-pipeline.txt"

# 7-Zip ships its CLI under different names depending on the build/platform:
# 7z / 7za on Windows + p7zip, 7zz on the modern `sevenzip` Homebrew formula.
SEVENZIP_BINS = ("7z", "7za", "7zz")


# --- tiny console helpers -------------------------------------------------

def say(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  [WARN] {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n>>> {msg}", flush=True)


def ask_yes_no(question: str, *, default: bool, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        raw = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw[0] == "y"


# --- detection ------------------------------------------------------------

def find_tool(*names: str) -> str | None:
    """Resolve the first of `names` on PATH, falling back to common install
    locations a GUI-launched process might not have on PATH (notably Homebrew's
    /opt/homebrew/bin under a minimal launchd PATH)."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    fallback_dirs = []
    if IS_WINDOWS:
        fallback_dirs = [
            Path(r"C:\Program Files\7-Zip"),
            Path(r"C:\Program Files (x86)\7-Zip"),
            Path(r"C:\ffmpeg\bin"),
            Path(r"C:\Program Files\ffmpeg\bin"),
        ]
        exts = (".exe", "")
    else:
        fallback_dirs = [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin")]
        exts = ("",)
    for d in fallback_dirs:
        for name in names:
            for ext in exts:
                cand = d / f"{name}{ext}"
                if cand.exists():
                    return str(cand)
    return None


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def have_package_manager() -> str | None:
    """Return the name of the platform package manager if present."""
    if IS_WINDOWS and shutil.which("winget"):
        return "winget"
    if IS_MAC and shutil.which("brew"):
        return "brew"
    if IS_LINUX:
        for mgr in ("apt-get", "dnf", "pacman"):
            if shutil.which(mgr):
                return mgr
    return None


# --- virtual environment --------------------------------------------------

def venv_python(venv_dir: Path) -> Path:
    """Path to the interpreter inside a venv on this OS."""
    if IS_WINDOWS:
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def in_virtualenv() -> bool:
    return sys.prefix != sys.base_prefix


def existing_target_python() -> tuple[str, str]:
    """Which interpreter the deps live in, WITHOUT creating anything.
    Used by --check and by the launchers' mental model."""
    if in_virtualenv():
        return sys.executable, "active virtualenv"
    vp = venv_python(HERE / ".venv")
    if vp.exists():
        return str(vp), "project .venv"
    return sys.executable, "base interpreter (no venv yet)"


def resolve_target_python() -> tuple[str, str]:
    """Pick (and if needed create) the interpreter to install into.

    Prefer an already-active virtualenv; otherwise create/use a project-local
    .venv. This is what sidesteps PEP 668's 'externally-managed-environment'
    pip error on Homebrew Python and most Linux distro Pythons -- you can't
    pip-install into those system interpreters, but a venv is always fair game."""
    if in_virtualenv():
        return sys.executable, "active virtualenv"
    venv_dir = HERE / ".venv"
    target = venv_python(venv_dir)
    if target.exists():
        return str(target), f"existing {venv_dir.name}"
    say(f"  Creating a virtual environment at {venv_dir} ...")
    r = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=False)
    if r.returncode == 0 and target.exists():
        ok(f"Virtual environment created ({venv_dir.name}).")
        return str(target), f"new {venv_dir.name}"
    warn("Could not create a virtual environment.")
    if IS_LINUX:
        say("        On Debian/Ubuntu, install the venv module first:")
        say("          sudo apt install python3-venv")
    say("        Falling back to the base interpreter. If pip then reports")
    say("        'externally-managed-environment', create a venv by hand:")
    say(f"          {sys.executable} -m venv .venv")
    say(f"          . .venv/bin/activate && pip install -r {CORE_REQ.name}")
    return sys.executable, "base interpreter (venv unavailable)"


# --- pip ------------------------------------------------------------------

def pip_install(req_file: Path, py: str) -> bool:
    if not req_file.exists():
        warn(f"{req_file.name} not found - skipping.")
        return False
    say(f"  Installing from {req_file.name} ...")
    subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip"], check=False)
    result = subprocess.run([py, "-m", "pip", "install", "-r", str(req_file)], check=False)
    if result.returncode != 0:
        warn(f"pip install -r {req_file.name} failed (exit {result.returncode}).")
        return False
    ok(f"{req_file.name} installed.")
    return True


def module_in(py: str, name: str) -> bool:
    """Is import `name` available to interpreter `py`? Runs a subprocess so we
    can probe the venv, not just the interpreter running this script."""
    try:
        r = subprocess.run(
            [py, "-c", f"import importlib.util,sys; "
                       f"sys.exit(0 if importlib.util.find_spec({name!r}) else 1)"],
            capture_output=True,
        )
        return r.returncode == 0
    except Exception:
        return False


# --- external tools -------------------------------------------------------

def ensure_vcredist(assume_yes: bool) -> None:
    """torch / faster-whisper need the VC++ 2015-2022 runtime on Windows."""
    if not IS_WINDOWS:
        return
    dll = Path(r"C:\Windows\System32\vcruntime140_1.dll")
    if dll.exists():
        ok("Visual C++ runtime already present.")
        return
    if shutil.which("winget"):
        say("  Installing Visual C++ Redistributable via winget ...")
        subprocess.run(
            ["winget", "install", "Microsoft.VCRedist.2015+.x64",
             "--accept-source-agreements", "--accept-package-agreements", "--silent"],
            check=False,
        )
    else:
        warn("Visual C++ runtime missing and winget unavailable.")
        say("        Install it from https://aka.ms/vs/17/release/vc_redist.x64.exe")


def winget_install(pkg_id: str, label: str) -> None:
    say(f"  Installing {label} via winget ...")
    r = subprocess.run(
        ["winget", "install", pkg_id, "--accept-source-agreements",
         "--accept-package-agreements", "--silent"],
        check=False,
    )
    if r.returncode != 0:
        warn(f"{label} install via winget failed (exit {r.returncode}). Install it manually.")


def brew_install(formula: str, label: str) -> None:
    say(f"  Installing {label} via Homebrew ({formula}) ...")
    r = subprocess.run(["brew", "install", formula], check=False)
    if r.returncode != 0:
        warn(f"{label} install via Homebrew failed. Try: brew install {formula}")


def manual_hint(tool: str) -> None:
    hints = {
        "ffmpeg": {
            "Windows": "winget install Gyan.FFmpeg   (or https://www.gyan.dev/ffmpeg/builds/)",
            "Darwin": "brew install ffmpeg",
            "Linux": "sudo apt install ffmpeg   (or your distro's package manager)",
        },
        "7-Zip": {
            "Windows": "winget install 7zip.7zip   (or https://www.7-zip.org/)",
            "Darwin": "brew install p7zip",
            "Linux": "sudo apt install p7zip-full",
        },
    }
    line = hints.get(tool, {}).get(SYSTEM)
    if line:
        say(f"        Install manually: {line}")


def ensure_external_tools(*, want_ffmpeg: bool, assume_yes: bool) -> None:
    """7-Zip is useful for any RAR/7z archive (offered always). ffmpeg is only
    needed for the transcription pipeline (offered when that's installed)."""
    pm = have_package_manager()

    # --- 7-Zip (RAR / 7z extraction + archive viewer) ---
    if find_tool(*SEVENZIP_BINS):
        ok("7-Zip found.")
    else:
        say("\n  7-Zip is needed to extract RAR / 7z archives (plain ZIP works without it).")
        if pm in ("winget", "brew") and ask_yes_no("  Install 7-Zip now?", default=True, assume_yes=assume_yes):
            if pm == "winget":
                winget_install("7zip.7zip", "7-Zip")
            else:
                brew_install("p7zip", "7-Zip")
        else:
            manual_hint("7-Zip")

    # --- ffmpeg (audio decode for Whisper) ---
    if not want_ffmpeg:
        return
    if find_tool("ffmpeg"):
        ok("ffmpeg found.")
    else:
        say("\n  ffmpeg is needed to decode audio for transcription.")
        if pm in ("winget", "brew") and ask_yes_no("  Install ffmpeg now?", default=True, assume_yes=assume_yes):
            if pm == "winget":
                winget_install("Gyan.FFmpeg", "ffmpeg")
            else:
                brew_install("ffmpeg", "ffmpeg")
        else:
            manual_hint("ffmpeg")


def check_tkinter() -> None:
    """The native file/folder pickers (os_utils.py) use tkinter. python.org
    builds bundle it; Homebrew Python needs the separate python-tk formula."""
    if has_module("tkinter"):
        ok("tkinter present (native file pickers will work).")
        return
    warn("tkinter is missing - the native file/folder pickers will not work.")
    if IS_MAC:
        ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        say(f"        Fix:  brew install python-tk@{ver}")
        say("        (or install Python from https://www.python.org/downloads/macos/, which bundles Tk)")
    elif IS_LINUX:
        say("        Fix:  sudo apt install python3-tk")
    else:
        say("        Reinstall Python from python.org with the tcl/tk option enabled.")


# --- profile selection ----------------------------------------------------

def choose_profile(args) -> str:
    if args.core:
        return "core"
    if args.full or args.yes:
        return "full"
    say("")
    say("Install the audio transcription pipeline?")
    say("  This adds Whisper + torch (~2-3 GB download). The library, scanning,")
    say("  metadata, and AI translation all work WITHOUT it.")
    if IS_MAC:
        say("  Note: on macOS transcription runs on the CPU (no GPU acceleration).")
    full = ask_yes_no("\n  Install pipeline extras?", default=False, assume_yes=False)
    return "full" if full else "core"


# --- check / doctor mode --------------------------------------------------

def run_check() -> None:
    step(f"Environment check - {SYSTEM} (Python {platform.python_version()})")
    target, note = existing_target_python()
    say(f"  Dependency interpreter: {target}  [{note}]")
    say("\n  Core dependencies:")
    for mod in ("fastapi", "uvicorn", "aiosqlite", "dotenv", "httpx", "bs4", "PIL"):
        present = module_in(target, mod)
        (ok if present else warn)(f"{mod}: {'present' if present else 'MISSING'}")
    say("\n  Pipeline dependencies (optional):")
    for mod in ("faster_whisper", "ctranslate2", "torch"):
        present = module_in(target, mod)
        (ok if present else warn)(f"{mod}: {'present' if present else 'not installed'}")
    say("\n  External tools:")
    sz = find_tool(*SEVENZIP_BINS)
    (ok if sz else warn)(f"7-Zip: {sz or 'not found'}")
    ff = find_tool("ffmpeg")
    (ok if ff else warn)(f"ffmpeg: {ff or 'not found'}")
    check_tkinter()


# --- main -----------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="DramaCD Browser installer")
    parser.add_argument("--core", action="store_true", help="lightweight install, no prompt")
    parser.add_argument("--full", action="store_true", help="full install with pipeline, no prompt")
    parser.add_argument("--check", action="store_true", help="report what's installed and exit")
    parser.add_argument("--yes", action="store_true", help="assume yes to every prompt (implies --full)")
    args = parser.parse_args()

    say("============================================")
    say("  DramaCD Browser - Setup")
    say(f"  Platform: {SYSTEM}  |  Python {platform.python_version()}")
    say("============================================")

    if args.check:
        run_check()
        return 0

    profile = choose_profile(args)
    req_file = PIPELINE_REQ if profile == "full" else CORE_REQ

    step("Preparing the Python environment")
    target_py, note = resolve_target_python()
    say(f"  Installing into: {target_py}  [{note}]")

    step(f"Installing Python dependencies ({profile})")
    if not pip_install(req_file, target_py):
        say("\nDependency install failed - see the messages above.")
        return 1

    if profile == "full":
        step("Checking the Visual C++ runtime (torch dependency)")
        ensure_vcredist(args.yes)

    step("Checking external tools")
    ensure_external_tools(want_ffmpeg=(profile == "full"), assume_yes=args.yes)
    check_tkinter()

    say("\n============================================")
    say("  All done!")
    if IS_WINDOWS:
        say("  Launch with:  start.bat")
    else:
        say("  Launch with:  ./start.command")
    say("  (the launchers use the .venv automatically)")
    say(f"  Installed profile: {profile}")
    say("============================================")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("\nAborted.")
        raise SystemExit(130)
