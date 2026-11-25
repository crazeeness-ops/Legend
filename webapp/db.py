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
CREATE TABLE IF NOT EXISTS sellers (
  user_id TEXT PRIMARY KEY REFERENCES users(id), legal_name TEXT NOT NULL, email TEXT NOT NULL,
  business_no TEXT NOT NULL DEFAULT '', terms_version TEXT NOT NULL, agreed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS listings (
  id TEXT PRIMARY KEY, seller_id TEXT NOT NULL REFERENCES users(id), project_id TEXT NOT NULL, clip_id TEXT NOT NULL,
  title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', credit TEXT NOT NULL,
  source_video_id TEXT NOT NULL, source_title TEXT NOT NULL, channel_title TEXT NOT NULL,
  duration REAL NOT NULL, score INTEGER NOT NULL DEFAULT 0, price INTEGER NOT NULL,
  license_type TEXT NOT NULL, license_days INTEGER NOT NULL, max_sales INTEGER NOT NULL, sold INTEGER NOT NULL DEFAULT 0,
  deadline REAL NOT NULL, status TEXT NOT NULL DEFAULT 'on_sale', master_file TEXT NOT NULL, preview_file TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY, listing_id TEXT NOT NULL REFERENCES listings(id), buyer_id TEXT NOT NULL REFERENCES users(id),
  seller_id TEXT NOT NULL, amount INTEGER NOT NULL, fee INTEGER NOT NULL, seller_amount INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', provider TEXT NOT NULL, payment_key TEXT,
  license_code TEXT, license_until REAL, paid_at REAL, downloaded_at REAL, published_url TEXT, refunded_at REAL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT NOT NULL, reporter_id TEXT NOT NULL, reason TEXT NOT NULL,
  created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS toss_customers (
  user_id TEXT PRIMARY KEY REFERENCES users(id), customer_key TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
  user_id TEXT PRIMARY KEY REFERENCES users(id), plan TEXT NOT NULL, pending_plan TEXT,
  status TEXT NOT NULL, billing_key TEXT, customer_key TEXT NOT NULL, card_label TEXT,
  period_start REAL, period_end REAL, next_charge_at REAL, retry_count INTEGER NOT NULL DEFAULT 0,
  cancel_at_period_end INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_charges (
  order_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, plan TEXT NOT NULL, amount INTEGER NOT NULL,
  status TEXT NOT NULL, payment_key TEXT, receipt_url TEXT, failure TEXT,
  period_start REAL, period_end REAL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, event_type TEXT, payment_key TEXT, order_id TEXT,
  status TEXT, note TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer_id);
CREATE INDEX IF NOT EXISTS idx_orders_seller ON orders(seller_id);
"""


MIGRATIONS = [
    ("orders", "receipt_url", "TEXT"),
    ("orders", "method", "TEXT"),
    ("orders", "failure", "TEXT"),
    ("orders", "settled_at", "REAL"),
]


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
            for table, col, decl in MIGRATIONS:  # additive columns for databases created by older versions
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

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
