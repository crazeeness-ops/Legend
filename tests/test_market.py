import json
import math
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "examples", ROOT / "tests"):
    sys.path.insert(0, str(p))

pytest.importorskip("fastapi")
pytest.importorskip("skimage")
if shutil.which("ffmpeg") is None:
    pytest.skip("ffmpeg missing", allow_module_level=True)

from fake_google import FakeGoogle  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from generate_sample import DIALOGUE, build  # noqa: E402
from make_demo_video import main as make_video  # noqa: E402

from highlight_engine.render.media import probe  # noqa: E402
from highlight_engine.signals import retention_residuals  # noqa: E402
from webapp.campaigns import compute_earnings  # noqa: E402
from webapp.server import create_app  # noqa: E402

CSRF = {"X-Requested-With": "legend"}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    d = tmp_path_factory.mktemp("market")
    t = build(DIALOGUE[:14])
    t.save(d / "t.json")
    make_video(str(d / "v.mp4"), duration=t.duration)
    secs = probe(str(d / "v.mp4")).duration
    (d / "client.json").write_text(json.dumps({"web": {"client_id": "cid", "client_secret": "secret"}}))
    fake = FakeGoogle()
    curve = [[i / 99, round(0.9 * math.exp(-1.2 * i / 99) + (0.3 if 40 <= i <= 48 else 0), 4)] for i in range(100)]
    fake.add_long_video("creator", "LONG1", "부업 풀버전", secs, 184_000, 7_300, curve)
    fake.add_long_video("creator", "LONG2", "다른 영상", 1260, 52_000, 1_900)
    fake.add_long_video("creator", "SHORT1", "숏츠", 40, 900_000, 9_000)
    mp = pytest.MonkeyPatch()
    mp.setenv("LEGEND_GOOGLE_BASE", "http://fake-google")
    mp.setattr("webapp.channel.SHORT_MAX_SEC", 50)  # our demo "long" video is ~66 s
    app = create_app(d / "data", google_client_secret=str(d / "client.json"), public_url="http://testserver",
                     youtube_http=httpx.Client(transport=httpx.MockTransport(fake.handle)))
    app.state.youtube.sleep = lambda s: None
    yield SimpleNamespace(app=app, fake=fake, dir=d, secs=secs)
    mp.undo()


def user(env, email, role="creator"):
    c = TestClient(env.app, headers=CSRF)
    r = c.post("/api/auth/signup", json={"email": email, "password": "password123", "name": email.split("@")[0], "role": role})
    assert r.status_code == 200, r.text
    return c


def connect(env, c, identity):
    env.fake.identity = identity
    auth = urlparse(c.get("/oauth/google/start", follow_redirects=False).headers["location"])
    _, h, _ = env.fake.respond("GET", auth.path, parse_qs(auth.query), {}, b"")
    cb = urlparse(h["location"])
    assert c.get(f"{cb.path}?{cb.query}", follow_redirects=False).status_code == 303
    st = c.get("/api/youtube/status").json()
    assert st["connected"] and st["analytics"]
    return st


def wait(env, jid, timeout=240):
    job = env.app.state.jobs.wait(jid, timeout=timeout)
    assert job["status"] == "done", job
    return job


def upload(c, env, **form):
    with open(env.dir / "v.mp4", "rb") as v, open(env.dir / "t.json", "rb") as t:
        return c.post("/api/projects", files={"video": ("v.mp4", v, "video/mp4"), "transcript": ("t.json", t, "application/json")},
                      data={"length": "short", "top": "3", "selector": "heuristic", **form})


# ---------------------------------------------------------------- accounts

def test_signup_login_logout_and_csrf(env):
    c = TestClient(env.app)
    assert c.get("/api/projects").status_code == 401
    assert c.post("/api/auth/signup", json={"email": "a@b.kr", "password": "password123"}).status_code == 403  # no CSRF header
    c.headers.update(CSRF)
    assert c.post("/api/auth/signup", json={"email": "bad", "password": "password123"}).status_code == 400
    assert c.post("/api/auth/signup", json={"email": "a@b.kr", "password": "short"}).status_code == 400
    assert c.post("/api/auth/signup", json={"email": "A@B.kr", "password": "password123"}).status_code == 200
    assert c.post("/api/auth/signup", json={"email": "a@b.kr", "password": "password123"}).status_code == 400  # duplicate
    me = c.get("/api/auth/me").json()
    assert me["user"]["email"] == "a@b.kr" and me["usage"]["limit_minutes"] == 60 and "pw_hash" not in me["user"]
    c.post("/api/auth/logout")
    assert c.get("/api/auth/me").status_code == 401
    assert c.post("/api/auth/login", json={"email": "a@b.kr", "password": "wrong-pass"}).status_code == 401
    assert c.post("/api/auth/login", json={"email": "a@b.kr", "password": "password123"}).status_code == 200
    assert c.patch("/api/auth/me", json={"role": "clipper"}).json()["user"]["role"] == "clipper"
    assert c.patch("/api/auth/me", json={"role": "admin"}).status_code == 400


def test_projects_are_private_and_quota_enforced(env):
    a, b = user(env, "owner@x.kr"), user(env, "other@x.kr")
    r = upload(a, env)
    assert r.status_code == 200, r.text
    pid, jid = r.json()["id"], r.json()["job_id"]
    wait(env, jid)
    assert b.get(f"/api/projects/{pid}").status_code == 404
    assert b.patch(f"/api/projects/{pid}/clips/c1", json={"title": "hack"}).status_code == 404
    assert b.get(f"/api/projects/{pid}/video").status_code == 404
    assert b.get(f"/api/jobs/{jid}").status_code == 404
    assert all(p["id"] != pid for p in b.get("/api/projects").json())
    usage = a.get("/api/auth/me").json()["usage"]
    assert usage["used_minutes"] == pytest.approx(env.secs / 60, abs=0.05)

    # fill the free plan, the next upload is refused with 402 and leaves no project behind
    uid = a.get("/api/auth/me").json()["user"]["id"]
    env.app.state.accounts.charge(uid, usage["remaining_minutes"] - 0.2, "manual")
    before = len(a.get("/api/projects").json())
    r = upload(a, env)
    assert r.status_code == 402 and "사용량" in r.json()["detail"]
    assert len(a.get("/api/projects").json()) == before
    env.app.state.accounts.refund(uid, "manual")
    assert a.get("/api/auth/me").json()["usage"]["remaining_minutes"] > 1


# ---------------------------------------------------------------- creator: own channel

def test_retention_detrending_finds_the_bump():
    curve = [[i / 99, 0.9 * math.exp(-1.2 * i / 99) + (0.3 if 40 <= i <= 48 else 0)] for i in range(100)]
    xs, zs = retention_residuals(curve)
    assert 0.38 < xs[zs.argmax()] < 0.5  # the bump, not the start of the video


def test_channel_videos_retention_and_own_video_project(env):
    c = user(env, "creator1@x.kr")
    assert c.get("/api/channel/videos").status_code == 400  # not connected yet
    connect(env, c, "creator")
    vids = c.get("/api/channel/videos").json()
    assert [v["id"] for v in vids] == ["LONG1", "LONG2"]  # shorts excluded, most viewed first
    assert vids[0]["views"] == 184_000 and vids[0]["like_rate"] == pytest.approx(3.97, abs=0.01)
    ret = c.get("/api/channel/videos/LONG1/retention").json()
    assert ret["peaks"] and abs(ret["peaks"][0]["at_sec"] - 0.44 * env.secs) < 0.1 * env.secs
    assert c.get("/api/channel/videos/NOPE/retention").status_code == 404

    assert upload(c, env, youtube_video_id="LONG2").status_code == 400  # file length doesn't match
    r = upload(c, env, youtube_video_id="LONG1")
    assert r.status_code == 200, r.text
    wait(env, r.json()["job_id"])
    p = c.get(f"/api/projects/{r.json()['id']}").json()
    assert p["source"]["type"] == "own_channel" and p["retention"]["curve"]
    assert any(cl["score_detail"].get("retention", 0) > 0.5 for cl in p["clips"])


# ---------------------------------------------------------------- clipping marketplace

def test_compute_earnings_respects_order_status_and_budget():
    subs = [{"id": "a", "status": "approved", "views": 3500, "created_at": 1},
            {"id": "b", "status": "pending", "views": 9000, "created_at": 2},
            {"id": "c", "status": "approved", "views": 4000, "created_at": 3}]
    assert compute_earnings(1000, 5000, subs) == {"a": 3500, "b": 0, "c": 1500}


def test_campaign_end_to_end(env):
    creator = user(env, "creator2@x.kr")
    connect(env, creator, "creator")
    clipper = user(env, "clipper@x.kr", role="clipper")
    connect(env, clipper, "clipper")
    deadline = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    def open_campaign(**over):
        form = {"video_id": "LONG1", "title": "부업 토크 클리핑", "rules": "욕설 금지", "rate_per_1k": "1000",
                "budget": "5000", "max_clip_sec": "30", "deadline": deadline, **over}
        with open(env.dir / "v.mp4", "rb") as v, open(env.dir / "t.json", "rb") as t:
            return creator.post("/api/campaigns", files={"video": ("v.mp4", v, "video/mp4"),
                                                         "transcript": ("t.json", t, "application/json")}, data=form)

    assert open_campaign(video_id="SOMEONE_ELSES").status_code == 400
    assert open_campaign(deadline="2020-01-01T00:00:00+00:00").status_code == 400
    r = open_campaign()
    assert r.status_code == 200, r.text
    camp = r.json()
    assert camp["credit"].startswith("원본: 부업 풀버전 (부업왕 김대표)") and camp["open"]
    assert clipper.post(f"/api/campaigns/{camp['id']}/join", json={"selector": "gpt"}).status_code == 400

    assert any(x["id"] == camp["id"] for x in clipper.get("/api/campaigns").json())
    assert creator.post(f"/api/campaigns/{camp['id']}/join", json={"selector": "heuristic"}).status_code == 400  # own campaign
    r = clipper.post(f"/api/campaigns/{camp['id']}/join", json={"selector": "heuristic", "length": "short", "top": 3})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    wait(env, r.json()["job_id"])
    proj = clipper.get(f"/api/projects/{pid}").json()
    assert proj["source"]["type"] == "campaign" and all(cl["end"] - cl["start"] <= 30.7 for cl in proj["clips"])
    c1 = proj["clips"][0]
    assert clipper.patch(f"/api/projects/{pid}/clips/c1", json={"end": c1["start"] + 40}).json()["detail"].endswith("(캠페인 규칙)")

    # the credit line can't be edited away
    d = clipper.post(f"/api/projects/{pid}/clips/c1/publish/draft", params={"ai": "false"}).json()["draft"]
    assert camp["credit"] in d["description"]
    clipper.patch(f"/api/projects/{pid}/clips/c1/publish/draft", json={"description": "출처 지움 #Shorts"})
    wait(env, clipper.post(f"/api/projects/{pid}/clips/c1/publish").json()["job_id"])
    yt1 = clipper.get(f"/api/projects/{pid}").json()["clips"][0]["publish"]["youtube_id"]
    assert camp["credit"] in env.fake.videos[yt1]["snippet"]["description"]
    assert env.fake.videos[yt1]["owner"] == "clipper"  # posted to the clipper's channel

    wait(env, clipper.post(f"/api/projects/{pid}/clips/c2/publish").json()["job_id"])
    yt2 = clipper.get(f"/api/projects/{pid}").json()["clips"][1]["publish"]["youtube_id"]

    det = creator.get(f"/api/campaigns/{camp['id']}").json()
    assert det["is_owner"] and [s["status"] for s in det["submissions"]] == ["pending", "pending"]
    s1 = next(s for s in det["submissions"] if s["youtube_id"] == yt1)
    s2 = next(s for s in det["submissions"] if s["youtube_id"] == yt2)
    assert clipper.post(f"/api/campaigns/{camp['id']}/submissions/{s1['id']}/review", json={"status": "approved"}).status_code == 404

    env.fake.set_views(yt1, 3500, 120)
    env.fake.set_views(yt2, 4000, 90)
    creator.post(f"/api/campaigns/{camp['id']}/submissions/{s1['id']}/review", json={"status": "approved"})
    creator.post(f"/api/campaigns/{camp['id']}/submissions/{s2['id']}/review", json={"status": "approved"})
    det = creator.post(f"/api/campaigns/{camp['id']}/refresh").json()
    earned = {s["youtube_id"]: s["earned"] for s in det["submissions"]}
    assert earned == {yt1: 3500, yt2: 1500} and det["spent"] == 5000 and det["budget_left"] == 0
    assert det["total_views"] == 7500

    mine = clipper.get("/api/me/submissions").json()
    assert mine["total_earned"] == 5000 and mine["total_views"] == 7500
    assert len(clipper.get(f"/api/campaigns/{camp['id']}").json()["submissions"]) == 2  # sees own only

    assert creator.patch(f"/api/campaigns/{camp['id']}", json={"budget": 100}).status_code == 400  # below spent
    creator.patch(f"/api/campaigns/{camp['id']}", json={"status": "closed"})
    assert all(x["id"] != camp["id"] for x in clipper.get("/api/campaigns").json())
    r = clipper.post(f"/api/projects/{pid}/clips/c3/publish")
    assert r.status_code == 400 and "종료" in r.json()["detail"]


def test_oauth_on_wrong_host_or_without_session(env):
    c = TestClient(env.app, headers=CSRF, base_url="http://localhost:8765")
    r = c.get("/oauth/google/start", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("http://testserver/oauth/google/start")
    anon = TestClient(env.app)
    r = anon.get("/oauth/google/callback?code=x&state=y")
    assert r.status_code == 401 and "로그인" in r.text and "text/html" in r.headers["content-type"]
