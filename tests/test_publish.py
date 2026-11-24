import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))
sys.path.insert(0, str(ROOT / "tests"))

from fake_google import FakeGoogle  # noqa: E402

from webapp import metadata  # noqa: E402
from webapp.youtube import Endpoints, OAuthConfig, VideoMeta, YouTubeAccount, YouTubeError  # noqa: E402

FAKE = "http://fake-google"
CFG = OAuthConfig("cid", "secret", "http://127.0.0.1:8000/oauth/google/callback")


def future(hours=2):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def connected_account(tmp_path, fake):
    acc = YouTubeAccount(tmp_path, CFG, _ep(),
                         http=httpx.Client(transport=httpx.MockTransport(fake.handle)))
    acc.sleep = lambda s: None
    url = acc.auth_url("/#/p/x")
    code, headers, _ = fake.respond("GET", urlparse(url).path, parse_qs(urlparse(url).query), {}, b"")
    q = parse_qs(urlparse(headers["location"]).query)
    assert acc.finish_auth(q["code"][0], q["state"][0]) == "/#/p/x"
    return acc


def _ep():
    return Endpoints(f"{FAKE}/o/oauth2/v2/auth", f"{FAKE}/token", f"{FAKE}/revoke", f"{FAKE}/youtube/v3",
                     f"{FAKE}/upload/youtube/v3/videos")


# ---------------------------------------------------------------- metadata rules

def test_video_meta_validation_and_body():
    VideoMeta("좋은 제목").validate()
    for bad in (VideoMeta(""), VideoMeta("가" * 101), VideoMeta("a <b>"), VideoMeta("ok", privacy="friends"),
                VideoMeta("ok", publish_at="2020-01-01T00:00:00+00:00"), VideoMeta("ok", tags=["x" * 600])):
        with pytest.raises(YouTubeError):
            bad.validate()
    body = VideoMeta("제목", privacy="public", publish_at="2031-05-01T18:00:00+09:00").body()
    assert body["status"] == {"selfDeclaredMadeForKids": False, "embeddable": True,
                              "privacyStatus": "private", "publishAt": "2031-05-01T09:00:00.000Z"}
    assert VideoMeta("제목", privacy="unlisted").body()["status"]["privacyStatus"] == "unlisted"


def test_metadata_finalize_and_fallback():
    out = metadata.finalize("제목 <위험>", "설명", ["부업", "#돈 버는 법"], ["부업", "부업", "재테크"])
    assert out["title"] == "제목 위험" and out["hashtags"] == ["#부업", "#돈버는법", "#Shorts"]
    assert out["description"].endswith("#부업 #돈버는법 #Shorts") and out["tags"] == ["부업", "재테크"]

    class Boom:
        class beta:
            class messages:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("no key")

    clip = {"title": "부업 월 100만 원", "text": "진짜 가능한가요? 네.", "keywords": ["부업"]}
    fb = metadata.generate(clip, client=Boom())
    assert fb["source"] == "basic" and fb["title"] == "부업 월 100만 원" and "#Shorts" in fb["description"]


def test_metadata_generate_with_claude():
    payload = {"title": "직장인 부업, 월 100만 원 진짜 될까?", "description": "3개월 안에 포기하는 이유",
               "hashtags": ["#부업", "직장인"], "tags": ["부업", "직장인 부업"]}
    calls = []

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))])

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    out = metadata.generate({"title": "t", "text": "본문", "keywords": []}, client=client)
    assert out["source"] == "ai" and out["hashtags"][-1] == "#Shorts" and out["tags"] == ["부업", "직장인부업"]
    assert calls[0]["model"] == "claude-opus-5-5" and calls[0]["output_config"]["format"]["type"] == "json_schema"


# ---------------------------------------------------------------- OAuth + upload

def test_oauth_flow_state_and_disconnect(tmp_path):
    fake = FakeGoogle()
    acc = connected_account(tmp_path, fake)
    st = acc.status()
    assert st["connected"] and st["channel"]["title"] == "레전드 테스트 채널"
    assert oct(acc.path.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(YouTubeError):
        acc.finish_auth("code-ok", "forged-state")
    acc.disconnect()
    assert not acc.status()["connected"] and fake.revoked == ["rt-1"]


def test_resumable_upload_survives_errors(tmp_path):
    fake = FakeGoogle()
    acc = connected_account(tmp_path, fake)
    f = tmp_path / "v.mp4"
    f.write_bytes(bytes(range(256)) * 4096 + b"tail")  # ~1 MiB, not a chunk multiple
    fake.fail_puts, fake.drop_puts, fake.expire_once = 1, 1, False
    seen = []
    video = acc.upload(f, VideoMeta("제목", publish_at=future()), seen.append, chunk_size=256 * 1024)
    assert fake.videos[video["id"]]["bytes"] == f.stat().st_size
    assert video["status"]["privacyStatus"] == "private" and "publishAt" in video["status"]
    assert seen[-1] == 1.0 and seen == sorted(seen)

    fake.expire_once = True  # access token revoked mid-flight → refresh and continue
    video2 = acc.upload(f, VideoMeta("두 번째"), chunk_size=512 * 1024)
    assert fake.videos[video2["id"]]["bytes"] == f.stat().st_size


def test_final_chunk_response_lost(tmp_path):
    fake = FakeGoogle()
    acc = connected_account(tmp_path, fake)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x" * 1000)
    fake.drop_puts = 1  # the only chunk arrives but its response is lost
    video = acc.upload(f, VideoMeta("제목"), chunk_size=256 * 1024)
    assert len(fake.videos) == 1 and video["id"] in fake.videos


def test_quota_error_is_friendly(tmp_path):
    fake = FakeGoogle()
    acc = connected_account(tmp_path, fake)
    fake.quota_exceeded = True
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x" * 10)
    with pytest.raises(YouTubeError, match="한도"):
        acc.upload(f, VideoMeta("제목"))


# ---------------------------------------------------------------- through the web app

needs_media = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")


@pytest.fixture(scope="module")
def app_env(tmp_path_factory):
    pytest.importorskip("skimage")
    from fastapi.testclient import TestClient
    from generate_sample import DIALOGUE, build
    from make_demo_video import main as make_video

    from webapp.server import create_app

    d = tmp_path_factory.mktemp("pub")
    t = build(DIALOGUE[:14])
    t.save(d / "t.json")
    make_video(str(d / "v.mp4"), duration=t.duration)
    (d / "client.json").write_text(json.dumps({"web": {"client_id": "cid", "client_secret": "secret"}}))
    fake = FakeGoogle()
    mp = pytest.MonkeyPatch()
    mp.setenv("LEGEND_GOOGLE_BASE", FAKE)
    app = create_app(d / "data", google_client_secret=str(d / "client.json"),
                     youtube_http=httpx.Client(transport=httpx.MockTransport(fake.handle)))
    app.state.youtube.sleep = lambda s: None
    client = TestClient(app)
    with open(d / "v.mp4", "rb") as v, open(d / "t.json", "rb") as tf:
        r = client.post("/api/projects", files={"video": ("v.mp4", v, "video/mp4"), "transcript": ("t.json", tf, "application/json")},
                        data={"length": "short", "top": "3", "selector": "heuristic"})
    pid = r.json()["id"]
    assert app.state.jobs.wait(client.get(f"/api/projects/{pid}").json()["job_id"])["status"] == "done"
    yield SimpleNamespace(client=client, app=app, fake=fake, pid=pid)
    mp.undo()


@needs_media
def test_publish_requires_connection_then_connects(app_env):
    c, pid = app_env.client, app_env.pid
    assert c.get("/api/youtube/status").json() == {"configured": True, "connected": False, "channel": None,
                                                   "redirect_uri": "http://127.0.0.1:8000/oauth/google/callback"}
    assert c.post(f"/api/projects/{pid}/clips/c1/publish").status_code == 400

    r = c.get("/oauth/google/start", params={"return_to": "//evil.example"}, follow_redirects=False)
    auth = urlparse(r.headers["location"])
    assert auth.netloc == "fake-google" and "youtube.upload" in parse_qs(auth.query)["scope"][0]
    _, h, _ = app_env.fake.respond("GET", auth.path, parse_qs(auth.query), {}, b"")
    cb = urlparse(h["location"])
    r = c.get(f"{cb.path}?{cb.query}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"  # off-site return_to was dropped
    assert c.get("/api/youtube/status").json()["channel"]["id"] == "UC-test"
    bad = c.get("/oauth/google/callback", params={"code": "x", "state": "nope"})
    assert bad.status_code == 400 and "만료" in bad.text


@needs_media
def test_draft_and_single_publish(app_env):
    c, pid, fake = app_env.client, app_env.pid, app_env.fake
    draft = c.post(f"/api/projects/{pid}/clips/c1/publish/draft", params={"ai": "false"}).json()["draft"]
    assert draft["title"] and "#Shorts" in draft["description"] and draft["privacy"] == "public"
    assert c.patch(f"/api/projects/{pid}/clips/c1/publish/draft", json={"title": "가" * 101}).status_code == 400
    assert c.patch(f"/api/projects/{pid}/clips/c1/publish/draft", json={"privacy": "friends"}).status_code == 400
    r = c.patch(f"/api/projects/{pid}/clips/c1/publish/draft", json={"title": "부업의 진실", "privacy": "unlisted", "tags": ["#부업", " 돈 "]})
    assert r.json()["draft"]["tags"] == ["부업", "돈"]

    jid = c.post(f"/api/projects/{pid}/clips/c1/publish").json()["job_id"]
    job = app_env.app.state.jobs.wait(jid, timeout=180)
    assert job["status"] == "done", job
    pub = c.get(f"/api/projects/{pid}").json()["clips"][0]["publish"]
    assert pub["status"] == "uploaded" and pub["url"] == f"https://youtube.com/shorts/{pub['youtube_id']}"
    v = fake.videos[pub["youtube_id"]]
    assert v["snippet"]["title"] == "부업의 진실" and v["status"]["privacyStatus"] == "unlisted"
    assert v["bytes"] == (app_env.app.state.store.dir(pid) / "renders" / pub["render_file"]).stat().st_size
    assert c.post(f"/api/projects/{pid}/clips/c1/publish").status_code == 409  # already on YouTube


@needs_media
def test_bulk_schedule(app_env):
    c, pid, fake = app_env.client, app_env.pid, app_env.fake
    assert c.post(f"/api/projects/{pid}/publish/bulk", json={"clip_ids": ["c2"], "first_at": future(0)}).status_code == 400
    first = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    r = c.post(f"/api/projects/{pid}/publish/bulk",
               json={"clip_ids": ["c2", "c3"], "first_at": first.isoformat(), "interval_hours": 24, "use_ai": False})
    assert r.status_code == 200, r.text
    job = app_env.app.state.jobs.wait(r.json()["job_id"], timeout=300)
    assert job["status"] == "done" and job["result"]["ok"] == 2, job
    clips = {x["id"]: x for x in c.get(f"/api/projects/{pid}").json()["clips"]}
    at2 = datetime.fromisoformat(clips["c2"]["publish"]["publish_at"].replace("Z", "+00:00"))
    at3 = datetime.fromisoformat(clips["c3"]["publish"]["publish_at"].replace("Z", "+00:00"))
    assert at2 == first and at3 - at2 == timedelta(hours=24)
    for cid in ("c2", "c3"):
        assert clips[cid]["publish"]["status"] == "scheduled"
        assert fake.videos[clips[cid]["publish"]["youtube_id"]]["status"]["privacyStatus"] == "private"  # until publishAt
        assert clips[cid]["publish"]["draft"]["privacy"] == "public"
