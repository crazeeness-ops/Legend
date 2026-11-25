"""Plan subscriptions with Toss Payments 자동결제 (billing), renewals, and the Toss webhook.

- Subscribe: browser ``requestBillingAuth`` → /billing/success?customerKey&authKey → issue a billing key,
  charge the first month right away, activate the plan.
- Renew: Toss does not schedule charges, so ``Billing.run_due`` (background thread, or
  ``python -m webapp run-billing`` from cron) charges subscriptions whose period ended.
  Failed charges retry daily up to 3 times, then the plan falls back to Free.
- Cancel: stays on the paid plan until the period ends, then Free (billing key deleted).
- Plan change: takes effect from the next billing date (no proration).
- Webhook: payload is only a hint; we re-read the payment from Toss before changing anything.
"""

from __future__ import annotations

import calendar
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .auth import PLANS
from .db import DB, new_id
from .payments import customer_key
from .toss import TossClient, TossError, card_label, receipt_url

log = logging.getLogger(__name__)
MAX_RETRIES = 3
RETRY_SEC = 86400
PAID_PLANS = [k for k, v in PLANS.items() if v["price_krw"] > 0]


def add_month(ts: float) -> float:
    d = datetime.fromtimestamp(ts, timezone.utc)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    day = min(d.day, calendar.monthrange(y, m)[1])
    return d.replace(year=y, month=m, day=day).timestamp()


class PlanBody(BaseModel):
    plan: str


class Billing:
    def __init__(self, app: FastAPI, client: TossClient | None, public_url: str) -> None:
        self.app, self.client, self.public_url = app, client, public_url.rstrip("/")
        self.db: DB = app.state.db
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def sub(self, uid: str) -> dict | None:
        return self.db.one("SELECT * FROM subscriptions WHERE user_id=?", uid)

    def set_plan(self, uid: str, plan: str) -> None:
        self.db.x("UPDATE users SET plan=? WHERE id=?", plan, uid)

    def view(self, uid: str) -> dict:
        s = self.sub(uid)
        sub = None
        if s:
            sub = {k: s[k] for k in ("plan", "pending_plan", "status", "card_label", "period_start", "period_end",
                                     "next_charge_at", "retry_count", "cancel_at_period_end", "last_error")}
            sub["plan_label"] = PLANS[s["plan"]]["label"]
            sub["price_krw"] = PLANS[s["plan"]]["price_krw"]
        charges = self.db.q("SELECT order_id, plan, amount, status, receipt_url, failure, period_start, period_end, created_at "
                            "FROM plan_charges WHERE user_id=? ORDER BY created_at DESC LIMIT 24", uid)
        return {"configured": self.client is not None,
                "test_mode": bool(self.client and self.client.config.test_mode),
                "subscription": sub, "charges": charges, "plans": PLANS}

    def charge(self, uid: str, plan: str, billing_key: str, ckey: str, order_id: str, period_start: float) -> tuple[bool, str]:
        user = self.db.one("SELECT email, name FROM users WHERE id=?", uid)
        amount = PLANS[plan]["price_krw"]
        period_end = add_month(period_start)
        self.db.x("INSERT OR IGNORE INTO plan_charges (order_id,user_id,plan,amount,status,period_start,period_end,created_at) "
                  "VALUES (?,?,?,?,?,?,?,?)", order_id, uid, plan, amount, "pending", period_start, period_end, time.time())
        try:
            p = self.client.charge_billing(billing_key, customer_key=ckey, amount=amount, order_id=order_id,
                                           order_name=f"Legend Clips {PLANS[plan]['label']} 1개월",
                                           customer_email=user["email"], customer_name=user["name"])
        except TossError as e:
            self.db.x("UPDATE plan_charges SET status='failed', failure=? WHERE order_id=?", e.message, order_id)
            return False, e.message
        if p.get("status") != "DONE" or int(p.get("totalAmount", amount)) != amount:
            self.db.x("UPDATE plan_charges SET status='failed', failure=? WHERE order_id=?", "검증 실패", order_id)
            return False, "결제 검증에 실패했어요"
        self.db.x("UPDATE plan_charges SET status='paid', payment_key=?, receipt_url=? WHERE order_id=?",
                  p.get("paymentKey"), receipt_url(p), order_id)
        return True, "결제됐어요"

    def end_subscription(self, s: dict, reason: str) -> None:
        if s["billing_key"]:
            self.client.delete_billing_key(s["billing_key"])
        self.db.x("UPDATE subscriptions SET status='canceled', billing_key=NULL, last_error=?, updated_at=? WHERE user_id=?",
                  reason, time.time(), s["user_id"])
        self.set_plan(s["user_id"], "free")

    # ------------------------------------------------------------------ renewals
    def run_due(self, now: float | None = None) -> list[dict]:
        if not self.client:
            return []
        now = now or time.time()
        out = []
        with self._lock:
            due = self.db.q("SELECT * FROM subscriptions WHERE status IN ('active','past_due') AND next_charge_at<=?", now)
            for s in due:
                uid = s["user_id"]
                if s["cancel_at_period_end"]:
                    self.end_subscription(s, "해지 예약에 따라 종료")
                    out.append({"user": uid, "result": "ended"})
                    continue
                plan = s["pending_plan"] or s["plan"]
                # deterministic order id per attempt: a crash/restart can't double-charge (Idempotency-Key)
                order_id = f"sub_{uid[-10:]}_{int(s['next_charge_at'])}_{s['retry_count']}"
                ok, msg = self.charge(uid, plan, s["billing_key"], s["customer_key"], order_id, now)
                if ok:
                    end = add_month(now)
                    self.db.x("UPDATE subscriptions SET plan=?, pending_plan=NULL, status='active', period_start=?, period_end=?, "
                              "next_charge_at=?, retry_count=0, last_error=NULL, updated_at=? WHERE user_id=?",
                              plan, now, end, end, now, uid)
                    self.set_plan(uid, plan)
                    out.append({"user": uid, "result": "renewed", "plan": plan})
                elif s["retry_count"] + 1 >= MAX_RETRIES:
                    self.end_subscription(s, f"결제 {MAX_RETRIES}회 실패: {msg}")
                    out.append({"user": uid, "result": "canceled", "error": msg})
                else:
                    self.db.x("UPDATE subscriptions SET status='past_due', retry_count=retry_count+1, next_charge_at=?, "
                              "last_error=?, updated_at=? WHERE user_id=?", now + RETRY_SEC, msg, now, uid)
                    out.append({"user": uid, "result": "retry", "error": msg})
        return out

    def start_scheduler(self, interval: float) -> None:
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(interval):
                try:
                    for r in self.run_due():
                        log.info("billing: %s", r)
                except Exception:  # keep the scheduler alive
                    log.exception("billing run failed")

        threading.Thread(target=loop, name="billing", daemon=True).start()
        self.app.state.billing_stop = stop

    # ------------------------------------------------------------------ routes
    def register(self) -> None:
        app, db = self.app, self.db

        def uid_of(request: Request) -> str:
            return request.state.user["id"]

        def need_client() -> TossClient:
            if not self.client:
                raise HTTPException(503, "결제가 아직 설정되지 않았어요 (관리자: 토스페이먼츠 키 설정 필요)")
            return self.client

        def done(ok: bool, msg: str) -> RedirectResponse:
            return RedirectResponse(f"/#/pay-result?kind=billing&status={'ok' if ok else 'fail'}&msg={quote(msg)}", 303)

        @app.get("/api/billing")
        def billing_status(request: Request):
            return self.view(uid_of(request))

        def auth_info(request: Request, success_query: str) -> dict:
            client = need_client()
            u = request.state.user
            return {"client_key": client.config.client_key, "sdk_url": client.config.sdk_url,
                    "customer_key": customer_key(db, u["id"]), "customer_email": u["email"], "customer_name": u["name"],
                    "success_url": f"{self.public_url}/billing/success?{success_query}",
                    "fail_url": f"{self.public_url}/billing/fail", "test_mode": client.config.test_mode}

        @app.post("/api/billing/start")
        def billing_start(request: Request, body: PlanBody):
            if body.plan not in PAID_PLANS:
                raise HTTPException(400, "구독할 수 없는 요금제예요")
            s = self.sub(uid_of(request))
            if s and s["status"] in ("active", "past_due"):
                raise HTTPException(409, "이미 구독 중이에요. 요금제 변경을 이용하세요")
            return auth_info(request, f"plan={body.plan}")

        @app.post("/api/billing/card")
        def billing_card(request: Request):
            s = self.sub(uid_of(request))
            if not s or s["status"] not in ("active", "past_due"):
                raise HTTPException(400, "구독 중이 아니에요")
            return auth_info(request, "mode=card")

        @app.get("/billing/success")
        def billing_success(request: Request, customerKey: str = "", authKey: str = "", plan: str = "", mode: str = ""):
            client = need_client()
            uid = uid_of(request)
            ckey = customer_key(db, uid)
            if customerKey != ckey or not authKey:
                return done(False, "카드 등록 정보가 올바르지 않아요")
            try:
                issued = client.issue_billing_key(authKey, ckey)
            except TossError as e:
                return done(False, f"카드 등록에 실패했어요: {e.message}")
            bkey, label = issued["billingKey"], card_label(issued)
            s = self.sub(uid)
            if mode == "card":
                if not s:
                    client.delete_billing_key(bkey)
                    return done(False, "구독 중이 아니에요")
                if s["billing_key"]:
                    client.delete_billing_key(s["billing_key"])
                db.x("UPDATE subscriptions SET billing_key=?, card_label=?, updated_at=? WHERE user_id=?",
                     bkey, label, time.time(), uid)
                if s["status"] == "past_due":  # retry the failed renewal on the new card right away
                    db.x("UPDATE subscriptions SET next_charge_at=? WHERE user_id=?", time.time(), uid)
                    self.run_due()
                return done(True, f"결제 카드를 {label}(으)로 바꿨어요")
            if plan not in PAID_PLANS:
                client.delete_billing_key(bkey)
                return done(False, "요금제를 확인해 주세요")
            if s and s["status"] in ("active", "past_due"):
                client.delete_billing_key(bkey)
                return done(False, "이미 구독 중이에요")
            now = time.time()
            ok, msg = self.charge(uid, plan, bkey, ckey, new_id("sub_"), now)
            if not ok:
                client.delete_billing_key(bkey)
                return done(False, f"첫 결제에 실패했어요: {msg}")
            end = add_month(now)
            db.x("INSERT INTO subscriptions (user_id,plan,status,billing_key,customer_key,card_label,period_start,period_end,"
                 "next_charge_at,retry_count,cancel_at_period_end,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,0,0,?,?) "
                 "ON CONFLICT(user_id) DO UPDATE SET plan=excluded.plan, pending_plan=NULL, status='active', "
                 "billing_key=excluded.billing_key, card_label=excluded.card_label, period_start=excluded.period_start, "
                 "period_end=excluded.period_end, next_charge_at=excluded.next_charge_at, retry_count=0, "
                 "cancel_at_period_end=0, last_error=NULL, updated_at=excluded.updated_at",
                 uid, plan, "active", bkey, ckey, label, now, end, end, now, now)
            self.set_plan(uid, plan)
            return done(True, f"{PLANS[plan]['label']} 구독을 시작했어요")

        @app.get("/billing/fail")
        def billing_fail(code: str = "", message: str = ""):
            return done(False, "카드 등록을 취소했어요" if code == "PAY_PROCESS_CANCELED" else (message or "카드 등록에 실패했어요"))

        def active_sub(uid: str) -> dict:
            s = self.sub(uid)
            if not s or s["status"] not in ("active", "past_due"):
                raise HTTPException(400, "구독 중이 아니에요")
            return s

        @app.post("/api/billing/cancel")
        def billing_cancel(request: Request):
            active_sub(uid_of(request))
            db.x("UPDATE subscriptions SET cancel_at_period_end=1, updated_at=? WHERE user_id=?", time.time(), uid_of(request))
            return self.view(uid_of(request))

        @app.post("/api/billing/resume")
        def billing_resume(request: Request):
            active_sub(uid_of(request))
            db.x("UPDATE subscriptions SET cancel_at_period_end=0, updated_at=? WHERE user_id=?", time.time(), uid_of(request))
            return self.view(uid_of(request))

        @app.post("/api/billing/change")
        def billing_change(request: Request, body: PlanBody):
            s = active_sub(uid_of(request))
            if body.plan not in PAID_PLANS:
                raise HTTPException(400, "무료로 바꾸려면 구독 해지를 이용하세요")
            pending = None if body.plan == s["plan"] else body.plan
            db.x("UPDATE subscriptions SET pending_plan=?, cancel_at_period_end=0, updated_at=? WHERE user_id=?",
                 pending, time.time(), uid_of(request))
            return self.view(uid_of(request))

        @app.post("/api/webhooks/toss")
        async def toss_webhook(request: Request):
            """PAYMENT_STATUS_CHANGED etc. The body is untrusted: we only use it to know which payment to re-read."""
            try:
                body = await request.json()
            except ValueError:
                return {"ok": False}
            data = body.get("data") or {}
            pk = data.get("paymentKey")
            if not pk or not self.client:
                return {"ok": True, "ignored": True}
            try:
                p = self.client.get(pk)
            except TossError as e:
                db.x("INSERT INTO payment_events (source,event_type,payment_key,note,created_at) VALUES (?,?,?,?,?)",
                     "toss", body.get("eventType"), pk, f"lookup failed: {e.code}", time.time())
                return {"ok": False}
            note = self.reconcile(p)
            db.x("INSERT INTO payment_events (source,event_type,payment_key,order_id,status,note,created_at) VALUES (?,?,?,?,?,?,?)",
                 "toss", body.get("eventType"), pk, p.get("orderId"), p.get("status"), note, time.time())
            return {"ok": True, "note": note}

    def reconcile(self, p: dict) -> str:
        """Bring our records in line with what Toss says about one payment."""
        db, oid, status = self.db, p.get("orderId", ""), p.get("status")
        o = db.one("SELECT * FROM orders WHERE id=?", oid)
        if o:
            if status == "DONE" and o["status"] == "pending":
                if int(p.get("totalAmount", -1)) != int(o["amount"]):
                    self.client.cancel(p["paymentKey"], "금액 불일치", idem=f"cancel-verify-{oid}")
                    db.x("UPDATE orders SET status='failed', failure='금액 불일치' WHERE id=?", oid)
                    return "amount mismatch: canceled"
                ok = self.app.state.sales_mark_paid(oid, {"payment_key": p["paymentKey"], "receipt_url": receipt_url(p),
                                                          "method": p.get("method")})
                if not ok:
                    self.client.cancel(p["paymentKey"], "재고 소진으로 자동 취소", idem=f"cancel-{oid}")
                    return "sold out: canceled"
                return "order paid (recovered)"
            if status in ("CANCELED", "PARTIAL_CANCELED") and o["status"] == "paid":
                self.app.state.sales_mark_refunded(oid)
                return "order refunded"
            if status in ("ABORTED", "EXPIRED") and o["status"] == "pending":
                db.x("UPDATE orders SET status='cancelled', failure=? WHERE id=?", status, oid)
                return "order cancelled"
            return "order unchanged"
        c = db.one("SELECT * FROM plan_charges WHERE order_id=?", oid)
        if c and status in ("CANCELED", "PARTIAL_CANCELED") and c["status"] == "paid":
            db.x("UPDATE plan_charges SET status='refunded' WHERE order_id=?", oid)
            return "plan charge refunded"
        return "no matching record"


def register(app: FastAPI, client: TossClient | None, public_url: str, *, scheduler_interval: float = 0) -> Billing:
    b = Billing(app, client, public_url)
    b.register()
    app.state.billing = b
    if client and scheduler_interval > 0:
        b.start_scheduler(scheduler_interval)
    return b
