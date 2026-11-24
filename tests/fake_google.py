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


class FakeGoogle:
    def __init__(self) -> None:
        self.tokens: set[str] = set()
        self.refresh_tokens = {"rt-1"}
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
        if path == "/o/oauth2/v2/auth":
            loc = f"{q['redirect_uri']}?{urlencode({'code': 'code-ok', 'state': q['state']})}"
            return 302, {"location": loc}, b""
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if form.get("grant_type") == "authorization_code" and form.get("code") == "code-ok":
                return self._json(200, {**self._issue(), "refresh_token": "rt-1",
                                        "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"})
            if form.get("grant_type") == "refresh_token" and form.get("refresh_token") in self.refresh_tokens:
                return self._json(200, self._issue())
            return self._json(400, {"error": "invalid_grant"})
        if path == "/revoke":
            self.revoked.append(q.get("token", ""))
            self.refresh_tokens.discard(q.get("token", ""))
            return 200, {}, b""

        auth = headers.get("authorization", "")
        if self.expire_once:
            self.expire_once = False
            self.tokens.clear()
        if auth.removeprefix("Bearer ") not in self.tokens:
            return self._json(401, {"error": {"code": 401, "message": "Invalid Credentials"}})

        if path == "/youtube/v3/channels":
            return self._json(200, {"items": [{"id": "UC-test", "snippet": {
                "title": "레전드 테스트 채널", "thumbnails": {"default": {"url": "https://example.invalid/a.jpg"}}}}]})
        if path == "/upload/youtube/v3/videos" and method == "POST":
            if self.quota_exceeded:
                return self._json(403, {"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
            sid = f"s{next(self._ids)}"
            self.sessions[sid] = {"meta": json.loads(body), "total": int(headers["x-upload-content-length"]), "data": b""}
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
                self.videos[vid] = {**s["video"], "bytes": len(s["data"])}
            return self._json(200, s["video"])
        hdr = {"range": f"bytes=0-{len(s['data']) - 1}"} if s["data"] else {}
        return 308, hdr, b""

    def _issue(self) -> dict:
        tok = f"at-{next(self._ids)}"
        self.tokens.add(tok)
        return {"access_token": tok, "expires_in": 3600, "token_type": "Bearer"}

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

        return Starlette(routes=[Route("/{path:path}", endpoint, methods=["GET", "POST", "PUT"])])


if __name__ == "__main__":
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9999)
    uvicorn.run(FakeGoogle().asgi(), host="127.0.0.1", port=ap.parse_args().port)
