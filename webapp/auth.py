"""Accounts: email + password, cookie sessions, plans and monthly usage quotas."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timezone

from .db import DB, new_id

SESSION_COOKIE = "legend_session"
SESSION_DAYS = 30
CSRF_HEADER = "x-requested-with"  # cross-site forms can't set custom headers
CSRF_VALUE = "legend"

PLANS = {
    "free": {"label": "Free", "minutes": 60, "price_krw": 0},
    "creator": {"label": "Creator", "minutes": 300, "price_krw": 19000},
    "pro": {"label": "Pro", "minutes": 1200, "price_krw": 49000},
}
ROLES = ("creator", "clipper")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    pass


class QuotaError(Exception):
    pass


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
    except ValueError:
        return False
    test = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(test.hex(), dk)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def public_user(u: dict) -> dict:
    return {k: u[k] for k in ("id", "email", "name", "role", "plan", "created_at")}


class Accounts:
    def __init__(self, db: DB) -> None:
        self.db = db

    # ---- users
    def signup(self, email: str, password: str, name: str, role: str = "creator") -> dict:
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            raise AuthError("이메일 형식이 올바르지 않습니다")
        if len(password) < 8:
            raise AuthError("비밀번호는 8자 이상이어야 합니다")
        if role not in ROLES:
            role = "creator"
        name = (name.strip() or email.split("@")[0])[:40]
        if self.db.one("SELECT id FROM users WHERE email=?", email):
            raise AuthError("이미 가입된 이메일입니다")
        uid = new_id("u")
        self.db.x("INSERT INTO users (id,email,name,pw_hash,role,plan,created_at) VALUES (?,?,?,?,?,?,?)",
                  uid, email, name, hash_password(password), role, "free", time.time())
        return self.db.one("SELECT * FROM users WHERE id=?", uid)

    def login(self, email: str, password: str) -> dict:
        u = self.db.one("SELECT * FROM users WHERE email=?", email.strip().lower())
        # verify even when the user is missing, so timing doesn't reveal which emails exist
        ok = verify_password(password, u["pw_hash"] if u else hash_password("x" * 12))
        if not u or not ok:
            raise AuthError("이메일 또는 비밀번호가 올바르지 않습니다")
        return u

    def get(self, uid: str) -> dict | None:
        return self.db.one("SELECT * FROM users WHERE id=?", uid)

    def update(self, uid: str, **fields) -> dict:
        allowed = {k: v for k, v in fields.items() if k in ("name", "role", "plan") and v is not None}
        if "role" in allowed and allowed["role"] not in ROLES:
            raise AuthError("역할이 올바르지 않습니다")
        if "plan" in allowed and allowed["plan"] not in PLANS:
            raise AuthError("요금제가 올바르지 않습니다")
        for k, v in allowed.items():
            self.db.x(f"UPDATE users SET {k}=? WHERE id=?", v, uid)
        return self.get(uid)

    # ---- sessions
    def create_session(self, uid: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.x("INSERT INTO sessions (token_hash,user_id,created_at,expires_at) VALUES (?,?,?,?)",
                  _token_hash(token), uid, now, now + SESSION_DAYS * 86400)
        return token

    def user_for_session(self, token: str | None) -> dict | None:
        if not token:
            return None
        row = self.db.one("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                          "WHERE s.token_hash=? AND s.expires_at>?", _token_hash(token), time.time())
        return row

    def end_session(self, token: str | None) -> None:
        if token:
            self.db.x("DELETE FROM sessions WHERE token_hash=?", _token_hash(token))

    # ---- usage / quota (minutes of source video analysed per calendar month)
    @staticmethod
    def _month_start() -> float:
        now = datetime.now(timezone.utc)
        return datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp()

    def usage(self, uid: str) -> dict:
        u = self.get(uid)
        used = self.db.one("SELECT COALESCE(SUM(minutes),0) AS m FROM usage WHERE user_id=? AND created_at>=?",
                           uid, self._month_start())["m"]
        limit = PLANS[u["plan"]]["minutes"]
        return {"plan": u["plan"], "plan_label": PLANS[u["plan"]]["label"], "used_minutes": round(used, 1),
                "limit_minutes": limit, "remaining_minutes": round(max(0.0, limit - used), 1)}

    def charge(self, uid: str, minutes: float, project_id: str) -> None:
        with self.db.tx():
            u = self.usage(uid)
            if minutes > u["remaining_minutes"] + 1e-6:
                raise QuotaError(f"이번 달 사용량이 부족합니다 (남은 {u['remaining_minutes']:.0f}분 / 필요 {minutes:.0f}분). "
                                 "요금제를 올리거나 다음 달에 이용하세요")
            self.db.x("INSERT INTO usage (user_id,minutes,reason,project_id,created_at) VALUES (?,?,?,?,?)",
                      uid, round(minutes, 2), "analysis", project_id, time.time())

    def refund(self, uid: str, project_id: str) -> None:
        row = self.db.one("SELECT COALESCE(SUM(minutes),0) AS m FROM usage WHERE user_id=? AND project_id=?", uid, project_id)
        if row["m"] > 0:
            self.db.x("INSERT INTO usage (user_id,minutes,reason,project_id,created_at) VALUES (?,?,?,?,?)",
                      uid, -row["m"], "refund", project_id, time.time())
