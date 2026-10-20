@echo off
cd /d "%~dp0"

rem A .venv copied from another PC points at that PC's Python; rebuild it if it does not run.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1 || (
        echo 다른 PC에서 복사된 가상환경을 지우고 새로 만듭니다...
        rmdir /s /q .venv
    )
)

echo [1/3] 파이썬 가상환경을 만드는 중...
if not exist ".venv\Scripts\python.exe" (
    for %%V in (3.12 3.13 3.14) do (
        if not exist ".venv\Scripts\python.exe" py -%%V -m venv .venv >nul 2>&1
    )
)
if not exist ".venv\Scripts\python.exe" (
    python -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1 && python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Python 3.12 이상이 필요합니다.
    echo https://www.python.org/downloads/ 에서 설치한 뒤 setup.bat 을 다시 실행하세요.
    echo 설치할 때 "Add python.exe to PATH" 를 꼭 체크하세요.
    pause
    exit /b 1
)

echo [2/3] 필요한 패키지를 설치하는 중... (처음에는 몇 분 걸립니다)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo 설치에 실패했습니다. 위 메시지를 확인해 주세요.
    pause
    exit /b 1
)

echo [3/3] FFmpeg를 준비하는 중... (처음에는 약 100MB를 내려받습니다)
".venv\Scripts\python.exe" tools\get_ffmpeg.py
if errorlevel 1 (
    echo FFmpeg를 받지 못했습니다. 인터넷 연결을 확인한 뒤 다시 실행해 주세요.
    pause
    exit /b 1
)
echo.
echo 설치 완료! run.bat 을 더블클릭해서 실행하세요.
if not exist "models\supertonic-3\onnx\vocoder.onnx" echo (처음 음성을 만들 때 음성 모델 약 400MB를 한 번 내려받습니다)
pause
