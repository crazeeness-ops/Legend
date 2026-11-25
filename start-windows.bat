@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Legend Clips

where python >nul 2>nul
if errorlevel 1 goto nopython
where ffmpeg >nul 2>nul
if errorlevel 1 goto noffmpeg

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 처음 실행 준비 중...
  python -m venv .venv
)
call ".venv\Scripts\activate.bat"
echo [2/3] 필요한 프로그램 설치 중... 처음에는 몇 분 걸려요
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
if errorlevel 1 goto pipfail
echo [3/3] Legend Clips 체험 모드를 시작해요. 브라우저가 열려요.
python -m webapp demo
pause
exit /b 0

:nopython
echo.
echo [!] Python이 설치되어 있지 않아요.
echo     https://www.python.org/downloads/ 에서 설치하세요.
echo     설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.
pause
exit /b 1

:noffmpeg
echo.
echo [!] ffmpeg 영상 처리 프로그램이 없어요.
echo     명령 프롬프트를 열고 다음을 입력한 뒤, 창을 닫고 이 파일을 다시 실행하세요:
echo         winget install Gyan.FFmpeg
pause
exit /b 1

:pipfail
echo.
echo [!] 설치 중 문제가 생겼어요. 위의 오류 메시지를 캡처해서 알려 주세요.
pause
exit /b 1
