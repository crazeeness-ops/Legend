"""Creator's own channel: most-viewed videos and their audience retention."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from highlight_engine.render.media import probe
from highlight_engine.signals import retention_residuals

from .youtube import AccountRegistry, YouTubeAccount, YouTubeError

CACHE_SEC = 600
SHORT_MAX_SEC = 180  # YouTube treats vertical videos up to 3 minutes as Shorts


def register(app: FastAPI, registry: AccountRegistry) -> None:
    cache: dict[str, tuple[float, list[dict]]] = {}

    def account(uid: str) -> YouTubeAccount:
        acc = registry.for_user(uid)
        if not acc.load():
            raise HTTPException(400, "먼저 YouTube 채널을 연결하세요")
        return acc

    def videos(uid: str, refresh: bool = False) -> list[dict]:
        hit = cache.get(uid)
        if hit and not refresh and time.time() - hit[0] < CACHE_SEC:
            return hit[1]
        try:
            items = account(uid).my_videos()
        except YouTubeError as e:
            raise HTTPException(502, str(e))
        for v in items:
            v["is_short"] = v["duration"] <= SHORT_MAX_SEC
            v["like_rate"] = round(v["likes"] / v["views"] * 100, 2) if v["views"] else 0.0
        cache[uid] = (time.time(), items)
        return items

    @app.get("/api/channel/videos")
    def channel_videos(request: Request, refresh: bool = False, include_shorts: bool = False):
        items = videos(request.state.user["id"], refresh)
        return [v for v in items if include_shorts or not v["is_short"]]

    @app.get("/api/channel/videos/{video_id}/retention")
    def channel_retention(request: Request, video_id: str):
        uid = request.state.user["id"]
        v = next((x for x in videos(uid) if x["id"] == video_id), None)
        if not v:
            raise HTTPException(404, "내 채널의 영상이 아닙니다")
        try:
            curve = account(uid).retention(video_id)
        except YouTubeError as e:
            return {"curve": [], "peaks": [], "message": str(e)}
        xs, zs = retention_residuals(curve)
        peaks = sorted(((float(z), float(x)) for x, z in zip(xs, zs)), reverse=True)[:3]
        return {"curve": curve, "peaks": [{"at_sec": round(x * v["duration"], 1), "strength": round(z, 2)}
                                          for z, x in peaks if z > 0.8],
                "message": None if curve else "아직 시청 유지율 데이터가 충분하지 않은 영상입니다"}

    def own_video_source(uid: str, video_id: str, file_path: Path) -> tuple[dict, dict]:
        """Verify the upload is this creator's video (by channel + duration) and attach retention."""
        v = next((x for x in videos(uid) if x["id"] == video_id), None)
        if not v:
            raise HTTPException(400, "선택한 영상이 연결된 내 채널의 영상이 아닙니다")
        file_dur = probe(str(file_path)).duration
        if abs(file_dur - v["duration"]) > max(5.0, v["duration"] * 0.02):
            raise HTTPException(400, f"올린 파일 길이({file_dur:.0f}초)가 선택한 영상({v['duration']:.0f}초)과 다릅니다. "
                                     "같은 영상의 원본 파일을 올려 주세요")
        try:
            curve = account(uid).retention(video_id)
        except YouTubeError:
            curve = []
        source = {"type": "own_channel", "video_id": video_id, "title": v["title"],
                  "url": f"https://youtu.be/{video_id}", "views": v["views"], "likes": v["likes"]}
        return source, {"retention": {"curve": curve, "fetched_at": time.time()} if curve else None}

    app.state.own_video_source = own_video_source
    app.state.channel_videos = videos
