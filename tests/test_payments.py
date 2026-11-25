import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    sys.path.insert(0, str(p))

pytest.importorskip("fastapi")
from fake_toss import FakeToss  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from webapp.billing import add_month  # noqa: E402
from webapp.db import new_id  # noqa: E402
from webapp.server import create_app  # noqa: E402
from webapp.settlement import mark_paid, payable, to_csv  # noqa: E402
from webapp.toss import TossClient, TossConfig, TossError  # noqa: E402

CSRF = {"X-Requested-With": "legend"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    fake = FakeToss()
    monkeypatch.setenv("TOSS_CLIENT_KEY", "test_ck_fake")
    monkeypatch.setenv("TOSS_SECRET_KEY", "test_sk_fake")
    monkeypatch.setenv("LEGEND_TOSS_BASE", "http://fake-toss")
    app = create_app(tmp_path / "data", public_url="http://testserver",
                     toss_http=httpx.Client(transport=httpx.MockTransport(fake.handle)))
    return SimpleNamespace(app=app, fake=fake, db=app.state.db)


def user(env, email):
    c = TestClient(env.app, headers=CSRF)
    assert c.post("/api/auth/signup", json={"email": email, "password": "password123", "name": email.split("@")[0]}).status_code == 200
    c.uid = c.get("/api/auth/me").json()["user"]["id"]
    return c


def listing(env, seller, price=12000, max_sales=5):
    """A listing row with dummy media (payments don't care about the video)."""
    lid = new_id("l")
    d = env.app.state.sales_root / lid
    d.mkdir(parents=True)
    (d / "master.mp4").write_bytes(b"master")
    (d / "preview.mp4").write_bytes(b"preview")
    env.db.x("INSERT INTO sellers (user_id,legal_name,email,business_no,terms_version,agreed_at) VALUES (?,?,?,?,?,?) "
             "ON CONFLICT(user_id) DO NOTHING", seller.uid, "판매자", "s@x.kr", "", "v1", time.time())
    env.db.x("INSERT INTO listings (id,seller_id,project_id,clip_id,title,credit,source_video_id,source_title,channel_title,"
             "duration,score,price,license_type,license_days,max_sales,deadline,status,master_file,preview_file,created_at) "
             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", lid, seller.uid, "p", "c1", "클립", "원본: X", "V", "X", "채널",
             20, 70, price, "nonexclusive", 365, max_sales, time.time() + 86400 * 30, "on_sale", "master.mp4", "preview.mp4",
             time.time())
    return lid


def result(resp):
    """Parse our /#/pay-result redirect."""
    assert resp.status_code == 303, resp.text
    q = parse_qs(urlparse(resp.headers["location"].replace("/#", "")).query)
    return q["status"][0], unquote(q["msg"][0])


# ---------------------------------------------------------------- client basics

def test_client_auth_idempotency_and_errors():
    fake = FakeToss()
    c = TossClient(TossConfig("test_ck", "test_sk_fake", "http://fake-toss"), httpx.Client(transport=httpx.MockTransport(fake.handle)))
    pk = fake.authorize("order_0001", 5000)
    a = c.confirm(pk, "order_0001", 5000)
    assert a["status"] == "DONE" and c.confirm(pk, "order_0001", 5000) == a  # same Idempotency-Key → same answer
    with pytest.raises(TossError) as e:
        TossClient(TossConfig("ck", "wrong", "http://fake-toss"), httpx.Client(transport=httpx.MockTransport(fake.handle))).get(pk)
    assert e.value.code == "UNAUTHORIZED_KEY"
    assert add_month(time.mktime((2026, 1, 31, 12, 0, 0, 0, 0, 0))) > 0  # Jan 31 → Feb 28, no crash


# ---------------------------------------------------------------- clip purchases

def test_toss_purchase_verify_refund_and_replay(env):
    seller, buyer = user(env, "seller@x.kr"), user(env, "buyer@x.kr")
    lid = listing(env, seller)
    co = buyer.post(f"/api/listings/{lid}/orders").json()
    assert co["provider"] == "toss" and co["client_key"] == "test_ck_fake" and co["success_url"] == "http://testserver/pay/success"
    assert co["customer_key"].startswith("cus_") and "secret" not in str(co).lower() and co["amount"] == 12000
    assert buyer.post(f"/api/orders/{co['order_id']}/confirm").status_code == 400  # test-mode shortcut is off

    # tampered amount in the success URL → no confirm, no charge
    pk = env.fake.authorize(co["order_id"], 12000)
    st, msg = result(buyer.get(f"/pay/success?paymentKey={pk}&orderId={co['order_id']}&amount=100", follow_redirects=False))
    assert st == "fail" and "금액" in msg and pk in env.fake.pending

    co = buyer.post(f"/api/listings/{lid}/orders").json()
    pk = env.fake.authorize(co["order_id"], 12000)
    url = f"/pay/success?paymentKey={pk}&orderId={co['order_id']}&amount=12000"
    assert seller.get(url, follow_redirects=False).headers["location"].count("status=fail")  # not the buyer
    st, msg = result(buyer.get(url, follow_redirects=False))
    assert st == "ok" and env.fake.payments[pk]["status"] == "DONE"
    assert result(buyer.get(url, follow_redirects=False))[0] == "ok"  # reload is harmless
    o = buyer.get("/api/orders").json()["orders"][0]
    assert o["status"] == "paid" and o["receipt_url"].endswith(pk) and o["method"] == "카드" and o["license_code"]
    assert env.db.one("SELECT sold FROM listings WHERE id=?", lid)["sold"] == 1

    assert buyer.post(f"/api/orders/{o['id']}/refund").json()["status"] == "refunded"
    assert env.fake.payments[pk]["status"] == "CANCELED"
    assert env.db.one("SELECT sold FROM listings WHERE id=?", lid)["sold"] == 0

    co = buyer.post(f"/api/listings/{lid}/orders").json()
    st, msg = result(buyer.get(f"/pay/fail?code=PAY_PROCESS_CANCELED&message=x&orderId={co['order_id']}", follow_redirects=False))
    assert st == "fail" and msg == "결제를 취소했어요"
    assert env.db.one("SELECT status FROM orders WHERE id=?", co["order_id"])["status"] == "cancelled"


def test_last_slot_race_and_declined_card(env):
    seller, a, b = user(env, "s2@x.kr"), user(env, "a@x.kr"), user(env, "b@x.kr")
    lid = listing(env, seller, max_sales=1)
    oa, ob = a.post(f"/api/listings/{lid}/orders").json(), b.post(f"/api/listings/{lid}/orders").json()
    pa, pb = env.fake.authorize(oa["order_id"], 12000), env.fake.authorize(ob["order_id"], 12000)
    assert result(a.get(f"/pay/success?paymentKey={pa}&orderId={oa['order_id']}&amount=12000", follow_redirects=False))[0] == "ok"
    st, msg = result(b.get(f"/pay/success?paymentKey={pb}&orderId={ob['order_id']}&amount=12000", follow_redirects=False))
    assert st == "fail" and "판매가 끝" in msg and pb not in env.fake.payments  # never charged

    lid2 = listing(env, seller)
    env.fake.fail_confirm = True
    o = a.post(f"/api/listings/{lid2}/orders").json()
    p = env.fake.authorize(o["order_id"], 12000)
    st, msg = result(a.get(f"/pay/success?paymentKey={p}&orderId={o['order_id']}&amount=12000", follow_redirects=False))
    assert st == "fail" and "한도초과" in msg


def test_webhook_recovers_and_syncs_but_ignores_forgeries(env):
    seller, buyer = user(env, "s3@x.kr"), user(env, "c@x.kr")
    lid = listing(env, seller)
    anon = TestClient(env.app)
    # our confirm call "timed out" but Toss did approve: the webhook recovers the order
    co = buyer.post(f"/api/listings/{lid}/orders").json()
    pk = env.fake.authorize(co["order_id"], 12000)
    env.app.state.toss.confirm(pk, co["order_id"], 12000)
    r = anon.post("/api/webhooks/toss", json={"eventType": "PAYMENT_STATUS_CHANGED", "data": {"paymentKey": pk, "status": "DONE"}})
    assert r.json()["note"] == "order paid (recovered)"
    assert env.db.one("SELECT status FROM orders WHERE id=?", co["order_id"])["status"] == "paid"
    # cancelled in the Toss dashboard → refunded here
    env.app.state.toss.cancel(pk, "관리자 취소", idem="x1")
    assert anon.post("/api/webhooks/toss", json={"data": {"paymentKey": pk}}).json()["note"] == "order refunded"
    # a forged event naming a payment Toss doesn't know changes nothing
    co2 = buyer.post(f"/api/listings/{lid}/orders").json()
    r = anon.post("/api/webhooks/toss", json={"data": {"paymentKey": "tpk_forged", "orderId": co2["order_id"], "status": "DONE"}})
    assert r.json()["ok"] is False
    assert env.db.one("SELECT status FROM orders WHERE id=?", co2["order_id"])["status"] == "pending"


def test_production_without_keys_refuses_to_sell(tmp_path, monkeypatch):
    for k in ("TOSS_CLIENT_KEY", "TOSS_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    app = create_app(tmp_path / "d", public_url="http://testserver", allow_mock_payments=False)
    env = SimpleNamespace(app=app, db=app.state.db)
    seller, buyer = user(env, "s@x.kr"), user(env, "b@x.kr")
    r = buyer.post(f"/api/listings/{listing(env, seller)}/orders")
    assert r.status_code == 503 and "결제" in r.json()["detail"]
    assert buyer.post("/api/billing/start", json={"plan": "creator"}).status_code == 503


# ---------------------------------------------------------------- subscriptions

def subscribe(env, c, plan="creator"):
    info = c.post("/api/billing/start", json={"plan": plan}).json()
    assert info["success_url"] == f"http://testserver/billing/success?plan={plan}"
    ak = env.fake.billing_auth(info["customer_key"])
    return result(c.get(f"/billing/success?plan={plan}&customerKey={info['customer_key']}&authKey={ak}", follow_redirects=False))


def test_subscribe_renew_change_and_cancel(env):
    c = user(env, "sub@x.kr")
    assert c.get("/api/billing").json()["configured"] is True
    info = c.post("/api/billing/start", json={"plan": "creator"}).json()
    bad = result(c.get(f"/billing/success?plan=creator&customerKey=cus_someone_else&authKey=x", follow_redirects=False))
    assert bad[0] == "fail"
    assert subscribe(env, c) == ("ok", "Creator 구독을 시작했어요")
    assert c.get("/api/auth/me").json()["usage"]["limit_minutes"] == 300
    b = c.get("/api/billing").json()
    assert b["subscription"]["status"] == "active" and b["subscription"]["card_label"].startswith("신한")
    assert [x["amount"] for x in b["charges"]] == [19000] and b["charges"][0]["receipt_url"]
    assert c.post("/api/billing/start", json={"plan": "pro"}).status_code == 409

    sub = env.db.one("SELECT * FROM subscriptions WHERE user_id=?", c.uid)
    assert env.app.state.billing.run_due(sub["next_charge_at"] - 10) == []  # not due yet
    c.post("/api/billing/change", json={"plan": "pro"})
    assert c.get("/api/billing").json()["subscription"]["pending_plan"] == "pro"
    r = env.app.state.billing.run_due(sub["next_charge_at"] + 1)
    assert r == [{"user": c.uid, "result": "renewed", "plan": "pro"}]
    assert c.get("/api/auth/me").json()["user"]["plan"] == "pro"
    assert [x["amount"] for x in c.get("/api/billing").json()["charges"]] == [49000, 19000]
    assert env.app.state.billing.run_due(sub["next_charge_at"] + 2) == []  # a second run doesn't double-charge

    c.post("/api/billing/cancel")
    sub = env.db.one("SELECT * FROM subscriptions WHERE user_id=?", c.uid)
    assert env.app.state.billing.run_due(sub["next_charge_at"] + 1)[0]["result"] == "ended"
    assert c.get("/api/auth/me").json()["user"]["plan"] == "free"
    assert not env.fake.billing_keys[sub["billing_key"]]["active"]  # billing key deleted at Toss


def test_failed_renewals_retry_then_downgrade_and_card_change_recovers(env):
    c = user(env, "late@x.kr")
    subscribe(env, c)
    sub = env.db.one("SELECT * FROM subscriptions WHERE user_id=?", c.uid)
    env.fake.fail_billing = 1
    t = sub["next_charge_at"] + 1
    assert env.app.state.billing.run_due(t)[0]["result"] == "retry"
    s = c.get("/api/billing").json()["subscription"]
    assert s["status"] == "past_due" and "거절" in s["last_error"] and c.get("/api/auth/me").json()["user"]["plan"] == "creator"
    # the user registers a new card: the overdue charge is retried right away
    info = c.post("/api/billing/card").json()
    ak = env.fake.billing_auth(info["customer_key"])
    st, _ = result(c.get(f"/billing/success?mode=card&customerKey={info['customer_key']}&authKey={ak}", follow_redirects=False))
    assert st == "ok" and c.get("/api/billing").json()["subscription"]["status"] == "active"

    sub = env.db.one("SELECT * FROM subscriptions WHERE user_id=?", c.uid)
    env.fake.fail_billing = 3
    t = sub["next_charge_at"] + 1
    results = [env.app.state.billing.run_due(t + i * 86400 + i)[0]["result"] for i in range(3)]
    assert results == ["retry", "retry", "canceled"]
    assert c.get("/api/auth/me").json()["user"]["plan"] == "free"


# ---------------------------------------------------------------- seller payouts

def test_payouts_after_refund_window(env):
    seller, buyer = user(env, "pay@x.kr"), user(env, "pb@x.kr")
    lid = listing(env, seller, price=10000)
    for _ in range(2):
        co = buyer.post(f"/api/listings/{lid}/orders").json()
        pk = env.fake.authorize(co["order_id"], 10000)
        buyer.get(f"/pay/success?paymentKey={pk}&orderId={co['order_id']}&amount=10000", follow_redirects=False)
    sold = seller.get("/api/orders?scope=sold").json()["totals"]
    assert sold["hold"] == 16000 and sold["payable"] == 0
    assert payable(env.db) == []
    later = time.time() + 8 * 86400
    rows = payable(env.db, now=later)
    assert len(rows) == 1 and rows[0]["net"] == 16000 and rows[0]["orders"] == 2
    assert "판매자,s@x.kr" in to_csv(rows).replace(",,", ",")
    assert mark_paid(env.db, rows, now=later) == 2 and payable(env.db, now=later) == []
    assert seller.get("/api/orders?scope=sold").json()["totals"]["settled"] == 16000
