"""Clip sales: creators sell licences to clips made from their own (verified) channel videos.

Legal-risk design (not legal advice — have a lawyer review before launch):
- Only clips from ``own_channel`` projects (channel ownership + matching file verified) can be listed.
- Sellers register seller info and warrant they hold copyright / portrait / music rights.
- Buyers get a non-exclusive or exclusive *licence* with a certificate: credit line required,
  licence period, permitted use, no resale. Publishing through Legend inserts the credit.
- Watermarked low-res preview before purchase; refunds allowed within 7 days only before the
  clip is downloaded or published (digital-content withdrawal limits must be notified up front).
- Legend is shown as a marketplace intermediary (통신판매중개자) with the seller's info.
- Payments go through a provider interface. ``MockPayments`` is a test mode; real money must go
  through a PG's escrow / settlement service so Legend never holds funds itself.
"""

from __future__ import annotations

import html
import math
import secrets
import shutil
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from highlight_engine.render.media import filter_path
from highlight_engine.render.textmetrics import font_file

from .db import DB, new_id
from .payments import MockPayments, PaymentUnavailable, customer_key
from .toss import TossError
from .youtube import AccountRegistry, VideoMeta, YouTubeError

FEE_RATE = 0.20
TERMS_VERSION = "2026-11-v1"
LICENSE_DAYS = (30, 90, 365, 0)  # 0 = no end date
REFUND_DAYS = 7
MIN_PRICE, MAX_PRICE = 1_000, 5_000_000


def suggest_price(source_views: int, score: int, duration: float) -> dict:
    """Rule of thumb: bigger source audience and stronger clip → higher price (non-exclusive)."""
    reach = math.log10(max(10, source_views))  # 1 (tiny) … 7 (10M views)
    quality = max(0.5, min(1.5, score / 60))
    length = 1.0 if duration <= 60 else 1.2
    raw = 2000 + 1800 * reach * quality * length
    price = int(min(200_000, max(3_000, round(raw / 1000) * 1000)))
    return {"price": price, "exclusive_price": price * 5,
            "basis": f"원본 조회수 {source_views:,}회 · 클립 점수 {score} · {duration:.0f}초 기준 추천가"}


class SellerInfo(BaseModel):
    legal_name: str = Field(min_length=1, max_length=60)
    email: str = Field(min_length=3, max_length=120)
    business_no: str = ""


class ListingCreate(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    description: str = Field("", max_length=1000)
    price: int
    license_type: str = "nonexclusive"
    license_days: int = 365
    deadline: str
    max_sales: int = Field(50, ge=1, le=1000)
    credit: str = ""
    seller: SellerInfo
    agree: bool = False


class ListingPatch(BaseModel):
    status: str | None = None
    price: int | None = None


class Report(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class Confirm(BaseModel):
    payment_key: str | None = None


class BuyerPublish(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    privacy: str = "public"
    publish_at: str | None = None


def make_preview(master: str, out: str) -> None:
    """Low-res, low-bitrate, watermarked preview (what buyers can watch before paying)."""
    ff = font_file("Noto Sans CJK KR", True)
    font = f"fontfile='{filter_path(ff[0])}':" if ff else ""
    mark = (f"drawtext={font}text='LEGEND 미리보기':fontsize=40:fontcolor=white@0.38:borderw=2:bordercolor=black@0.25:"
            "x=(w-text_w)/2:y=h*{y}")
    vf = f"scale=540:-2,{mark.format(y='0.28')},{mark.format(y='0.62')}"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", master, "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "33", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", out], check=True)


def register(app: FastAPI, registry: AccountRegistry) -> None:
    db: DB = app.state.db
    store, jobs = app.state.store, app.state.jobs
    root = store.root / "sales"
    root.mkdir(parents=True, exist_ok=True)
    if not hasattr(app.state, "payments"):
        app.state.payments = MockPayments()
    app.state.sales_root = root

    def uid_of(request: Request) -> str:
        return request.state.user["id"]

    def listing(lid: str) -> dict:
        row = db.one("SELECT * FROM listings WHERE id=?", lid)
        if not row:
            raise HTTPException(404, "판매 클립을 찾을 수 없습니다")
        return row

    def is_open(l: dict) -> bool:
        return l["status"] == "on_sale" and l["deadline"] > time.time() and l["sold"] < l["max_sales"]

    def summary(l: dict) -> dict:
        seller = db.one("SELECT legal_name, email, business_no FROM sellers WHERE user_id=?", l["seller_id"]) or {}
        return {**{k: v for k, v in l.items() if k not in ("master_file", "preview_file")}, "open": is_open(l),
                "remaining": max(0, l["max_sales"] - l["sold"]), "seller": seller,
                "preview_url": f"/api/listings/{l['id']}/preview.mp4", "source_url": f"https://youtu.be/{l['source_video_id']}"}

    def order(oid: str, uid: str, *, buyer_only: bool = False) -> dict:
        o = db.one("SELECT * FROM orders WHERE id=?", oid)
        if not o or (o["buyer_id"] != uid and (buyer_only or o["seller_id"] != uid)):
            raise HTTPException(404, "주문을 찾을 수 없습니다")
        return o

    def license_valid(o: dict) -> bool:
        return o["status"] == "paid" and (not o["license_until"] or o["license_until"] > time.time())

    # ------------------------------------------------------------------ seller
    @app.get("/api/me/seller")
    def my_seller(request: Request):
        return db.one("SELECT legal_name, email, business_no, agreed_at FROM sellers WHERE user_id=?", uid_of(request))

    @app.get("/api/projects/{pid}/clips/{cid}/listing")
    def clip_listing(pid: str, cid: str):
        """Current listing for this clip (if any) plus a suggested price."""
        project = store.get(pid)
        clip = store.find_clip(project, cid)
        src = project.get("source") or {}
        row = db.one("SELECT * FROM listings WHERE project_id=? AND clip_id=? AND status!='removed' "
                     "ORDER BY created_at DESC", pid, cid)
        return {"eligible": src.get("type") == "own_channel",
                "listing": summary(row) if row else None,
                "suggestion": suggest_price(int(src.get("views") or 0), int(clip.get("score") or 0),
                                            clip["end"] - clip["start"]),
                "default_credit": f"원본: {src.get('title', '')} https://youtu.be/{src.get('video_id', '')}" if src else "",
                "fee_rate": FEE_RATE, "license_days": LICENSE_DAYS, "terms_version": TERMS_VERSION}

    @app.post("/api/projects/{pid}/clips/{cid}/listing")
    def create_listing(request: Request, pid: str, cid: str, body: ListingCreate):
        uid = uid_of(request)
        project = store.get(pid)
        clip = store.find_clip(project, cid)
        src = project.get("source") or {}
        if src.get("type") != "own_channel":
            raise HTTPException(400, "판매는 '내 채널'에서 소유 확인을 거친 영상의 클립만 가능합니다")
        if not body.agree:
            raise HTTPException(400, "권리 보유 확인과 판매 약관에 동의해야 합니다")
        if not MIN_PRICE <= body.price <= MAX_PRICE:
            raise HTTPException(400, f"가격은 {MIN_PRICE:,}~{MAX_PRICE:,}원이어야 합니다")
        if body.license_type not in ("nonexclusive", "exclusive"):
            raise HTTPException(400, "라이선스 종류가 올바르지 않습니다")
        if body.license_days not in LICENSE_DAYS:
            raise HTTPException(400, "라이선스 기간이 올바르지 않습니다")
        if "@" not in body.seller.email:
            raise HTTPException(400, "판매자 연락 이메일을 확인하세요")
        try:
            dl = datetime.fromisoformat(body.deadline.replace("Z", "+00:00"))
            assert dl.tzinfo is not None
        except (ValueError, AssertionError):
            raise HTTPException(400, "판매 마감일 형식이 올바르지 않습니다")
        if not time.time() + 3600 < dl.timestamp() < time.time() + 366 * 86400:
            raise HTTPException(400, "판매 마감일은 1시간 이후 ~ 1년 이내여야 합니다")
        if db.one("SELECT 1 FROM listings WHERE project_id=? AND clip_id=? AND status='on_sale'", pid, cid):
            raise HTTPException(409, "이미 판매 중인 클립입니다")
        max_sales = 1 if body.license_type == "exclusive" else body.max_sales
        credit = (body.credit.strip() or f"원본: {src['title']} https://youtu.be/{src['video_id']}")[:300]
        db.x("INSERT INTO sellers (user_id,legal_name,email,business_no,terms_version,agreed_at) VALUES (?,?,?,?,?,?) "
             "ON CONFLICT(user_id) DO UPDATE SET legal_name=excluded.legal_name, email=excluded.email, "
             "business_no=excluded.business_no, terms_version=excluded.terms_version, agreed_at=excluded.agreed_at",
             uid, body.seller.legal_name.strip(), body.seller.email.strip(), body.seller.business_no.strip()[:20],
             TERMS_VERSION, time.time())
        channel = (registry.for_user(uid).load() or {}).get("channel") or {}

        def work(*, progress) -> dict:
            c = store.find_clip(store.get(pid), cid)
            if not app.state.render_is_current(pid, c):
                app.state.render_clip(pid, cid, lambda f, m=None: progress(0.8 * (f or 0), "렌더링 중"))
                c = store.find_clip(store.get(pid), cid)
            lid = new_id("l")
            d = root / lid
            d.mkdir(parents=True)
            master = d / "master.mp4"
            # a frozen copy: later edits to the project never change what buyers paid for
            shutil.copyfile(store.dir(pid) / "renders" / c["render"]["file"], master)
            progress(0.85, "미리보기 만드는 중")
            make_preview(str(master), str(d / "preview.mp4"))
            db.x("INSERT INTO listings (id,seller_id,project_id,clip_id,title,description,credit,source_video_id,source_title,"
                 "channel_title,duration,score,price,license_type,license_days,max_sales,deadline,status,master_file,"
                 "preview_file,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 lid, uid, pid, cid, body.title.strip(), body.description.strip(), credit, src["video_id"], src["title"],
                 channel.get("title", ""), c["end"] - c["start"], int(c.get("score") or 0), body.price,
                 body.license_type, body.license_days, max_sales, dl.timestamp(), "on_sale", "master.mp4",
                 "preview.mp4", time.time())
            return {"listing_id": lid}

        jid = jobs.submit("listing", work, project=pid, clip=cid, owner=uid)
        return {"job_id": jid}

    @app.patch("/api/listings/{lid}")
    def patch_listing(request: Request, lid: str, body: ListingPatch):
        l = listing(lid)
        if l["seller_id"] != uid_of(request):
            raise HTTPException(404, "판매 클립을 찾을 수 없습니다")
        if body.status is not None:
            if body.status not in ("on_sale", "closed"):
                raise HTTPException(400, "상태가 올바르지 않습니다")
            if body.status == "on_sale" and l["sold"] >= l["max_sales"]:
                raise HTTPException(400, "이미 모두 판매되었습니다")
            db.x("UPDATE listings SET status=? WHERE id=?", body.status, lid)
        if body.price is not None:
            if not MIN_PRICE <= body.price <= MAX_PRICE:
                raise HTTPException(400, f"가격은 {MIN_PRICE:,}~{MAX_PRICE:,}원이어야 합니다")
            db.x("UPDATE listings SET price=? WHERE id=?", body.price, lid)  # applies to future orders only
        return summary(listing(lid))

    # ------------------------------------------------------------------ shop
    @app.get("/api/listings")
    def list_listings(request: Request, scope: str = "shop"):
        if scope == "mine":
            rows = db.q("SELECT * FROM listings WHERE seller_id=? AND status!='removed' ORDER BY created_at DESC", uid_of(request))
        else:
            rows = [l for l in db.q("SELECT * FROM listings WHERE status='on_sale' ORDER BY created_at DESC") if is_open(l)]
        return [summary(l) for l in rows]

    @app.get("/api/listings/{lid}")
    def get_listing(request: Request, lid: str):
        l = listing(lid)
        uid = uid_of(request)
        mine = l["seller_id"] == uid
        if l["status"] == "removed" and not mine:
            raise HTTPException(404, "판매 클립을 찾을 수 없습니다")
        bought = db.q("SELECT id, status FROM orders WHERE listing_id=? AND buyer_id=? AND status='paid'", lid, uid)
        return {**summary(l), "is_seller": mine, "my_orders": bought, "fee_rate": FEE_RATE, "refund_days": REFUND_DAYS}

    @app.get("/api/listings/{lid}/preview.mp4")
    def preview(lid: str):
        l = listing(lid)
        return FileResponse(root / lid / l["preview_file"], media_type="video/mp4")

    @app.post("/api/listings/{lid}/report")
    def report(request: Request, lid: str, body: Report):
        listing(lid)
        db.x("INSERT INTO reports (listing_id,reporter_id,reason,created_at) VALUES (?,?,?,?)",
             lid, uid_of(request), body.reason.strip(), time.time())
        return {"ok": True, "message": "신고가 접수되었어요. 확인 후 조치할게요."}

    # ------------------------------------------------------------------ orders
    @app.post("/api/listings/{lid}/orders")
    def create_order(request: Request, lid: str):
        uid = uid_of(request)
        l = listing(lid)
        if l["seller_id"] == uid:
            raise HTTPException(400, "내 클립은 구매할 수 없습니다")
        if not is_open(l):
            raise HTTPException(400, "판매가 끝난 클립입니다")
        fee = int(round(l["price"] * FEE_RATE))
        oid = new_id("o")
        order_row = {"id": oid, "amount": l["price"]}
        try:
            checkout = app.state.payments.checkout(order_row, request.state.user, customer_key(db, uid))
        except PaymentUnavailable as e:
            raise HTTPException(503, str(e))
        db.x("INSERT INTO orders (id,listing_id,buyer_id,seller_id,amount,fee,seller_amount,status,provider,created_at) "
             "VALUES (?,?,?,?,?,?,?,?,?,?)", oid, lid, uid, l["seller_id"], l["price"], fee, l["price"] - fee, "pending",
             app.state.payments.name, time.time())
        return {"order_id": oid, "amount": l["price"], "order_name": l["title"][:40], **checkout}

    def mark_paid(oid: str, paid: dict) -> bool:
        """Grant the licence for a confirmed payment. False if the clip sold out meanwhile."""
        with db.tx():
            o = db.one("SELECT * FROM orders WHERE id=?", oid)
            if o["status"] == "paid":
                return True
            l = listing(o["listing_id"])
            if not is_open(l):
                db.x("UPDATE orders SET status='cancelled', failure=? WHERE id=?", "판매 종료", oid)
                return False
            now = time.time()
            until = now + l["license_days"] * 86400 if l["license_days"] else None
            code = "LGD-" + secrets.token_hex(4).upper()
            db.x("UPDATE orders SET status='paid', payment_key=?, paid_at=?, license_code=?, license_until=?, receipt_url=?, "
                 "method=? WHERE id=?", paid["payment_key"], now, code, until, paid.get("receipt_url"), paid.get("method"), oid)
            db.x("UPDATE listings SET sold=sold+1, status=CASE WHEN sold+1>=max_sales THEN 'sold_out' ELSE status END "
                 "WHERE id=?", l["id"])
            return True

    def settle_payment(o: dict, payment_key: str, amount: int) -> tuple[bool, str]:
        """Server-side checks, then confirm with the provider, then grant the licence."""
        if o["status"] == "paid":
            return True, "이미 결제된 주문이에요"
        if o["status"] != "pending":
            return False, "이미 처리된 주문이에요"
        if int(amount) != int(o["amount"]):  # the browser can't change what we charge
            db.x("UPDATE orders SET status='failed', failure=? WHERE id=?", "금액 불일치", o["id"])
            return False, "결제 금액이 주문과 달라 결제하지 않았어요"
        if not is_open(listing(o["listing_id"])):  # don't charge for something we can't deliver
            db.x("UPDATE orders SET status='cancelled', failure=? WHERE id=?", "판매 종료", o["id"])
            return False, "그 사이 판매가 끝났어요. 결제되지 않았어요"
        try:
            paid = app.state.payments.confirm(o, payment_key, int(amount))
        except (TossError, PaymentUnavailable) as e:
            db.x("UPDATE orders SET status='failed', failure=? WHERE id=?", str(e), o["id"])
            return False, str(e)
        if not mark_paid(o["id"], paid):  # sold out between the check and the confirm: give the money back
            try:
                app.state.payments.refund({**o, "payment_key": paid["payment_key"]}, "재고 소진으로 자동 취소")
            except TossError:
                pass
            return False, "그 사이 판매가 끝나 결제를 자동으로 취소했어요"
        return True, "구매가 완료됐어요"

    @app.post("/api/orders/{oid}/confirm")
    def confirm(request: Request, oid: str, body: Confirm = Confirm()):
        """Test-mode confirmation (real payments come back through /pay/success)."""
        uid = uid_of(request)
        o = order(oid, uid, buyer_only=True)
        if o["provider"] != "mock" or app.state.payments.name != "mock":
            raise HTTPException(400, "이 주문은 결제창에서 결제해야 해요")
        if o["status"] != "pending":
            raise HTTPException(400, "이미 처리된 주문입니다")
        ok, msg = settle_payment(o, body.payment_key or f"mock_{oid}", o["amount"])
        if not ok:
            raise HTTPException(409, msg)
        return order_view(order(oid, uid))

    def result_redirect(kind: str, ok: bool, msg: str, oid: str = "") -> RedirectResponse:
        return RedirectResponse(f"/#/pay-result?kind={kind}&status={'ok' if ok else 'fail'}&order={oid}&msg={quote(msg)}",
                                status_code=303)

    @app.get("/pay/success")
    def pay_success(request: Request, paymentKey: str = "", orderId: str = "", amount: int = 0):
        o = db.one("SELECT * FROM orders WHERE id=?", orderId)
        if not o or o["buyer_id"] != uid_of(request):
            return result_redirect("order", False, "주문을 찾을 수 없어요", orderId)
        ok, msg = settle_payment(o, paymentKey, amount)
        return result_redirect("order", ok, msg, orderId)

    @app.get("/pay/fail")
    def pay_fail(request: Request, code: str = "", message: str = "", orderId: str = ""):
        o = db.one("SELECT * FROM orders WHERE id=?", orderId)
        if o and o["buyer_id"] == uid_of(request) and o["status"] == "pending":
            db.x("UPDATE orders SET status='cancelled', failure=? WHERE id=?", f"{code}: {message}"[:300], orderId)
        msg = "결제를 취소했어요" if code == "PAY_PROCESS_CANCELED" else (message or "결제에 실패했어요")
        return result_redirect("order", False, msg, orderId)

    app.state.sales_mark_paid, app.state.sales_listing_open = mark_paid, lambda lid: is_open(listing(lid))

    def order_view(o: dict) -> dict:
        l = db.one("SELECT title, credit, license_type, license_days, source_video_id, channel_title, duration "
                   "FROM listings WHERE id=?", o["listing_id"])
        buyer = db.one("SELECT name FROM users WHERE id=?", o["buyer_id"])
        refundable = (o["status"] == "paid" and not o["downloaded_at"] and not o["published_url"]
                      and time.time() - (o["paid_at"] or 0) < REFUND_DAYS * 86400)
        settle = ("settled" if o["settled_at"] else "hold" if time.time() - (o["paid_at"] or 0) < REFUND_DAYS * 86400
                  else "payable") if o["status"] == "paid" else None
        return {**{k: v for k, v in o.items() if k != "payment_key"}, "listing": l, "buyer_name": buyer["name"] if buyer else "",
                "license_valid": license_valid(o), "refundable": refundable, "settle_status": settle}

    @app.get("/api/orders")
    def my_orders(request: Request, scope: str = "bought"):
        uid = uid_of(request)
        col = "seller_id" if scope == "sold" else "buyer_id"
        rows = db.q(f"SELECT * FROM orders WHERE {col}=? AND status IN ('paid','refunded') ORDER BY created_at DESC", uid)
        views = [order_view(o) for o in rows]
        paid = [o for o in rows if o["status"] == "paid"]
        if scope == "sold":
            now = time.time()
            hold = [o for o in paid if not o["settled_at"] and now - o["paid_at"] < REFUND_DAYS * 86400]
            payable = [o for o in paid if not o["settled_at"] and now - o["paid_at"] >= REFUND_DAYS * 86400]
            totals = {"gross": sum(o["amount"] for o in paid), "fee": sum(o["fee"] for o in paid),
                      "net": sum(o["seller_amount"] for o in paid),
                      "hold": sum(o["seller_amount"] for o in hold), "payable": sum(o["seller_amount"] for o in payable),
                      "settled": sum(o["seller_amount"] for o in paid if o["settled_at"])}
        else:
            totals = {"spent": sum(o["amount"] for o in paid)}
        return {"orders": views, "totals": totals}

    @app.get("/api/orders/{oid}/download")
    def download(request: Request, oid: str):
        o = order(oid, uid_of(request), buyer_only=True)
        if not license_valid(o):
            raise HTTPException(403, "유효한 라이선스가 없습니다")
        l = listing(o["listing_id"])
        if not o["downloaded_at"]:
            db.x("UPDATE orders SET downloaded_at=? WHERE id=?", time.time(), oid)
        return FileResponse(root / l["id"] / l["master_file"], media_type="video/mp4", filename=f"{o['license_code']}.mp4")

    @app.post("/api/orders/{oid}/refund")
    def refund(request: Request, oid: str):
        o = order(oid, uid_of(request), buyer_only=True)
        if not order_view(o)["refundable"]:
            raise HTTPException(400, f"환불은 결제 후 {REFUND_DAYS}일 이내, 다운로드·게시 전에만 가능합니다")
        try:
            app.state.payments.refund(o, "구매자 환불 요청 (다운로드·게시 전)")
        except TossError as e:
            raise HTTPException(502, f"환불 처리에 실패했어요: {e}")
        mark_refunded(oid)
        return order_view(order(oid, uid_of(request)))

    def mark_refunded(oid: str) -> None:
        """Record a refund and put the licence slot back on sale (also used by the Toss webhook)."""
        with db.tx():
            o = db.one("SELECT * FROM orders WHERE id=?", oid)
            if o["status"] != "paid":
                return
            db.x("UPDATE orders SET status='refunded', refunded_at=? WHERE id=?", time.time(), oid)
            db.x("UPDATE listings SET sold=MAX(0, sold-1), status=CASE WHEN status='sold_out' AND deadline>? THEN 'on_sale' "
                 "ELSE status END WHERE id=?", time.time(), o["listing_id"])

    app.state.sales_mark_refunded = mark_refunded

    @app.post("/api/orders/{oid}/publish")
    def publish_bought(request: Request, oid: str, body: BuyerPublish):
        uid = uid_of(request)
        o = order(oid, uid, buyer_only=True)
        if not license_valid(o):
            raise HTTPException(403, "유효한 라이선스가 없습니다")
        acc = registry.for_user(uid)
        if not acc.load():
            raise HTTPException(400, "먼저 YouTube 채널을 연결하세요")
        l = listing(o["listing_id"])
        desc = f"{l['credit']}\n이용 허락: Legend Clips 라이선스 {o['license_code']}\n\n#Shorts"
        meta = VideoMeta(title=body.title, description=desc, tags=["Shorts"], privacy=body.privacy, publish_at=body.publish_at)
        try:
            meta.validate()
        except YouTubeError as e:
            raise HTTPException(400, str(e))

        def work(*, progress) -> dict:
            video = acc.upload(root / l["id"] / l["master_file"], meta, lambda f: progress(f, "YouTube에 업로드 중"))
            url = f"https://youtube.com/shorts/{video['id']}"
            db.x("UPDATE orders SET published_url=? WHERE id=?", url, oid)
            return {"url": url}

        return {"job_id": jobs.submit("buyer_publish", work, owner=uid)}

    @app.get("/api/orders/{oid}/license")
    def license_page(request: Request, oid: str):
        o = order(oid, uid_of(request))
        if o["status"] not in ("paid", "refunded"):
            raise HTTPException(404, "주문을 찾을 수 없습니다")
        return HTMLResponse(license_html(order_view(o), db.one("SELECT legal_name, email, business_no FROM sellers WHERE user_id=?",
                                                               o["seller_id"]) or {}))


def _date(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y년 %m월 %d일") if ts else "기간 제한 없음"


def license_html(o: dict, seller: dict) -> str:
    e = html.escape
    l = o["listing"]
    kind = "독점(1인 한정)" if l["license_type"] == "exclusive" else "비독점"
    state = {"paid": "유효" if o["license_valid"] else "기간 만료", "refunded": "환불로 효력 없음"}.get(o["status"], o["status"])
    rows = [
        ("라이선스 번호", o["license_code"]), ("상태", state), ("클립", l["title"]),
        ("원본 영상", f"https://youtu.be/{l['source_video_id']} ({l['channel_title']})"),
        ("권리자(판매자)", f"{seller.get('legal_name', '')} · {seller.get('email', '')}"
                       + (f" · 사업자 {seller['business_no']}" if seller.get("business_no") else "")),
        ("이용자(구매자)", o["buyer_name"]), ("종류", kind),
        ("기간", f"{_date(o['paid_at'])} ~ {_date(o['license_until']) if o['license_until'] else '기간 제한 없음'}"),
        ("결제 금액", f"{o['amount']:,}원"),
    ]
    terms = [
        "이용 범위: 이용자 본인이 운영하는 채널의 숏폼 영상 플랫폼(YouTube Shorts, Instagram Reels, TikTok)에 게시할 수 있습니다.",
        f"출처 표기 의무: 게시물 설명란에 다음 문구를 그대로 표기해야 합니다 — “{l['credit']}”",
        "허용되는 편집: 자막 추가·수정, 길이 줄이기, 화면 비율 조정. 원작자나 출연자를 비방하거나 사실과 다르게 오해하게 만드는 편집은 금지됩니다.",
        "금지: 클립의 재판매·양도·재배포, 광고 소재·상업 광고로의 사용(별도 계약 필요), 다른 사람에게 이용을 허락하는 행위.",
        "기간이 끝나면 이용자는 게시물을 비공개 또는 삭제해야 합니다." if o["license_until"] else "이 라이선스는 기간 제한이 없습니다.",
        "권리 보증: 판매자는 이 클립에 필요한 저작권, 출연자 초상권, 음원 등 권리를 보유하고 있음을 보증합니다.",
        "Legend Clips는 통신판매중개자로서 거래 당사자가 아니며, 라이선스 내용에 대한 책임은 판매자에게 있습니다.",
    ]
    body = "".join(f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>" for k, v in rows)
    items = "".join(f"<li>{e(t)}</li>" for t in terms)
    return f"""<!doctype html><html lang="ko"><meta charset="utf-8"><title>이용 허락서 {e(o['license_code'])}</title>
<style>body{{font-family:'Noto Sans KR',sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#111;line-height:1.6}}
h1{{font-size:24px;margin-bottom:4px}}table{{width:100%;border-collapse:collapse;margin:18px 0}}th,td{{border:1px solid #ddd;padding:8px 10px;
text-align:left;font-size:14px}}th{{background:#f6f6f6;width:150px}}li{{margin:6px 0;font-size:14px}}.muted{{color:#666;font-size:12px}}
@media print{{button{{display:none}}}}</style>
<h1>클립 이용 허락서</h1><div class="muted">Legend Clips · 약관 버전 {e(TERMS_VERSION)}</div>
<table>{body}</table><h2 style="font-size:17px">이용 조건</h2><ol>{items}</ol>
<p class="muted">이 문서는 거래 기록으로 보관됩니다. 분쟁이 생기면 라이선스 번호로 문의해 주세요.</p>
<button onclick="print()">인쇄 / PDF로 저장</button></html>"""
