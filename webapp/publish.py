"""Routes: YouTube channel connection, Shorts metadata drafts, single and scheduled bulk publishing."""

from __future__ import annotations

import html
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import metadata
from .youtube import PRIVACY, AccountRegistry, VideoMeta, YouTubeAccount, YouTubeError


class DraftPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    privacy: str | None = None
    publish_at: str | None = None  # ISO 8601 with timezone, or "" to clear
    made_for_kids: bool | None = None


class PublishReq(BaseModel):
    force: bool = False  # upload again even if this clip is already on YouTube


class BulkReq(BaseModel):
    clip_ids: list[str] = Field(min_length=1, max_length=50)
    first_at: str | None = None  # first scheduled time; None = publish now
    interval_hours: float = Field(24, ge=0.25, le=24 * 30)
    privacy: str = "public"
    use_ai: bool = True


class CampaignHooks:
    """Campaign rules applied to clips made from a campaign source (set by campaigns.register)."""

    def credit(self, project: dict) -> str | None:
        return None

    def check(self, project: dict) -> None:
        pass

    def submitted(self, project: dict, clip_id: str, pub: dict) -> None:
        pass


def register(app: FastAPI, registry: AccountRegistry) -> None:
    store, jobs = app.state.store, app.state.jobs
    if not hasattr(app.state, "campaign_hooks"):
        app.state.campaign_hooks = CampaignHooks()

    def hooks() -> CampaignHooks:
        return app.state.campaign_hooks

    def lang_of(pid: str) -> str:
        return (store.get(pid).get("language") or "ko")[:2]

    def me(request: Request) -> YouTubeAccount:
        return registry.for_user(request.state.user["id"])

    def owner_account(pid: str) -> YouTubeAccount:
        return registry.for_user(store.owner_of(pid))

    def with_credit(pid: str, draft: dict) -> dict:
        credit = hooks().credit(store.get(pid))
        if credit and credit not in draft.get("description", ""):
            draft = {**draft, "description": (credit + "\n\n" + draft.get("description", "")).strip()}
        return draft

    # ------------------------------------------------------------------ channel
    @app.get("/api/youtube/status")
    def yt_status(request: Request):
        return me(request).status()

    @app.get("/oauth/google/start")
    def oauth_start(request: Request, return_to: str = "/"):
        if not return_to.startswith("/") or return_to.startswith("//"):
            return_to = "/"  # never redirect off-site
        try:
            return RedirectResponse(me(request).auth_url(return_to))
        except YouTubeError as e:
            raise HTTPException(400, str(e))

    @app.get("/oauth/google/callback")
    def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        try:
            if error:
                raise YouTubeError("Google 로그인이 취소되었습니다" if error == "access_denied" else f"Google 오류: {error}")
            return RedirectResponse(me(request).finish_auth(code, state), status_code=303)
        except YouTubeError as e:
            msg = html.escape(str(e))
            return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>연결 실패</title>"
                                f"<body style='font-family:sans-serif;background:#0f1115;color:#e8eaf0;padding:40px'>"
                                f"<h2>YouTube 연결에 실패했어요</h2><p>{msg}</p><a style='color:#ff4d6d' href='/'>돌아가기</a>",
                                status_code=400)

    @app.post("/api/youtube/disconnect")
    def yt_disconnect(request: Request):
        me(request).disconnect()
        return me(request).status()

    # ------------------------------------------------------------------ drafts
    def clip_and_view(pid: str, cid: str) -> tuple[dict, dict]:
        clip = store.find_clip(store.get(pid), cid)
        return clip, store.clip_view(pid, clip)

    def save_publish(pid: str, cid: str, **fields) -> dict:
        def fn(p: dict) -> None:
            c = store.find_clip(p, cid)
            c["publish"] = {**(c.get("publish") or {}), **fields}

        p = store.update(pid, fn)
        return store.find_clip(p, cid)["publish"]

    def default_draft(view: dict, use_ai: bool, lang: str, pid: str | None = None) -> dict:
        meta = metadata.generate(view, language=lang) if use_ai else {**metadata.heuristic(view), "source": "basic"}
        draft = {"title": meta["title"], "description": meta["description"], "tags": meta["tags"],
                 "privacy": "public", "publish_at": None, "made_for_kids": False, "source": meta["source"],
                 **({"warning": meta["warning"]} if meta.get("warning") else {})}
        return with_credit(pid, draft) if pid else draft

    @app.post("/api/projects/{pid}/clips/{cid}/publish/draft")
    def make_draft(pid: str, cid: str, ai: bool = True):
        _, view = clip_and_view(pid, cid)
        draft = default_draft(view, ai, lang_of(pid), pid)
        return save_publish(pid, cid, draft=draft)

    @app.patch("/api/projects/{pid}/clips/{cid}/publish/draft")
    def patch_draft(pid: str, cid: str, body: DraftPatch):
        clip, view = clip_and_view(pid, cid)
        draft = dict((clip.get("publish") or {}).get("draft") or default_draft(view, False, lang_of(pid), pid))
        changes = body.model_dump(exclude_none=True)
        if changes.get("publish_at") == "":
            changes["publish_at"] = None
        if "privacy" in changes and changes["privacy"] not in PRIVACY:
            raise HTTPException(400, "공개 설정이 올바르지 않습니다")
        if "tags" in changes:
            changes["tags"] = [t.strip().lstrip("#") for t in changes["tags"] if t.strip()]
        draft.update(changes)
        draft.pop("warning", None)
        try:
            to_meta(draft, lang_of(pid)).validate(now=datetime.now(timezone.utc) - timedelta(days=3650))
        except YouTubeError as e:  # schedule-in-the-past is checked at publish time, not while typing
            raise HTTPException(400, str(e))
        return save_publish(pid, cid, draft=draft)

    def to_meta(draft: dict, lang: str) -> VideoMeta:
        return VideoMeta(title=draft["title"], description=draft.get("description", ""), tags=draft.get("tags", []),
                         privacy=draft.get("privacy", "private"), publish_at=draft.get("publish_at"),
                         made_for_kids=bool(draft.get("made_for_kids")), language=lang)

    # ------------------------------------------------------------------ publishing
    def publish_clip(pid: str, cid: str, draft: dict, progress, base: float = 0.0, span: float = 1.0) -> dict:
        """Render if needed, then upload. Records status on the clip at every step."""
        account = owner_account(pid)
        hooks().check(store.get(pid))  # campaign still open, before its deadline
        draft = with_credit(pid, draft)  # campaign credit can't be edited away
        meta = to_meta(draft, lang_of(pid))
        meta.validate()
        save_publish(pid, cid, status="rendering", error=None, draft=draft)
        clip = store.find_clip(store.get(pid), cid)
        if not app.state.render_is_current(pid, clip):
            app.state.render_clip(pid, cid, lambda f, m=None: progress(base + span * 0.5 * (f or 0), "렌더링 중"))
            clip = store.find_clip(store.get(pid), cid)
        save_publish(pid, cid, status="uploading")
        path = store.dir(pid) / "renders" / clip["render"]["file"]
        try:
            video = account.upload(path, meta, lambda f: progress(base + span * (0.5 + 0.5 * f), "YouTube에 업로드 중"))
        except YouTubeError as e:
            save_publish(pid, cid, status="error", error=str(e))
            raise
        vid = video["id"]
        st = video.get("status", {})
        pub = save_publish(
            pid, cid, status="scheduled" if meta.publish_at else "uploaded", error=None, youtube_id=vid,
            url=f"https://youtube.com/shorts/{vid}", privacy=st.get("privacyStatus", meta.body()["status"]["privacyStatus"]),
            publish_at=meta.body()["status"].get("publishAt"), uploaded_at=time.time(),
            render_file=clip["render"]["file"], channel=(account.load() or {}).get("channel"))
        hooks().submitted(store.get(pid), cid, {**pub, "title": meta.title})
        return pub

    def require_ready(pid: str) -> None:
        account = owner_account(pid)
        if not account.configured:
            raise HTTPException(400, "Google OAuth가 설정되지 않았습니다 (설정 안내 참고)")
        if not account.load():
            raise HTTPException(400, "먼저 YouTube 채널을 연결하세요")

    @app.post("/api/projects/{pid}/clips/{cid}/publish")
    def publish(pid: str, cid: str, body: PublishReq = PublishReq()):
        require_ready(pid)
        try:
            hooks().check(store.get(pid))
        except YouTubeError as e:
            raise HTTPException(400, str(e))
        clip, view = clip_and_view(pid, cid)
        pub = clip.get("publish") or {}
        if pub.get("youtube_id") and not body.force:
            raise HTTPException(409, "이미 YouTube에 올린 클립입니다. 다시 올리려면 확인 후 진행하세요")
        if pub.get("status") in ("rendering", "uploading") and pub.get("job_id") and \
                (jobs.get(pub["job_id"]) or {}).get("status") in ("queued", "running"):
            raise HTTPException(409, "이미 업로드 중입니다")
        draft = with_credit(pid, pub.get("draft") or default_draft(view, True, lang_of(pid), pid))
        try:
            to_meta(draft, lang_of(pid)).validate()
        except YouTubeError as e:
            raise HTTPException(400, str(e))
        jid = jobs.submit("publish", lambda *, progress: publish_clip(pid, cid, draft, progress), project=pid, clip=cid,
                          owner=store.owner_of(pid))
        save_publish(pid, cid, status="queued", job_id=jid, draft=draft, error=None)
        return {"job_id": jid}

    @app.post("/api/projects/{pid}/publish/bulk")
    def publish_bulk(pid: str, body: BulkReq):
        require_ready(pid)
        project = store.get(pid)
        clips = [store.find_clip(project, cid) for cid in body.clip_ids]
        if body.privacy not in PRIVACY:
            raise HTTPException(400, "공개 설정이 올바르지 않습니다")
        times: list[str | None] = [None] * len(clips)
        if body.first_at:
            try:
                first = datetime.fromisoformat(body.first_at.replace("Z", "+00:00"))
                if first.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise HTTPException(400, "예약 시작 시간 형식이 올바르지 않습니다")
            if first <= datetime.now(timezone.utc) + timedelta(minutes=10):
                raise HTTPException(400, "예약 시작 시간은 지금부터 10분 이후여야 합니다")
            times = [(first + timedelta(hours=body.interval_hours * i)).astimezone(timezone.utc).isoformat()
                     for i in range(len(clips))]

        def run_bulk(*, progress) -> dict:
            results = []
            n = len(clips)
            for i, (c, when) in enumerate(zip(clips, times)):
                cid = c["id"]
                try:
                    view = store.clip_view(pid, store.find_clip(store.get(pid), cid))
                    existing = (store.find_clip(store.get(pid), cid).get("publish") or {}).get("draft")
                    draft = dict(existing or default_draft(view, body.use_ai, lang_of(pid), pid))
                    # VideoMeta.body() uploads scheduled videos as private + publishAt; YouTube makes them public then
                    draft.update(privacy="public" if when else body.privacy, publish_at=when)
                    progress(i / n, f"{i + 1}/{n} 처리 중: {view['title'][:24]}")
                    pub = publish_clip(pid, cid, draft, progress, base=i / n, span=1 / n)
                    results.append({"clip": cid, "ok": True, "url": pub["url"], "publish_at": pub.get("publish_at")})
                except Exception as e:  # keep going with the remaining clips
                    save_publish(pid, cid, status="error", error=str(e))
                    results.append({"clip": cid, "ok": False, "error": str(e)})
            ok = sum(r["ok"] for r in results)
            progress(1.0, f"완료: {ok}/{n}개 성공")
            return {"results": results, "ok": ok, "total": n}

        jid = jobs.submit("publish_bulk", run_bulk, project=pid, owner=store.owner_of(pid))
        for c, when in zip(clips, times):
            save_publish(pid, c["id"], status="queued", job_id=jid, error=None, planned_at=when)
        return {"job_id": jid, "schedule": [{"clip": c["id"], "publish_at": t} for c, t in zip(clips, times)]}
