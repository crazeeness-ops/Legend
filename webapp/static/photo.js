// Photo shorts: creation form (home) and the photo editor.
import { h, api, toast, debounce, upload, modal, clamp } from "./util.js";
import { ytStatus, setupGuide } from "./publish.js";

let CATALOG = null;
export async function photoCatalog() {
  if (!CATALOG) CATALOG = await api("/api/photo/templates");
  return CATALOG;
}

// ------------------------------------------------------------------ creation form
export function photoCreateForm(cat, features) {
  let tpl = cat.templates[0];
  let files = [];
  const tplRow = h("div", { class: "tpl-cards" });
  const fieldsBox = h("div");
  const fieldInputs = {};
  const drawTemplates = () => {
    tplRow.replaceChildren(...cat.templates.map(t => h("button", { type: "button", class: "tpl" + (t.id === tpl.id ? " on" : ""),
      onclick: () => { tpl = t; drawTemplates(); drawFields(); } }, h("span", { class: "ic" }, t.icon), t.label)));
  };
  const drawFields = () => {
    fieldsBox.replaceChildren(...tpl.fields.map(f => {
      fieldInputs[f.key] = fieldInputs[f.key] || h("input", { type: "text" });
      fieldInputs[f.key].placeholder = `예: ${f.placeholder}`;
      return h("div", { class: "field" }, h("label", {}, f.label), fieldInputs[f.key]);
    }));
  };
  drawTemplates(); drawFields();

  const input = h("input", { type: "file", accept: "image/*,.heic,.heif", multiple: true, style: { display: "none" } });
  const strip = h("div", { class: "photo-strip" });
  const drop = h("div", { class: "drop", onclick: () => input.click() }, `사진을 끌어다 놓거나 클릭해서 선택하세요 (최대 ${cat.max_photos}장${cat.heic ? ", 아이폰 HEIC 가능" : ""})`);
  const setFiles = list => {
    files = [...files, ...list].slice(0, cat.max_photos);
    drop.classList.toggle("has", files.length > 0);
    drop.textContent = files.length ? `📸 ${files.length}장 선택됨 · 더 추가하려면 클릭` : drop.textContent;
    strip.replaceChildren(...files.map((f, i) => {
      const url = /\.(heic|heif)$/i.test(f.name) ? null : URL.createObjectURL(f);
      return h("div", { class: "ph" }, url ? h("img", { src: url, alt: "" }) : h("span", {}, "HEIC"),
        h("button", { class: "x", title: "빼기", onclick: e => { e.stopPropagation(); files.splice(i, 1); setFiles([]); } }, "×"));
    }));
    go.disabled = !files.length;
  };
  input.onchange = () => setFiles([...input.files]);
  drop.ondragover = e => { e.preventDefault(); drop.classList.add("has"); };
  drop.ondrop = e => { e.preventDefault(); setFiles([...e.dataTransfer.files].filter(f => /image|heic|heif/i.test(f.type + f.name))); };

  const notes = h("input", { type: "text", placeholder: "예: 밝고 귀엽게, 20대 타깃 (선택)" });
  const ai = h("input", { type: "checkbox", checked: !!features.claude });
  const voice = h("input", { type: "checkbox", checked: false, disabled: !cat.tts.available });
  const bar = h("div"), progress = h("div", { class: "progress", style: { display: "none", marginBottom: "10px" } }, bar);
  const go = h("button", { class: "primary", style: { width: "100%" }, disabled: true }, "사진으로 숏츠 만들기");
  go.onclick = async () => {
    const f = new FormData();
    files.forEach(x => f.append("photos", x));
    f.append("template", tpl.id);
    f.append("fields", JSON.stringify(Object.fromEntries(tpl.fields.map(x => [x.key, fieldInputs[x.key].value]))));
    f.append("notes", notes.value);
    f.append("ai", ai.checked ? "true" : "false");
    f.append("voice", voice.checked ? "true" : "false");
    go.disabled = true; go.textContent = "올리는 중…"; progress.style.display = "block";
    try {
      const p = await upload("/api/photo-projects", f, x => (bar.style.width = `${x * 100}%`));
      location.hash = `#/photo/${p.id}`;
    } catch (e) { toast(e.message, true); go.disabled = false; go.textContent = "사진으로 숏츠 만들기"; }
  };

  return h("div", {},
    h("div", { class: "field" }, h("label", {}, "어떤 숏츠인가요?"), tplRow),
    h("div", { class: "field" }, drop, input, strip),
    fieldsBox,
    h("div", { class: "field" }, h("label", {}, "추가 요청 (선택)"), notes),
    h("label", { class: "row check" }, ai, features.claude ? "✨ AI가 사진을 보고 대본 쓰기" : "✨ AI 대본 (API 키 필요 — 없으면 템플릿 문구로 채워요)"),
    h("label", { class: "row check", style: { margin: "6px 0 12px" } }, voice,
      cat.tts.available ? "🎙️ AI 목소리로 읽어 주기" : "🎙️ AI 목소리 (음성 서비스 키 필요)"),
    progress, go,
    h("p", { class: "hint" }, "직접 찍었거나 사용 권리가 있는 사진만 올려 주세요. 사람 얼굴이 나오면 동의를 받아 주세요."));
}

// ------------------------------------------------------------------ editor
const PH = (pid, file) => `/api/photo-projects/${pid}/photos/${file}`;

export async function photoView(app, topbar, pid, presets) {
  const cat = await photoCatalog();
  let p = await api(`/api/photo-projects/${pid}`);
  if (p.status !== "ready" || !p.script) return waiting(app, topbar, p, () => photoView(app, topbar, pid, presets));

  const tpl = cat.templates.find(t => t.id === p.settings.template) || cat.templates[0];
  const S = { script: structuredClone(p.script), settings: structuredClone(p.settings), sel: 0, playing: false, renderJob: null };
  const saveState = h("span", { class: "save-state" }, "저장됨");
  // edits within the debounce window are merged, so a settings change is never dropped by a script edit
  let pending = null;
  const send = debounce(async () => {
    if (!pending) return;
    const patch = pending; pending = null;
    saveState.textContent = "저장 중…";
    try { p = await api(`/api/photo-projects/${pid}`, { method: "PATCH", body: patch }); saveState.textContent = "저장됨"; }
    catch (e) { toast(e.message, true); saveState.textContent = "저장 실패"; }
  }, 500);
  const save = patch => {
    pending = pending || {};
    for (const [k, v] of Object.entries(patch)) pending[k] = k === "settings" ? { ...(pending.settings || {}), ...v } : v;
    saveState.textContent = "저장 대기";
    send();
  };
  save.flush = () => send.flush();
  const saveScript = () => save({ script: { title: S.script.title || "", slides: S.script.slides.map(s => ({ photo: s.photo, text: s.text || "", duration: +s.duration || 3 })), keywords: S.script.keywords || [], hashtags: S.script.hashtags || [] } });
  const saveSettings = patch => { Object.assign(S.settings, patch); save({ settings: patch }); };

  // ---- images for the preview
  const imgs = {};
  const img = file => imgs[file] || (imgs[file] = Object.assign(new Image(), { src: PH(pid, file) }));
  p.photos.forEach(ph => img(ph.file));

  // ---- preview (approximation of the server render)
  const canvas = h("canvas", { width: 540, height: 960 });
  const ctx = canvas.getContext("2d");
  const capLine = h("div", { class: "ph-cap" });
  const phone = h("div", { class: "phone" }, canvas, capLine);
  let t0 = 0, raf;
  const total = () => S.script.slides.reduce((a, s) => a + (+s.duration || 3), 0);
  const drawImage = (im, prog, i) => {
    const W = canvas.width, H = canvas.height;
    if (!im.complete || !im.naturalWidth) return;
    const e = prog * prog * (3 - 2 * prog), z = i % 2 === 0 ? 1 + 0.1 * e : 1.1 - 0.1 * e;
    const iw = im.naturalWidth, ih = im.naturalHeight;
    if (iw / ih <= 0.8) {
      const s = Math.max(W / iw, H / ih) * z;
      ctx.drawImage(im, (W - iw * s) / 2 + ((i % 3) - 1) * (e - 0.5) * 30, (H - ih * s) / 2, iw * s, ih * s);
    } else {
      const s0 = Math.max(W / iw, H / ih);
      ctx.filter = "blur(12px) brightness(0.5)"; ctx.drawImage(im, (W - iw * s0) / 2, (H - ih * s0) / 2, iw * s0, ih * s0); ctx.filter = "none";
      const s = W / iw * z;
      ctx.drawImage(im, (W - iw * s) / 2, H * 0.4 - ih * s / 2, iw * s, ih * s);
    }
  };
  const style = () => presets[S.settings.preset] || presets.legend;
  const showCaption = text => {
    const st = style();
    const scale = phone.clientHeight / 1920;
    capLine.textContent = text || "";
    Object.assign(capLine.style, {
      fontFamily: `"${st.font}", "Noto Sans KR", sans-serif`, fontSize: `${st.size * scale * 0.69}px`, color: "#" + st.color,
      top: `${1920 * 0.74 * scale}px`, fontWeight: st.bold ? 700 : 400,
      textShadow: st.box ? "none" : Array.from({ length: 12 }, (_, k) => { const a = k / 12 * Math.PI * 2, r = st.outline * scale; return `${(Math.cos(a) * r).toFixed(1)}px ${(Math.sin(a) * r).toFixed(1)}px 0 #${st.outline_color}`; }).join(","),
      background: st.box ? "rgba(0,0,0,.65)" : st.mode === "signature" ? `linear-gradient(transparent 55%, #${st.word_box_color} 55%)` : "transparent",
    });
  };
  const frame = now => {
    const W = canvas.width, H = canvas.height;
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
    const slides = S.script.slides;
    if (!slides.length) { raf = requestAnimationFrame(frame); return; }
    let i = S.sel, prog = 0.4;
    if (S.playing) {
      let t = ((now - t0) / 1000) % total(), acc = 0;
      for (i = 0; i < slides.length; i++) { const d = +slides[i].duration || 3; if (t < acc + d) { prog = (t - acc) / d; break; } acc += d; }
      i = Math.min(i, slides.length - 1);
      if (i !== S.cur) { S.cur = i; highlight(i); }
    }
    drawImage(img(slides[i].photo), prog, i);
    showCaption(slides[i].text);
    raf = requestAnimationFrame(frame);
  };
  raf = requestAnimationFrame(frame);
  const playBtn = h("button", { class: "primary", onclick: () => { S.playing = !S.playing; t0 = performance.now(); playBtn.textContent = S.playing ? "❚❚ 멈춤" : "▶ 미리보기"; } }, "▶ 미리보기");

  // ---- slide list
  const list = h("div", { class: "slides" });
  const highlight = i => list.querySelectorAll(".slide").forEach((el, k) => el.classList.toggle("on", k === i));
  const drawSlides = () => {
    list.replaceChildren(...S.script.slides.map((s, i) => {
      const ph = p.photos.find(x => x.file === s.photo);
      const move = d => { const j = i + d; if (j < 0 || j >= S.script.slides.length) return; const a = S.script.slides; [a[i], a[j]] = [a[j], a[i]]; S.sel = j; drawSlides(); saveScript(); };
      return h("div", { class: "slide" + (i === S.sel ? " on" : ""), onclick: () => { S.sel = i; S.playing = false; playBtn.textContent = "▶ 미리보기"; highlight(i); } },
        h("img", { src: ph ? ph.thumb_url : "", alt: "" }),
        h("div", { class: "grow" },
          h("textarea", { rows: 2, maxLength: 60, value: s.text || "", placeholder: "이 장면의 문구", oninput: e => { s.text = e.target.value; saveScript(); } }),
          h("div", { class: "row" },
            h("input", { type: "number", min: 1, max: 10, step: 0.5, value: s.duration, title: "보여줄 시간(초)", oninput: e => { s.duration = clamp(+e.target.value || 3, 1, 10); saveScript(); } }),
            h("span", { class: "muted" }, "초"), h("span", { class: "grow" }),
            h("button", { class: "small ghost", title: "위로", onclick: e => { e.stopPropagation(); move(-1); } }, "↑"),
            h("button", { class: "small ghost", title: "아래로", onclick: e => { e.stopPropagation(); move(1); } }, "↓"),
            h("button", { class: "small ghost danger", title: "이 장면 빼기", onclick: e => {
              e.stopPropagation(); if (S.script.slides.length === 1) return toast("장면이 하나는 있어야 해요", true);
              S.script.slides.splice(i, 1); S.sel = Math.max(0, S.sel - (S.sel >= i ? 1 : 0)); drawSlides(); saveScript(); } }, "✕"))));
    }));
  };
  drawSlides();
  const addInput = h("input", { type: "file", accept: "image/*,.heic,.heif", multiple: true, style: { display: "none" }, onchange: async () => {
    const f = new FormData(); [...addInput.files].forEach(x => f.append("photos", x));
    try { p = await upload(`/api/photo-projects/${pid}/photos`, f); S.script.slides = structuredClone(p.script.slides); p.photos.forEach(ph => img(ph.file)); drawSlides(); toast("사진을 추가했어요"); }
    catch (e) { toast(e.message, true); }
  } });
  const unused = () => p.photos.filter(ph => !S.script.slides.some(s => s.photo === ph.file));

  // ---- settings panel
  const panel = h("div", { class: "tab-body" });
  const drawPanel = () => {
    const st = S.settings, voices = cat.tts.voices;
    const fieldEls = tpl.fields.map(f => h("div", { class: "field" }, h("label", {}, f.label),
      h("input", { type: "text", value: (st.fields || {})[f.key] || "", placeholder: f.placeholder, oninput: e => saveSettings({ fields: { ...S.settings.fields, [f.key]: e.target.value } }) })));
    const job = S.renderJob, running = job && !["done", "error"].includes(job.status);
    panel.replaceChildren(
      h("div", { class: "section" }, h("h3", {}, "제목"),
        h("input", { type: "text", value: S.script.title || "", maxLength: 80, oninput: e => { S.script.title = e.target.value; saveScript(); } }),
        h("label", { class: "row check", style: { marginTop: "8px" } }, h("input", { type: "checkbox", checked: !!st.show_title, onchange: e => saveSettings({ show_title: e.target.checked }) }), "영상 위쪽에 제목 표시")),
      h("div", { class: "section" }, h("h3", {}, "자막 스타일"),
        h("select", { onchange: e => saveSettings({ preset: e.target.value }) }, Object.values(presets).map(x => h("option", { value: x.id, selected: x.id === st.preset }, x.label + (["signature", "sticker"].includes(x.mode) ? " ★" : ""))))),
      h("div", { class: "section" }, h("h3", {}, `${tpl.icon} ${tpl.label} 정보`),
        h("p", { class: "hint" }, "마지막 장면의 안내 카드와 AI 대본에 쓰여요."), ...fieldEls,
        h("label", { class: "row check" }, h("input", { type: "checkbox", checked: st.end_card !== false, onchange: e => saveSettings({ end_card: e.target.checked }) }), "마지막에 안내 카드 넣기")),
      h("div", { class: "section" }, h("h3", {}, "🎙️ AI 목소리"),
        cat.tts.available ? [
          h("label", { class: "row check" }, h("input", { type: "checkbox", checked: !!st.voice, onchange: e => saveSettings({ voice: e.target.checked }) }), "문구를 목소리로 읽어 주기"),
          voices.length > 1 ? h("select", { style: { marginTop: "8px" }, onchange: e => saveSettings({ voice_id: e.target.value }) },
            voices.map(v => h("option", { value: v.id, selected: v.id === st.voice_id }, v.label))) : null,
          h("p", { class: "hint" }, "목소리 길이에 맞춰 장면 시간이 자동으로 늘어나요.")]
          : h("p", { class: "hint" }, "네이버 클로바 보이스나 ElevenLabs 키를 설정하면 쓸 수 있어요.")),
      h("div", { class: "section" }, h("h3", {}, "🎵 배경음악"),
        st.music ? h("div", { class: "row" }, h("span", { class: "grow" }, st.music_name || "음악"),
          h("button", { class: "small ghost", onclick: async () => { p = await api(`/api/photo-projects/${pid}/music`, { method: "DELETE" }); S.settings = p.settings; drawPanel(); } }, "빼기"))
          : h("button", { class: "small", onclick: () => musicInput.click() }, "음악 파일 올리기"),
        h("label", { style: { marginTop: "10px" } }, `음량 ${Math.round((st.music_volume ?? 0.3) * 100)}%`),
        h("input", { type: "range", min: 0, max: 100, value: Math.round((st.music_volume ?? 0.3) * 100), oninput: e => { saveSettings({ music_volume: +e.target.value / 100 }); e.target.previousSibling.textContent = `음량 ${e.target.value}%`; } }),
        h("p", { class: "hint" }, "저작권 걱정 없는 음원만 써 주세요 (예: YouTube 오디오 보관함).")),
      h("div", { class: "section" }, h("h3", {}, "✨ 대본 다시 만들기"),
        h("div", { class: "row" },
          h("button", { class: "small", onclick: () => regen(true) }, "AI로 다시 쓰기"),
          h("button", { class: "small ghost", onclick: () => regen(false) }, "템플릿 문구로")),
        unused().length ? h("p", { class: "hint" }, `대본에 안 쓰인 사진 ${unused().length}장이 있어요. `,
          h("a", { href: "#", onclick: e => { e.preventDefault(); unused().forEach(ph => S.script.slides.push({ photo: ph.file, text: "", duration: 3 })); drawSlides(); saveScript(); drawPanel(); } }, "모두 추가")) : null),
      h("div", { class: "section" },
        h("button", { class: "primary", style: { width: "100%" }, disabled: running, onclick: render }, running ? "만드는 중…" : p.render ? "🎬 영상 다시 만들기" : "🎬 영상 만들기"),
        running ? [h("div", { class: "progress", style: { marginTop: "10px" } }, h("div", { style: { width: `${(job.progress || 0) * 100}%` } })), h("p", { class: "hint" }, job.message)] : null,
        job?.status === "error" ? h("p", { style: { color: "var(--danger)" } }, job.error) : null),
      p.render ? h("div", { class: "section result" },
        h("video", { src: `/api/photo-projects/${pid}/files/${p.render.file}`, controls: true, playsInline: true }),
        h("div", { class: "row", style: { marginTop: "10px" } },
          h("a", { href: `/api/photo-projects/${pid}/files/${p.render.file}?download=true` }, h("button", {}, "⬇ 다운로드")),
          h("button", { onclick: publish }, "▶ YouTube에 올리기")),
        p.publish ? h("p", { class: "hint" }, "올린 영상: ", h("a", { href: p.publish.url, target: "_blank", rel: "noopener" }, p.publish.url)) : null) : null);
  };
  const musicInput = h("input", { type: "file", accept: "audio/*", style: { display: "none" }, onchange: async () => {
    const f = new FormData(); f.append("music", musicInput.files[0]);
    try { p = await upload(`/api/photo-projects/${pid}/music`, f); S.settings = p.settings; drawPanel(); } catch (e) { toast(e.message, true); }
  } });

  const poll = (jid, onDone) => {
    const tick = async () => {
      const job = await api(`/api/jobs/${jid}`);
      S.renderJob = job; drawPanel();
      if (job.status === "done") onDone(job);
      else if (job.status === "error") toast(job.error, true);
      else timer = setTimeout(tick, 900);
    };
    let timer = setTimeout(tick, 400);
    cleanups.push(() => clearTimeout(timer));
  };
  async function render() {
    await save.flush?.();
    try {
      const { job_id } = await api(`/api/photo-projects/${pid}/render`, { method: "POST" });
      S.renderJob = { status: "queued", progress: 0, message: "준비 중" }; drawPanel();
      poll(job_id, async () => { p = await api(`/api/photo-projects/${pid}`); toast("영상이 완성됐어요"); drawPanel(); app.querySelector(".photo-editor video")?.scrollIntoView({ behavior: "smooth", block: "center" }); });
    } catch (e) { toast(e.message, true); }
  }
  async function regen(ai) {
    if (!confirm("지금 문구가 새 대본으로 바뀌어요. 계속할까요?")) return;
    try {
      await api(`/api/photo-projects/${pid}/script?ai=${ai}`, { method: "POST" });
      photoView(app, topbar, pid, presets);
    } catch (e) { toast(e.message, true); }
  }
  async function publish() {
    const st = await ytStatus(true);
    if (!st.connected) return st.configured ? (location.href = `/oauth/google/start?return_to=${encodeURIComponent("/" + location.hash)}`) : setupGuide(st);
    const title = h("input", { type: "text", value: S.script.title || p.name, maxLength: 100 });
    const desc = h("textarea", { rows: 4, value: (S.script.hashtags || ["#Shorts"]).join(" ") });
    const go = h("button", { class: "primary", style: { width: "100%", marginTop: "12px" } }, `▶ '${st.channel.title}'에 올리기`);
    const msg = h("p", { class: "hint" });
    const close = modal("YouTube에 올리기", h("div", {}, h("label", {}, "제목"), title, h("label", { style: { marginTop: "10px" } }, "설명"), desc, go, msg));
    go.onclick = async () => {
      go.disabled = true;
      try {
        const { job_id } = await api(`/api/photo-projects/${pid}/publish`, { method: "POST", body: { title: title.value, description: desc.value } });
        const tick = async () => {
          const job = await api(`/api/jobs/${job_id}`);
          msg.textContent = `${job.message || ""} ${Math.round((job.progress || 0) * 100)}%`;
          if (job.status === "done") { toast("올렸어요"); close(); p = await api(`/api/photo-projects/${pid}`); drawPanel(); }
          else if (job.status === "error") { toast(job.error, true); go.disabled = false; }
          else setTimeout(tick, 800);
        };
        tick();
      } catch (e) { toast(e.message, true); go.disabled = false; }
    };
  }

  const cleanups = [];
  const nameIn = h("input", { type: "text", class: "name-in", value: p.name, maxLength: 80, oninput: e => save({ name: e.target.value }) });
  app.replaceChildren(topbar(h("span", { class: "crumb" }, "/ 📸"), nameIn, h("span", { class: "grow" }), saveState),
    h("div", { class: "editor photo-editor" },
      h("aside", { class: "clips" }, h("div", { class: "row", style: { marginBottom: "8px" } }, h("b", { class: "grow" }, `장면 ${S.script.slides.length}개`),
        h("button", { class: "small", onclick: () => addInput.click() }, "+ 사진")), addInput, list),
      h("section", { class: "stage" }, h("div", { class: "viewer" }, phone),
        h("div", { class: "controls" }, playBtn, h("span", { class: "muted", style: { fontSize: "12px" } },
          p.script.warning ? "⚠️ " + p.script.warning : p.script.source === "ai" ? "✨ AI가 사진을 보고 쓴 대본이에요. 자유롭게 고치세요." : "미리보기는 대략적인 모습이에요. 최종 영상은 '영상 만들기'로 확인하세요."))),
      h("aside", { class: "side" }, panel, musicInput)));
  drawPanel();
  return () => { cancelAnimationFrame(raf); cleanups.forEach(f => f()); };
}

function waiting(app, topbar, p, retry) {
  const bar = h("div"), msg = h("p", { class: "muted" }, p.job?.message || "준비 중");
  const body = h("div", { class: "center-msg" }, h("h2", {}, p.name), h("div", { class: "progress" }, bar), msg);
  app.replaceChildren(topbar(h("span", { class: "crumb" }, "/ 📸 ", p.name)), body);
  if (p.status === "error") { body.replaceChildren(h("h2", {}, "대본을 만들지 못했어요"), h("p", { class: "muted" }, p.error || ""), h("a", { href: "#/" }, "처음으로")); return; }
  const tick = async () => {
    const q = await api(`/api/photo-projects/${p.id}`);
    if (q.status === "ready" && q.script) return retry();
    if (q.status === "error") return waiting(app, topbar, q, retry);
    bar.style.width = `${Math.max(8, (q.job?.progress || 0) * 100)}%`;
    msg.textContent = q.job?.message || "준비 중";
    setTimeout(tick, 1200);
  };
  setTimeout(tick, 600);
}
