#!/bin/bash
# DramaCD Browser - macOS launcher. Double-click in Finder to start the server
# (it opens your browser automatically).

cd "$(dirname "$0")" || exit 1

# Prefer the project virtualenv created by install.command; fall back to a
# global Python 3.
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "Python 3 not found. Run install.command first."
    read -r -p "Press Return to close..." _
    exit 1
fi

exec "$PY" main.py
