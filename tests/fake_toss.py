"""A fake Toss Payments (SDK + API) for offline testing.

In tests: ``httpx.MockTransport(fake.handle)`` for the server API, plus ``fake.authorize()`` /
``fake.billing_auth()`` to simulate a user finishing the payment window.

Live (browser walkthrough):
    python tests/fake_toss.py --port 9998
    TOSS_CLIENT_KEY=test_ck_fake TOSS_SECRET_KEY=test_sk_fake LEGEND_TOSS_BASE=http://127.0.0.1:9998 \\
    LEGEND_TOSS_SDK=http://127.0.0.1:9998/v2/standard python -m webapp serve
"""

from __future__ import annotations

import base64
import html
import itertools
import json
import time
from urllib.parse import parse_qs, urlencode

import httpx

SECRET = "test_sk_fake"


class FakeToss:
    def __init__(self, secret: str = SECRET) -> None:
        self.secret = secret
        self.payments: dict[str, dict] = {}  # paymentKey -> payment
        self.pending: dict[str, dict] = {}  # paymentKey -> authorized but not confirmed {orderId, amount, orderName}
        self.auth_keys: dict[str, str] = {}  # authKey -> customerKey
        self.billing_keys: dict[str, dict] = {}  # billingKey -> {customerKey, active}
        self.idem: dict[str, tuple[int, dict]] = {}
        self.fail_billing = 0  # next N billing charges are declined
        self.fail_confirm = False
        self._ids = itertools.count(1)

    # --------------------------------------------------------------- simulate the browser part
    def authorize(self, order_id: str, amount: int, order_name: str = "주문") -> str:
        pk = f"tpk_{next(self._ids)}_{order_id}"
        self.pending[pk] = {"orderId": order_id, "amount": int(amount), "orderName": order_name}
        return pk

    def billing_auth(self, customer_key: str) -> str:
        ak = f"auth_{next(self._ids)}"
        self.auth_keys[ak] = customer_key
        return ak

    # --------------------------------------------------------------- API
    def respond(self, method: str, path: str, query: dict, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
        q = {k: v[0] for k, v in query.items()}
        if path == "/v2/standard":
            return 200, {"content-type": "application/javascript"}, self.sdk_js(headers.get("x-base", "")).encode()
        if path == "/_fake/checkout":
            return self.page(q, billing=False)
        if path == "/_fake/billing":
            return self.page(q, billing=True)
        if path == "/_fake/approve":
            if q.get("billing"):
                ak = self.billing_auth(q["customerKey"])
                return 302, {"location": _join(q["successUrl"], {"customerKey": q["customerKey"], "authKey": ak})}, b""
            pk = self.authorize(q["orderId"], int(q["amount"]), q.get("orderName", ""))
            return 302, {"location": _join(q["successUrl"], {"paymentType": "NORMAL", "orderId": q["orderId"],
                                                              "paymentKey": pk, "amount": q["amount"]})}, b""
        if path == "/_fake/cancel":
            return 302, {"location": _join(q["failUrl"], {"code": "PAY_PROCESS_CANCELED",
                                                           "message": "사용자에 의해 결제가 취소되었습니다",
                                                           "orderId": q.get("orderId", "")})}, b""

        if headers.get("authorization") != "Basic " + base64.b64encode(f"{self.secret}:".encode()).decode():
            return self._json(401, {"code": "UNAUTHORIZED_KEY", "message": "인증되지 않은 시크릿 키 혹은 클라이언트 키 입니다."})
        idem = headers.get("idempotency-key")
        if idem and idem in self.idem:
            code, data = self.idem[idem]
            return self._json(code, data)
        data = json.loads(body) if body else {}
        code, out = self._api(method, path, data)
        if idem and method == "POST":
            self.idem[idem] = (code, out)
        return self._json(code, out)

    def _api(self, method: str, path: str, d: dict) -> tuple[int, dict]:
        if path == "/v1/payments/confirm" and method == "POST":
            pend = self.pending.get(d.get("paymentKey"))
            if not pend or pend["orderId"] != d.get("orderId"):
                return 404, {"code": "NOT_FOUND_PAYMENT_SESSION", "message": "결제 시간이 만료되어 결제 진행 데이터가 존재하지 않습니다."}
            if int(d.get("amount", -1)) != pend["amount"]:
                return 400, {"code": "INVALID_REQUEST", "message": "결제 금액이 일치하지 않습니다."}
            if self.fail_confirm:
                return 400, {"code": "REJECT_CARD_PAYMENT", "message": "한도초과 혹은 잔액부족으로 결제에 실패했습니다."}
            self.pending.pop(d["paymentKey"])
            return 200, self._payment(d["paymentKey"], pend["orderId"], pend["amount"], pend["orderName"])
        if path.startswith("/v1/payments/") and path.endswith("/cancel") and method == "POST":
            pk = path.split("/")[3]
            p = self.payments.get(pk)
            if not p:
                return 404, {"code": "NOT_FOUND_PAYMENT", "message": "존재하지 않는 결제 정보 입니다."}
            if p["status"] == "CANCELED":
                return 400, {"code": "ALREADY_CANCELED_PAYMENT", "message": "이미 취소된 결제 입니다."}
            p["status"] = "CANCELED"
            p["cancels"] = [{"cancelAmount": p["totalAmount"], "cancelReason": d.get("cancelReason", "")}]
            return 200, p
        if path.startswith("/v1/payments/") and method == "GET":
            p = self.payments.get(path.split("/")[3])
            return (200, p) if p else (404, {"code": "NOT_FOUND_PAYMENT", "message": "존재하지 않는 결제 정보 입니다."})
        if path == "/v1/billing/authorizations/issue" and method == "POST":
            ck = self.auth_keys.pop(d.get("authKey"), None)
            if not ck or ck != d.get("customerKey"):
                return 400, {"code": "INVALID_AUTH_KEY", "message": "유효하지 않은 authKey 입니다."}
            bk = f"bk_{next(self._ids)}"
            self.billing_keys[bk] = {"customerKey": ck, "active": True}
            return 200, {"billingKey": bk, "customerKey": ck, "cardCompany": "신한", "cardNumber": "43301234****123*",
                         "card": {"issuerCode": "4V", "number": "43301234****123*", "cardType": "신용"}}
        if path.startswith("/v1/billing/") and method == "POST":
            bk = path.split("/")[3]
            b = self.billing_keys.get(bk)
            if not b or not b["active"]:
                return 404, {"code": "NOT_FOUND_BILLING_KEY", "message": "존재하지 않는 빌링키 입니다."}
            if b["customerKey"] != d.get("customerKey"):
                return 400, {"code": "NOT_MATCHES_CUSTOMER_KEY", "message": "빌링키와 customerKey가 일치하지 않습니다."}
            if self.fail_billing:
                self.fail_billing -= 1
                return 400, {"code": "REJECT_CARD_COMPANY", "message": "카드사에서 거절했습니다. 카드사 문의가 필요합니다."}
            pk = f"tpk_b{next(self._ids)}"
            return 200, self._payment(pk, d["orderId"], int(d["amount"]), d.get("orderName", ""), billing=True)
        if path.startswith("/v1/billing/") and method == "DELETE":
            b = self.billing_keys.get(path.split("/")[3])
            if b:
                b["active"] = False
            return 200, {}
        return 404, {"code": "NOT_FOUND", "message": f"no route {method} {path}"}

    def _payment(self, pk: str, order_id: str, amount: int, name: str, billing: bool = False) -> dict:
        p = {"paymentKey": pk, "orderId": order_id, "orderName": name, "status": "DONE", "totalAmount": amount,
             "method": "카드", "type": "BILLING" if billing else "NORMAL",
             "approvedAt": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
             "receipt": {"url": f"https://fake-toss.invalid/receipt/{pk}"}, "cancels": None}
        self.payments[pk] = p
        return p

    # --------------------------------------------------------------- browser side (SDK + payment window)
    @staticmethod
    def sdk_js(base: str) -> str:
        return """(function(){var BASE=%s;
function go(path, p){ location.href = BASE + path + "?" + new URLSearchParams(p).toString(); }
window.TossPayments = function(clientKey){ return { payment: function(opts){ return {
  requestPayment: function(r){ var a = (r.amount && r.amount.value !== undefined) ? r.amount.value : r.amount;
    go("/_fake/checkout", {orderId:r.orderId, amount:a, orderName:r.orderName, successUrl:r.successUrl, failUrl:r.failUrl,
      customerKey:opts.customerKey, method:r.method}); return new Promise(function(){}); },
  requestBillingAuth: function(r){ go("/_fake/billing", {successUrl:r.successUrl, failUrl:r.failUrl,
      customerKey:opts.customerKey, method:r.method}); return new Promise(function(){}); } }; } }; };
window.TossPayments.ANONYMOUS = "__ANONYMOUS__";})();""" % json.dumps(base)

    def page(self, q: dict, billing: bool) -> tuple[int, dict, bytes]:
        e = html.escape
        approve = "/_fake/approve?" + urlencode({**q, **({"billing": "1"} if billing else {})})
        cancel = "/_fake/cancel?" + urlencode(q)
        title = "카드 등록 (자동결제)" if billing else f"{e(q.get('orderName', ''))}"
        amount = "" if billing else f"<div class=amt>{int(q.get('amount', 0)):,}원</div>"
        body = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>토스페이먼츠 테스트 결제창</title>
<style>body{{font-family:sans-serif;background:#f2f4f6;margin:0;display:flex;justify-content:center;padding-top:60px}}
.box{{background:#fff;border-radius:16px;padding:28px;width:340px;box-shadow:0 8px 30px #0002}}.tag{{color:#3182f6;font-weight:700;font-size:13px}}
h1{{font-size:18px}}.amt{{font-size:28px;font-weight:800;margin:10px 0 20px}}.card{{border:1px solid #e5e8eb;border-radius:10px;padding:12px;margin:12px 0;color:#4e5968}}
a{{display:block;text-align:center;padding:14px;border-radius:10px;text-decoration:none;margin-top:10px;font-weight:700}}
.ok{{background:#3182f6;color:#fff}}.no{{background:#f2f4f6;color:#4e5968}}</style>
<div class=box><div class=tag>토스페이먼츠 · 테스트 환경 (가짜 서버)</div><h1>{title}</h1>{amount}
<div class=card>💳 신한카드 4330-12**-****-123*</div>
<a class=ok id=approve href="{e(approve)}">{'카드 등록하기' if billing else '결제하기'}</a>
<a class=no id=cancel href="{e(cancel)}">취소</a></div>"""
        return 200, {"content-type": "text/html; charset=utf-8"}, body.encode()

    @staticmethod
    def _json(code: int, data: dict) -> tuple[int, dict, bytes]:
        return code, {"content-type": "application/json"}, json.dumps(data, ensure_ascii=False).encode()

    # --------------------------------------------------------------- adapters
    def handle(self, request: httpx.Request) -> httpx.Response:
        code, headers, body = self.respond(request.method, request.url.path, parse_qs(request.url.query.decode()),
                                           {k.lower(): v for k, v in request.headers.items()}, request.content)
        return httpx.Response(code, headers=headers, content=body)

    def asgi(self):
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import Response
        from starlette.routing import Route

        async def endpoint(request: Request) -> Response:
            hdrs = {k.lower(): v for k, v in request.headers.items()}
            hdrs["x-base"] = str(request.base_url).rstrip("/")
            code, headers, content = self.respond(request.method, request.url.path, parse_qs(request.url.query), hdrs,
                                                  await request.body())
            return Response(content, status_code=code, headers=headers)

        return Starlette(routes=[Route("/{path:path}", endpoint, methods=["GET", "POST", "DELETE"])])


def _join(url: str, params: dict) -> str:
    return url + ("&" if "?" in url else "?") + urlencode(params)


if __name__ == "__main__":
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9998)
    uvicorn.run(FakeToss().asgi(), host="127.0.0.1", port=ap.parse_args().port)
