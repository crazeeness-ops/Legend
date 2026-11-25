"""A fake Google OAuth + YouTube Data API server for offline testing.

Use in tests through ``httpx.MockTransport(fake.handle)``, or run it live for a browser
walkthrough (auto-consents to OAuth):

    python tests/fake_google.py --port 9999
    LEGEND_GOOGLE_BASE=http://127.0.0.1:9999 GOOGLE_CLIENT_ID=x GOOGLE_CLIENT_SECRET=y python -m webapp serve
"""

from __future__ import annotations

import itertools
import json
from urllib.parse import parse_qs, urlencode

import httpx


CHANNELS = {
    "main": ("UC-test", "레전드 테스트 채널"),
    "creator": ("UC-creator", "부업왕 김대표"),
    "clipper": ("UC-clipper", "클립 공장"),
}


class FakeGoogle:
    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}  # access token -> identity
        self.refresh_tokens: dict[str, str] = {}  # refresh token -> identity
        self.identity = "main"  # who "logs in" on the next consent screen
        self.identity_queue: list[str] = []  # demo: successive logins become these channels in turn
        self.uploads: dict[str, list[str]] = {k: [] for k in CHANNELS}  # identity -> video ids
        self.stats: dict[str, dict] = {}  # video id -> {views, likes}
        self.long_videos: dict[str, dict] = {}  # video id -> {identity, title, seconds}
        self.retention: dict[str, list[list[float]]] = {}
        self.sessions: dict[str, dict] = {}
        self.videos: dict[str, dict] = {}
        self.fail_puts = 0  # next N chunk PUTs return 503
        self.drop_puts = 0  # next N chunk PUTs are received but the response is lost
        self.expire_once = False  # next authorised request gets 401
        self.quota_exceeded = False
        self.revoked: list[str] = []
        self._ids = itertools.count(1)

    # --------------------------------------------------------------- core
    def respond(self, method: str, path: str, query: dict, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
        q = {k: v[0] for k, v in query.items()}
        if path == "/_fake/identity" and method == "POST":  # live walkthroughs: choose who logs in next
            self.identity = json.loads(body)["name"]
            return self._json(200, {"identity": self.identity})
        if path == "/_fake/views" and method == "POST":
            d = json.loads(body)
            self.set_views(d["id"], int(d["views"]), int(d.get("likes", 0)))
            return self._json(200, d)
        if path == "/_fake/videos" and method == "GET":
            return self._json(200, {k: {"owner": v.get("owner"), "title": v["snippet"]["title"],
                                        "description": v["snippet"]["description"]} for k, v in self.videos.items()})
        if path == "/o/oauth2/v2/auth":
            who = q.get("login_hint") or (self.identity_queue.pop(0) if self.identity_queue else self.identity)
            loc = f"{q['redirect_uri']}?{urlencode({'code': f'code-ok-{who}', 'state': q['state']})}"
            return 302, {"location": loc}, b""
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            code = form.get("code", "")
            if form.get("grant_type") == "authorization_code" and code.startswith("code-ok-"):
                who = code.removeprefix("code-ok-")
                rt = f"rt-{who}-{next(self._ids)}"
                self.refresh_tokens[rt] = who
                return self._json(200, {**self._issue(who), "refresh_token": rt, "scope": " ".join([
                    "https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly",
                    "https://www.googleapis.com/auth/yt-analytics.readonly"])})
            if form.get("grant_type") == "refresh_token" and form.get("refresh_token") in self.refresh_tokens:
                return self._json(200, self._issue(self.refresh_tokens[form["refresh_token"]]))
            return self._json(400, {"error": "invalid_grant"})
        if path == "/revoke":
            self.revoked.append(q.get("token", ""))
            self.refresh_tokens.pop(q.get("token", ""), None)
            return 200, {}, b""

        auth = headers.get("authorization", "")
        if self.expire_once:
            self.expire_once = False
            self.tokens.clear()
        who = self.tokens.get(auth.removeprefix("Bearer "))
        if who is None:
            return self._json(401, {"error": {"code": 401, "message": "Invalid Credentials"}})
        ch_id, ch_title = CHANNELS[who]

        if path == "/youtube/v3/channels":
            return self._json(200, {"items": [{"id": ch_id, "snippet": {
                "title": ch_title, "thumbnails": {"default": {"url": "https://example.invalid/a.jpg"}}},
                "contentDetails": {"relatedPlaylists": {"uploads": f"UU-{who}"}}}]})
        if path == "/youtube/v3/playlistItems":
            owner = q["playlistId"].removeprefix("UU-")
            ids = [v for v, meta in self.long_videos.items() if meta["identity"] == owner] + self.uploads.get(owner, [])
            return self._json(200, {"items": [{"contentDetails": {"videoId": v}} for v in ids]})
        if path == "/youtube/v3/videos" and method == "GET":
            items = []
            for vid in q["id"].split(","):
                meta = self.long_videos.get(vid) or self.videos.get(vid)
                if not meta:
                    continue
                owner = meta.get("identity") or meta.get("owner")
                secs = meta.get("seconds", 30)
                st = self.stats.get(vid, {"views": 0, "likes": 0})
                items.append({"id": vid, "snippet": {"title": meta.get("title") or meta["snippet"]["title"],
                                                      "channelId": CHANNELS[owner][0], "channelTitle": CHANNELS[owner][1],
                                                      "publishedAt": "2026-01-01T00:00:00Z",
                                                      "thumbnails": {"medium": {"url": f"https://example.invalid/{vid}.jpg"}}},
                              "statistics": {"viewCount": str(st["views"]), "likeCount": str(st["likes"]), "commentCount": "3"},
                              "contentDetails": {"duration": f"PT{int(secs // 60)}M{int(secs % 60)}S"}})
            return self._json(200, {"items": items})
        if path == "/v2/reports":
            vid = q["filters"].removeprefix("video==")
            if vid not in self.long_videos or self.long_videos[vid]["identity"] != who:
                return self._json(403, {"error": {"code": 403, "message": "forbidden", "errors": [{"reason": "forbidden"}]}})
            return self._json(200, {"rows": self.retention.get(vid, [])})
        if path == "/upload/youtube/v3/videos" and method == "POST":
            if self.quota_exceeded:
                return self._json(403, {"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
            sid = f"s{next(self._ids)}"
            self.sessions[sid] = {"meta": json.loads(body), "total": int(headers["x-upload-content-length"]), "data": b"",
                                  "owner": who}
            return 200, {"location": f"http://fake-google/upload/session/{sid}"}, b""
        if path.startswith("/upload/session/") and method == "PUT":
            s = self.sessions[path.rsplit("/", 1)[1]]
            rng = headers.get("content-range", "")
            if rng.startswith("bytes */"):
                return self._progress(s)
            if self.fail_puts:
                self.fail_puts -= 1
                return self._json(503, {"error": {"code": 503, "message": "backend error"}})
            start = int(rng.split(" ")[1].split("-")[0])
            if start != len(s["data"]):
                return self._json(400, {"error": {"message": f"bad offset {start} != {len(s['data'])}"}})
            s["data"] += body
            if self.drop_puts:
                self.drop_puts -= 1
                return 599, {}, b""  # signals the transport to raise (response lost)
            return self._progress(s)
        return self._json(404, {"error": {"message": f"no route {method} {path}"}})

    def _progress(self, s: dict) -> tuple[int, dict, bytes]:
        if len(s["data"]) >= s["total"]:
            if "video" not in s:
                vid = f"vid{next(self._ids)}"
                status = dict(s["meta"]["status"])
                s["video"] = {"id": vid, "snippet": s["meta"]["snippet"], "status": {**status, "uploadStatus": "uploaded"}}
                self.videos[vid] = {**s["video"], "bytes": len(s["data"]), "owner": s.get("owner", "main")}
                self.uploads.setdefault(s.get("owner", "main"), []).append(vid)
            return self._json(200, s["video"])
        hdr = {"range": f"bytes=0-{len(s['data']) - 1}"} if s["data"] else {}
        return 308, hdr, b""

    def _issue(self, who: str) -> dict:
        tok = f"at-{next(self._ids)}"
        self.tokens[tok] = who
        return {"access_token": tok, "expires_in": 3600, "token_type": "Bearer"}

    # --------------------------------------------------------------- test setup helpers
    def add_long_video(self, identity: str, vid: str, title: str, seconds: float, views: int, likes: int,
                       retention: list[list[float]] | None = None) -> None:
        self.long_videos[vid] = {"identity": identity, "title": title, "seconds": seconds}
        self.stats[vid] = {"views": views, "likes": likes}
        if retention:
            self.retention[vid] = retention

    def set_views(self, vid: str, views: int, likes: int = 0) -> None:
        self.stats[vid] = {"views": views, "likes": likes}

    @staticmethod
    def _json(code: int, data: dict) -> tuple[int, dict, bytes]:
        return code, {"content-type": "application/json"}, json.dumps(data).encode()

    # --------------------------------------------------------------- adapters
    def handle(self, request: httpx.Request) -> httpx.Response:
        code, headers, body = self.respond(request.method, request.url.path, parse_qs(request.url.query.decode()),
                                           {k.lower(): v for k, v in request.headers.items()}, request.content)
        if code == 599:
            raise httpx.ReadError("connection reset", request=request)
        return httpx.Response(code, headers=headers, content=body)

    def asgi(self):
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import Response
        from starlette.routing import Route

        async def endpoint(request: Request) -> Response:
            body = await request.body()
            code, headers, content = self.respond(request.method, request.url.path, parse_qs(request.url.query),
                                                  {k.lower(): v for k, v in request.headers.items()}, body)
            if code == 599:
                code = 503
            if code == 200 and "location" in headers:  # make session URLs point at this live server
                headers["location"] = headers["location"].replace("http://fake-google", str(request.base_url).rstrip("/"))
            return Response(content, status_code=code, headers=headers)

        return Starlette(routes=[Route("/{path:path}", endpoint, methods=["GET", "POST", "PUT"])])  # noqa


if __name__ == "__main__":
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--demo-video-seconds", type=float, default=0, help="give 'creator' a long video of this length")
    args = ap.parse_args()
    fake = FakeGoogle()
    if args.demo_video_seconds:
        import math
        curve = [[i / 99, round(0.9 * math.exp(-1.2 * i / 99) + (0.25 if 40 <= i <= 48 else 0), 4)] for i in range(100)]
        fake.add_long_video("creator", "LONG1", "직장인 부업 월 100만 원, 진짜 가능할까? (풀버전)", args.demo_video_seconds,
                            184_000, 7_300, curve)
        fake.add_long_video("creator", "LONG2", "부업 실패담 TOP 5", 1260, 52_000, 1_900)
    uvicorn.run(fake.asgi(), host="127.0.0.1", port=args.port)
