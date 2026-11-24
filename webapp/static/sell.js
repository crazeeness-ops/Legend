// Editor "판매" tab: list a clip for sale (licence) in the clip shop.
import { h, api, toast, won } from "./util.js";

const pad = n => String(n).padStart(2, "0");
const dateInput = days => { const d = new Date(Date.now() + days * 86400e3); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
const DAYS_LABEL = { 30: "30일", 90: "90일", 365: "1년", 0: "기간 제한 없음" };

// ctx: { pid, clip(), cleanupFns, rerender() }
export function sellTab(ctx) {
  const root = h("div", {}, h("p", { class: "muted" }, "불러오는 중…"));
  load(ctx, root).catch(e => root.replaceChildren(h("p", { class: "muted" }, e.message)));
  return [root];
}

async function load(ctx, root) {
  const clip = ctx.clip();
  const [info, seller] = await Promise.all([
    api(`/api/projects/${ctx.pid}/clips/${clip.id}/listing`),
    api("/api/me/seller").catch(() => null),
  ]);
  if (!info.eligible) {
    root.replaceChildren(h("div", { class: "section" }, h("h3", {}, "클립 판매"),
      h("p", { class: "hint" }, "판매는 '내 채널'에서 소유 확인을 거친 영상으로 만든 클립만 할 수 있어요. 남의 영상이나 확인되지 않은 영상의 클립을 팔면 저작권 문제가 생기기 때문이에요."),
      h("a", { href: "#/channel" }, h("button", {}, "내 채널로 가기"))));
    return;
  }
  if (info.listing) { root.replaceChildren(...listingStatus(ctx, info.listing)); return; }
  root.replaceChildren(...form(ctx, info, seller));
}

function listingStatus(ctx, l) {
  const statusText = { on_sale: l.open ? "판매 중" : "판매 기간 끝남", sold_out: "모두 판매됨", closed: "판매 중지" }[l.status] || l.status;
  return [h("div", { class: "section pub-status" },
    h("div", { class: "row" }, h("span", { class: "badge " + (l.open ? "ok" : "") }, statusText), h("span", { class: "grow" }),
      h("a", { href: `#/shop/${l.id}` }, "판매 페이지 보기 →")),
    h("div", { class: "stat-row" },
      h("div", { class: "stat" }, h("div", { class: "muted" }, "가격"), h("b", {}, won(l.price))),
      h("div", { class: "stat" }, h("div", { class: "muted" }, "판매"), h("b", {}, `${l.sold} / ${l.max_sales}`))),
    h("p", { class: "hint" }, `${l.license_type === "exclusive" ? "독점" : "비독점"} · 라이선스 ${DAYS_LABEL[l.license_days]} · 판매 마감 ${new Date(l.deadline * 1000).toLocaleDateString("ko-KR")}`),
    h("div", { class: "row" },
      h("button", { class: "small", onclick: async () => {
        await api(`/api/listings/${l.id}`, { method: "PATCH", body: { status: l.status === "on_sale" ? "closed" : "on_sale" } }).catch(e => toast(e.message, true));
        ctx.rerender();
      } }, l.status === "on_sale" ? "판매 중지" : "다시 판매"))),
    h("p", { class: "hint" }, "판매 등록 시점의 영상이 그대로 판매돼요. 클립을 고쳐도 이미 산 사람의 영상은 바뀌지 않아요.")];
}

function form(ctx, info, seller) {
  const clip = ctx.clip();
  const s = info.suggestion;
  const title = h("input", { type: "text", value: clip.title, maxLength: 80 });
  const desc = h("textarea", { rows: 3, placeholder: "어떤 장면인지, 어떤 채널에 어울리는지 (선택)" });
  const price = h("input", { type: "number", min: 1000, step: 1000, value: s.price });
  const fee = h("p", { class: "hint" });
  let kind = "nonexclusive";
  const kindSeg = h("div", { class: "seg" });
  const maxSales = h("input", { type: "number", min: 1, max: 1000, value: 50 });
  const maxWrap = h("div", { class: "grow" }, h("label", {}, "최대 판매 수"), maxSales);
  const drawKind = () => {
    kindSeg.replaceChildren(...[["nonexclusive", "비독점 (여러 명에게)"], ["exclusive", "독점 (1명만)"]].map(([v, l]) =>
      h("label", { class: kind === v ? "on" : "" }, h("input", { type: "radio", name: "kind", checked: kind === v, onchange: () => {
        kind = v; price.value = v === "exclusive" ? s.exclusive_price : s.price; drawKind(); updateFee();
      } }), l)));
    maxWrap.style.display = kind === "exclusive" ? "none" : "";
  };
  const days = h("select", {}, info.license_days.map(d => h("option", { value: d, selected: d === 365 }, DAYS_LABEL[d])));
  const deadline = h("input", { type: "date", value: dateInput(30), min: dateInput(1), max: dateInput(365) });
  const credit = h("input", { type: "text", value: info.default_credit, maxLength: 300 });
  const sName = h("input", { type: "text", value: seller?.legal_name || "", placeholder: "이름 또는 상호" });
  const sEmail = h("input", { type: "email", value: seller?.email || "", placeholder: "구매자 문의를 받을 이메일" });
  const sBiz = h("input", { type: "text", value: seller?.business_no || "", placeholder: "사업자등록번호 (있으면)" });
  const agree = h("input", { type: "checkbox" });
  const go = h("button", { class: "primary", style: { width: "100%" } }, "판매 등록");
  const bar = h("div"), progress = h("div", { class: "progress", style: { display: "none", marginTop: "10px" } }, bar);
  const updateFee = () => {
    const p = +price.value || 0;
    fee.textContent = `판매 1건당 받는 금액 ${won(p - Math.round(p * info.fee_rate))} (수수료 ${Math.round(info.fee_rate * 100)}% 제외)`;
  };
  price.oninput = updateFee;
  drawKind(); updateFee();

  go.onclick = async () => {
    if (!agree.checked) return toast("권리 보유 확인에 동의해 주세요", true);
    go.disabled = true; progress.style.display = "block";
    try {
      const { job_id } = await api(`/api/projects/${ctx.pid}/clips/${clip.id}/listing`, { method: "POST", body: {
        title: title.value, description: desc.value, price: +price.value, license_type: kind, license_days: +days.value,
        deadline: new Date(`${deadline.value}T23:59:00`).toISOString(), max_sales: +maxSales.value, credit: credit.value,
        seller: { legal_name: sName.value, email: sEmail.value, business_no: sBiz.value }, agree: true } });
      const tick = async () => {
        const job = await api(`/api/jobs/${job_id}`);
        bar.style.width = `${(job.progress || 0) * 100}%`;
        if (job.status === "done") { toast("판매를 시작했어요"); ctx.rerender(); }
        else if (job.status === "error") { toast(job.error, true); go.disabled = false; }
        else { const t = setTimeout(tick, 800); ctx.cleanupFns.push(() => clearTimeout(t)); }
      };
      tick();
    } catch (e) { toast(e.message, true); go.disabled = false; }
  };

  return [
    h("div", { class: "section" }, h("h3", {}, "클립 판매하기"),
      h("p", { class: "hint" }, "이 클립의 이용 허락(라이선스)을 다른 채널·클리퍼·기업에 팔 수 있어요. 산 사람은 출처를 표기하고 정해진 기간 동안 자기 채널에 올릴 수 있어요."),
      h("label", {}, "판매 제목"), title,
      h("label", { style: { marginTop: "10px" } }, "설명"), desc),
    h("div", { class: "section" }, h("h3", {}, "가격"),
      kindSeg,
      h("div", { class: "row", style: { marginTop: "10px" } },
        h("div", { class: "grow" }, h("label", {}, "가격 (원)"), price), maxWrap),
      h("p", { class: "hint" }, `💡 ${s.basis}: 비독점 ${won(s.price)} · 독점 ${won(s.exclusive_price)}`), fee),
    h("div", { class: "section" }, h("h3", {}, "조건"),
      h("div", { class: "row" },
        h("div", { class: "grow" }, h("label", {}, "라이선스 기간 (구매일부터)"), days),
        h("div", { class: "grow" }, h("label", {}, "판매 마감일"), deadline)),
      h("label", { style: { marginTop: "10px" } }, "출처 표기 문구 (산 사람이 반드시 표기)"), credit),
    h("div", { class: "section" }, h("h3", {}, "판매자 정보"),
      h("p", { class: "hint" }, "전자상거래법에 따라 구매자에게 공개돼요."),
      h("div", { class: "custom-grid" }, sName, sEmail), h("div", { style: { marginTop: "8px" } }, sBiz)),
    h("div", { class: "section" },
      h("label", { class: "row check" }, agree,
        h("span", {}, "이 클립의 저작권, 출연자 초상권(출연자 동의), 배경 음악 등 필요한 모든 권리를 제가 갖고 있으며, 문제가 생기면 판매자인 제가 책임집니다. 판매 약관(", info.terms_version, ")에 동의합니다.")),
      go, progress)];
}
