"""YouTube channel connection (OAuth 2.0) and Shorts upload (YouTube Data API v3, resumable upload).

Setup (once, by the operator):
1. Google Cloud console → create a project → enable "YouTube Data API v3".
2. OAuth consent screen (External; add yourself as a test user while unverified).
3. Credentials → OAuth client ID → "Web application", authorised redirect URI
   ``http://127.0.0.1:8000/oauth/google/callback`` → download the JSON.
4. ``python -m webapp serve --google-client-secret client_secret.json``
   (or set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).

Notes
- Videos uploaded from an API project that has not passed YouTube's API compliance audit
  are locked to *private*. Apply for the audit before relying on public / scheduled posts.
- Default quota is 10,000 units/day; one upload costs ~1,600 units (≈6 uploads/day).
- Vertical videos up to 3 minutes are classified as Shorts automatically.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import httpx

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
PRIVACY = ("public", "unlisted", "private")
CHUNK = 8 * 1024 * 1024  # must be a multiple of 256 KiB
MAX_RETRIES = 6


class YouTubeError(Exception):
    """Error with a user-facing (Korean) message."""


@dataclass
class Endpoints:
    auth: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token: str = "https://oauth2.googleapis.com/token"
    revoke: str = "https://oauth2.googleapis.com/revoke"
    api: str = "https://www.googleapis.com/youtube/v3"
    upload: str = "https://www.googleapis.com/upload/youtube/v3/videos"

    @classmethod
    def from_env(cls) -> "Endpoints":
        """LEGEND_GOOGLE_BASE points every endpoint at a fake server (local end-to-end tests)."""
        base = os.environ.get("LEGEND_GOOGLE_BASE")
        if not base:
            return cls()
        base = base.rstrip("/")
        return cls(f"{base}/o/oauth2/v2/auth", f"{base}/token", f"{base}/revoke", f"{base}/youtube/v3",
                   f"{base}/upload/youtube/v3/videos")


@dataclass
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def load(cls, client_secret_file: str | None, redirect_uri: str) -> "OAuthConfig | None":
        if client_secret_file:
            data = json.loads(Path(client_secret_file).read_text())
            web = data.get("web") or data.get("installed") or data
            return cls(web["client_id"], web["client_secret"], redirect_uri)
        cid, secret = os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_CLIENT_SECRET")
        return cls(cid, secret, redirect_uri) if cid and secret else None


# --------------------------------------------------------------------------- metadata

@dataclass
class VideoMeta:
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    privacy: str = "private"
    publish_at: str | None = None  # ISO 8601 UTC; requires privacy="private" at upload time
    made_for_kids: bool = False
    category_id: str = "22"  # People & Blogs
    language: str = "ko"

    def validate(self, now: datetime | None = None) -> None:
        t = self.title.strip()
        if not t:
            raise YouTubeError("제목을 입력하세요")
        if len(t) > 100:
            raise YouTubeError("제목은 100자 이하여야 합니다")
        if "<" in t or ">" in t or "<" in self.description or ">" in self.description:
            raise YouTubeError("제목과 설명에는 < > 문자를 쓸 수 없습니다")
        if len(self.description.encode()) > 5000:
            raise YouTubeError("설명은 5000바이트 이하여야 합니다")
        if sum(len(x) + 1 for x in self.tags) > 500:
            raise YouTubeError("태그는 모두 합쳐 500자 이하여야 합니다")
        if self.privacy not in PRIVACY:
            raise YouTubeError("공개 설정이 올바르지 않습니다")
        if self.publish_at:
            when = parse_time(self.publish_at)
            if when <= (now or datetime.now(timezone.utc)):
                raise YouTubeError("예약 시간은 현재보다 미래여야 합니다")

    def body(self) -> dict:
        status: dict = {"selfDeclaredMadeForKids": self.made_for_kids, "embeddable": True}
        if self.publish_at:
            status["privacyStatus"] = "private"  # YouTube makes it public at publishAt
            status["publishAt"] = parse_time(self.publish_at).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        else:
            status["privacyStatus"] = self.privacy
        return {
            "snippet": {
                "title": self.title.strip(), "description": self.description, "tags": self.tags,
                "categoryId": self.category_id, "defaultLanguage": self.language,
                "defaultAudioLanguage": self.language,
            },
            "status": status,
        }


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise YouTubeError("예약 시간에 시간대 정보가 필요합니다")
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- account / tokens

class YouTubeAccount:
    """Connected channel + OAuth tokens, stored in ``<data>/youtube/account.json`` (mode 600)."""

    def __init__(self, data_dir: str | Path, config: OAuthConfig | None, endpoints: Endpoints | None = None,
                 http: httpx.Client | None = None) -> None:
        self.path = Path(data_dir) / "youtube" / "account.json"
        self.config = config
        self.ep = endpoints or Endpoints.from_env()
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
        self._lock = threading.Lock()
        self._states: dict[str, tuple[float, str]] = {}
        self.sleep: Callable[[float], None] = time.sleep  # retry backoff (tests replace it)

    # ---- status
    @property
    def configured(self) -> bool:
        return self.config is not None

    def load(self) -> dict | None:
        return json.loads(self.path.read_text()) if self.path.exists() else None

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def status(self) -> dict:
        acc = self.load()
        return {"configured": self.configured, "connected": bool(acc),
                "channel": acc.get("channel") if acc else None,
                "redirect_uri": self.config.redirect_uri if self.config else None}

    # ---- OAuth
    def auth_url(self, return_to: str = "/") -> str:
        if not self.config:
            raise YouTubeError("Google OAuth가 설정되지 않았습니다")
        state = secrets.token_urlsafe(24)
        now = time.time()
        self._states = {k: v for k, v in self._states.items() if now - v[0] < 600}
        self._states[state] = (now, return_to)
        q = {"client_id": self.config.client_id, "redirect_uri": self.config.redirect_uri,
             "response_type": "code", "scope": " ".join(SCOPES), "access_type": "offline",
             "prompt": "consent", "include_granted_scopes": "true", "state": state}
        return f"{self.ep.auth}?{urlencode(q)}"

    def finish_auth(self, code: str, state: str) -> str:
        entry = self._states.pop(state, None)
        if not entry or time.time() - entry[0] > 600:
            raise YouTubeError("로그인 요청이 만료되었거나 올바르지 않습니다. 다시 시도하세요")
        r = self.http.post(self.ep.token, data={
            "code": code, "client_id": self.config.client_id, "client_secret": self.config.client_secret,
            "redirect_uri": self.config.redirect_uri, "grant_type": "authorization_code"})
        if r.status_code != 200:
            raise YouTubeError(f"Google 인증 실패 ({r.status_code})")
        tok = r.json()
        if not tok.get("refresh_token"):
            raise YouTubeError("refresh token을 받지 못했습니다. Google 계정의 앱 권한을 해제한 뒤 다시 연결하세요")
        granted = set(tok.get("scope", "").split())
        if SCOPES[0] not in granted:
            raise YouTubeError("업로드 권한이 허용되지 않았습니다. 연결할 때 모든 권한을 체크하세요")
        data = {"access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
                "expires_at": time.time() + int(tok.get("expires_in", 3600)), "connected_at": time.time()}
        self._save(data)
        try:
            data["channel"] = self._channel(data["access_token"])
        except YouTubeError:
            self.path.unlink(missing_ok=True)
            raise
        self._save(data)
        return entry[1]

    def disconnect(self) -> None:
        acc = self.load()
        if acc:
            try:
                self.http.post(self.ep.revoke, params={"token": acc["refresh_token"]})
            except httpx.HTTPError:
                pass  # local removal still disconnects
            self.path.unlink(missing_ok=True)

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            acc = self.load()
            if not acc:
                raise YouTubeError("YouTube 채널이 연결되어 있지 않습니다")
            if force_refresh or acc["expires_at"] - time.time() < 120:
                r = self.http.post(self.ep.token, data={
                    "client_id": self.config.client_id, "client_secret": self.config.client_secret,
                    "refresh_token": acc["refresh_token"], "grant_type": "refresh_token"})
                if r.status_code != 200:
                    raise YouTubeError("YouTube 연결이 만료되었습니다. 채널을 다시 연결하세요")
                tok = r.json()
                acc["access_token"] = tok["access_token"]
                acc["expires_at"] = time.time() + int(tok.get("expires_in", 3600))
                self._save(acc)
            return acc["access_token"]

    def _channel(self, token: str) -> dict:
        r = self.http.get(f"{self.ep.api}/channels", params={"part": "snippet", "mine": "true"},
                          headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            raise YouTubeError(f"채널 정보를 가져오지 못했습니다 ({r.status_code})")
        items = r.json().get("items") or []
        if not items:
            raise YouTubeError("이 Google 계정에 YouTube 채널이 없습니다. 먼저 채널을 만드세요")
        sn = items[0]["snippet"]
        thumb = (sn.get("thumbnails") or {}).get("default", {}).get("url")
        return {"id": items[0]["id"], "title": sn.get("title"), "thumbnail": thumb}

    # ---- upload
    def upload(self, path: str | Path, meta: VideoMeta, progress: Callable[[float], None] | None = None,
               *, chunk_size: int = CHUNK) -> dict:
        """Resumable upload with retries; returns the created video resource."""
        meta.validate()
        path = Path(path)
        total = path.stat().st_size
        session = self._start_session(meta, total)
        offset, attempt = 0, 0
        with path.open("rb") as f:
            while True:
                f.seek(offset)
                chunk = f.read(chunk_size)
                end = offset + len(chunk) - 1
                try:
                    r = self.http.put(session, content=chunk, headers={
                        "Authorization": f"Bearer {self.access_token()}",
                        "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{total}"})
                except httpx.TransportError:
                    r = None
                if r is not None and r.status_code in (200, 201):
                    if progress:
                        progress(1.0)
                    return r.json()
                if r is not None and r.status_code == 308:
                    offset = _next_offset(r)
                    attempt = 0
                    if progress:
                        progress(offset / total)
                    continue
                if r is not None and r.status_code == 401:
                    self.access_token(force_refresh=True)
                elif r is not None and r.status_code not in (500, 502, 503, 504):
                    raise YouTubeError(_api_error(r))
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise YouTubeError("업로드 중 연결 오류가 계속됩니다. 잠시 후 다시 시도하세요")
                self.sleep(min(60, 2 ** attempt))
                known, video = self._query_offset(session, total)
                if video is not None:  # the final chunk had arrived; only its response was lost
                    if progress:
                        progress(1.0)
                    return video
                if known is not None:
                    offset = known

    def _start_session(self, meta: VideoMeta, total: int) -> str:
        for retry in (False, True):
            r = self.http.post(
                self.ep.upload, params={"uploadType": "resumable", "part": "snippet,status"},
                json=meta.body(),
                headers={"Authorization": f"Bearer {self.access_token(force_refresh=retry)}",
                         "X-Upload-Content-Length": str(total), "X-Upload-Content-Type": "video/mp4"})
            if r.status_code == 401 and not retry:
                continue
            if r.status_code != 200 or "location" not in r.headers:
                raise YouTubeError(_api_error(r))
            return r.headers["location"]
        raise YouTubeError("YouTube 인증 오류")

    def _query_offset(self, session: str, total: int) -> tuple[int | None, dict | None]:
        """Ask YouTube how many bytes it has. (None, None) if unreachable: keep our offset."""
        try:
            r = self.http.put(session, headers={"Authorization": f"Bearer {self.access_token()}",
                                                "Content-Length": "0", "Content-Range": f"bytes */{total}"})
        except httpx.TransportError:
            return None, None
        if r.status_code == 308:
            return _next_offset(r), None
        if r.status_code in (200, 201):
            return total, r.json()
        return None, None


def _next_offset(r: httpx.Response) -> int:
    rng = r.headers.get("range")  # "bytes=0-524287"
    return int(rng.split("-")[1]) + 1 if rng else 0


def _api_error(r: httpx.Response) -> str:
    try:
        err = r.json().get("error", {})
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        msg = err.get("message", "")
    except ValueError:
        reason, msg = "", r.text[:200]
    friendly = {
        "quotaExceeded": "오늘의 YouTube API 업로드 한도를 초과했습니다. 내일 다시 시도하거나 할당량 증설을 신청하세요",
        "uploadLimitExceeded": "이 채널의 업로드 한도를 초과했습니다",
        "invalidTitle": "YouTube가 제목을 거부했습니다",
        "invalidDescription": "YouTube가 설명을 거부했습니다",
        "invalidPublishAt": "예약 시간이 올바르지 않습니다",
        "forbidden": "업로드 권한이 없습니다. 채널을 다시 연결하세요",
    }.get(reason)
    return friendly or f"YouTube 오류 {r.status_code}: {reason or msg}"
