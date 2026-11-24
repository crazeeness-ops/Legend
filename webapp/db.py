"""SQLite storage for users, sessions, usage, campaigns and submissions.

Projects stay file-based (``store.py``) with an ``owner`` field; everything that needs
queries across users lives here. Production plan: move to Postgres with the same tables.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, pw_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'creator', plan TEXT NOT NULL DEFAULT 'free', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, minutes REAL NOT NULL,
  reason TEXT NOT NULL, project_id TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
  title TEXT NOT NULL, rules TEXT NOT NULL DEFAULT '', credit TEXT NOT NULL,
  video_id TEXT NOT NULL, video_title TEXT NOT NULL, channel_id TEXT NOT NULL, channel_title TEXT NOT NULL,
  thumbnail TEXT, duration REAL NOT NULL, source_file TEXT NOT NULL, has_transcript INTEGER NOT NULL DEFAULT 0,
  rate_per_1k INTEGER NOT NULL, budget INTEGER NOT NULL, max_clip_sec INTEGER NOT NULL,
  deadline REAL NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id), clipper_id TEXT NOT NULL REFERENCES users(id),
  project_id TEXT NOT NULL, clip_id TEXT NOT NULL, youtube_id TEXT NOT NULL, url TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '', views INTEGER NOT NULL DEFAULT 0, likes INTEGER NOT NULL DEFAULT 0,
  stats_at REAL, status TEXT NOT NULL DEFAULT 'pending', earned INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
  UNIQUE (youtube_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sub_campaign ON submissions(campaign_id);
CREATE INDEX IF NOT EXISTS idx_sub_clipper ON submissions(clipper_id);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


class DB:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def q(self, sql: str, *args) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def one(self, sql: str, *args) -> dict | None:
        rows = self.q(sql, *args)
        return rows[0] if rows else None

    def x(self, sql: str, *args) -> int:
        with self._lock:
            return self._conn.execute(sql, args).rowcount

    def tx(self):
        """``with db.tx():`` groups writes atomically."""
        db = self

        class _Tx:
            def __enter__(self):
                db._lock.acquire()
                db._conn.execute("BEGIN IMMEDIATE")
                return db

            def __exit__(self, exc_type, *_):
                db._conn.execute("ROLLBACK" if exc_type else "COMMIT")
                db._lock.release()

        return _Tx()

    @staticmethod
    def now() -> float:
        return time.time()
