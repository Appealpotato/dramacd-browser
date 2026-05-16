@echo off
cd /d "%~dp0"
echo Pulling latest from GitHub...
git pull
echo.
echo Installing/updating dependencies...
pip install -r requirements.txt
echo.
echo Done. You can now run start.bat to launch the app.
pause
