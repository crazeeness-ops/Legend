"""Seller payouts ledger.

A clip sale becomes payable once its refund window (7 days) has passed. Until Toss Payments
지급대행 (payout service, separate contract) is connected, an admin exports the payable list,
pays sellers, and marks the batch as paid:

    python -m webapp payouts               # preview + CSV
    python -m webapp payouts --mark-paid   # after paying
"""

from __future__ import annotations

import csv
import io
import time

from .db import DB
from .sales import REFUND_DAYS


def payable(db: DB, now: float | None = None) -> list[dict]:
    now = now or time.time()
    cutoff = now - REFUND_DAYS * 86400
    rows = db.q(
        "SELECT o.seller_id, s.legal_name, s.email, s.business_no, COUNT(*) AS orders, SUM(o.amount) AS gross, "
        "SUM(o.fee) AS fee, SUM(o.seller_amount) AS net, GROUP_CONCAT(o.id) AS order_ids "
        "FROM orders o LEFT JOIN sellers s ON s.user_id=o.seller_id "
        "WHERE o.status='paid' AND o.settled_at IS NULL AND o.paid_at<=? GROUP BY o.seller_id ORDER BY net DESC", cutoff)
    return rows


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["판매자", "이메일", "사업자번호", "건수", "판매금액", "수수료", "지급액"])
    for r in rows:
        w.writerow([r["legal_name"], r["email"], r["business_no"], r["orders"], r["gross"], r["fee"], r["net"]])
    return buf.getvalue()


def mark_paid(db: DB, rows: list[dict], now: float | None = None) -> int:
    now = now or time.time()
    ids = [oid for r in rows for oid in (r["order_ids"] or "").split(",") if oid]
    with db.tx():
        for oid in ids:
            db.x("UPDATE orders SET settled_at=? WHERE id=? AND status='paid' AND settled_at IS NULL", now, oid)
    return len(ids)
