@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   DramaCD Browser - First-Time Setup
echo ============================================
echo.

set "PYTHON=python"

REM --- Check for an existing Python ---
%PYTHON% --version >nul 2>&1
if !errorlevel! equ 0 goto :install_deps

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
        goto :install_deps
    )
)

echo.
echo Python was installed, but it isn't visible to this cmd window yet.
echo Please close this window and double-click install.bat one more time.
pause
exit /b 0

:install_deps
echo [OK] Using Python at: %PYTHON%
"%PYTHON%" --version
echo.

REM --- Microsoft Visual C++ Redistributable 2015-2022 (required by torch / faster-whisper) ---
echo Checking Visual C++ Runtime...
if exist "%WINDIR%\System32\vcruntime140_1.dll" (
    echo [OK] Visual C++ Runtime 2015-2022 already installed.
) else (
    echo Installing Visual C++ Redistributable 2015-2022 ^(x64^)...
    where winget >nul 2>&1
    if !errorlevel! equ 0 (
        winget install Microsoft.VCRedist.2015+.x64 --accept-source-agreements --accept-package-agreements --silent
    ) else (
        echo Downloading vc_redist.x64.exe...
        set "VC_URL=https://aka.ms/vs/17/release/vc_redist.x64.exe"
        set "VC_EXE=%TEMP%\vc_redist.x64.exe"
        powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '!VC_URL!' -OutFile '!VC_EXE!' -UseBasicParsing } catch { exit 1 }"
        if exist "!VC_EXE!" (
            "!VC_EXE!" /install /quiet /norestart
            del "!VC_EXE!" 2>nul
        ) else (
            echo [WARN] Could not auto-install VC++ Runtime. torch/faster-whisper may fail to load.
            echo        Install manually from https://aka.ms/vs/17/release/vc_redist.x64.exe
        )
    )
)

echo.
echo Installing dependencies (this may take a few minutes)...
echo.
"%PYTHON%" -m pip install --upgrade pip
"%PYTHON%" -m pip install -r requirements.txt
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] pip install failed. See the messages above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   All done! Double-click start.bat to launch.
echo ============================================
echo.
echo Optional for transcription / RAR / 7Z extraction:
echo   * 7-Zip:  https://www.7-zip.org/
echo   * FFmpeg: https://www.gyan.dev/ffmpeg/builds/
echo.
pause
