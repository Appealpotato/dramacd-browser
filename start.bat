@echo off
cd /d "%~dp0"
REM Prefer the project virtualenv created by install; fall back to a global Python.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
) else (
    python main.py
)
pause
