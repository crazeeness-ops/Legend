import { h, api, toast, fmt, debounce, clamp } from "./util.js";
import { Preview } from "./preview.js";
import { ytChip, publishTab, openBulk, PUBLISH_BADGE } from "./publish.js";

const app = document.getElementById("app");
let cleanup = () => {};
let PRESETS = null; // id -> style

const LAYOUT_LABEL = { auto: "자동", track: "얼굴 추적", split: "2인 분할", fit: "전체 화면" };
const STATUS_LABEL = { created: "준비", queued: "대기 중", analyzing: "분석 중", ready: "완료", error: "오류" };
const FONTS = ["Noto Sans CJK KR", "Noto Serif CJK KR", "Noto Sans KR"];

async function loadPresets() {
  if (!PRESETS) {
    const data = await api("/api/presets");
    PRESETS = Object.fromEntries(data.presets.map(p => [p.id, p]));
  }
  return PRESETS;
}

function topbar(...right) {
  return h("header", { class: "topbar" },
    h("a", { class: "logo", href: "#/" }, "Legend", h("span", {}, " Clips")), ...right);
}

// ------------------------------------------------------------------ router
async function route() {
  cleanup(); cleanup = () => {};
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  try {
    if (parts[0] === "p" && parts[1]) await projectView(parts[1], parts[2]);
    else await homeView();
  } catch (e) {
    app.replaceChildren(topbar(), h("div", { class: "center-msg" }, h("h2", {}, "문제가 발생했어요"), h("p", { class: "muted" }, e.message),
      h("a", { href: "#/" }, "처음으로")));
  }
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ home
async function homeView() {
  const projects = await api("/api/projects");
  const state = { video: null, transcript: null };
  const videoInput = h("input", { type: "file", accept: "video/*", style: { display: "none" } });
  const trInput = h("input", { type: "file", accept: ".json,application/json", style: { display: "none" } });
  const drop = h("div", { class: "drop", onclick: () => videoInput.click() }, "영상 파일을 끌어다 놓거나 클릭해서 선택하세요");
  const nameIn = h("input", { type: "text", placeholder: "예: 3월 팟캐스트 1화" });
  const length = h("select", {}, h("option", { value: "short" }, "짧게 (15~30초)"), h("option", { value: "medium", selected: true }, "보통 (30~60초)"), h("option", { value: "long" }, "길게 (60~90초)"));
  const top = h("input", { type: "number", min: 1, max: 30, value: 10 });
  const prompt = h("input", { type: "text", placeholder: "예: 가격 얘기만 (비워두면 전체에서 추천)" });
  const selector = h("select", {}, h("option", { value: "claude" }, "Claude AI (권장)"), h("option", { value: "heuristic" }, "오프라인 기본 (API 키 없이)"));
  const trLabel = h("span", { class: "muted" }, "없음 — 음성 인식으로 자동 생성");
  const bar = h("div"), progress = h("div", { class: "progress", style: { display: "none" } }, bar);
  const submit = h("button", { class: "primary", style: { width: "100%" }, disabled: true }, "클립 만들기");

  const setVideo = f => {
    state.video = f;
    drop.classList.toggle("has", !!f);
    drop.textContent = f ? `🎬 ${f.name} (${(f.size / 1e6).toFixed(1)}MB)` : "영상 파일을 끌어다 놓거나 클릭해서 선택하세요";
    if (f && !nameIn.value) nameIn.value = f.name.replace(/\.[^.]+$/, "");
    submit.disabled = !f;
  };
  videoInput.onchange = () => setVideo(videoInput.files[0]);
  trInput.onchange = () => { state.transcript = trInput.files[0]; trLabel.textContent = state.transcript?.name || "없음"; };
  drop.ondragover = e => { e.preventDefault(); drop.classList.add("has"); };
  drop.ondragleave = () => drop.classList.toggle("has", !!state.video);
  drop.ondrop = e => { e.preventDefault(); setVideo(e.dataTransfer.files[0]); };

  submit.onclick = () => {
    const form = new FormData();
    form.append("video", state.video);
    if (state.transcript) form.append("transcript", state.transcript);
    form.append("name", nameIn.value);
    form.append("length", length.value);
    form.append("top", top.value);
    form.append("prompt", prompt.value);
    form.append("selector", selector.value);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/projects");
    progress.style.display = "block";
    submit.disabled = true; submit.textContent = "업로드 중…";
    xhr.upload.onprogress = e => { if (e.lengthComputable) bar.style.width = `${(e.loaded / e.total) * 100}%`; };
    xhr.onload = () => {
      const data = JSON.parse(xhr.responseText || "{}");
      if (xhr.status >= 400) { toast(data.detail || "업로드 실패", true); submit.disabled = false; submit.textContent = "클립 만들기"; return; }
      location.hash = `#/p/${data.id}`;
    };
    xhr.onerror = () => { toast("네트워크 오류", true); submit.disabled = false; };
    xhr.send(form);
  };

  const list = projects.length ? h("div", { class: "projects" }, projects.map(p =>
    h("a", { class: "project-item", href: `#/p/${p.id}` },
      h("div", { class: "grow" }, h("div", { style: { fontWeight: 700 } }, p.name),
        h("div", { class: "muted", style: { fontSize: "12px", marginTop: "3px" } },
          new Date(p.created_at * 1000).toLocaleString("ko-KR"), p.duration ? ` · 원본 ${fmt(p.duration, 0)}` : "", ` · 클립 ${p.clip_count}개`)),
      h("span", { class: "badge " + ({ ready: "ok", error: "err" }[p.status] || "warn") }, STATUS_LABEL[p.status] || p.status),
      h("button", { class: "ghost small danger", title: "삭제", onclick: async e => {
        e.preventDefault();
        if (!confirm(`"${p.name}" 프로젝트를 삭제할까요?`)) return;
        await api(`/api/projects/${p.id}`, { method: "DELETE" }); route();
      } }, "삭제"))))
    : h("div", { class: "empty" }, "아직 프로젝트가 없어요. 왼쪽에서 영상을 올려보세요.");

  app.replaceChildren(topbar(h("span", { class: "grow" }), ytChip()), h("main", { class: "home" },
    h("section", { class: "card" }, h("h2", {}, "새 영상으로 숏츠 만들기"),
      h("div", { class: "field" }, drop, videoInput),
      h("div", { class: "field" }, h("label", {}, "프로젝트 이름"), nameIn),
      h("div", { class: "row field" }, h("div", { class: "grow" }, h("label", {}, "클립 길이"), length), h("div", { style: { width: "90px" } }, h("label", {}, "개수"), top)),
      h("div", { class: "field" }, h("label", {}, "원하는 내용 (선택)"), prompt),
      h("div", { class: "field" }, h("label", {}, "하이라이트 추천 방식"), selector),
      h("div", { class: "field" }, h("label", {}, "자막 파일 (선택, .json)"), h("div", { class: "row" }, h("button", { class: "small", onclick: () => trInput.click() }, "선택"), trLabel), trInput),
      h("div", { class: "field" }, progress), submit,
      h("p", { class: "hint" }, "본인이 권리를 가진 영상만 업로드하세요.")),
    h("section", {}, h("h2", { style: { fontSize: "16px", margin: "0 0 14px" } }, "내 프로젝트"), list)));
}

// ------------------------------------------------------------------ project / waiting
async function projectView(pid, cid) {
  await loadPresets();
  let project = await api(`/api/projects/${pid}`);
  if (project.status !== "ready") return waitingView(project);
  if (!project.clips.length) {
    app.replaceChildren(topbar(h("span", { class: "crumb" }, project.name)), h("div", { class: "center-msg" }, h("h2", {}, "추천할 만한 구간을 찾지 못했어요"), h("p", { class: "muted" }, "클립 길이나 원하는 내용을 바꿔 다시 시도해 보세요.")));
    return;
  }
  const clip = project.clips.find(c => c.id === cid);
  if (!clip) { location.replace(`#/p/${pid}/${project.clips[0].id}`); return; }
  editorView(project, clip);
}

function waitingView(project) {
  const bar = h("div"), msg = h("p", { class: "muted" });
  const body = h("div", { class: "center-msg" }, h("h2", {}, project.name), h("div", { class: "progress" }, bar), msg);
  app.replaceChildren(topbar(h("span", { class: "crumb" }, project.name)), body);
  const tick = async () => {
    const p = await api(`/api/projects/${project.id}`);
    if (p.status === "ready") return route();
    if (p.status === "error") {
      body.replaceChildren(h("h2", {}, "분석에 실패했어요"), h("p", { class: "muted" }, p.error || ""),
        h("p", { class: "hint" }, "Claude 추천을 쓰려면 서버에 ANTHROPIC_API_KEY가 필요합니다. 오프라인 기본 방식으로 다시 올려볼 수 있어요."),
        h("a", { href: "#/" }, "처음으로"));
      return;
    }
    bar.style.width = `${Math.max(5, (p.job?.progress || 0) * 100)}%`;
    msg.textContent = p.job?.message || STATUS_LABEL[p.status];
    timer = setTimeout(tick, 1500);
  };
  let timer = setTimeout(tick, 300);
  cleanup = () => clearTimeout(timer);
}

// ------------------------------------------------------------------ editor
function editorView(project, initialClip) {
  const pid = project.id;
  const S = { clip: initialClip, plan: null, words: [], win: { start: 0, end: 1 }, tab: "trim", sel: null, loop: true, renderJob: null };
  const duration = project.duration;
  const style = () => ({ ...PRESETS[S.clip.edits.preset], ...S.clip.edits.style_overrides });

  // ---- save indicator + clip saving
  const saveState = h("span", { class: "save-state" }, "저장됨");
  let pending = 0;
  async function patchClip(patch) {
    pending++; saveState.textContent = "저장 중…";
    try {
      const clip = await api(`/api/projects/${pid}/clips/${S.clip.id}`, { method: "PATCH", body: patch });
      S.clip = { ...S.clip, ...clip };
      return clip;
    } catch (e) { toast(e.message, true); throw e; }
    finally { if (--pending === 0) saveState.textContent = "저장됨"; }
  }
  const patchEdits = edits => patchClip({ edits: { ...S.clip.edits, ...edits } });

  // ---- video + preview
  const video = h("video", { src: `/api/projects/${pid}/video`, preload: "auto", playsInline: true, style: { display: "none" } });
  const preview = new Preview(video);
  preview.setClip(S.clip);
  preview.setStyle(style(), S.clip.edits.show_title);
  preview.onCropChange = (shotStart, cx) => setShotOverride(shotStart, { cx });
  video.addEventListener("loadedmetadata", () => { video.currentTime = S.clip.start; });

  const loadPlan = debounce(async () => {
    preview.setBusy(true);
    try {
      S.plan = await api(`/api/projects/${pid}/clips/${S.clip.id}/reframe`);
      preview.setPlan(S.plan);
      renderTimeline(); if (S.tab === "layout") renderTab();
    } catch (e) { toast(e.message, true); }
    finally { preview.setBusy(false); }
  }, 350);

  async function loadWords() {
    const pad = Math.max(10, (S.clip.end - S.clip.start) * 0.6);
    S.win = { start: Math.max(0, S.clip.start - pad), end: Math.min(duration, S.clip.end + pad) };
    S.words = await api(`/api/projects/${pid}/words?start=${S.win.start}&end=${S.win.end}`);
    preview.setWords(S.words);
    renderTimeline(); if (S.tab === "trim") renderTab();
  }

  // ---- trimming
  const commitRange = debounce(async () => {
    const clip = await patchClip({ start: S.clip.start, end: S.clip.end });
    preview.setClip(S.clip);
    if (clip.start < S.win.start + 2 || clip.end > S.win.end - 2) await loadWords();
    loadPlan();
    updateClipList();
  }, 300);

  function setRange(start, end, commit = true) {
    start = Math.max(0, start); end = Math.min(duration, end);
    if (end - start < 3 || end - start > 180) { toast("클립 길이는 3~180초여야 해요", true); return; }
    S.clip = { ...S.clip, start: +start.toFixed(3), end: +end.toFixed(3) };
    preview.setClip(S.clip); preview.setWords(S.words);
    renderTimeline(); if (S.tab === "trim") renderTab();
    if (commit) commitRange();
  }

  const snap = (t, edge) => {
    let best = t, dist = Infinity;
    for (const w of S.words) {
      const v = edge === "start" ? w.start : w.end;
      if (Math.abs(v - t) < dist) { dist = Math.abs(v - t); best = v; }
    }
    return dist < 1.5 ? best : t;
  };

  // ---- layout overrides
  async function setShotOverride(shotStart, patch) {
    const list = (S.clip.edits.shot_overrides || []).map(o => ({ ...o }));
    let o = list.find(x => Math.abs(x.start - shotStart) < 0.35);
    if (!o) { o = { start: shotStart }; list.push(o); }
    Object.assign(o, patch);
    for (const k of Object.keys(o)) if (o[k] === null) delete o[k];
    const cleaned = list.filter(x => Object.keys(x).length > 1);
    await patchEdits({ shot_overrides: cleaned });
    loadPlan();
  }

  // ---- playback
  const playBtn = h("button", { class: "primary", style: { width: "92px" }, onclick: () => togglePlay() }, "▶ 재생");
  const timeLbl = h("span", { class: "time" });
  const loopBtn = h("button", { class: "small", onclick: () => { S.loop = !S.loop; loopBtn.textContent = S.loop ? "반복 켜짐" : "반복 꺼짐"; } }, "반복 켜짐");
  function togglePlay() {
    if (video.paused) {
      if (video.currentTime < S.clip.start || video.currentTime >= S.clip.end - 0.05) video.currentTime = S.clip.start;
      video.play();
    } else video.pause();
  }
  video.addEventListener("play", () => playBtn.textContent = "❚❚ 정지");
  video.addEventListener("pause", () => playBtn.textContent = "▶ 재생");
  const seek = t => { video.currentTime = clamp(t, 0, duration); };

  // ---- timeline
  const timeline = h("div", { class: "timeline" });
  const playhead = h("div", { class: "playhead" });
  const x2t = x => S.win.start + (x / timeline.clientWidth) * (S.win.end - S.win.start);
  const t2p = t => `${((t - S.win.start) / (S.win.end - S.win.start)) * 100}%`;
  function renderTimeline() {
    const kids = [];
    for (const w of S.words) kids.push(h("div", { class: "tick", style: { left: t2p(w.start) } }));
    if (S.plan) for (const s of S.plan.shots.slice(1)) kids.push(h("div", { class: "shot", title: "장면 전환", style: { left: t2p(S.plan.clip_start + s.start) } }));
    kids.push(h("div", { class: "range", style: { left: t2p(S.clip.start), width: `calc(${t2p(S.clip.end)} - ${t2p(S.clip.start)})` } }));
    const hl = h("div", { class: "handle l", title: "시작점", style: { left: t2p(S.clip.start) } });
    const hr = h("div", { class: "handle r", title: "끝점", style: { left: t2p(S.clip.end) } });
    for (const [el, edge] of [[hl, "start"], [hr, "end"]]) {
      el.addEventListener("pointerdown", e => {
        e.stopPropagation(); el.setPointerCapture(e.pointerId);
        const rect = timeline.getBoundingClientRect();
        const move = ev => {
          const t = snap(x2t(ev.clientX - rect.left), edge);
          if (edge === "start" && S.clip.end - t >= 3) setRange(t, S.clip.end, false);
          if (edge === "end" && t - S.clip.start >= 3) setRange(S.clip.start, t, false);
          seek(edge === "start" ? S.clip.start : Math.max(S.clip.start, S.clip.end - 2));
        };
        const up = () => { el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up); commitRange(); };
        el.addEventListener("pointermove", move); el.addEventListener("pointerup", up);
      });
    }
    kids.push(hl, hr, playhead,
      h("div", { class: "label", style: { left: "6px" } }, fmt(S.win.start, 0)),
      h("div", { class: "label", style: { right: "6px" } }, fmt(S.win.end, 0)));
    timeline.replaceChildren(...kids);
  }
  timeline.addEventListener("pointerdown", e => { if (e.target === timeline || e.target.classList.contains("range") || e.target.classList.contains("tick")) seek(x2t(e.offsetX)); });

  // ---- side panel tabs
  const tabBody = h("div", { class: "tab-body" });
  const tabs = h("div", { class: "tabs" });
  const TABS = [["trim", "구간"], ["style", "자막"], ["layout", "화면"], ["export", "내보내기"], ["publish", "게시"]];
  function renderTabs() {
    tabs.replaceChildren(...TABS.map(([id, label]) => h("button", { class: S.tab === id ? "on" : "", onclick: () => { S.tab = id; renderTabs(); renderTab(); } }, label)));
  }
  function renderTab() {
    const fn = { trim: trimTab, style: styleTab, layout: layoutTab, export: exportTab, publish: () => publishTab(pubCtx) }[S.tab];
    const scroll = tabBody.scrollTop;
    tabBody.replaceChildren(...fn());
    tabBody.scrollTop = scroll;
  }

  // --- tab: trim & text
  const saveTitle = debounce(v => patchClip({ title: v }).then(updateClipList), 500);
  const saveKeywords = debounce(v => patchClip({ keywords: v.split(",") }).then(() => preview.setClip(S.clip)), 500);
  function trimTab() {
    const c = S.clip;
    const selWord = S.words.find(w => w.i === S.sel);
    const tools = h("div", { class: "word-tools row wrap" },
      selWord ? [h("span", { class: "muted" }, `"${selWord.text}"`),
        h("button", { class: "small", onclick: () => setRange(selWord.start, S.clip.end) }, "여기서 시작"),
        h("button", { class: "small", onclick: () => setRange(S.clip.start, selWord.end) }, "여기서 끝"),
        h("button", { class: "small", onclick: () => editWord(selWord) }, "단어 수정")]
        : h("span", { class: "hint" }, "단어를 클릭하면 그 위치로 이동하고, 시작·끝점을 지정하거나 오타를 고칠 수 있어요. 단축키: Space 재생, I 시작점, O 끝점"));
    const words = h("div", { class: "words" }, S.words.map(w => {
      const inside = w.end > c.start && w.start < c.end;
      const span = h("span", { class: "word" + (inside ? "" : " out") + (w.i === S.sel ? " sel" : ""), "data-i": w.i,
        onclick: () => { S.sel = w.i; seek(w.start); renderTab(); }, ondblclick: () => editWord(w) }, w.text);
      return [span, " "];
    }));
    return [
      h("div", { class: "section" }, h("h3", {}, "구간", h("span", { class: "badge" }, `${fmt(c.end - c.start)}초`)),
        h("div", { class: "row" }, h("span", { class: "muted" }, `${fmt(c.start)} → ${fmt(c.end)}`), h("span", { class: "grow" }),
          h("button", { class: "small", disabled: c.start === c.original.start && c.end === c.original.end, onclick: async () => {
            const clip = await api(`/api/projects/${pid}/clips/${c.id}/reset`, { method: "POST" });
            S.clip = { ...S.clip, ...clip }; preview.setClip(S.clip); await loadWords(); loadPlan();
          } }, "AI 추천 구간으로"))),
      h("div", { class: "section" }, h("h3", {}, "제목"),
        h("input", { type: "text", value: c.title, maxLength: 80, oninput: e => { S.clip.title = e.target.value; preview.setClip(S.clip); saveTitle(e.target.value); } }),
        h("label", { style: { marginTop: "10px" } }, "강조 키워드 (쉼표로 구분)"),
        h("input", { type: "text", value: (c.keywords || []).join(", "), oninput: e => { S.clip.keywords = e.target.value.split(",").map(s => s.trim()); preview.setClip(S.clip); saveKeywords(e.target.value); } }),
        c.reason ? h("p", { class: "hint" }, "AI 추천 이유: ", c.reason) : null),
      h("div", { class: "section" }, h("h3", {}, "자막 텍스트"), tools, words),
    ];
  }

  function editWord(w) {
    const span = tabBody.querySelector(`span.word[data-i="${w.i}"]`);
    if (!span) return;
    const input = h("input", { type: "text", value: w.text, size: Math.max(3, w.text.length + 1) });
    const save = async () => {
      const text = input.value.trim();
      if (text && text !== w.text) {
        try {
          const nw = await api(`/api/projects/${pid}/words/${w.i}`, { method: "PATCH", body: { text } });
          Object.assign(w, nw); preview.setWords(S.words); toast("자막을 고쳤어요");
        } catch (e) { toast(e.message, true); }
      }
      renderTab();
    };
    input.addEventListener("keydown", e => { if (e.key === "Enter") save(); if (e.key === "Escape") renderTab(); });
    input.addEventListener("blur", save);
    span.replaceWith(input); input.focus(); input.select();
  }

  // --- tab: caption style
  const saveOverrides = debounce(ov => patchEdits({ style_overrides: ov }), 400);
  function setOverride(key, value) {
    const ov = { ...S.clip.edits.style_overrides };
    if (value === null || value === PRESETS[S.clip.edits.preset][key]) delete ov[key]; else ov[key] = value;
    S.clip.edits = { ...S.clip.edits, style_overrides: ov };
    preview.setStyle(style(), S.clip.edits.show_title);
    saveOverrides(ov);
  }
  function styleTab() {
    const st = style(), ov = S.clip.edits.style_overrides;
    const color = (key, label) => h("div", {}, h("label", {}, label),
      h("div", { class: "color-row" },
        h("input", { type: "color", value: "#" + (st[key] || "FFFFFF"), oninput: e => setOverride(key, e.target.value.slice(1).toUpperCase()) }),
        h("code", {}, st[key] ? "#" + st[key] : "없음"),
        key in ov ? h("button", { class: "small ghost", onclick: () => { setOverride(key, null); renderTab(); } }, "되돌리기") : null));
    return [
      h("div", { class: "section" }, h("h3", {}, "스타일 선택"),
        h("div", { class: "presets" }, Object.values(PRESETS).map(p => h("button", {
          class: "preset" + (p.id === S.clip.edits.preset ? " on" : ""), title: p.description,
          onclick: async () => {
            S.clip.edits = { ...S.clip.edits, preset: p.id, style_overrides: {} };
            preview.setStyle(style(), S.clip.edits.show_title); renderTab();
            await patchEdits({ preset: p.id, style_overrides: {} });
          },
        }, h("img", { src: `/api/projects/${pid}/style-preview/${p.id}.jpg`, loading: "lazy", alt: "" }),
          h("div", { class: "n" }, p.label, ["signature", "sticker"].includes(p.mode) ? h("span", { class: "sig" }, "SIGNATURE") : null),
          h("div", { class: "d" }, p.description.replace(/^시그니처 · /, "")))))),
      h("div", { class: "section" }, h("h3", {}, "세부 조정", Object.keys(ov).length ? h("button", { class: "small ghost", onclick: () => {
          S.clip.edits = { ...S.clip.edits, style_overrides: {} }; preview.setStyle(style(), S.clip.edits.show_title); saveOverrides({}); renderTab();
        } }, "모두 초기화") : null),
        h("div", { class: "custom-grid" },
          h("div", {}, h("label", {}, "글꼴"), h("select", { onchange: e => setOverride("font", e.target.value) },
            [...new Set([st.font, ...FONTS])].map(f => h("option", { value: f, selected: f === st.font }, f)))),
          h("div", {}, h("label", {}, "위치"), h("select", { onchange: e => setOverride("position", e.target.value) },
            [["auto", "자동 (화면 따라)"], ["top", "위"], ["middle", "가운데"], ["bottom", "아래"]].map(([v, l]) => h("option", { value: v, selected: v === st.position }, l)))),
          h("div", { style: { gridColumn: "1 / -1" } }, h("label", {}, `글자 크기 ${st.size}px`),
            h("input", { type: "range", min: 40, max: 150, value: st.size, oninput: e => { setOverride("size", +e.target.value); e.target.previousSibling.textContent = `글자 크기 ${e.target.value}px`; } })),
          color("color", "글자색"),
          st.mode === "box" ? color("word_box_color", "단어 박스 색") : color("highlight", st.mode === "sweep" ? "채워지는 색" : "말하는 단어 색"),
          color("keyword_color", "키워드 색"),
          color("title_box_color", "제목 배경색")),
        h("label", { class: "row", style: { marginTop: "14px", color: "var(--text)", fontSize: "13px" } },
          h("input", { type: "checkbox", checked: S.clip.edits.show_title, onchange: e => {
            S.clip.edits.show_title = e.target.checked; preview.setStyle(style(), e.target.checked); patchEdits({ show_title: e.target.checked });
          } }), "상단에 제목 표시")),
      h("p", { class: "hint" }, "미리보기는 근사치예요. 최종 영상은 내보내기에서 정확하게 렌더링됩니다."),
    ];
  }

  // --- tab: layout
  function layoutTab() {
    const rel = video.currentTime - (S.plan?.clip_start ?? S.clip.start);
    const overrides = S.clip.edits.shot_overrides || [];
    const shots = S.plan ? S.plan.shots : [];
    return [
      h("div", { class: "section" }, h("h3", {}, "전체 레이아웃"),
        h("div", { class: "layouts" }, Object.entries(LAYOUT_LABEL).map(([v, l]) => h("button", { class: S.clip.edits.layout === v ? "on" : "",
          onclick: async () => { await patchEdits({ layout: v, shot_overrides: [] }); renderTab(); loadPlan(); } }, l))),
        h("p", { class: "hint" }, "자동: 장면마다 얼굴 수와 위치를 보고 고릅니다. 얼굴 추적 장면에서는 미리보기를 좌우로 드래그해 화면 위치를 옮길 수 있어요.")),
      h("div", { class: "section" }, h("h3", {}, "장면별 조정", h("span", { class: "badge" }, `${shots.length}개 장면`)),
        S.plan ? h("div", { class: "shots" }, shots.map(s => {
          const o = overrides.find(x => Math.abs(x.start - s.start) < 0.35) || {};
          const cur = rel >= s.start && rel < s.end;
          return h("div", { class: "shot-row" + (cur ? " cur" : "") },
            h("button", { class: "ghost small", style: { textAlign: "left" }, onclick: () => seek(S.plan.clip_start + s.start + 0.05) },
              `${fmt(s.start)} – ${fmt(s.end)}`, h("div", { class: "muted", style: { fontSize: "11px" } }, LAYOUT_LABEL[s.layout] + (o.cx !== undefined ? " · 위치 조정됨" : ""))),
            h("select", { onchange: e => setShotOverride(s.start, { layout: e.target.value === "auto" ? null : e.target.value, cx: e.target.value === "auto" ? null : o.cx ?? null }) },
              Object.entries(LAYOUT_LABEL).map(([v, l]) => h("option", { value: v, selected: (o.layout || "auto") === v }, l))),
            o.layout || o.cx !== undefined ? h("button", { class: "small ghost", title: "자동으로 되돌리기", onclick: () => setShotOverride(s.start, { layout: null, cx: null }) }, "↺") : h("span"));
        })) : h("p", { class: "muted" }, "분석 중…")),
    ];
  }

  // --- tab: export
  function exportTab() {
    const r = S.clip.render;
    const job = S.renderJob;
    const running = job && (job.status === "queued" || job.status === "running");
    return [
      h("div", { class: "section" }, h("h3", {}, "세로 숏츠로 내보내기"),
        h("p", { class: "hint" }, "1080×1920 MP4로 렌더링합니다. 화면 구성·자막 스타일·구간이 모두 반영돼요."),
        h("button", { class: "primary", style: { width: "100%" }, disabled: running, onclick: startRender }, running ? "렌더링 중…" : r ? "다시 렌더링" : "렌더링 시작"),
        running ? h("div", { style: { marginTop: "12px" } }, h("div", { class: "progress" }, h("div", { style: { width: `${(job.progress || 0) * 100}%` } })),
          h("p", { class: "hint" }, `${job.message} · ${Math.round((job.progress || 0) * 100)}%`)) : null,
        job?.status === "error" ? h("p", { style: { color: "var(--danger)" } }, job.error) : null),
      r ? h("div", { class: "section result" }, h("h3", {}, "최근 결과", h("span", { class: "badge ok" }, new Date(r.rendered_at * 1000).toLocaleTimeString("ko-KR"))),
        h("video", { src: `/api/projects/${pid}/files/${r.file}`, controls: true, playsInline: true }),
        h("div", { class: "row", style: { marginTop: "10px" } },
          h("a", { href: `/api/projects/${pid}/files/${r.file}?download=true`, download: r.file }, h("button", {}, "⬇ 다운로드")),
          (r.start !== S.clip.start || r.end !== S.clip.end) ? h("span", { class: "badge warn" }, "편집 내용이 바뀌었어요 — 다시 렌더링하세요") : null)) : null,
    ];
  }

  async function startRender() {
    try {
      await saveTitle.flush?.(S.clip.title);
      const { job_id } = await api(`/api/projects/${pid}/clips/${S.clip.id}/render`, { method: "POST" });
      S.renderJob = { status: "queued", progress: 0, message: "대기 중" };
      renderTab();
      const poll = async () => {
        S.renderJob = await api(`/api/jobs/${job_id}`);
        if (S.renderJob.status === "done") {
          const p = await api(`/api/projects/${pid}`);
          S.clip = { ...S.clip, render: p.clips.find(c => c.id === S.clip.id).render };
          toast("렌더링이 끝났어요"); updateClipList();
        } else if (S.renderJob.status !== "error") renderTimer = setTimeout(poll, 800);
        if (S.tab === "export") renderTab();
      };
      let renderTimer = setTimeout(poll, 500);
      cleanupFns.push(() => clearTimeout(renderTimer));
    } catch (e) { toast(e.message, true); }
  }

  // ---- publishing context (게시 tab)
  const pubCtx = {
    pid, cleanupFns: null, publishJob: null,
    clip: () => S.clip,
    setPublish: (pub, rerender = true) => { S.clip = { ...S.clip, publish: pub }; updateClipList(); if (rerender && S.tab === "publish") renderTab(); },
    rerender: () => { if (S.tab === "publish") renderTab(); },
  };

  // ---- clip list
  const clipList = h("nav", { class: "clips" });
  async function reloadClips() {
    const p = await api(`/api/projects/${pid}`);
    project.clips = p.clips;
    const me = p.clips.find(c => c.id === S.clip.id);
    if (me) S.clip = { ...S.clip, publish: me.publish, render: me.render };
    updateClipList(); if (S.tab === "publish") renderTab();
  }
  function updateClipList() {
    project.clips = project.clips.map(c => c.id === S.clip.id ? { ...c, ...S.clip } : c);
    const badge = c => {
      const b = c.publish?.status && PUBLISH_BADGE[c.publish.status];
      if (b) return h("span", { class: "badge " + b[0] }, b[1]);
      return c.render ? h("span", { class: "badge ok" }, "렌더링됨") : null;
    };
    clipList.replaceChildren(
      h("button", { class: "bulk-btn", onclick: () => openBulk(project, reloadClips) }, "📅 일괄 예약 게시"),
      ...project.clips.map(c => h("a", { class: "clip-item" + (c.id === S.clip.id ? " active" : ""), href: `#/p/${pid}/${c.id}` },
      h("img", { src: `/api/projects/${pid}/clips/${c.id}/thumb.jpg`, alt: "", loading: "lazy" }),
      h("div", {}, h("div", { class: "t" }, c.title),
        h("div", { class: "m" }, h("span", { class: "badge score" }, c.score), `${fmt(c.end - c.start, 0)}`, badge(c))))));
  }

  // ---- per-frame UI sync (playhead, time, loop, current word)
  let lastWord = null, raf;
  const tickUI = () => {
    const t = video.currentTime;
    if (!video.paused && t >= S.clip.end) { if (S.loop) video.currentTime = S.clip.start; else video.pause(); }
    playhead.style.left = t2p(t);
    timeLbl.textContent = `${fmt(Math.max(0, t - S.clip.start))} / ${fmt(S.clip.end - S.clip.start)}`;
    const w = S.words.find(w => t >= w.start && t < w.end);
    const wi = w ? w.i : null;
    if (wi !== lastWord && S.tab === "trim") {
      tabBody.querySelector("span.word.cur")?.classList.remove("cur");
      if (wi !== null) tabBody.querySelector(`span.word[data-i="${wi}"]`)?.classList.add("cur");
      lastWord = wi;
    }
    raf = requestAnimationFrame(tickUI);
  };
  raf = requestAnimationFrame(tickUI);

  // ---- keyboard
  const onKey = e => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    if (e.code === "Space") { e.preventDefault(); togglePlay(); }
    if (e.key === "i" || e.key === "I") setRange(snap(video.currentTime, "start"), S.clip.end);
    if (e.key === "o" || e.key === "O") setRange(S.clip.start, snap(video.currentTime, "end"));
  };
  document.addEventListener("keydown", onKey);

  const renderTop = h("button", { class: "primary", onclick: () => { S.tab = "export"; renderTabs(); renderTab(); startRender(); } }, "렌더링");
  app.replaceChildren(
    topbar(h("span", { class: "crumb grow" }, "/ ", project.name), saveState, ytChip(), renderTop),
    h("div", { class: "editor" },
      clipList,
      h("section", { class: "stage" },
        h("div", { class: "viewer" }, preview.el, video),
        h("div", { class: "controls" }, playBtn, h("button", { class: "small", onclick: () => seek(S.clip.start) }, "⏮ 처음"), loopBtn, timeLbl, h("span", { class: "grow" }),
          h("span", { class: "muted", style: { fontSize: "12px" } }, "타임라인 양쪽 손잡이로 구간을 조절하세요 (단어 단위로 맞춰짐)")),
        timeline),
      h("aside", { class: "side" }, tabs, tabBody)));

  const cleanupFns = [];
  pubCtx.cleanupFns = cleanupFns;
  cleanup = () => { preview.destroy(); cancelAnimationFrame(raf); document.removeEventListener("keydown", onKey); video.pause(); cleanupFns.forEach(f => f()); };
  updateClipList(); renderTabs(); renderTimeline(); renderTab();
  loadWords(); loadPlan.flush();
}

route();
