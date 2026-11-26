import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("skimage")
if shutil.which("ffmpeg") is None:
    pytest.skip("ffmpeg missing", allow_module_level=True)

import cv2  # noqa: E402
from skimage import data  # noqa: E402

from highlight_engine.photos import (  # noqa: E402
    AUDIO_SR, HEIC_SUPPORTED, Slide, _balanced_breaks, caption_words, load_photo, render_slideshow, timeline,
)
from highlight_engine.render.captions import paginate  # noqa: E402
from highlight_engine.render.media import probe  # noqa: E402
from webapp.photo_script import ai_script, card_for, fallback_script  # noqa: E402

CSRF = {"X-Requested-With": "legend"}


@pytest.fixture(scope="module")
def photos(tmp_path_factory):
    d = tmp_path_factory.mktemp("photos")
    out = []
    for name in ("coffee", "chelsea", "rocket"):
        p = d / f"{name}.jpg"
        cv2.imwrite(str(p), cv2.cvtColor(getattr(data, name)(), cv2.COLOR_RGB2BGR))
        out.append(p)
    return out


# ---------------------------------------------------------------- engine

def test_captions_break_per_slide_and_balance():
    sl = [Slide("a", "버터 향 가득한 바삭한 겉면", 3), Slide("b", "사장님 고양이도 손님 맞이 중", 3)]
    spans, total = timeline(sl, end_card=True)
    assert spans == [(0, 3), (3, 6)] and total == 9
    pages = [[w["text"] for w in p] for p in paginate(caption_words(sl, spans), 13, 4)]
    assert pages == [["버터", "향", "가득한"], ["바삭한", "겉면"], ["사장님", "고양이도"], ["손님", "맞이", "중"]]
    assert _balanced_breaks(["짧다"], 13, 4) == set()
    narrated = [Slide("a", "하나 둘 셋", 5, narration=np.zeros(AUDIO_SR * 2, dtype=np.float32))]
    w = caption_words(narrated, timeline(narrated, False)[0])
    assert w[-1]["end"] == pytest.approx(0.25 + 2.0, abs=0.01)  # captions follow the voice, not the slide


def test_exif_rotation_and_heic(tmp_path):
    from PIL import Image

    img = Image.fromarray(np.zeros((100, 200, 3), dtype=np.uint8))
    exif = img.getexif()
    exif[0x0112] = 6  # rotated 90° as phones store portrait shots
    img.save(tmp_path / "r.jpg", exif=exif)
    assert load_photo(str(tmp_path / "r.jpg")).shape[:2] == (200, 100)
    if HEIC_SUPPORTED:
        import pillow_heif

        pillow_heif.from_pillow(Image.fromarray(data.coffee())).save(str(tmp_path / "x.heic"))
        assert load_photo(str(tmp_path / "x.heic")).shape[:2] == data.coffee().shape[:2]


def test_render_with_end_card_voice_and_music(photos, tmp_path):
    music = tmp_path / "bgm.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=3", str(music)], check=True)
    voice = (0.2 * np.sin(np.linspace(0, 800, AUDIO_SR))).astype(np.float32)
    slides = [Slide(str(photos[0]), "성수동 크로플 맛집", 1.5, voice), Slide(str(photos[1]), "고양이 손님", 1.2)]
    out = tmp_path / "s.mp4"
    meta = render_slideshow(slides, str(out), style="legend_sticker", end_card={"title": "크로플하우스", "line1": "6,500원"},
                            music=str(music), keywords=["크로플"])
    assert meta == {"duration": 5.7, "slides": 2, "narration": True, "music": True}
    info = probe(str(out))
    assert (info.width, info.height) == (1080, 1920) and info.has_audio and abs(info.duration - 5.7) < 0.2


# ---------------------------------------------------------------- scripts

def test_fallback_script_and_card():
    f = {"name": "크로플하우스", "menu": "버터 크로플 6,500원", "place": "성수동", "hours": "", "point": "겉바속촉"}
    s = fallback_script("restaurant", f, 4)
    assert [x["text"] for x in s["slides"]] == ["크로플하우스, 여기 진짜예요", "겉바속촉", "대표 메뉴는 버터 크로플 6,500원", "분위기까지 완벽"]
    assert card_for("restaurant", f) == {"title": "크로플하우스", "line1": "버터 크로플 6,500원", "line2": "성수동"}
    assert card_for("free", {"topic": "x"}) is None


def test_ai_script_sends_photos_and_validates(photos):
    calls = []

    def create(**kw):
        calls.append(kw)
        out = {"title": "크로플 맛집", "slides": [{"photo": 2, "text": "줄 서는 이유"}, {"photo": 2, "text": "중복"},
                                                {"photo": 9, "text": "없는 사진"}, {"photo": 0, "text": "바삭"}],
               "keywords": ["크로플"], "hashtags": ["성수맛집", "#Shorts"]}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(out))])

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    s = ai_script("restaurant", {"name": "크로플하우스"}, [str(p) for p in photos], "밝게", client=client)
    assert [x["photo"] for x in s["slides"]] == [2, 0] and s["hashtags"] == ["#성수맛집", "#Shorts"] and s["source"] == "ai"
    content = calls[0]["messages"][0]["content"]
    assert sum(1 for b in content if b["type"] == "image") == 3 and "크로플하우스" in content[-1]["text"]
    assert calls[0]["model"] == "claude-opus-5-5"

    broken = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("no key")))))
    fb = ai_script("product", {"name": "가습기"}, [str(p) for p in photos], client=broken)
    assert fb["source"] == "template" and "warning" in fb


# ---------------------------------------------------------------- web app

class FakeTTS:
    name, voices = "fake", [("v1", "테스트 목소리")]

    def __init__(self):
        self.calls = 0

    def synth(self, text, voice=None):
        self.calls += 1
        r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=330:duration={0.15 * len(text):.2f}",
                            "-f", "mp3", "-"], capture_output=True, check=True)
        return r.stdout


@pytest.fixture()
def web(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from webapp.server import create_app

    app = create_app(tmp_path / "d", public_url="http://testserver")

    def user(email):
        c = TestClient(app, headers=CSRF)
        c.post("/api/auth/signup", json={"email": email, "password": "password123"})
        return c

    return SimpleNamespace(app=app, user=user)


def test_photo_project_flow(web, photos):
    c, other = web.user("ph@x.kr"), web.user("other@x.kr")
    cat = c.get("/api/photo/templates").json()
    assert {t["id"] for t in cat["templates"]} == {"restaurant", "product", "travel", "realestate", "free"}
    assert cat["tts"]["available"] is False

    files = [("photos", (p.name, open(p, "rb"), "image/jpeg")) for p in photos]
    r = c.post("/api/photo-projects", files=files + [("photos", ("x.txt", b"hi", "text/plain"))],
               data={"template": "restaurant"})
    assert r.status_code == 400  # a non-image is refused and nothing is charged
    assert c.get("/api/auth/me").json()["usage"]["used_minutes"] == 0
    files = [("photos", (p.name, open(p, "rb"), "image/jpeg")) for p in photos]
    r = c.post("/api/photo-projects", files=files, data={
        "template": "restaurant", "ai": "false", "fields": json.dumps({"name": "크로플하우스", "menu": "6,500원"})})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    web.app.state.jobs.wait(r.json()["job_id"])
    p = c.get(f"/api/photo-projects/{pid}").json()
    assert p["status"] == "ready" and len(p["script"]["slides"]) == 3 and p["settings"]["preset"] == "legend_sticker"
    assert c.get(p["photos"][0]["thumb_url"]).headers["content-type"] == "image/jpeg"
    assert other.get(f"/api/photo-projects/{pid}").status_code == 404
    assert c.get("/api/auth/me").json()["usage"]["used_minutes"] == 1.0
    assert any(x["kind"] == "photo" for x in c.get("/api/projects").json())

    slides = [{"photo": p["photos"][2]["file"], "text": "우주급 크로플", "duration": 1.5},
              {"photo": p["photos"][0]["file"], "text": "바삭바삭", "duration": 1.5}]
    assert c.patch(f"/api/photo-projects/{pid}", json={"script": {"slides": [{"photo": "nope.jpg"}]}}).status_code == 400
    assert c.patch(f"/api/photo-projects/{pid}", json={"settings": {"voice": True}}).status_code == 400  # no TTS set up
    r = c.patch(f"/api/photo-projects/{pid}", json={"script": {"title": "t", "slides": slides}, "settings": {"preset": "neon"}})
    assert r.json()["script"]["slides"][0]["text"] == "우주급 크로플"

    with open(photos[0], "rb") as f:
        assert c.post(f"/api/photo-projects/{pid}/music", files={"music": ("a.jpg", f, "image/jpeg")}).status_code == 400

    # narration with a (fake) TTS provider: slides stretch to fit the voice, and audio is cached
    tts = FakeTTS()
    web.app.state.tts = tts
    c.patch(f"/api/photo-projects/{pid}", json={"settings": {"voice": True, "end_card": False}})
    job = web.app.state.jobs.wait(c.post(f"/api/photo-projects/{pid}/render").json()["job_id"], timeout=120)
    assert job["status"] == "done", job
    assert job["result"]["narration"] and job["result"]["duration"] > 3.0 and tts.calls == 2
    out = c.get(job["result"]["url"])
    assert out.status_code == 200 and out.headers["content-type"] == "video/mp4"
    web.app.state.jobs.wait(c.post(f"/api/photo-projects/{pid}/render").json()["job_id"], timeout=120)
    assert tts.calls == 2  # second render reused the cached narration

    assert c.post(f"/api/photo-projects/{pid}/publish", json={"title": "크로플"}).status_code == 400  # YouTube not connected
