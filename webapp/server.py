"""FastAPI backend for the clip editor."""

from __future__ import annotations

import copy
import os
import re
from html import escape as html_escape
from urllib.parse import urlparse
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import cv2
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from highlight_engine.pipeline import LENGTH_PRESETS, EngineConfig, run
from highlight_engine.render.captions import MODES, POSITIONS, PRESETS, preset_catalog, with_brand
from highlight_engine.render.compose import render_short
from highlight_engine.render.detect import HaarFaceDetector
from highlight_engine.render.media import probe
from highlight_engine.render.reframe import ReframePlan, analyze, apply_overrides

from .auth import CSRF_HEADER, CSRF_VALUE, PLANS, SESSION_COOKIE, SESSION_DAYS, Accounts, AuthError, QuotaError, public_user
from .db import DB
from .jobs import Jobs
from .store import NotFound, Store

STATIC = Path(__file__).parent / "static"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
MIN_CLIP = 3.0
MAX_CLIP = 180.0


class ClipPatch(BaseModel):
    start: float | None = None
    end: float | None = None
    title: str | None = None
    keywords: list[str] | None = None
    edits: dict | None = None


class WordPatch(BaseModel):
    text: str


class SignupBody(BaseModel):
    email: str
    password: str
    name: str = ""
    role: str = "creator"


class LoginBody(BaseModel):
    email: str
    password: str


class MePatch(BaseModel):
    name: str | None = None
    role: str | None = None


def clip_signature(clip: dict) -> str:
    """Hash of everything that changes the rendered video (range, text, style, layout)."""
    import hashlib
    import json

    payload = {k: clip.get(k) for k in ("start", "end", "title", "keywords", "edits")}
    payload["words"] = [(w["text"], w["start"]) for w in clip.get("words", [])]
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def make_selector(name: str):
    if name == "claude":
        from highlight_engine.llm import ClaudeSelector

        return ClaudeSelector()
    from highlight_engine.heuristic import HeuristicSelector

    return HeuristicSelector()


def create_app(data_dir: str | Path = "data", *, workers: int = 2, public_url: str = "http://127.0.0.1:8000",
               google_client_secret: str | None = None, youtube_http=None, toss_http=None,
               allow_mock_payments: bool = True, billing_interval: float = 0) -> FastAPI:
    store = Store(data_dir)
    jobs = Jobs(workers)
    db = DB(Path(data_dir) / "legend.db")
    accounts = Accounts(db)
    detector = HaarFaceDetector()
    plan_cache: dict[tuple, ReframePlan] = {}
    app = FastAPI(title="Legend Clips Editor")
    app.state.store, app.state.jobs, app.state.db, app.state.accounts = store, jobs, db, accounts
    secure_cookie = public_url.startswith("https://")
    public_host = urlparse(public_url).netloc

    @app.exception_handler(NotFound)
    def not_found(_, exc: NotFound):
        return JSONResponse({"detail": f"not found: {exc}"}, status_code=404)

    @app.exception_handler(QuotaError)
    def quota(_, exc: QuotaError):
        return JSONResponse({"detail": str(exc)}, status_code=402)

    # ------------------------------------------------------------------ access control
    project_path = re.compile(r"^/api/projects/([^/]+)")
    job_path = re.compile(r"^/api/jobs/([^/]+)")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        request.state.user = accounts.user_for_session(request.cookies.get(SESSION_COOKIE))
        if path.startswith("/api/webhooks/"):
            return await call_next(request)  # verified by re-reading the payment from Toss, not by cookies
        if path.startswith(("/api/", "/oauth/", "/pay/", "/billing/")):
            if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
                return JSONResponse({"detail": "잘못된 요청입니다"}, status_code=403)
            redirect_path = path.startswith(("/oauth/", "/pay/", "/billing/"))
            if redirect_path and request.url.netloc != public_host:
                # cookies are per host: send the user to the address the OAuth redirect uses
                target = public_url.rstrip("/") + ("/#/channel" if path.endswith("/callback") else str(request.url.path)
                                                   + ("?" + request.url.query if request.url.query else ""))
                return RedirectResponse(target, status_code=303)
            if redirect_path and request.state.user is None:
                return HTMLResponse(
                    "<!doctype html><meta charset=utf-8><title>로그인 필요</title><body style='font-family:sans-serif;"
                    "background:#0f1115;color:#e8eaf0;padding:40px'><h2>로그인 세션을 찾을 수 없어요</h2>"
                    f"<p>Legend Clips에 로그인한 뒤 다시 시도해 주세요. 항상 <b>{html_escape(public_url)}</b> 주소로 접속해야 해요.</p>"
                    f"<a style='color:#ff4d6d' href='{html_escape(public_url)}/#/login'>로그인하러 가기</a>", status_code=401)
            if not path.startswith("/api/auth/") and request.state.user is None:
                return JSONResponse({"detail": "로그인이 필요합니다"}, status_code=401)
            uid = request.state.user["id"] if request.state.user else None
            m = project_path.match(path)
            if m:  # someone else's project is indistinguishable from a missing one
                try:
                    if store.owner_of(m.group(1)) != uid:
                        raise NotFound(m.group(1))
                except NotFound:
                    return JSONResponse({"detail": "not found"}, status_code=404)
            m = job_path.match(path)
            if m and (jobs.get(m.group(1)) or {}).get("owner") != uid:
                return JSONResponse({"detail": "not found"}, status_code=404)
        return await call_next(request)

    def user_of(request: Request) -> dict:
        return request.state.user

    # ------------------------------------------------------------------ auth
    def set_session(response: Response, uid: str) -> None:
        response.set_cookie(SESSION_COOKIE, accounts.create_session(uid), max_age=SESSION_DAYS * 86400,
                            httponly=True, samesite="lax", secure=secure_cookie, path="/")

    @app.post("/api/auth/signup")
    def signup(body: SignupBody, response: Response):
        try:
            u = accounts.signup(body.email, body.password, body.name, body.role)
        except AuthError as e:
            raise HTTPException(400, str(e))
        set_session(response, u["id"])
        return {"user": public_user(u)}

    @app.post("/api/auth/login")
    def login(body: LoginBody, response: Response):
        try:
            u = accounts.login(body.email, body.password)
        except AuthError as e:
            raise HTTPException(401, str(e))
        set_session(response, u["id"])
        return {"user": public_user(u)}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        accounts.end_session(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(request: Request):
        u = user_of(request)
        if not u:
            raise HTTPException(401, "로그인이 필요합니다")
        return {"user": public_user(u), "usage": accounts.usage(u["id"]), "plans": PLANS,
                "features": {"claude": bool(os.environ.get("ANTHROPIC_API_KEY"))}}

    @app.patch("/api/auth/me")
    def patch_me(request: Request, body: MePatch):
        u = user_of(request)
        if not u:
            raise HTTPException(401, "로그인이 필요합니다")
        try:
            return {"user": public_user(accounts.update(u["id"], name=body.name, role=body.role))}
        except AuthError as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------------ analysis job
    def analyze_project(pid: str, transcript_src: str | None, cfg: dict, *, progress) -> dict:
        store.update(pid, lambda p: p.update(status="analyzing", error=None))
        d = store.dir(pid)
        video = str(store.video_path(pid))
        lo, hi = LENGTH_PRESETS.get(cfg.get("length", "medium"), LENGTH_PRESETS["medium"])
        source = store.get(pid).get("source") or {}
        if source.get("max_clip_sec"):  # campaign rule
            hi = min(hi, float(source["max_clip_sec"]))
            lo = min(lo, max(MIN_CLIP, hi * 0.5))
        config = EngineConfig(min_len=lo, max_len=hi, top_n=int(cfg.get("top", 10)),
                              user_prompt=cfg.get("prompt") or None, language=cfg.get("language") or None)
        selector = make_selector(cfg.get("selector", "claude"))
        retention = (store.get(pid).get("retention") or {}).get("curve")
        if transcript_src:
            shutil.copyfile(transcript_src, d / "transcript.json")
            progress(0.3, "하이라이트 분석 중")
            result = run(str(d / "transcript.json"), selector, config, audio=video, retention=retention)
        else:
            progress(0.05, "음성 인식 중 (영상 길이에 따라 수 분 소요)")
            result = run(video, selector, config, transcript_out=str(d / "transcript.json"), retention=retention)
        store.set_clips(pid, result)
        store.update(pid, lambda p: p.update(status="ready", transcript="transcript.json"))
        return {"clips": len(result["clips"])}

    def start_analysis(pid: str, transcript_src: str | None, cfg: dict) -> str:
        owner = store.owner_of(pid)

        def fail(err: str) -> None:
            store.update(pid, lambda p: p.update(status="error", error=err))
            if owner:
                accounts.refund(owner, pid)

        jid = jobs.submit("analyze", analyze_project, pid, transcript_src, cfg, on_error=fail, project=pid, owner=owner)
        store.update(pid, lambda p: p.update(job_id=jid, status="queued", config=cfg))
        return jid

    def new_project(owner: str, name: str, video_path: Path, *, transcript_src: str | None, cfg: dict,
                    move: bool = False, source: dict | None = None, extra: dict | None = None) -> dict:
        """Create a project, charge the owner's monthly minutes, start analysis."""
        minutes = probe(str(video_path)).duration / 60
        project = store.create(name, video_path, move=move, owner=owner, source=source)
        if extra:
            store.update(project["id"], lambda p: p.update(extra))
        try:
            accounts.charge(owner, minutes, project["id"])
        except QuotaError:
            store.delete(project["id"])
            raise
        start_analysis(project["id"], transcript_src, cfg)
        return store.get(project["id"])

    app.state.start_analysis, app.state.new_project = start_analysis, new_project

    # ------------------------------------------------------------------ catalog
    @app.get("/api/presets")
    def presets():
        return {"presets": preset_catalog(), "modes": list(MODES), "positions": ["auto", *POSITIONS],
                "lengths": {k: list(v) for k, v in LENGTH_PRESETS.items()}}

    # ------------------------------------------------------------------ projects
    @app.get("/api/projects")
    def list_projects(request: Request):
        return store.list(owner=user_of(request)["id"])

    @app.post("/api/projects")
    def create_project(
        request: Request,
        video: UploadFile = File(...),
        transcript: UploadFile | None = File(None),
        name: str = Form(""),
        length: str = Form("medium"),
        top: int = Form(10),
        prompt: str = Form(""),
        selector: str = Form("claude"),
        youtube_video_id: str = Form(""),
    ):
        user = user_of(request)
        suffix = Path(video.filename or "").suffix.lower()
        if suffix not in VIDEO_EXT:
            raise HTTPException(400, f"지원하지 않는 영상 형식입니다: {suffix or '(없음)'}")
        if length not in LENGTH_PRESETS or selector not in ("claude", "heuristic") or not 1 <= top <= 30:
            raise HTTPException(400, "잘못된 옵션입니다")
        tmpdir = Path(tempfile.mkdtemp())
        vpath = tmpdir / f"upload{suffix}"
        with vpath.open("wb") as f:
            shutil.copyfileobj(video.file, f)
        try:
            probe(str(vpath))
        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise HTTPException(400, "영상 파일을 읽을 수 없습니다")
        tpath = None
        if transcript is not None and transcript.filename:
            tpath = tmpdir / "transcript.json"
            with tpath.open("wb") as f:
                shutil.copyfileobj(transcript.file, f)
        cfg = {"length": length, "top": top, "prompt": prompt.strip(), "selector": selector}
        source, extra = {"type": "upload"}, None
        if youtube_video_id:  # one of the creator's own channel videos: attach its audience retention
            source, extra = app.state.own_video_source(user["id"], youtube_video_id, vpath)
        try:
            return new_project(user["id"], name.strip() or Path(video.filename).stem, vpath,
                               transcript_src=str(tpath) if tpath else None, cfg=cfg, move=True, source=source,
                               extra=extra)
        except QuotaError:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        p = store.get(pid)
        if p["status"] == "ready":
            p["clips"] = [store.clip_view(pid, c) for c in p["clips"]]
        info = probe(str(store.video_path(pid)))
        p["video_info"] = {"width": info.width, "height": info.height, "fps": info.fps, "duration": info.duration}
        p["job"] = jobs.get(p["job_id"]) if p.get("job_id") else None
        return p

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        store.delete(pid)
        return {"ok": True}

    @app.get("/api/projects/{pid}/video")
    def project_video(pid: str):
        return FileResponse(store.video_path(pid))

    @app.get("/api/projects/{pid}/words")
    def words(pid: str, start: float, end: float):
        return store.words_in(pid, start, end)

    @app.patch("/api/projects/{pid}/words/{index}")
    def patch_word(pid: str, index: int, body: WordPatch):
        text = body.text.strip()
        if not text or len(text) > 60:
            raise HTTPException(400, "단어는 1~60자여야 합니다")
        return store.update_word(pid, index, text)

    # ------------------------------------------------------------------ clips
    @app.patch("/api/projects/{pid}/clips/{cid}")
    def patch_clip(pid: str, cid: str, body: ClipPatch):
        project = store.get(pid)
        duration = project["duration"] or 0
        max_len = min(MAX_CLIP, float((project.get("source") or {}).get("max_clip_sec") or MAX_CLIP))

        def fn(p: dict) -> None:
            c = store.find_clip(p, cid)
            start = c["start"] if body.start is None else round(max(0.0, body.start), 3)
            end = c["end"] if body.end is None else round(min(duration, body.end), 3)
            if not MIN_CLIP <= end - start <= max_len:
                raise HTTPException(400, f"클립 길이는 {MIN_CLIP:.0f}~{max_len:.0f}초여야 합니다"
                                    + (" (캠페인 규칙)" if max_len < MAX_CLIP else ""))
            c["start"], c["end"] = start, end
            if body.title is not None:
                c["title"] = body.title.strip()[:80]
            if body.keywords is not None:
                c["keywords"] = [k.strip() for k in body.keywords if k.strip()][:5]
            if body.edits is not None:
                edits = {**c["edits"], **body.edits}
                if edits["preset"] not in PRESETS or edits["layout"] not in ("auto", "track", "split", "fit"):
                    raise HTTPException(400, "잘못된 스타일/레이아웃입니다")
                try:
                    with_brand(edits["preset"], **edits.get("style_overrides", {}))
                except (ValueError, TypeError) as e:
                    raise HTTPException(400, f"자막 스타일 오류: {e}")
                c["edits"] = edits

        p = store.update(pid, fn)
        return store.clip_view(pid, store.find_clip(p, cid))

    @app.post("/api/projects/{pid}/clips/{cid}/reset")
    def reset_clip(pid: str, cid: str):
        def fn(p: dict) -> None:
            c = store.find_clip(p, cid)
            c["start"], c["end"] = c["original"]["start"], c["original"]["end"]
            c["edits"]["shot_overrides"] = []

        p = store.update(pid, fn)
        return store.clip_view(pid, store.find_clip(p, cid))

    def clip_plan(pid: str, clip: dict) -> ReframePlan:
        video = str(store.video_path(pid))
        key = (pid, round(clip["start"], 2), round(clip["end"], 2), clip["edits"]["layout"])
        if key not in plan_cache:
            info = probe(video)
            plan_cache[key] = analyze(video, clip["start"], clip["end"] - clip["start"], info, detector,
                                      layout=clip["edits"]["layout"])
        plan = copy.deepcopy(plan_cache[key])
        return apply_overrides(plan, clip["edits"].get("shot_overrides", []))

    @app.get("/api/projects/{pid}/clips/{cid}/reframe")
    def reframe(pid: str, cid: str):
        clip = store.find_clip(store.get(pid), cid)
        plan = clip_plan(pid, clip)
        return {**plan.to_dict(), "clip_start": clip["start"], "clip_end": clip["end"]}

    def render_clip(pid: str, cid: str, progress) -> dict:
        """Render the clip as it is now; returns the render record saved on the clip."""
        clip = store.clip_view(pid, store.find_clip(store.get(pid), cid))
        progress(0.02, "화면 분석 중")
        plan = clip_plan(pid, clip)
        ed = clip["edits"]
        style = with_brand(ed["preset"], **ed.get("style_overrides", {}))
        name = f"{cid}_{int(time.time() * 1000)}.mp4"
        out = store.dir(pid) / "renders" / name
        progress(0.05, "렌더링 중")
        meta = render_short(
            str(store.video_path(pid)), clip, str(out), style=style, show_title=ed.get("show_title", True),
            plan=plan, progress=lambda f: progress(0.05 + 0.93 * f, "렌더링 중"),
        )
        record = {"file": name, "status": "done", "rendered_at": time.time(), "start": clip["start"],
                  "end": clip["end"], "style": asdict(style), "signature": clip_signature(clip),
                  "timings": meta["timings_sec"]}

        def done(p: dict) -> None:
            c = store.find_clip(p, cid)
            old = (c.get("render") or {}).get("file")
            c["render"] = record
            if old and old != name:  # keep only the latest render per clip
                for f in (store.dir(pid) / "renders" / old, (store.dir(pid) / "renders" / old).with_suffix(".json")):
                    f.unlink(missing_ok=True)

        store.update(pid, done)
        return record

    def render_is_current(pid: str, clip: dict) -> bool:
        r = clip.get("render")
        return bool(r and (store.dir(pid) / "renders" / r["file"]).exists()
                    and r.get("signature") == clip_signature(store.clip_view(pid, clip)))

    app.state.render_clip, app.state.render_is_current = render_clip, render_is_current

    @app.post("/api/projects/{pid}/clips/{cid}/render")
    def render(pid: str, cid: str):
        store.find_clip(store.get(pid), cid)

        def do_render(*, progress) -> dict:
            r = render_clip(pid, cid, progress)
            return {"file": r["file"], "url": f"/api/projects/{pid}/files/{r['file']}", "timings": r["timings"]}

        jid = jobs.submit("render", do_render, project=pid, clip=cid, owner=store.owner_of(pid))
        return {"job_id": jid}

    @app.get("/api/projects/{pid}/files/{name}")
    def render_file(pid: str, name: str, download: bool = False):
        path = store.dir(pid) / "renders" / Path(name).name
        if not path.exists() or path.suffix != ".mp4":
            raise HTTPException(404)
        return FileResponse(path, filename=name if download else None)

    def _frame(pid: str, at: float, out: Path, width: int) -> Path:
        if not out.exists():
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(store.video_path(pid)),
                            "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out)], check=True)
        return out

    @app.get("/api/projects/{pid}/clips/{cid}/thumb.jpg")
    def thumb(pid: str, cid: str):
        c = store.find_clip(store.get(pid), cid)
        at = min(c["start"] + 1.0, c["end"])
        out = store.dir(pid) / "cache" / f"thumb_{cid}_{at:.1f}.jpg"
        return FileResponse(_frame(pid, at, out, 320), headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/projects/{pid}/style-preview/{preset}.jpg")
    def style_preview(pid: str, preset: str):
        if preset not in PRESETS:
            raise HTTPException(404)
        from highlight_engine.render.compose import _fit_with_blur
        from highlight_engine.render.preview import render_still

        out = store.dir(pid) / "cache" / f"style3_{preset}.jpg"
        if not out.exists():
            p = store.get(pid)
            at = p["clips"][0]["start"] + 1.0 if p.get("clips") else 1.0
            frame = cv2.imread(str(_frame(pid, at, store.dir(pid) / "cache" / "style_bg.jpg", 1280)))
            from dataclasses import replace

            st = replace(PRESETS[preset], position="auto")  # thumbnails compare the look, not the position
            still = render_still(st, _fit_with_blur(frame))[1190:1500, 40:1040]  # caption band
            cv2.imwrite(str(out), cv2.resize(still, (500, 155), interpolation=cv2.INTER_AREA),
                        [cv2.IMWRITE_JPEG_QUALITY, 85])
        return FileResponse(out, headers={"Cache-Control": "max-age=3600"})

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs/{jid}")
    def job(jid: str):
        j = jobs.get(jid)
        if not j:
            raise HTTPException(404)
        return j

    # ------------------------------------------------------------------ YouTube publishing
    from . import billing, campaigns, channel, publish, sales
    from .payments import DisabledPayments, MockPayments, TossProvider
    from .toss import TossClient, TossConfig
    from .youtube import AccountRegistry, OAuthConfig

    oauth = OAuthConfig.load(google_client_secret, public_url.rstrip("/") + "/oauth/google/callback")
    app.state.youtube = AccountRegistry(data_dir, oauth, http=youtube_http)
    publish.register(app, app.state.youtube)
    channel.register(app, app.state.youtube)
    campaigns.register(app, app.state.youtube)
    toss_cfg = TossConfig.from_env()
    toss = TossClient(toss_cfg, http=toss_http) if toss_cfg else None
    app.state.toss = toss
    app.state.payments = (TossProvider(toss, public_url) if toss
                          else MockPayments() if allow_mock_payments else DisabledPayments())
    sales.register(app, app.state.youtube)
    billing.register(app, toss, public_url, scheduler_interval=billing_interval)

    # ------------------------------------------------------------------ frontend
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
