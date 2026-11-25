#!/bin/bash
# Legend Clips 체험 모드 (macOS). Finder에서 더블클릭하면 터미널에서 실행돼요.
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[!] Python이 없어요. 터미널에서:  brew install python   (Homebrew: https://brew.sh)"
  read -r -p "Enter를 누르면 닫혀요"; exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[!] ffmpeg가 없어요. 터미널에서:  brew install ffmpeg"
  read -r -p "Enter를 누르면 닫혀요"; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "[1/3] 처음 실행 준비 중..."
  python3 -m venv .venv
fi
source .venv/bin/activate
echo "[2/3] 필요한 프로그램 설치 중... (처음에는 몇 분 걸려요)"
python -m pip install --upgrade pip >/dev/null
pip install -r requirements.txt || { echo "[!] 설치 중 문제가 생겼어요. 위 메시지를 캡처해서 알려 주세요."; read -r; exit 1; }
echo "[3/3] Legend Clips 체험 모드를 시작해요. 브라우저가 열려요."
python -m webapp demo
