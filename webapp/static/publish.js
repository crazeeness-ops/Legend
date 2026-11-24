// YouTube publishing UI: channel chip, per-clip "게시" tab, bulk scheduling dialog.
import { h, api, toast, debounce } from "./util.js";

let YT = null; // cached /api/youtube/status

export async function ytStatus(refresh = false) {
  if (!YT || refresh) YT = await api("/api/youtube/status");
  return YT;
}

const connectUrl = () => `/oauth/google/start?return_to=${encodeURIComponent("/" + location.hash)}`;

// ---------------------------------------------------------------- helpers
const pad = n => String(n).padStart(2, "0");
export function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
export const fromLocalInput = v => (v ? new Date(v).toISOString() : "");
export function defaultSlot(daysAhead = 1, hour = 18) {
  const d = new Date();
  d.setDate(d.getDate() + daysAhead); d.setHours(hour, 0, 0, 0);
  return toLocalInput(d.toISOString());
}
const whenText = iso => new Date(iso).toLocaleString("ko-KR", { month: "long", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit" });

export const PUBLISH_BADGE = {
  queued: ["warn", "게시 대기"], rendering: ["warn", "렌더링 중"], uploading: ["warn", "업로드 중"],
  uploaded: ["ok", "YouTube 게시됨"], scheduled: ["ok", "예약됨"], error: ["err", "게시 실패"],
};

function modal(title, body, onClose) {
  const close = () => { overlay.remove(); onClose?.(); };
  const overlay = h("div", { class: "modal-bg", onclick: e => { if (e.target === overlay) close(); } },
    h("div", { class: "modal" }, h("div", { class: "row" }, h("h2", { class: "grow" }, title), h("button", { class: "ghost small", onclick: close }, "닫기")), body));
  document.body.append(overlay);
  return close;
}

export function setupGuide(st) {
  return modal("YouTube 연결 설정 안내", h("div", { class: "hint", style: { fontSize: "13px" } },
    h("ol", { style: { paddingLeft: "18px", lineHeight: 1.8 } },
      h("li", {}, "Google Cloud 콘솔에서 프로젝트를 만들고 ", h("b", {}, "YouTube Data API v3"), "를 사용 설정"),
      h("li", {}, "OAuth 동의 화면 구성 (외부, 테스트 사용자에 본인 Google 계정 추가)"),
      h("li", {}, "사용자 인증 정보 → OAuth 클라이언트 ID → ", h("b", {}, "웹 애플리케이션"), ", 승인된 리디렉션 URI에 아래 주소 추가 후 JSON 다운로드"),
      h("li", {}, h("code", {}, "python -m webapp serve --google-client-secret client_secret.json"), " 으로 다시 실행")),
    h("p", {}, "리디렉션 URI: ", h("code", {}, st.redirect_uri || "http://127.0.0.1:8000/oauth/google/callback")),
    h("p", {}, "참고: YouTube API 감사(audit)를 통과하기 전에는 업로드한 영상이 비공개로 고정됩니다. 공개·예약 게시를 쓰려면 감사를 신청하세요. 기본 할당량으로는 하루 약 6개까지 업로드할 수 있어요.")));
}

// ---------------------------------------------------------------- top bar chip
export function ytChip() {
  const el = h("button", { class: "small yt-chip" }, "YouTube …");
  ytStatus().then(st => {
    if (st.connected) {
      el.replaceChildren(st.channel.thumbnail ? h("img", { src: st.channel.thumbnail, alt: "" }) : "▶", st.channel.title);
      el.title = "연결된 채널 (클릭하면 연결 해제)";
      el.onclick = async () => {
        if (!confirm(`'${st.channel.title}' 채널 연결을 해제할까요?`)) return;
        YT = await api("/api/youtube/disconnect", { method: "POST" });
        toast("연결을 해제했어요"); el.replaceWith(ytChip());
      };
    } else {
      el.replaceChildren("▶ YouTube 연결");
      el.onclick = () => (st.configured ? (location.href = connectUrl()) : setupGuide(st));
    }
  }).catch(() => el.replaceChildren("YouTube ?"));
  return el;
}

// ---------------------------------------------------------------- per-clip tab
// ctx: { pid, clip(), setPublish(pub), rerender(), cleanupFns }
export function publishTab(ctx) {
  const root = h("div", {}, h("p", { class: "muted" }, "불러오는 중…"));
  (async () => {
    const st = await ytStatus();
    root.replaceChildren(...build(ctx, st));
  })().catch(e => root.replaceChildren(h("p", { class: "muted" }, e.message)));
  return [root];
}

function build(ctx, st) {
  const clip = ctx.clip();
  const pub = clip.publish || {};
  const draft = pub.draft;
  const out = [];

  // channel
  if (!st.connected) {
    out.push(h("div", { class: "section yt-connect" },
      h("h3", {}, "YouTube 채널 연결"),
      h("p", { class: "hint" }, "채널을 연결하면 이 클립을 바로 Shorts로 올리거나 예약할 수 있어요."),
      st.configured ? h("a", { href: connectUrl() }, h("button", { class: "primary" }, "▶ Google 계정으로 연결"))
        : h("button", { onclick: () => setupGuide(st) }, "설정 방법 보기")));
    return out;
  }
  out.push(h("div", { class: "section row channel" },
    st.channel.thumbnail ? h("img", { src: st.channel.thumbnail, alt: "" }) : null,
    h("div", { class: "grow" }, h("div", { style: { fontWeight: 700 } }, st.channel.title), h("div", { class: "muted", style: { fontSize: "12px" } }, "에 게시합니다"))));

  // status
  const job = ctx.publishJob;
  const running = ["queued", "rendering", "uploading"].includes(pub.status) && job && !["done", "error"].includes(job.status);
  if (pub.status) {
    const [cls, label] = PUBLISH_BADGE[pub.status] || ["", pub.status];
    out.push(h("div", { class: "section pub-status" },
      h("div", { class: "row" }, h("span", { class: "badge " + cls }, label),
        pub.status === "scheduled" && pub.publish_at ? h("span", {}, `${whenText(pub.publish_at)} 공개`) : null),
      running ? [h("div", { class: "progress", style: { marginTop: "10px" } }, h("div", { style: { width: `${(job.progress || 0) * 100}%` } })),
        h("p", { class: "hint" }, `${job.message} · ${Math.round((job.progress || 0) * 100)}%`)] : null,
      pub.url ? h("p", {}, h("a", { href: pub.url, target: "_blank", rel: "noopener" }, pub.url)) : null,
      pub.status === "error" ? h("p", { style: { color: "var(--danger)" } }, pub.error) : null));
  }

  if (!draft) {
    out.push(h("div", { class: "section" }, h("h3", {}, "게시 정보"),
      h("p", { class: "hint" }, "제목·설명·해시태그를 AI가 영상 내용으로 써 줍니다. 만든 뒤 자유롭게 고칠 수 있어요."),
      h("div", { class: "row" },
        h("button", { class: "primary", onclick: e => makeDraft(ctx, true, e.target) }, "✨ AI로 작성"),
        h("button", { onclick: e => makeDraft(ctx, false, e.target) }, "직접 작성"))));
    return out;
  }

  // draft form
  const save = debounce(async patch => {
    try {
      const p = await api(`/api/projects/${ctx.pid}/clips/${clip.id}/publish/draft`, { method: "PATCH", body: patch });
      ctx.setPublish(p, false);
    } catch (e) { toast(e.message, true); }
  }, 500);
  const counter = h("span", { class: "muted", style: { fontSize: "11px" } }, `${draft.title.length}/100`);
  const scheduled = !!draft.publish_at;
  const when = h("input", { type: "datetime-local", value: toLocalInput(draft.publish_at) || defaultSlot(), disabled: !scheduled,
    onchange: e => save({ publish_at: fromLocalInput(e.target.value) }) });
  const radios = (name, opts, cur, fn, disabled = false) => h("div", { class: "seg" + (disabled ? " disabled" : "") }, opts.map(([v, l]) =>
    h("label", { class: cur === v ? "on" : "" }, h("input", { type: "radio", name, value: v, checked: cur === v, disabled, onchange: () => fn(v) }), l)));

  out.push(h("div", { class: "section" },
    h("h3", {}, "게시 정보", draft.source === "ai" ? h("span", { class: "badge" }, "AI 작성") : null,
      h("button", { class: "small ghost", onclick: e => makeDraft(ctx, true, e.target) }, "✨ AI로 다시 쓰기")),
    draft.warning ? h("p", { class: "hint", style: { color: "var(--accent-2)" } }, draft.warning) : null,
    h("label", { class: "row" }, h("span", { class: "grow" }, "제목"), counter),
    h("input", { type: "text", value: draft.title, maxLength: 100, oninput: e => { counter.textContent = `${e.target.value.length}/100`; save({ title: e.target.value }); } }),
    h("label", { style: { marginTop: "10px" } }, "설명 (해시태그 포함)"),
    h("textarea", { rows: 5, value: draft.description, oninput: e => save({ description: e.target.value }) }),
    h("label", { style: { marginTop: "10px" } }, "검색 태그 (쉼표로 구분)"),
    h("input", { type: "text", value: (draft.tags || []).join(", "), oninput: e => save({ tags: e.target.value.split(",") }) })));

  out.push(h("div", { class: "section" },
    h("h3", {}, "공개 설정"),
    radios("privacy", [["public", "공개"], ["unlisted", "일부 공개"], ["private", "비공개"]], scheduled ? "public" : draft.privacy,
      v => { save({ privacy: v }); ctx.setPublish({ ...pub, draft: { ...draft, privacy: v } }); }, scheduled),
    h("label", { class: "row check", style: { marginTop: "12px" } },
      h("input", { type: "checkbox", checked: scheduled, onchange: e => {
        const v = e.target.checked ? fromLocalInput(when.value || defaultSlot()) : "";
        save.flush({ publish_at: v });
        ctx.setPublish({ ...pub, draft: { ...draft, publish_at: v || null } });
      } }), "예약 게시 (정한 시간에 자동 공개)"),
    when,
    scheduled ? h("p", { class: "hint" }, "예약한 영상은 그 시간까지 비공개로 있다가 YouTube가 자동으로 '공개'로 바꿉니다.") : null,
    h("label", { style: { marginTop: "12px" } }, "아동용 콘텐츠인가요? (YouTube 필수 항목)"),
    radios("kids", [["no", "아니요"], ["yes", "예, 아동용"]], draft.made_for_kids ? "yes" : "no",
      v => { save({ made_for_kids: v === "yes" }); ctx.setPublish({ ...pub, draft: { ...draft, made_for_kids: v === "yes" } }); })));

  const already = !!pub.youtube_id;
  out.push(h("div", { class: "section" },
    h("button", { class: "primary", style: { width: "100%" }, disabled: running, onclick: () => publish(ctx, already) },
      running ? "진행 중…" : already ? "다시 올리기" : scheduled ? "📅 예약하기" : "▶ YouTube에 올리기"),
    h("p", { class: "hint" }, "편집 내용이 마지막 렌더링과 다르면 자동으로 다시 렌더링한 뒤 업로드합니다. ",
      "API 감사를 통과하지 않은 Google 프로젝트에서는 영상이 비공개로 고정될 수 있어요.")));
  return out;
}

async function makeDraft(ctx, ai, btn) {
  if (btn) { btn.disabled = true; btn.textContent = ai ? "작성 중…" : "…"; }
  try {
    const p = await api(`/api/projects/${ctx.pid}/clips/${ctx.clip().id}/publish/draft?ai=${ai}`, { method: "POST" });
    ctx.setPublish(p);
    if (p.draft.warning) toast(p.draft.warning, true);
  } catch (e) { toast(e.message, true); if (btn) btn.disabled = false; }
}

async function publish(ctx, already) {
  const clip = ctx.clip();
  if (already && !confirm("이미 YouTube에 올린 클립이에요. 새 영상으로 한 번 더 올릴까요?")) return;
  try {
    const { job_id } = await api(`/api/projects/${ctx.pid}/clips/${clip.id}/publish`, { method: "POST", body: { force: already } });
    ctx.publishJob = { status: "queued", progress: 0, message: "대기 중" };
    ctx.setPublish({ ...(clip.publish || {}), status: "queued", job_id });
    pollJob(ctx, job_id, async job => {
      const p = await api(`/api/projects/${ctx.pid}`);
      ctx.setPublish(p.clips.find(c => c.id === clip.id).publish);
      if (job.status === "done") toast(job.result.status === "scheduled" ? "예약을 마쳤어요" : "YouTube에 올렸어요");
      else toast(job.error || "게시에 실패했어요", true);
    });
  } catch (e) { toast(e.message, true); }
}

function pollJob(ctx, jid, onEnd) {
  let timer;
  const tick = async () => {
    const job = await api(`/api/jobs/${jid}`);
    ctx.publishJob = job;
    if (job.status === "done" || job.status === "error") return onEnd(job);
    ctx.rerender();
    timer = setTimeout(tick, 900);
  };
  timer = setTimeout(tick, 400);
  ctx.cleanupFns.push(() => clearTimeout(timer));
}

// ---------------------------------------------------------------- bulk scheduling
export async function openBulk(project, onDone) {
  const st = await ytStatus(true);
  if (!st.connected) {
    if (!st.configured) return setupGuide(st);
    if (confirm("먼저 YouTube 채널을 연결해야 해요. 지금 연결할까요?")) location.href = connectUrl();
    return;
  }
  const pid = project.id;
  const checks = project.clips.map(c => {
    const done = !!c.publish?.youtube_id;
    return { c, box: h("input", { type: "checkbox", checked: !done }) , done };
  });
  const first = h("input", { type: "datetime-local", value: defaultSlot(1, 18) });
  const interval = h("select", {}, [[6, "6시간"], [12, "12시간"], [24, "하루"], [48, "이틀"], [168, "일주일"]].map(([v, l]) => h("option", { value: v, selected: v === 24 }, l)));
  const ai = h("input", { type: "checkbox", checked: true });
  const preview = h("div", { class: "hint" });
  const status = h("div");
  const start = h("button", { class: "primary", style: { width: "100%" } }, "예약 시작");
  const refresh = () => {
    const n = checks.filter(x => x.box.checked).length;
    const t0 = new Date(first.value);
    const last = new Date(t0.getTime() + (n - 1) * +interval.value * 3600e3);
    preview.textContent = n ? `${n}개 클립을 ${whenText(t0.toISOString())}부터 ${interval.selectedOptions[0].text} 간격으로 공개합니다${n > 1 ? ` (마지막: ${whenText(last.toISOString())})` : ""}.` : "클립을 선택하세요.";
    start.disabled = !n;
  };
  [first, interval, ...checks.map(x => x.box)].forEach(el => el.addEventListener("change", refresh));

  start.onclick = async () => {
    start.disabled = true;
    try {
      const r = await api(`/api/projects/${pid}/publish/bulk`, { method: "POST", body: {
        clip_ids: checks.filter(x => x.box.checked).map(x => x.c.id), first_at: fromLocalInput(first.value),
        interval_hours: +interval.value, privacy: "public", use_ai: ai.checked } });
      const bar = h("div"), msg = h("p", { class: "hint" });
      status.replaceChildren(h("div", { class: "progress", style: { marginTop: "12px" } }, bar), msg);
      const tick = async () => {
        const job = await api(`/api/jobs/${r.job_id}`);
        bar.style.width = `${(job.progress || 0) * 100}%`;
        msg.textContent = job.message;
        if (job.status === "done") {
          status.append(h("ul", { class: "results" }, job.result.results.map(x => {
            const c = project.clips.find(c => c.id === x.clip);
            return h("li", {}, x.ok ? "✅ " : "⚠️ ", c?.title || x.clip, " — ", x.ok ? (x.publish_at ? whenText(x.publish_at) + " 공개" : "게시됨") : x.error);
          })));
          onDone?.();
        } else if (job.status === "error") msg.textContent = job.error;
        else setTimeout(tick, 1000);
      };
      tick();
    } catch (e) { toast(e.message, true); start.disabled = false; }
  };

  modal("일괄 예약 게시", h("div", {},
    h("p", { class: "hint" }, "선택한 클립을 순서대로 렌더링하고 YouTube에 예약합니다. 이 창을 닫아도 서버에서 계속 진행돼요."),
    h("div", { class: "bulk-list" }, checks.map(({ c, box, done }) => h("label", { class: "row check" }, box,
      h("span", { class: "badge score" }, c.score), h("span", { class: "grow" }, c.title), done ? h("span", { class: "badge ok" }, "게시됨") : null))),
    h("div", { class: "row", style: { marginTop: "12px" } },
      h("div", { class: "grow" }, h("label", {}, "첫 공개 시간"), first),
      h("div", { style: { width: "130px" } }, h("label", {}, "간격"), interval)),
    h("label", { class: "row check", style: { marginTop: "10px" } }, ai, "제목·설명이 없는 클립은 AI로 작성"),
    preview, start, status));
  refresh();
}
