"""Toss Payments (토스페이먼츠) API client: one-time payments, cancels, and billing (자동결제).

Flow references (docs.tosspayments.com, SDK v2):
- Browser: ``TossPayments(clientKey).payment({customerKey}).requestPayment({method, amount: {currency, value},
  orderId, orderName, successUrl, failUrl, ...})`` → redirect to successUrl?paymentKey&orderId&amount
  → server verifies the amount against its own record and calls ``POST /v1/payments/confirm`` within 10 minutes.
- Browser: ``payment.requestBillingAuth({method: "CARD", successUrl, failUrl, ...})`` → successUrl?customerKey&authKey
  → server ``POST /v1/billing/authorizations/issue`` → billingKey (store it; there is no lookup API)
  → server charges with ``POST /v1/billing/{billingKey}``. Toss does not schedule renewals; we do.
- Auth: ``Authorization: Basic base64(secretKey + ":")``. ``Idempotency-Key`` makes retries safe.

Verify field names against the current official docs before going live; endpoints are configurable so the
whole flow can be exercised against ``tests/fake_toss.py``.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

import httpx

API_BASE = "https://api.tosspayments.com"
SDK_URL = "https://js.tosspayments.com/v2/standard"


class TossError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass
class TossConfig:
    client_key: str
    secret_key: str
    api_base: str = API_BASE
    sdk_url: str = SDK_URL

    @classmethod
    def from_env(cls) -> "TossConfig | None":
        ck, sk = os.environ.get("TOSS_CLIENT_KEY"), os.environ.get("TOSS_SECRET_KEY")
        if not ck or not sk:
            return None
        return cls(ck, sk, os.environ.get("LEGEND_TOSS_BASE", API_BASE).rstrip("/"),
                   os.environ.get("LEGEND_TOSS_SDK", SDK_URL))

    @property
    def test_mode(self) -> bool:
        return self.secret_key.startswith("test_")


class TossClient:
    def __init__(self, config: TossConfig, http: httpx.Client | None = None) -> None:
        self.config = config
        self.http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        token = base64.b64encode(f"{config.secret_key}:".encode()).decode()
        self._auth = {"Authorization": f"Basic {token}"}

    def _call(self, method: str, path: str, body: dict | None = None, idem: str | None = None) -> dict:
        headers = dict(self._auth)
        if idem:
            headers["Idempotency-Key"] = idem
        try:
            r = self.http.request(method, f"{self.config.api_base}{path}", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise TossError("NETWORK_ERROR", "결제사와 통신하지 못했어요. 잠시 후 다시 시도해 주세요", 502) from e
        if r.status_code >= 400:
            try:
                err = r.json()
            except ValueError:
                err = {}
            raise TossError(err.get("code", f"HTTP_{r.status_code}"), err.get("message", "결제 처리 중 오류가 났어요"),
                            r.status_code)
        return r.json() if r.content else {}

    # ---- one-time payments
    def confirm(self, payment_key: str, order_id: str, amount: int) -> dict:
        return self._call("POST", "/v1/payments/confirm",
                          {"paymentKey": payment_key, "orderId": order_id, "amount": amount}, idem=f"confirm-{order_id}")

    def get(self, payment_key: str) -> dict:
        return self._call("GET", f"/v1/payments/{payment_key}")

    def cancel(self, payment_key: str, reason: str, *, idem: str) -> dict:
        return self._call("POST", f"/v1/payments/{payment_key}/cancel", {"cancelReason": reason}, idem=idem)

    # ---- billing (자동결제)
    def issue_billing_key(self, auth_key: str, customer_key: str) -> dict:
        return self._call("POST", "/v1/billing/authorizations/issue", {"authKey": auth_key, "customerKey": customer_key})

    def charge_billing(self, billing_key: str, *, customer_key: str, amount: int, order_id: str, order_name: str,
                       customer_email: str | None = None, customer_name: str | None = None) -> dict:
        body = {"customerKey": customer_key, "amount": amount, "orderId": order_id, "orderName": order_name}
        if customer_email:
            body["customerEmail"] = customer_email
        if customer_name:
            body["customerName"] = customer_name
        return self._call("POST", f"/v1/billing/{billing_key}", body, idem=f"bill-{order_id}")

    def delete_billing_key(self, billing_key: str) -> None:
        try:
            self._call("DELETE", f"/v1/billing/{billing_key}")
        except TossError:
            pass  # already gone; we forget it locally either way


def receipt_url(payment: dict) -> str | None:
    return (payment.get("receipt") or {}).get("url")


def card_label(issued: dict) -> str:
    card = issued.get("card") or {}
    company = issued.get("cardCompany") or card.get("issuerCode") or "카드"
    number = issued.get("cardNumber") or card.get("number") or ""
    return f"{company} {number[-4:] if number else ''}".strip()
