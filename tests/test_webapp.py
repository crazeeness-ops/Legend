import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

pytest.importorskip("fastapi")
pytest.importorskip("skimage")
if shutil.which("ffmpeg") is None:
    pytest.skip("ffmpeg missing", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402
from generate_sample import DIALOGUE, build  # noqa: E402
from make_demo_video import main as make_video  # noqa: E402

from highlight_engine.render.reframe import ReframePlan, ShotPath, apply_overrides  # noqa: E402
from webapp.server import create_app  # noqa: E402


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    t = build(DIALOGUE[:14])  # ~60s of dialogue
    t.save(d / "t.json")
    make_video(str(d / "v.mp4"), duration=t.duration)
    return d


@pytest.fixture(scope="module")
def client(media, tmp_path_factory):
    app = create_app(tmp_path_factory.mktemp("data"))
    with TestClient(app, headers={"X-Requested-With": "legend"}) as c:
        assert c.post("/api/auth/signup", json={"email": "editor@test.kr", "password": "password123"}).status_code == 200
        c.app_ref = app
        yield c


@pytest.fixture(scope="module")
def project(client, media):
    with open(media / "v.mp4", "rb") as v, open(media / "t.json", "rb") as t:
        r = client.post("/api/projects", files={"video": ("demo.mp4", v, "video/mp4"),
                                                 "transcript": ("t.json", t, "application/json")},
                        data={"name": "테스트", "length": "short", "top": "3", "selector": "heuristic"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    job = client.app_ref.state.jobs.wait(r.json()["job_id"] or client.get(f"/api/projects/{pid}").json()["job_id"])
    assert job["status"] == "done", job
    return client.get(f"/api/projects/{pid}").json()


def test_upload_rejects_bad_files(client):
    r = client.post("/api/projects", files={"video": ("x.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    r = client.post("/api/projects", files={"video": ("x.mp4", b"not a video", "video/mp4")})
    assert r.status_code == 400


def test_project_ready_with_clips_and_words(client, project):
    assert project["status"] == "ready" and project["clips"]
    c = project["clips"][0]
    assert c["id"] == "c1" and c["words"] and c["edits"]["preset"] == "legend"
    assert project["video_info"]["width"] == 1280
    assert any(p["id"] == project["id"] for p in client.get("/api/projects").json())


def test_video_supports_range_requests(client, project):
    r = client.get(f"/api/projects/{project['id']}/video", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100


def test_trim_title_and_validation(client, project):
    pid, c = project["id"], project["clips"][0]
    r = client.patch(f"/api/projects/{pid}/clips/c1", json={"start": c["start"] + 1, "title": "새 제목"})
    assert r.status_code == 200 and r.json()["title"] == "새 제목" and r.json()["start"] == pytest.approx(c["start"] + 1)
    assert client.patch(f"/api/projects/{pid}/clips/c1", json={"end": c["start"] + 2}).status_code == 400
    r = client.post(f"/api/projects/{pid}/clips/c1/reset")
    assert r.json()["start"] == c["original"]["start"]


def test_style_edits_are_validated(client, project):
    pid = project["id"]
    ok = client.patch(f"/api/projects/{pid}/clips/c1", json={"edits": {"preset": "neon", "style_overrides": {"size": 100}}})
    assert ok.status_code == 200 and ok.json()["edits"]["preset"] == "neon"
    for bad in ({"preset": "nope"}, {"style_overrides": {"color": "red"}}, {"style_overrides": {"sizee": 1}}, {"layout": "zoom"}):
        assert client.patch(f"/api/projects/{pid}/clips/c1", json={"edits": bad}).status_code == 400


def test_word_edit_flows_into_clip(client, project):
    pid, w = project["id"], project["clips"][0]["words"][0]
    r = client.patch(f"/api/projects/{pid}/words/{w['i']}", json={"text": "수정됨"})
    assert r.status_code == 200
    clip = client.get(f"/api/projects/{pid}").json()["clips"][0]
    assert clip["words"][0]["text"] == "수정됨" and clip["text"].startswith("수정됨")
    assert client.patch(f"/api/projects/{pid}/words/{w['i']}", json={"text": " "}).status_code == 400


def test_reframe_and_shot_override(client, project):
    pid = project["id"]
    plan = client.get(f"/api/projects/{pid}/clips/c2/reframe").json()
    assert plan["shots"] and plan["source"] == [1280, 720]
    first = plan["shots"][0]
    r = client.patch(f"/api/projects/{pid}/clips/c2",
                     json={"edits": {"shot_overrides": [{"start": first["start"], "layout": "track", "cx": 300}]}})
    assert r.status_code == 200
    plan2 = client.get(f"/api/projects/{pid}/clips/c2/reframe").json()
    s = plan2["shots"][0]
    assert s["layout"] == "track" and all(k[1] == pytest.approx(max(300, plan2["crop_w"] / 2)) for k in s["keyframes"])


def test_render_job_and_files(client, project):
    pid = project["id"]
    client.patch(f"/api/projects/{pid}/clips/c1", json={"edits": {"preset": "highlight_box", "style_overrides": {}}})
    jid = client.post(f"/api/projects/{pid}/clips/c1/render").json()["job_id"]
    job = client.app_ref.state.jobs.wait(jid, timeout=180)
    assert job["status"] == "done", job
    url = job["result"]["url"]
    r = client.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4" and len(r.content) > 10_000
    clip = client.get(f"/api/projects/{pid}").json()["clips"][0]
    assert clip["render"]["file"] == job["result"]["file"] and clip["render"]["style"]["mode"] == "box"
    assert client.get(f"/api/projects/{pid}/files/..%2Fproject.json").status_code == 404
    assert client.get(f"/api/projects/{pid}/clips/c1/thumb.jpg").headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/projects/{pid}/style-preview/neon.jpg").status_code == 200
    assert client.get(f"/api/projects/{pid}/style-preview/../x.jpg").status_code == 404


def test_frontend_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "app.js" in r.text
    assert client.get("/static/app.js").status_code == 200


def test_unknown_project_is_404(client):
    assert client.get("/api/projects/doesnotexist").status_code == 404
    assert client.get("/api/projects/..%2F..").status_code == 404


def test_apply_overrides_unit():
    plan = ReframePlan(1920, 1080, 608, [ShotPath(0, 3, "fit"), ShotPath(3, 6, "split", split=None)], 1, 0)
    apply_overrides(plan, [{"start": 0.1, "cx": 5000}, {"start": 3.0, "layout": "split"}, {"start": 9, "layout": "fit"}])
    assert plan.shots[0].layout == "track" and plan.shots[0].centers == [1920 - 304] * 2  # clamped
    assert plan.shots[1].layout == "split"  # unchanged: no split rects → override ignored, auto kept
