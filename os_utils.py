"""Small cross-platform OS helpers shared by routers.

`open_folder_focused()` exists because the obvious `os.startfile()` call on
Windows opens Explorer behind the currently-foreground app (i.e. the user's
browser, since this server is invoked over HTTP from the frontend).
Spawning `explorer.exe` directly tends to succeed in taking foreground
because explorer is the shell and has shell-foreground privileges that
`os.startfile`'s ShellExecuteW path does not always grant. We additionally
call `AllowSetForegroundWindow(ASFW_ANY)` as a hint — it's a no-op if our
process doesn't have the right to grant in the first place, but it does no
harm.

`pick_file()` pops a native file-picker dialog. Implemented as a one-shot
subprocess running tkinter so the dialog gets its own main thread + event
loop (calling tkinter from a FastAPI worker thread tends to deadlock).
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Sequence


_ASFW_ANY = -1  # 0xFFFFFFFF; signals "any process may take the foreground"


def open_folder_focused(path: str | Path) -> None:
    """Open `path` in the host OS file browser, taking foreground when the OS
    allows it. Raises OSError on launch failure; foreground failure is a
    best-effort no-op (we still consider the call successful)."""
    target = str(path)
    system = platform.system()

    if system == "Windows":
        # Spawn explorer.exe directly. CREATE_NEW_PROCESS_GROUP detaches the
        # child so killing the server doesn't take Explorer down with it.
        creationflags = 0
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(
                ["explorer.exe", target],
                shell=False,
                creationflags=creationflags,
                close_fds=True,
            )
        except FileNotFoundError:
            # Should never happen on Windows, but cover the case where
            # explorer.exe isn't resolvable on PATH.
            os.startfile(target)  # type: ignore[attr-defined]

        # Best-effort foreground grant. No-op when our process can't grant.
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(_ASFW_ANY)
        except Exception:
            pass
        return

    if system == "Darwin":
        subprocess.Popen(["open", target])
        return

    subprocess.Popen(["xdg-open", target])


# tkinter script run in a fresh subprocess. Reading stdin avoids quoting hell
# with the file-type spec on Windows. Writes a JSON `{"paths": [...]}`
# payload as raw UTF-8 bytes — never plain text — because `sys.stdout.write`
# crashes under the Windows cp1252 default the moment a filename contains
# non-Latin characters (Japanese filenames are the common case here).
# `multi=True` flips it to askopenfilenames; either way the output shape
# stays a list so callers don't need to branch on the response.
_PICK_FILE_TK_SCRIPT = r"""
import json, sys, tkinter as tk
from tkinter import filedialog
spec = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except Exception:
    pass
root.update_idletasks()
if spec.get("directory"):
    raw = filedialog.askdirectory(
        title=spec.get("title") or "Pick folder",
        initialdir=spec.get("initial_dir") or None,
        mustexist=True,
    )
    paths = [raw] if raw else []
else:
    kwargs = dict(
        title=spec.get("title") or "Pick file",
        initialdir=spec.get("initial_dir") or None,
        filetypes=[tuple(p) for p in (spec.get("filetypes") or [])],
    )
    if spec.get("multi"):
        raw = filedialog.askopenfilenames(**kwargs)
        paths = list(raw) if raw else []
    else:
        raw = filedialog.askopenfilename(**kwargs)
        paths = [raw] if raw else []
root.destroy()
sys.stdout.buffer.write(json.dumps({"paths": paths}).encode("utf-8"))
"""


def _run_pick_dialog(spec: dict, *, timeout: float = 600.0) -> list[str]:
    """Shared subprocess driver for `pick_file` / `pick_files`. Returns the
    list of picked paths (empty when the user cancelled)."""
    payload = json.dumps(spec).encode("utf-8")

    # Force UTF-8 stdio in the subprocess so non-Latin paths (e.g. Japanese
    # filenames) survive the trip back. Without this, Python 3 on Windows
    # picks cp1252 for stdout and crashes inside `sys.stdout.write` before
    # we ever see the path.
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.run(
        [sys.executable, "-c", _PICK_FILE_TK_SCRIPT],
        input=payload,
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    if proc.returncode != 0:
        stderr_text = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"pick_file subprocess failed (exit {proc.returncode}): "
            f"{stderr_text or 'no stderr'}"
        )
    raw = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"pick_file subprocess returned non-JSON: {raw!r}")
    out = []
    for p in data.get("paths", []) or []:
        if p:
            out.append(os.path.normpath(p))
    return out


def pick_file(
    *,
    title: str = "Pick file",
    initial_dir: str | None = None,
    filetypes: Sequence[Sequence[str]] | None = None,
    timeout: float = 600.0,
) -> str | None:
    """Pop a native OS file-picker dialog and return the absolute path the
    user picked, or `None` if they cancelled.

    `filetypes` is a list of `(label, pattern)` pairs in tkinter's format —
    e.g. `[("Archives", "*.7z *.zip *.rar"), ("All files", "*.*")]`.

    Runs synchronously and blocks until the user picks or cancels. Call from
    an async context via `asyncio.to_thread(...)` so the FastAPI event loop
    stays responsive while the dialog is open.
    """
    spec = {
        "title": title,
        "initial_dir": initial_dir,
        "filetypes": [list(p) for p in (filetypes or [])],
        "multi": False,
    }
    paths = _run_pick_dialog(spec, timeout=timeout)
    return paths[0] if paths else None


def pick_files(
    *,
    title: str = "Pick files",
    initial_dir: str | None = None,
    filetypes: Sequence[Sequence[str]] | None = None,
    timeout: float = 600.0,
) -> list[str]:
    """Multi-select variant of `pick_file`. Returns the list of absolute
    paths the user picked, or an empty list if they cancelled."""
    spec = {
        "title": title,
        "initial_dir": initial_dir,
        "filetypes": [list(p) for p in (filetypes or [])],
        "multi": True,
    }
    return _run_pick_dialog(spec, timeout=timeout)


def pick_directory(
    *,
    title: str = "Pick folder",
    initial_dir: str | None = None,
    timeout: float = 600.0,
) -> str | None:
    """Pop a native OS folder-picker dialog and return the absolute path the
    user picked, or `None` if they cancelled. Used for pointing a manual entry
    at a folder of already-extracted loose audio instead of an archive."""
    spec = {
        "title": title,
        "initial_dir": initial_dir,
        "directory": True,
    }
    paths = _run_pick_dialog(spec, timeout=timeout)
    return paths[0] if paths else None
