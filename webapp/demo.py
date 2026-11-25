"""One-command local demo: every feature works offline, no API keys needed.

    python -m webapp demo          (or double-click start-windows.bat / start-mac.sh)

Starts fake Google (YouTube) and fake Toss Payments servers next to the app, creates a
demo video, two demo accounts and a ready-made project, then opens the browser.
Nothing leaves your computer; no real money moves.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT / "tests", ROOT / "examples"):
    sys.path.insert(0, str(extra))

GOOGLE_PORT, TOSS_PORT = 9999, 9998
DEMO_SECONDS = 200
ACCOUNTS = [("creator@demo.kr", "크리에이터", "creator"), ("buyer@demo.kr", "구매자", "clipper")]
PASSWORD = "demo1234"


def _serve(app, port: int) -> None:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return
        time.sleep(0.05)


def prepare(data: Path) -> dict:
    """Demo media + fake services' environment. Returns paths the user can upload."""
    from generate_sample import build
    from make_demo_video import main as make_video

    data.mkdir(parents=True, exist_ok=True)
    video, transcript = data / "demo.mp4", data / "demo_transcript.json"
    if not video.exists():
        print("· 체험용 영상을 만드는 중이에요 (처음 한 번, 1분 정도)…")
        make_video(str(video), duration=DEMO_SECONDS)
    if not transcript.exists():
        build().save(transcript)
    os.environ.update({
        "LEGEND_GOOGLE_BASE": f"http://127.0.0.1:{GOOGLE_PORT}", "GOOGLE_CLIENT_ID": "demo", "GOOGLE_CLIENT_SECRET": "demo",
        "TOSS_CLIENT_KEY": "test_ck_fake", "TOSS_SECRET_KEY": "test_sk_fake",
        "LEGEND_TOSS_BASE": f"http://127.0.0.1:{TOSS_PORT}", "LEGEND_TOSS_SDK": f"http://127.0.0.1:{TOSS_PORT}/v2/standard",
        "LEGEND_WHISPER_MODEL": os.environ.get("LEGEND_WHISPER_MODEL", "small"),
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    })
    return {"video": video, "transcript": transcript}


def start_fakes() -> None:
    import math

    from fake_google import FakeGoogle
    from fake_toss import FakeToss

    google = FakeGoogle()
    curve = [[i / 99, round(0.9 * math.exp(-1.2 * i / 99) + (0.25 if 40 <= i <= 48 else 0), 4)] for i in range(100)]
    google.add_long_video("creator", "LONG1", "직장인 부업 월 100만 원, 진짜 가능할까? (풀버전)", DEMO_SECONDS,
                          184_000, 7_300, curve)
    google.add_long_video("creator", "LONG2", "부업 실패담 TOP 5", 1260, 52_000, 1_900)
    google.identity_queue = ["creator", "clipper"]  # first YouTube connection = creator, second = buyer
    google.identity = "clipper"
    _serve(google.asgi(), GOOGLE_PORT)
    _serve(FakeToss().asgi(), TOSS_PORT)


def seed(app, files: dict) -> None:
    accounts = app.state.accounts
    for email, name, role in ACCOUNTS:
        if not app.state.db.one("SELECT 1 FROM users WHERE email=?", email):
            accounts.signup(email, PASSWORD, name, role)
    creator = app.state.db.one("SELECT id FROM users WHERE email=?", ACCOUNTS[0][0])["id"]
    if not app.state.store.list(owner=creator):
        print("· 체험용 프로젝트를 만드는 중이에요…")
        p = app.state.new_project(creator, "체험: 부업 팟캐스트", files["video"], transcript_src=str(files["transcript"]),
                                  cfg={"length": "short", "top": 6, "prompt": "", "selector": "heuristic"})
        app.state.jobs.wait(p["job_id"], timeout=600)


def main(data_dir: str = "demo_data", port: int = 8000, open_browser: bool = True) -> int:
    data = Path(data_dir)
    files = prepare(data)
    start_fakes()
    from .server import create_app

    url = f"http://127.0.0.1:{port}"
    app = create_app(data, public_url=url, billing_interval=60)
    seed(app, files)
    line = "=" * 64
    print(f"""
{line}
  Legend Clips 체험 모드가 켜졌어요 →  {url}

  체험 계정 (비밀번호 모두 {PASSWORD})
   · {ACCOUNTS[0][0]}  크리에이터: 프로젝트가 미리 만들어져 있어요
   · {ACCOUNTS[1][0]}  구매자/클리퍼: 클립 구매, 캠페인 참여

  YouTube 연결·결제는 모두 '가짜 서버'로 동작해요 (실제 돈·실제 업로드 없음)
  '내 채널'에서 클립을 만들 때 올릴 파일:
   · 영상: {files['video'].resolve()}
   · 자막: {files['transcript'].resolve()}

  끝내려면 이 창에서 Ctrl+C
{line}
""")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
