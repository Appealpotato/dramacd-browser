@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   DramaCD Browser - Optional Extras
echo ============================================
echo.
echo This installs two optional tools:
echo   * FFmpeg  - needed for audio transcription
echo   * 7-Zip   - needed for RAR / 7Z archive extraction
echo.
echo The app runs fine without them; you only need these if you
echo plan to use the pipeline / transcription features or your
echo archives are RAR/7Z (plain ZIP works without 7-Zip).
echo.
pause

REM --- Need winget for this script. It ships with Win 10 (1809+) and Win 11. ---
where winget >nul 2>&1
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] winget is not available on this system.
    echo Please install the tools manually:
    echo   * FFmpeg: https://www.gyan.dev/ffmpeg/builds/
    echo   * 7-Zip:  https://www.7-zip.org/
    echo.
    pause
    exit /b 1
)

echo.
echo --- Installing FFmpeg ---
where ffmpeg >nul 2>&1
if !errorlevel! equ 0 (
    echo [OK] FFmpeg already installed, skipping.
) else (
    winget install Gyan.FFmpeg --accept-source-agreements --accept-package-agreements --silent --scope user
    if !errorlevel! neq 0 (
        echo [WARN] FFmpeg install via winget failed. Try manual install:
        echo        https://www.gyan.dev/ffmpeg/builds/
    )
)

echo.
echo --- Installing 7-Zip ---
if exist "%ProgramFiles%\7-Zip\7z.exe" (
    echo [OK] 7-Zip already installed, skipping.
) else if exist "%ProgramFiles(x86)%\7-Zip\7z.exe" (
    echo [OK] 7-Zip already installed, skipping.
) else (
    winget install 7zip.7zip --accept-source-agreements --accept-package-agreements --silent
    if !errorlevel! neq 0 (
        echo [WARN] 7-Zip install via winget failed. Try manual install:
        echo        https://www.7-zip.org/
    )
)

echo.
echo ============================================
echo   Done! Close this window and start the app.
echo ============================================
echo.
echo Note: if start.bat was already running, restart it so
echo it sees the new tools on PATH.
echo.
pause
