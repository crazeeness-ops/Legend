// "내 채널" (creator) and "클리핑 마켓" (creators + clippers) screens.
import { h, api, toast, fmt, upload, won, num, modal } from "./util.js";
import { ytStatus, setupGuide } from "./publish.js";

const connectUrl = () => `/oauth/google/start?return_to=${encodeURIComponent("/" + location.hash)}`;
const dday = ts => {
  const d = Math.ceil((ts * 1000 - Date.now()) / 86400e3);
  return d <= 0 ? "오늘 마감" : `D-${d}`;
};
const pad = n => String(n).padStart(2, "0");
const dateInput = (days) => { const d = new Date(Date.now() + days * 86400e3); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };

function analysisOptions() {
  const length = h("select", {}, h("option", { value: "short" }, "짧게 (15~30초)"), h("option", { value: "medium", selected: true }, "보통 (30~60초)"), h("option", { value: "long" }, "길게 (60~90초)"));
  const top = h("input", { type: "number", min: 1, max: 30, value: 8 });
  const prompt = h("input", { type: "text", placeholder: "예: 실패담만 (선택)" });
  const selector = h("select", {}, h("option", { value: "claude" }, "Claude AI (권장)"), h("option", { value: "heuristic" }, "오프라인 기본"));
  const el = h("div", {},
    h("div", { class: "row field" }, h("div", { class: "grow" }, h("label", {}, "클립 길이"), length), h("div", { style: { width: "90px" } }, h("label", {}, "개수"), top)),
    h("div", { class: "field" }, h("label", {}, "원하는 내용"), prompt),
    h("div", { class: "field" }, h("label", {}, "추천 방식"), selector));
  return { el, values: () => ({ length: length.value, top: +top.value, prompt: prompt.value, selector: selector.value }) };
}

function fileField(label, accept, hint) {
  const input = h("input", { type: "file", accept });
  return { input, el: h("div", { class: "field" }, h("label", {}, label), input, hint ? h("p", { class: "hint" }, hint) : null) };
}

function sparkline(curve, peaks, duration) {
  const W = 220, H = 44;
  if (!curve.length) return h("div", { class: "spark empty" }, "유지율 데이터 없음");
  const max = Math.max(...curve.map(p => p[1]), 1);
  const pts = curve.map(([x, y]) => `${(x * W).toFixed(1)},${(H - (y / max) * (H - 4) - 2).toFixed(1)}`).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "spark");
  svg.innerHTML = `<polyline points="${pts}" fill="none" stroke="#8b93a5" stroke-width="1.5"/>` +
    peaks.map(p => `<circle cx="${(p.at_sec / duration) * W}" cy="${H / 2}" r="4" fill="#ff4d6d"><title>${fmt(p.at_sec, 0)} 구간</title></circle>`).join("");
  return svg;
}

// ------------------------------------------------------------------ 내 채널
export async function channelView(app, topbar) {
  const st = await ytStatus(true);
  const body = h("main", { class: "page" });
  app.replaceChildren(topbar(), body);
  if (!st.connected) {
    body.append(h("div", { class: "card center-card" },
      h("h2", {}, "내 YouTube 채널 연결"),
      h("p", { class: "muted" }, "채널을 연결하면 조회수·좋아요가 높은 영상을 순서대로 보여 드리고, 실제 시청자가 오래 본 구간(시청 유지율)을 바탕으로 클립을 추천해요."),
      st.configured ? h("a", { href: connectUrl() }, h("button", { class: "primary" }, "▶ Google 계정으로 연결")) : h("button", { onclick: () => setupGuide(st) }, "설정 방법 보기"),
      h("p", { class: "hint" }, "영상이 없나요? '클리핑 마켓'에서 다른 크리에이터가 허락한 영상으로 클립을 만들 수 있어요.")));
    return;
  }
  const sort = h("select", {}, [["views", "조회수 순"], ["likes", "좋아요 순"], ["like_rate", "좋아요율 순"], ["published_at", "최신 순"]].map(([v, l]) => h("option", { value: v }, l)));
  const grid = h("div", { class: "video-grid" }, h("p", { class: "muted" }, "불러오는 중…"));
  body.append(h("div", { class: "page-head" },
    h("div", { class: "grow" }, h("h1", {}, st.channel.title), h("p", { class: "muted" }, "긴 영상만 표시 · 시청 유지율 그래프의 빨간 점이 시청자가 몰입한 구간이에요")),
    sort, h("button", { onclick: () => load(true) }, "새로고침")), grid);

  let videos = [];
  const render = () => {
    const key = sort.value;
    const list = [...videos].sort((a, b) => (key === "published_at" ? String(b[key]).localeCompare(String(a[key])) : b[key] - a[key]));
    if (!list.length) { grid.replaceChildren(h("div", { class: "empty" }, "3분이 넘는 영상이 없어요.")); return; }
    grid.replaceChildren(...list.map((v, i) => {
      const spark = h("div", { class: "spark-wrap" }, h("span", { class: "muted", style: { fontSize: "11px" } }, "유지율 불러오는 중…"));
      api(`/api/channel/videos/${v.id}/retention`).then(r => spark.replaceChildren(...[sparkline(r.curve, r.peaks, v.duration),
        r.message ? h("div", { class: "hint" }, r.message) : null].filter(Boolean))).catch(() => spark.replaceChildren());
      return h("div", { class: "video-card" },
        h("div", { class: "thumb" }, v.thumbnail ? h("img", { src: v.thumbnail, alt: "", loading: "lazy" }) : null,
          h("span", { class: "dur" }, fmt(v.duration, 0)), key === "views" && i < 3 ? h("span", { class: "rank" }, `TOP ${i + 1}`) : null),
        h("div", { class: "vc-body" },
          h("div", { class: "vc-title" }, v.title),
          h("div", { class: "vc-stats" }, `조회수 ${num(v.views)} · 좋아요 ${num(v.likes)} (${v.like_rate}%)`),
          spark,
          h("div", { class: "row", style: { marginTop: "8px" } },
            h("button", { class: "primary small grow", onclick: () => clipFromVideo(v) }, "✂️ 클립 만들기"),
            h("button", { class: "small grow", onclick: () => openCampaign(v) }, "📣 클리핑 캠페인"))));
    }));
  };
  const load = async (refresh = false) => {
    try { videos = await api(`/api/channel/videos${refresh ? "?refresh=true" : ""}`); render(); }
    catch (e) { grid.replaceChildren(h("p", { class: "muted" }, e.message)); }
  };
  sort.onchange = render;
  load();
}

function clipFromVideo(v) {
  const file = fileField("원본 영상 파일", "video/*", "YouTube는 API로 원본 파일을 내려받을 수 없어요. YouTube 스튜디오 → 콘텐츠 → 영상 옵션(⋮) → 다운로드로 받은 파일을 올려 주세요. 길이가 같은지 확인해요.");
  const tr = fileField("자막 파일 (선택, .json)", ".json,application/json");
  const opts = analysisOptions();
  const bar = h("div"), progress = h("div", { class: "progress", style: { display: "none", marginTop: "10px" } }, bar);
  const go = h("button", { class: "primary", style: { width: "100%" } }, "클립 만들기");
  const close = modal(`클립 만들기 · ${v.title}`, h("div", {},
    h("p", { class: "hint" }, "이 영상의 시청 유지율을 하이라이트 점수에 반영해, 실제 시청자가 오래 본 구간을 우선 추천해요."),
    file.el, tr.el, opts.el, go, progress));
  go.onclick = async () => {
    if (!file.input.files[0]) return toast("원본 파일을 선택하세요", true);
    const f = new FormData();
    f.append("video", file.input.files[0]);
    if (tr.input.files[0]) f.append("transcript", tr.input.files[0]);
    f.append("name", v.title);
    f.append("youtube_video_id", v.id);
    for (const [k, val] of Object.entries(opts.values())) f.append(k, val);
    go.disabled = true; progress.style.display = "block";
    try {
      const p = await upload("/api/projects", f, x => (bar.style.width = `${x * 100}%`));
      close(); location.hash = `#/p/${p.id}`;
    } catch (e) { toast(e.message, true); go.disabled = false; }
  };
}

function openCampaign(v) {
  const file = fileField("원본 영상 파일 (클리퍼에게 제공)", "video/*", "클리퍼는 이 파일로 작업해요. YouTube에서 영상을 내려받지 않으므로 약관 문제가 없어요.");
  const tr = fileField("자막 파일 (선택, .json)", ".json,application/json");
  const title = h("input", { type: "text", value: `${v.title} 클리핑`, maxLength: 80 });
  const rules = h("textarea", { rows: 3, placeholder: "예: 욕설·자극적 썸네일 금지, 제품 언급 구간 우선" });
  const credit = h("input", { type: "text", placeholder: "비워두면 '원본: 영상 제목 (채널) 링크'로 자동 작성" });
  const rate = h("input", { type: "number", min: 0, step: 100, value: 1000 });
  const budget = h("input", { type: "number", min: 0, step: 10000, value: 100000 });
  const maxSec = h("select", {}, [30, 45, 60, 90].map(s => h("option", { value: s, selected: s === 60 }, `${s}초`)));
  const deadline = h("input", { type: "date", value: dateInput(14), min: dateInput(1) });
  const bar = h("div"), progress = h("div", { class: "progress", style: { display: "none", marginTop: "10px" } }, bar);
  const go = h("button", { class: "primary", style: { width: "100%" } }, "캠페인 열기");
  const close = modal(`클리핑 캠페인 · ${v.title}`, h("div", {},
    h("p", { class: "hint" }, "클리퍼들이 이 영상으로 숏츠를 만들어 자기 채널에 올리고, 조회수만큼 보상을 받아요. 출처 표기는 자동으로 들어가요."),
    file.el, tr.el,
    h("div", { class: "field" }, h("label", {}, "캠페인 제목"), title),
    h("div", { class: "field" }, h("label", {}, "규칙"), rules),
    h("div", { class: "field" }, h("label", {}, "출처 표기 문구"), credit),
    h("div", { class: "row field" },
      h("div", { class: "grow" }, h("label", {}, "조회수 1,000회당 보상(원)"), rate),
      h("div", { class: "grow" }, h("label", {}, "총 예산(원)"), budget)),
    h("div", { class: "row field" },
      h("div", { class: "grow" }, h("label", {}, "최대 클립 길이"), maxSec),
      h("div", { class: "grow" }, h("label", {}, "마감일"), deadline)),
    h("p", { class: "hint" }, "보상 지급은 아직 자동화되지 않았어요. 마감 후 정산 금액을 확인해 직접 지급해 주세요."),
    go, progress));
  go.onclick = async () => {
    if (!file.input.files[0]) return toast("원본 파일을 선택하세요", true);
    const f = new FormData();
    f.append("video", file.input.files[0]);
    if (tr.input.files[0]) f.append("transcript", tr.input.files[0]);
    f.append("video_id", v.id);
    f.append("title", title.value); f.append("rules", rules.value); f.append("credit", credit.value);
    f.append("rate_per_1k", rate.value); f.append("budget", budget.value); f.append("max_clip_sec", maxSec.value);
    f.append("deadline", new Date(`${deadline.value}T23:59:00`).toISOString());
    go.disabled = true; progress.style.display = "block";
    try {
      const c = await upload("/api/campaigns", f, x => (bar.style.width = `${x * 100}%`));
      close(); toast("캠페인을 열었어요"); location.hash = `#/market/${c.id}`;
    } catch (e) { toast(e.message, true); go.disabled = false; }
  };
}

// ------------------------------------------------------------------ 클리핑 마켓
const SUB_BADGE = { pending: ["warn", "검토 대기"], approved: ["ok", "승인"], rejected: ["err", "반려"] };

function campaignCard(c) {
  return h("a", { class: "camp-card", href: `#/market/${c.id}` },
    h("div", { class: "thumb" }, c.thumbnail ? h("img", { src: c.thumbnail, alt: "", loading: "lazy" }) : null,
      h("span", { class: "dur" }, `최대 ${c.max_clip_sec}초`)),
    h("div", { class: "vc-body" },
      h("div", { class: "vc-title" }, c.title),
      h("div", { class: "muted", style: { fontSize: "12px" } }, c.channel_title),
      h("div", { class: "camp-reward" }, `1,000회당 ${won(c.rate_per_1k)}`),
      h("div", { class: "vc-stats" }, `남은 예산 ${won(c.budget_left)} · 제출 ${c.submission_count}개 · `,
        c.open ? h("span", { class: "badge warn" }, dday(c.deadline)) : h("span", { class: "badge" }, "마감"))));
}

const LEGAL_NOTICE = "Legend Clips는 통신판매중개자로서 거래 당사자가 아니며, 판매 클립의 권리와 이용 조건에 대한 책임은 판매자에게 있습니다.";
const DAYS_LABEL = { 30: "30일", 90: "90일", 365: "1년", 0: "기간 제한 없음" };

function shopCard(l) {
  const vid = h("video", { src: l.preview_url, muted: true, loop: true, playsInline: true, preload: "metadata" });
  return h("a", { class: "shop-card", href: `#/shop/${l.id}`, onmouseenter: () => vid.play().catch(() => {}), onmouseleave: () => vid.pause() },
    h("div", { class: "vthumb" }, vid, h("span", { class: "dur" }, `${Math.round(l.duration)}초`),
      l.license_type === "exclusive" ? h("span", { class: "rank" }, "독점") : null),
    h("div", { class: "vc-body" },
      h("div", { class: "vc-title" }, l.title),
      h("div", { class: "muted", style: { fontSize: "12px" } }, l.channel_title),
      h("div", { class: "row" }, h("span", { class: "camp-reward grow" }, won(l.price)),
        l.open ? h("span", { class: "badge warn" }, dday(l.deadline)) : h("span", { class: "badge" }, "판매 종료")),
      h("div", { class: "vc-stats" }, `${l.license_type === "exclusive" ? "1명 한정" : `남은 수량 ${l.remaining}`} · 라이선스 ${DAYS_LABEL[l.license_days]}`)));
}

export async function marketView(app, topbar, tab = "shop") {
  const tabs = [["shop", "클립 구매"], ["open", "클리핑 캠페인"], ["mine", "내 캠페인"], ["earnings", "캠페인 수익"], ["orders", "구매·판매 내역"]];
  const body = h("div", {});
  app.replaceChildren(topbar(), h("main", { class: "page" },
    h("div", { class: "page-head" }, h("div", { class: "grow" }, h("h1", {}, "클리핑 마켓"),
      h("p", { class: "muted" }, "크리에이터가 직접 파는 클립을 사거나, 허락한 영상으로 숏츠를 만들어 조회수만큼 보상을 받아요."))),
    h("div", { class: "subtabs" }, tabs.map(([id, l]) => h("a", { class: tab === id ? "on" : "", href: `#/market${id === "shop" ? "" : "?" + id}` }, l))),
    body));
  if (tab === "shop") {
    const list = await api("/api/listings");
    body.append(h("p", { class: "hint" }, "크리에이터가 직접 판매하는 클립이에요. 사면 출처를 표기하고 정해진 기간 동안 내 채널에 올릴 수 있어요. 마우스를 올리면 미리보기가 재생돼요."),
      list.length ? h("div", { class: "video-grid" }, list.map(shopCard)) : h("div", { class: "empty" }, "지금 판매 중인 클립이 없어요."),
      h("p", { class: "legal" }, LEGAL_NOTICE));
  } else if (tab === "orders") {
    await ordersTab(body);
  } else if (tab === "open") {
    const list = await api("/api/campaigns");
    body.append(list.length ? h("div", { class: "video-grid" }, list.map(campaignCard))
      : h("div", { class: "empty" }, "지금 열린 캠페인이 없어요."));
  } else if (tab === "mine") {
    const list = await api("/api/campaigns?scope=mine");
    body.append(h("p", { class: "hint" }, "새 캠페인은 ", h("a", { href: "#/channel" }, "내 채널"), "에서 영상을 골라 열 수 있어요."),
      list.length ? h("div", { class: "video-grid" }, list.map(campaignCard)) : h("div", { class: "empty" }, "아직 연 캠페인이 없어요."));
  } else {
    const me = await api("/api/me/submissions");
    body.append(h("div", { class: "stat-row" },
      h("div", { class: "stat" }, h("div", { class: "muted" }, "확정 수익"), h("b", {}, won(me.total_earned))),
      h("div", { class: "stat" }, h("div", { class: "muted" }, "총 조회수"), h("b", {}, num(me.total_views))),
      h("div", { class: "stat" }, h("div", { class: "muted" }, "제출한 클립"), h("b", {}, me.submissions.length))),
      me.submissions.length ? subTable(me.submissions, { showCampaign: true }) : h("div", { class: "empty" }, "아직 제출한 클립이 없어요. 캠페인에 참여해 보세요."));
  }
}

function subTable(subs, { showCampaign = false, review = null } = {}) {
  return h("table", { class: "table" },
    h("thead", {}, h("tr", {}, showCampaign ? h("th", {}, "캠페인") : h("th", {}, "클리퍼"), h("th", {}, "클립"),
      h("th", { class: "r" }, "조회수"), h("th", {}, "상태"), h("th", { class: "r" }, "정산"), review ? h("th", {}) : null)),
    h("tbody", {}, subs.map(s => {
      const [cls, label] = SUB_BADGE[s.status] || ["", s.status];
      return h("tr", {},
        h("td", {}, showCampaign ? h("a", { href: `#/market/${s.campaign_id}` }, s.campaign_title) : s.clipper_name),
        h("td", {}, h("a", { href: s.url, target: "_blank", rel: "noopener" }, s.title || s.url)),
        h("td", { class: "r" }, num(s.views)),
        h("td", {}, h("span", { class: "badge " + cls }, label)),
        h("td", { class: "r" }, won(s.earned)),
        review ? h("td", { class: "r" },
          h("button", { class: "small", disabled: s.status === "approved", onclick: () => review(s, "approved") }, "승인"),
          h("button", { class: "small ghost", disabled: s.status === "rejected", onclick: () => review(s, "rejected") }, "반려")) : null);
    })));
}

export async function campaignView(app, topbar, cid) {
  const c = await api(`/api/campaigns/${cid}`);
  const body = h("main", { class: "page" });
  app.replaceChildren(topbar(h("span", { class: "crumb grow" }, "/ 클리핑 마켓 / ", c.title)), body);
  const spentPct = c.budget ? Math.min(100, (c.spent / c.budget) * 100) : 0;
  const head = h("div", { class: "camp-head card" },
    h("div", { class: "thumb" }, c.thumbnail ? h("img", { src: c.thumbnail, alt: "" }) : null),
    h("div", { class: "grow" },
      h("div", { class: "row" }, h("h1", { class: "grow" }, c.title), c.open ? h("span", { class: "badge warn" }, dday(c.deadline)) : h("span", { class: "badge" }, "마감")),
      h("p", { class: "muted" }, c.channel_title, " · ", h("a", { href: c.video_url, target: "_blank", rel: "noopener" }, "원본 영상"), " · 최대 ", c.max_clip_sec, "초"),
      h("div", { class: "stat-row" },
        h("div", { class: "stat" }, h("div", { class: "muted" }, "1,000회당"), h("b", {}, won(c.rate_per_1k))),
        h("div", { class: "stat" }, h("div", { class: "muted" }, "예산 사용"), h("b", {}, `${won(c.spent)} / ${won(c.budget)}`),
          h("div", { class: "progress", style: { marginTop: "6px" } }, h("div", { style: { width: `${spentPct}%` } }))),
        h("div", { class: "stat" }, h("div", { class: "muted" }, "제출 · 조회수"), h("b", {}, `${c.submission_count}개 · ${num(c.total_views)}`))),
      c.rules ? h("div", { class: "rules" }, h("b", {}, "규칙"), h("p", {}, c.rules)) : null,
      h("p", { class: "hint" }, "출처 표기: ", c.credit)));
  body.append(head);

  if (c.is_owner) {
    const review = async (s, status) => {
      try { await api(`/api/campaigns/${cid}/submissions/${s.id}/review`, { method: "POST", body: { status } }); campaignView(app, topbar, cid); }
      catch (e) { toast(e.message, true); }
    };
    body.append(h("div", { class: "row", style: { margin: "16px 0" } },
      h("h2", { class: "grow", style: { fontSize: "16px", margin: 0 } }, "제출된 클립"),
      h("button", { onclick: async e => { e.target.disabled = true; try { await api(`/api/campaigns/${cid}/refresh`, { method: "POST" }); toast("조회수를 갱신했어요"); campaignView(app, topbar, cid); } catch (err) { toast(err.message, true); e.target.disabled = false; } } }, "↻ 조회수 새로고침"),
      h("button", { onclick: async () => { await api(`/api/campaigns/${cid}`, { method: "PATCH", body: { status: c.status === "open" ? "closed" : "open" } }); campaignView(app, topbar, cid); } },
        c.status === "open" ? "캠페인 마감" : "다시 열기")),
      c.submissions.length ? subTable(c.submissions, { review }) : h("div", { class: "empty" }, "아직 제출된 클립이 없어요."),
      h("p", { class: "hint" }, "정산 = 승인된 클립의 조회수 ÷ 1,000 × 단가, 먼저 제출된 순서대로 예산 한도까지. 실제 지급은 직접 진행해 주세요."));
    return;
  }

  const opts = analysisOptions();
  const go = h("button", { class: "primary", style: { width: "100%" }, disabled: !c.open }, c.open ? "참여하고 클립 만들기" : "마감된 캠페인");
  go.onclick = async () => {
    go.disabled = true; go.textContent = "준비 중…";
    try { const p = await api(`/api/campaigns/${cid}/join`, { method: "POST", body: opts.values() }); location.hash = `#/p/${p.id}`; }
    catch (e) { toast(e.message, true); go.disabled = false; go.textContent = "참여하고 클립 만들기"; }
  };
  body.append(h("div", { class: "two-col" },
    h("section", { class: "card" }, h("h2", {}, "참여하기"),
      h("ol", { class: "steps" },
        h("li", {}, "AI가 이 영상에서 클립 후보를 뽑아요 (내 월 사용량에서 차감)"),
        h("li", {}, "편집기에서 다듬고 내 YouTube 채널에 올려요 — 출처 표기는 자동"),
        h("li", {}, "올리면 자동으로 제출되고, 승인되면 조회수만큼 정산돼요")),
      opts.el, go),
    h("section", {}, h("h2", { style: { fontSize: "16px" } }, "내 제출물"),
      c.submissions.length ? subTable(c.submissions.map(s => ({ ...s, clipper_name: "나" }))) : h("div", { class: "empty" }, "아직 없어요."))));
}


// ------------------------------------------------------------------ clip shop: detail, checkout, orders
export async function listingView(app, topbar, lid) {
  const l = await api(`/api/listings/${lid}`);
  const body = h("main", { class: "page" });
  app.replaceChildren(topbar(h("span", { class: "crumb grow" }, "/ 클립 구매 / ", l.title)), body);
  const terms = [
    "이용 범위: 내가 운영하는 채널의 숏폼(YouTube Shorts, Instagram Reels, TikTok) 게시",
    `출처 표기 의무: “${l.credit}”`,
    "허용: 자막 수정, 길이 줄이기, 화면 비율 조정 · 금지: 재판매, 다른 사람에게 넘기기, 광고 소재 사용",
    l.license_days ? `기간: 구매일부터 ${DAYS_LABEL[l.license_days]} (끝나면 게시물 비공개 또는 삭제)` : "기간: 제한 없음",
    `환불: 결제 후 ${l.refund_days}일 이내, 다운로드·게시 전에만 가능 (디지털 상품 특성상 사용 후에는 환불이 제한돼요. 구매 전 미리보기를 확인하세요)`,
  ];
  const buy = h("button", { class: "primary", style: { width: "100%" }, disabled: !l.open || l.is_seller },
    l.is_seller ? "내가 판매하는 클립" : l.open ? `${won(l.price)} 구매하기` : "판매가 끝났어요");
  buy.onclick = () => checkout(l);
  body.append(h("div", { class: "listing" },
    h("div", { class: "listing-video" }, h("video", { src: l.preview_url, controls: true, playsInline: true }),
      h("p", { class: "hint" }, "워터마크가 있는 저화질 미리보기예요. 구매하면 원본 화질(1080×1920)로 받아요.")),
    h("div", {},
      h("h1", { style: { fontSize: "20px", margin: "0 0 6px" } }, l.title),
      h("p", { class: "muted" }, l.channel_title, " · ", h("a", { href: l.source_url, target: "_blank", rel: "noopener" }, "원본 영상"),
        ` · ${Math.round(l.duration)}초`),
      l.description ? h("p", {}, l.description) : null,
      h("div", { class: "stat-row" },
        h("div", { class: "stat" }, h("div", { class: "muted" }, "가격"), h("b", {}, won(l.price))),
        h("div", { class: "stat" }, h("div", { class: "muted" }, "라이선스"), h("b", {}, l.license_type === "exclusive" ? "독점" : "비독점")),
        h("div", { class: "stat" }, h("div", { class: "muted" }, "판매 마감"), h("b", {}, l.open ? dday(l.deadline) : "종료")),
        l.license_type === "exclusive" ? null : h("div", { class: "stat" }, h("div", { class: "muted" }, "남은 수량"), h("b", {}, l.remaining))),
      h("div", { class: "rules" }, h("b", {}, "이용 조건"), h("ul", {}, terms.map(t => h("li", {}, t)))),
      l.my_orders.length ? h("p", { class: "hint" }, "이미 구매한 클립이에요. ", h("a", { href: "#/market?orders" }, "구매 내역 보기")) : null,
      buy,
      h("div", { class: "seller-info" }, h("b", {}, "판매자 정보"),
        h("div", {}, `${l.seller.legal_name || "-"} · ${l.seller.email || "-"}`, l.seller.business_no ? ` · 사업자 ${l.seller.business_no}` : " · 개인 판매자")),
      h("button", { class: "small ghost", style: { marginTop: "8px" }, onclick: () => reportListing(l) }, "🚩 권리 침해 신고"),
      h("p", { class: "legal" }, LEGAL_NOTICE))));
}

function checkout(l) {
  const agree = h("input", { type: "checkbox" });
  const pay = h("button", { class: "primary", style: { width: "100%" } }, `${won(l.price)} 결제하기 (테스트 모드)`);
  const close = modal("결제", h("div", {},
    h("table", { class: "table" }, h("tbody", {},
      h("tr", {}, h("td", {}, "상품"), h("td", {}, l.title)),
      h("tr", {}, h("td", {}, "라이선스"), h("td", {}, `${l.license_type === "exclusive" ? "독점" : "비독점"} · ${DAYS_LABEL[l.license_days]}`)),
      h("tr", {}, h("td", {}, "결제 금액"), h("td", {}, h("b", {}, won(l.price)))))),
    h("label", { class: "row check", style: { margin: "12px 0" } }, agree,
      h("span", {}, "이용 조건(출처 표기, 기간, 재판매 금지)과 환불 규정(다운로드·게시 후 환불 불가)을 확인했어요.")),
    pay,
    h("p", { class: "hint" }, "지금은 실제 돈이 오가지 않는 테스트 결제예요. 실제 결제는 결제사(PG) 연동 후 열려요.")));
  pay.onclick = async () => {
    if (!agree.checked) return toast("이용 조건과 환불 규정을 확인해 주세요", true);
    pay.disabled = true;
    try {
      const o = await api(`/api/listings/${l.id}/orders`, { method: "POST" });
      await api(`/api/orders/${o.order_id}/confirm`, { method: "POST", body: {} });
      close(); toast("구매가 완료됐어요"); location.hash = "#/market?orders";
    } catch (e) { toast(e.message, true); pay.disabled = false; }
  };
}

function reportListing(l) {
  const reason = h("textarea", { rows: 4, placeholder: "예: 제가 저작권자인 영상이에요 / 출연자 동의 없이 판매되고 있어요" });
  const send = h("button", { class: "primary", style: { width: "100%", marginTop: "10px" } }, "신고하기");
  const close = modal("권리 침해 신고", h("div", {}, h("p", { class: "hint" }, "확인 후 판매를 중지하고 판매자에게 알려요."), reason, send));
  send.onclick = async () => {
    try { const r = await api(`/api/listings/${l.id}/report`, { method: "POST", body: { reason: reason.value } }); toast(r.message); close(); }
    catch (e) { toast(e.message, true); }
  };
}

async function ordersTab(body) {
  const [bought, sold] = await Promise.all([api("/api/orders?scope=bought"), api("/api/orders?scope=sold")]);
  const status = o => o.status === "refunded" ? h("span", { class: "badge" }, "환불됨")
    : o.license_valid ? h("span", { class: "badge ok" }, "이용 가능") : h("span", { class: "badge err" }, "기간 만료");
  const until = o => (o.license_until ? new Date(o.license_until * 1000).toLocaleDateString("ko-KR") + "까지" : "기간 제한 없음");
  body.append(
    h("h2", { class: "sec-title" }, "산 클립", h("span", { class: "muted" }, ` 총 ${won(bought.totals.spent)}`)),
    bought.orders.length ? h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "클립"), h("th", {}, "라이선스"), h("th", {}, "상태"), h("th", { class: "r" }, "금액"), h("th", {}))),
      h("tbody", {}, bought.orders.map(o => h("tr", {},
        h("td", {}, o.listing.title, o.published_url ? h("div", {}, h("a", { href: o.published_url, target: "_blank", rel: "noopener" }, "게시됨 ↗")) : null),
        h("td", {}, h("div", {}, o.license_code), h("div", { class: "muted", style: { fontSize: "11px" } }, until(o))),
        h("td", {}, status(o)),
        h("td", { class: "r" }, won(o.amount)),
        h("td", { class: "r actions" },
          o.license_valid ? [
            h("a", { href: `/api/orders/${o.id}/download` }, h("button", { class: "small" }, "⬇ 받기")),
            h("button", { class: "small", onclick: () => publishBought(o) }, "▶ 내 채널에 올리기")] : null,
          h("a", { href: `/api/orders/${o.id}/license`, target: "_blank" }, h("button", { class: "small ghost" }, "이용 허락서")),
          o.refundable ? h("button", { class: "small ghost", onclick: async () => {
            if (!confirm("환불할까요? 라이선스가 사라져요.")) return;
            try { await api(`/api/orders/${o.id}/refund`, { method: "POST" }); toast("환불했어요"); location.reload(); } catch (e) { toast(e.message, true); }
          } }, "환불") : null))))) : h("div", { class: "empty" }, "아직 산 클립이 없어요."),
    h("h2", { class: "sec-title" }, "판 클립"),
    h("div", { class: "stat-row" },
      h("div", { class: "stat" }, h("div", { class: "muted" }, "판매 금액"), h("b", {}, won(sold.totals.gross))),
      h("div", { class: "stat" }, h("div", { class: "muted" }, "수수료"), h("b", {}, won(sold.totals.fee))),
      h("div", { class: "stat" }, h("div", { class: "muted" }, "정산 예정"), h("b", {}, won(sold.totals.net)))),
    sold.orders.length ? h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "클립"), h("th", {}, "구매자"), h("th", {}, "라이선스"), h("th", {}, "상태"), h("th", { class: "r" }, "정산 금액"))),
      h("tbody", {}, sold.orders.map(o => h("tr", {},
        h("td", {}, o.listing.title), h("td", {}, o.buyer_name), h("td", {}, h("a", { href: `/api/orders/${o.id}/license`, target: "_blank" }, o.license_code)),
        h("td", {}, status(o)), h("td", { class: "r" }, o.status === "paid" ? won(o.seller_amount) : "-"))))) : h("div", { class: "empty" }, "아직 판 클립이 없어요."),
    h("p", { class: "hint" }, "정산 금액은 결제사 정산 연동 후 등록한 계좌로 지급돼요. 지금은 테스트 모드예요."));
}

async function publishBought(o) {
  const st = await ytStatus(true);
  if (!st.connected) {
    if (st.configured && confirm("먼저 YouTube 채널을 연결해야 해요. 지금 연결할까요?")) location.href = connectUrl();
    else if (!st.configured) setupGuide(st);
    return;
  }
  const title = h("input", { type: "text", value: o.listing.title, maxLength: 100 });
  const go = h("button", { class: "primary", style: { width: "100%", marginTop: "12px" } }, `▶ '${st.channel.title}'에 올리기`);
  const msg = h("p", { class: "hint" });
  const close = modal("내 채널에 올리기", h("div", {},
    h("label", {}, "제목"), title,
    h("p", { class: "hint" }, "설명란에 출처 문구와 라이선스 번호가 자동으로 들어가요. 올리고 나면 환불할 수 없어요."),
    go, msg));
  go.onclick = async () => {
    go.disabled = true;
    try {
      const { job_id } = await api(`/api/orders/${o.id}/publish`, { method: "POST", body: { title: title.value } });
      const tick = async () => {
        const job = await api(`/api/jobs/${job_id}`);
        msg.textContent = `${job.message || ""} ${Math.round((job.progress || 0) * 100)}%`;
        if (job.status === "done") { toast("올렸어요"); close(); location.reload(); }
        else if (job.status === "error") { toast(job.error, true); go.disabled = false; }
        else setTimeout(tick, 800);
      };
      tick();
    } catch (e) { toast(e.message, true); go.disabled = false; }
  };
}
