"""Clipping marketplace.

Creators open a *campaign* on one of their own channel videos (ownership verified through
their connected channel) and upload the source file themselves, so clippers never download
from YouTube. Clippers join, make clips in the editor, and publish to their own channel;
the credit line is enforced, the campaign's length and deadline rules apply, and every
published clip becomes a *submission* the creator reviews. Earnings = views / 1000 × rate,
capped by the campaign budget (paid out manually for now).
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from highlight_engine.pipeline import LENGTH_PRESETS
from highlight_engine.render.media import probe

from .auth import QuotaError
from .db import DB, new_id
from .publish import CampaignHooks
from .youtube import AccountRegistry, YouTubeError

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


class JoinBody(BaseModel):
    length: str = "medium"
    top: int = Field(8, ge=1, le=30)
    selector: str = "claude"
    prompt: str = ""


class CampaignPatch(BaseModel):
    status: str | None = None
    budget: int | None = Field(None, ge=0)
    rules: str | None = None
    deadline: str | None = None


class Review(BaseModel):
    status: str


def parse_deadline(value: str) -> float:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "마감일 형식이 올바르지 않습니다")
    if dt.tzinfo is None:
        raise HTTPException(400, "마감일에 시간대 정보가 필요합니다")
    if dt.timestamp() <= time.time() + 3600:
        raise HTTPException(400, "마감일은 지금부터 1시간 이후여야 합니다")
    return dt.timestamp()


def compute_earnings(rate_per_1k: int, budget: int, subs: list[dict]) -> dict[str, int]:
    """Approved submissions, oldest first, earn views/1000 × rate until the budget runs out."""
    left = budget
    out = {}
    for s in sorted(subs, key=lambda x: x["created_at"]):
        if s["status"] != "approved":
            out[s["id"]] = 0
            continue
        earned = min(left, math.floor(s["views"] / 1000 * rate_per_1k))
        out[s["id"]] = earned
        left -= earned
    return out


def register(app: FastAPI, registry: AccountRegistry) -> None:
    db: DB = app.state.db
    store = app.state.store
    root = Path(store.root) / "campaigns"
    root.mkdir(parents=True, exist_ok=True)

    def get(cid: str) -> dict:
        c = db.one("SELECT * FROM campaigns WHERE id=?", cid)
        if not c:
            raise HTTPException(404, "캠페인을 찾을 수 없습니다")
        return c

    def is_open(c: dict) -> bool:
        return c["status"] == "open" and c["deadline"] > time.time()

    def summary(c: dict) -> dict:
        agg = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(views),0) AS views, COALESCE(SUM(CASE WHEN status='approved' "
                     "THEN earned ELSE 0 END),0) AS spent FROM submissions WHERE campaign_id=?", c["id"])
        owner = db.one("SELECT name FROM users WHERE id=?", c["owner_id"])
        return {**{k: v for k, v in c.items() if k != "source_file"}, "open": is_open(c),
                "owner_name": owner["name"] if owner else "", "submission_count": agg["n"],
                "total_views": agg["views"], "spent": agg["spent"], "budget_left": max(0, c["budget"] - agg["spent"]),
                "video_url": f"https://youtu.be/{c['video_id']}"}

    def recompute(cid: str) -> None:
        c = get(cid)
        subs = db.q("SELECT * FROM submissions WHERE campaign_id=?", cid)
        for sid, earned in compute_earnings(c["rate_per_1k"], c["budget"], subs).items():
            db.x("UPDATE submissions SET earned=? WHERE id=?", earned, sid)

    # ------------------------------------------------------------------ hooks used by publishing
    class Hooks(CampaignHooks):
        def _campaign(self, project: dict) -> dict | None:
            src = project.get("source") or {}
            return db.one("SELECT * FROM campaigns WHERE id=?", src["campaign_id"]) if src.get("type") == "campaign" else None

        def credit(self, project: dict) -> str | None:
            c = self._campaign(project)
            return c["credit"] if c else None

        def check(self, project: dict) -> None:
            src = project.get("source") or {}
            if src.get("type") != "campaign":
                return
            c = self._campaign(project)
            if not c or not is_open(c):
                raise YouTubeError("캠페인이 종료되었거나 마감되어 더 이상 게시할 수 없습니다")

        def submitted(self, project: dict, clip_id: str, pub: dict) -> None:
            c = self._campaign(project)
            if not c:
                return
            db.x("INSERT OR IGNORE INTO submissions (id,campaign_id,clipper_id,project_id,clip_id,youtube_id,url,title,"
                 "status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", new_id("s"), c["id"], project["owner"], project["id"],
                 clip_id, pub["youtube_id"], pub["url"], pub.get("title", ""), "pending", time.time())

    app.state.campaign_hooks = Hooks()

    # ------------------------------------------------------------------ creator side
    @app.post("/api/campaigns")
    def create_campaign(
        request: Request,
        video: UploadFile = File(...),
        transcript: UploadFile | None = File(None),
        video_id: str = Form(...),
        title: str = Form(...),
        rules: str = Form(""),
        credit: str = Form(""),
        rate_per_1k: int = Form(...),
        budget: int = Form(...),
        max_clip_sec: int = Form(60),
        deadline: str = Form(...),
    ):
        uid = request.state.user["id"]
        if not 0 <= rate_per_1k <= 1_000_000 or not 0 <= budget <= 1_000_000_000:
            raise HTTPException(400, "보상 단가와 예산을 확인하세요")
        if not 15 <= max_clip_sec <= 180:
            raise HTTPException(400, "최대 클립 길이는 15~180초여야 합니다")
        if not title.strip():
            raise HTTPException(400, "캠페인 제목을 입력하세요")
        dl = parse_deadline(deadline)
        v = next((x for x in app.state.channel_videos(uid) if x["id"] == video_id), None)
        if not v:
            raise HTTPException(400, "연결된 내 채널의 영상만 캠페인으로 열 수 있습니다")
        suffix = Path(video.filename or "").suffix.lower()
        if suffix not in VIDEO_EXT:
            raise HTTPException(400, "지원하지 않는 영상 형식입니다")
        cid = new_id("c")
        d = root / cid
        d.mkdir(parents=True)
        dest = d / f"source{suffix}"
        with dest.open("wb") as f:
            shutil.copyfileobj(video.file, f)
        try:
            dur = probe(str(dest)).duration
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(400, "영상 파일을 읽을 수 없습니다")
        if abs(dur - v["duration"]) > max(5.0, v["duration"] * 0.02):
            shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(400, f"올린 파일 길이({dur:.0f}초)가 선택한 영상({v['duration']:.0f}초)과 다릅니다")
        has_tr = False
        if transcript is not None and transcript.filename:
            data = transcript.file.read()
            try:
                json.loads(data)
            except ValueError:
                shutil.rmtree(d, ignore_errors=True)
                raise HTTPException(400, "자막 파일(json)을 읽을 수 없습니다")
            (d / "transcript.json").write_bytes(data)
            has_tr = True
        ch = registry.for_user(uid).load()["channel"]
        credit = credit.strip() or f"원본: {v['title']} ({ch['title']}) https://youtu.be/{video_id}"
        db.x("INSERT INTO campaigns (id,owner_id,title,rules,credit,video_id,video_title,channel_id,channel_title,thumbnail,"
             "duration,source_file,has_transcript,rate_per_1k,budget,max_clip_sec,deadline,status,created_at) "
             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             cid, uid, title.strip()[:80], rules.strip()[:2000], credit[:300], video_id, v["title"], ch["id"], ch["title"],
             v.get("thumbnail"), dur, dest.name, int(has_tr), rate_per_1k, budget, max_clip_sec, dl, "open", time.time())
        return summary(get(cid))

    @app.get("/api/campaigns")
    def list_campaigns(request: Request, scope: str = "open"):
        uid = request.state.user["id"]
        if scope == "mine":
            rows = db.q("SELECT * FROM campaigns WHERE owner_id=? ORDER BY created_at DESC", uid)
        else:
            rows = [c for c in db.q("SELECT * FROM campaigns WHERE status='open' ORDER BY created_at DESC") if is_open(c)]
        return [summary(c) for c in rows]

    @app.get("/api/campaigns/{cid}")
    def campaign_detail(request: Request, cid: str):
        uid = request.state.user["id"]
        c = get(cid)
        mine = c["owner_id"] == uid
        if not mine and not is_open(c) and not db.one("SELECT 1 FROM submissions WHERE campaign_id=? AND clipper_id=?", cid, uid):
            raise HTTPException(404, "캠페인을 찾을 수 없습니다")
        subs = db.q("SELECT s.*, u.name AS clipper_name FROM submissions s JOIN users u ON u.id=s.clipper_id "
                    "WHERE campaign_id=? " + ("" if mine else "AND clipper_id=? ") + "ORDER BY created_at DESC",
                    *((cid,) if mine else (cid, uid)))
        return {**summary(c), "is_owner": mine, "submissions": subs}

    @app.patch("/api/campaigns/{cid}")
    def patch_campaign(request: Request, cid: str, body: CampaignPatch):
        c = get(cid)
        if c["owner_id"] != request.state.user["id"]:
            raise HTTPException(404, "캠페인을 찾을 수 없습니다")
        if body.status is not None:
            if body.status not in ("open", "closed"):
                raise HTTPException(400, "상태가 올바르지 않습니다")
            db.x("UPDATE campaigns SET status=? WHERE id=?", body.status, cid)
        if body.budget is not None:
            spent = summary(c)["spent"]
            if body.budget < spent:
                raise HTTPException(400, f"이미 정산된 금액({spent:,}원)보다 적게 설정할 수 없습니다")
            db.x("UPDATE campaigns SET budget=? WHERE id=?", body.budget, cid)
        if body.rules is not None:
            db.x("UPDATE campaigns SET rules=? WHERE id=?", body.rules.strip()[:2000], cid)
        if body.deadline is not None:
            db.x("UPDATE campaigns SET deadline=? WHERE id=?", parse_deadline(body.deadline), cid)
        recompute(cid)
        return summary(get(cid))

    @app.post("/api/campaigns/{cid}/submissions/{sid}/review")
    def review(request: Request, cid: str, sid: str, body: Review):
        c = get(cid)
        if c["owner_id"] != request.state.user["id"]:
            raise HTTPException(404, "캠페인을 찾을 수 없습니다")
        if body.status not in ("approved", "rejected", "pending"):
            raise HTTPException(400, "상태가 올바르지 않습니다")
        if not db.x("UPDATE submissions SET status=? WHERE id=? AND campaign_id=?", body.status, sid, cid):
            raise HTTPException(404, "제출물을 찾을 수 없습니다")
        recompute(cid)
        return campaign_detail(request, cid)

    @app.post("/api/campaigns/{cid}/refresh")
    def refresh_stats(request: Request, cid: str):
        """Pull current views/likes for every submitted clip (public statistics)."""
        uid = request.state.user["id"]
        c = get(cid)
        if c["owner_id"] != uid:
            raise HTTPException(404, "캠페인을 찾을 수 없습니다")
        subs = db.q("SELECT id, youtube_id FROM submissions WHERE campaign_id=?", cid)
        if subs:
            try:
                stats = {v["id"]: v for v in registry.for_user(uid).video_details([s["youtube_id"] for s in subs])}
            except YouTubeError as e:
                raise HTTPException(502, str(e))
            now = time.time()
            for s in subs:
                v = stats.get(s["youtube_id"])
                if v:
                    db.x("UPDATE submissions SET views=?, likes=?, stats_at=? WHERE id=?", v["views"], v["likes"], now, s["id"])
        recompute(cid)
        return campaign_detail(request, cid)

    # ------------------------------------------------------------------ clipper side
    @app.post("/api/campaigns/{cid}/join")
    def join(request: Request, cid: str, body: JoinBody):
        uid = request.state.user["id"]
        c = get(cid)
        if c["owner_id"] == uid:
            raise HTTPException(400, "내 캠페인에는 참여할 수 없습니다. '내 채널'에서 직접 클립을 만드세요")
        if not is_open(c):
            raise HTTPException(400, "마감된 캠페인입니다")
        if body.length not in LENGTH_PRESETS or body.selector not in ("claude", "heuristic"):
            raise HTTPException(400, "잘못된 옵션입니다")
        d = root / cid
        source = {"type": "campaign", "campaign_id": cid, "campaign_title": c["title"], "max_clip_sec": c["max_clip_sec"],
                  "credit": c["credit"], "url": f"https://youtu.be/{c['video_id']}", "channel_title": c["channel_title"]}
        cfg = {"length": body.length, "top": body.top, "prompt": body.prompt.strip(), "selector": body.selector}
        try:
            return app.state.new_project(uid, f"[캠페인] {c['title']}", d / c["source_file"],
                                         transcript_src=str(d / "transcript.json") if c["has_transcript"] else None,
                                         cfg=cfg, source=source)
        except QuotaError:
            raise

    @app.get("/api/me/submissions")
    def my_submissions(request: Request):
        uid = request.state.user["id"]
        rows = db.q("SELECT s.*, c.title AS campaign_title, c.rate_per_1k, c.channel_title FROM submissions s "
                    "JOIN campaigns c ON c.id=s.campaign_id WHERE s.clipper_id=? ORDER BY s.created_at DESC", uid)
        return {"submissions": rows, "total_earned": sum(r["earned"] for r in rows if r["status"] == "approved"),
                "total_views": sum(r["views"] for r in rows)}
