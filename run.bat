@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -c "import sys" >nul 2>&1 || (
    echo 아직 설치가 안 됐습니다. 먼저 setup.bat 을 실행해 주세요.
    pause
    exit /b 1
)
if not exist "tools\ffmpeg\ffmpeg.exe" (
    echo FFmpeg가 없습니다. 먼저 setup.bat 을 실행해 주세요.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" app.py
