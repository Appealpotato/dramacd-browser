@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   DramaCD Browser - First-Time Setup
echo ============================================
echo.

REM This launcher only guarantees a Python interpreter exists. All of the real
REM setup (dependency install, VC++ runtime, ffmpeg/7-Zip, core-vs-full choice)
REM lives in the cross-platform install.py so it never has to be duplicated here.

set "PYTHON=python"

REM --- Check for an existing Python ---
%PYTHON% --version >nul 2>&1
if !errorlevel! equ 0 goto :handoff

echo Python not found. Installing Python 3.12 (per-user, no admin needed)...
echo.

REM --- Try winget first (native on Win 10/11) ---
where winget >nul 2>&1
if !errorlevel! equ 0 (
    winget install Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent --scope user
    goto :find_python
)

REM --- Fallback: download the official installer ---
echo winget not available. Downloading installer from python.org...
set "PY_URL=https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
set "PY_EXE=%TEMP%\python-installer.exe"
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_EXE%' -UseBasicParsing } catch { exit 1 }"
if not exist "%PY_EXE%" (
    echo.
    echo [ERROR] Could not download Python.
    echo Please install Python 3.10+ manually from https://www.python.org/downloads/
    echo Make sure to tick "Add python.exe to PATH" during the installer.
    pause
    exit /b 1
)
echo Running installer (this can take a minute)...
"%PY_EXE%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
del "%PY_EXE%" 2>nul

:find_python
REM --- Locate the newly installed Python (its PATH update won't reach this cmd) ---
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
) do (
    if exist %%P (
        set "PYTHON=%%~P"
        goto :handoff
    )
)

echo.
echo Python was installed, but it isn't visible to this cmd window yet.
echo Please close this window and double-click install.bat one more time.
pause
exit /b 0

:handoff
echo [OK] Using Python at: %PYTHON%
"%PYTHON%" --version
echo.

REM --- Hand off to the cross-platform installer ---
"%PYTHON%" install.py %*
set "RC=!errorlevel!"

echo.
if !RC! neq 0 (
    echo [ERROR] Setup did not complete. See the messages above.
) else (
    echo Done! Double-click start.bat to launch the app.
)
pause
exit /b !RC!
