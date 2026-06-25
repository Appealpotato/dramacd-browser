#!/bin/bash
# DramaCD Browser - macOS setup launcher.
# Double-click in Finder, or run ./install.command from Terminal.
# This shim only guarantees a Python 3 interpreter exists, then hands off to
# install.py, which does all the real (cross-platform) work.

cd "$(dirname "$0")" || exit 1

echo "============================================"
echo "  DramaCD Browser - Setup (macOS)"
echo "============================================"

# Locate a Python 3 interpreter.
PY=""
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
fi

if [ -z "$PY" ]; then
    echo
    echo "Python 3 was not found."
    if command -v brew >/dev/null 2>&1; then
        read -r -p "Install Python 3 via Homebrew now? [Y/n]: " ans
        case "${ans:-Y}" in
            [yY]*) brew install python && PY=python3 ;;
        esac
    fi
fi

if [ -z "$PY" ]; then
    echo
    echo "Please install Python 3 first, then run install.command again:"
    echo "  - Homebrew:  brew install python"
    echo "  - Installer: https://www.python.org/downloads/macos/"
    echo
    read -r -p "Press Return to close..." _
    exit 1
fi

echo "Using: $("$PY" --version 2>&1)"
"$PY" install.py "$@"
status=$?

echo
read -r -p "Press Return to close..." _
exit $status
