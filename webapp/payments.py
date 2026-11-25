"""Payment providers behind one interface.

- ``TossProvider``: real payments through Toss Payments (결제창 → successUrl → 서버 승인).
- ``MockPayments``: local development only (no money moves). Disabled with ``allow_mock=False``
  (``python -m webapp serve --production``).
"""

from __future__ import annotations

import secrets

from .db import DB
from .toss import TossClient, TossError, receipt_url


class PaymentUnavailable(Exception):
    pass


def customer_key(db: DB, uid: str) -> str:
    """Stable, unguessable per-user key for Toss (never an email/phone; 2-50 chars of [A-Za-z0-9_-])."""
    row = db.one("SELECT customer_key FROM toss_customers WHERE user_id=?", uid)
    if row:
        return row["customer_key"]
    key = "cus_" + secrets.token_urlsafe(24)
    db.x("INSERT OR IGNORE INTO toss_customers (user_id, customer_key) VALUES (?,?)", uid, key)
    return db.one("SELECT customer_key FROM toss_customers WHERE user_id=?", uid)["customer_key"]


class MockPayments:
    """Test mode: confirms instantly, no money moves."""

    name = "mock"

    def checkout(self, order: dict, user: dict, ckey: str) -> dict:
        return {"provider": self.name}

    def confirm(self, order: dict, payment_key: str | None, amount: int | None = None) -> dict:
        return {"payment_key": payment_key or f"mock_{order['id']}", "receipt_url": None, "method": "테스트"}

    def refund(self, order: dict, reason: str = "") -> None:
        return None


class DisabledPayments(MockPayments):
    """Production without Toss keys: refuse to sell rather than silently fake payments."""

    name = "disabled"

    def checkout(self, order: dict, user: dict, ckey: str) -> dict:
        raise PaymentUnavailable("결제가 아직 설정되지 않았어요 (관리자: 토스페이먼츠 키 설정 필요)")

    def confirm(self, order: dict, payment_key: str | None, amount: int | None = None) -> dict:
        raise PaymentUnavailable("결제가 아직 설정되지 않았어요")


class TossProvider:
    name = "toss"

    def __init__(self, client: TossClient, public_url: str) -> None:
        self.client = client
        self.public_url = public_url.rstrip("/")

    def checkout(self, order: dict, user: dict, ckey: str) -> dict:
        """What the browser needs for ``payment.requestPayment`` (never the secret key)."""
        return {
            "provider": self.name, "client_key": self.client.config.client_key, "sdk_url": self.client.config.sdk_url,
            "customer_key": ckey, "success_url": f"{self.public_url}/pay/success", "fail_url": f"{self.public_url}/pay/fail",
            "customer_email": user["email"], "customer_name": user["name"], "test_mode": self.client.config.test_mode,
        }

    def confirm(self, order: dict, payment_key: str | None, amount: int | None = None) -> dict:
        if not payment_key or amount is None:
            raise TossError("INVALID_REQUEST", "결제 정보가 없어요")
        p = self.client.confirm(payment_key, order["id"], int(amount))
        if p.get("status") != "DONE" or int(p.get("totalAmount", amount)) != int(order["amount"]):
            # never keep money we didn't agree on
            self.client.cancel(payment_key, "결제 검증 실패", idem=f"cancel-verify-{order['id']}")
            raise TossError("VERIFY_FAILED", "결제 검증에 실패해 취소했어요")
        return {"payment_key": payment_key, "receipt_url": receipt_url(p), "method": p.get("method")}

    def refund(self, order: dict, reason: str = "구매자 환불 요청") -> None:
        self.client.cancel(order["payment_key"], reason, idem=f"cancel-{order['id']}")
