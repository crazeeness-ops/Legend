// Live 9:16 preview: draws the reframed video on a canvas (same rules as the server
// renderer) and overlays HTML captions that approximate the ASS styles. The final MP4
// is rendered server-side by libass, so this is a close, fast approximation.
import { h, clamp } from "./util.js";

const OUT_W = 1080, OUT_H = 1920;
const STRIP_TRAILING = /[.,。、]+$/;
const BREAK_AFTER = /[.?!。？！,]$/;
const POSITIONS = { top: 560, middle: 960, bottom: 1560 };
// libass sizes fonts by line height (ascender + descender ≈ 1.45em for Noto Sans CJK); CSS sizes by em.
const ASS_EM = 1 / 1.45;
const TILTS = [-2.0, 1.4, -1.0, 2.2, -1.6, 0.8]; // same sequence as signature.py
const STICKER_TILTS = [-3.5, 2.8, -2.2, 3.6, -3.0, 2.0];

export function paginate(words, maxChars, maxWords, maxGap = 0.7) {
  const pages = [];
  let cur = [], chars = 0;
  for (const w of words) {
    const text = w.text.trim().replace(STRIP_TRAILING, "");
    if (!text) continue;
    const n = text.replace(/ /g, "").length;
    if (cur.length && (chars + n > maxChars || cur.length >= maxWords || w.start - cur[cur.length - 1].end > maxGap
        || BREAK_AFTER.test(cur[cur.length - 1].raw.trim()))) {
      pages.push(cur); cur = []; chars = 0;
    }
    cur.push({ text, raw: w.text, start: w.start, end: w.end, i: w.i });
    chars += n;
  }
  if (cur.length) pages.push(cur);
  return pages;
}

const isKeyword = (word, kws) => {
  const w = word.toLowerCase();
  return kws.some(k => k && (w.includes(k.toLowerCase()) || (w.length >= 2 && k.toLowerCase().includes(w))));
};

function rgba(hex, alpha255 = 0) {
  const n = parseInt(hex, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${(1 - alpha255 / 255).toFixed(3)})`;
}

export class Preview {
  constructor(video) {
    this.video = video;
    this.canvas = h("canvas", { width: 540, height: 960 });
    this.ctx = this.canvas.getContext("2d");
    this.capLayer = h("div", { class: "cap-layer" });
    this.shotTag = h("div", { class: "shot-tag" });
    this.busy = h("div", { class: "busy", style: { display: "none" } }, "화면 분석 중…");
    this.el = h("div", { class: "phone" }, this.canvas, this.capLayer, this.shotTag, this.busy);
    this.plan = null; this.clip = null; this.style = null; this.words = []; this.pages = [];
    this.showTitle = true; this.onCropChange = null; this._capKey = ""; this._drag = null;
    this._bindDrag();
    const loop = () => { this.draw(); this._raf = requestAnimationFrame(loop); };
    this._raf = requestAnimationFrame(loop);
  }

  destroy() { cancelAnimationFrame(this._raf); }

  setBusy(on) { this.busy.style.display = on ? "flex" : "none"; }
  setPlan(plan) { this.plan = plan; this._capKey = ""; }
  setClip(clip) { this.clip = clip; this._capKey = ""; }
  setWords(words) { this.words = words; this._repage(); }
  setStyle(style, showTitle) { this.style = style; this.showTitle = showTitle; this._repage(); }

  _repage() {
    if (!this.style || !this.clip) return;
    const st = this.style;
    const inClip = this.words.filter(w => w.end > this.clip.start && w.start < this.clip.end);
    this.pages = paginate(inClip, st.max_chars, st.mode === "single" ? 1 : st.max_words);
    this._capKey = "";
  }

  shotAt(rel) {
    if (!this.plan) return null;
    let shot = this.plan.shots[0];
    for (const s of this.plan.shots) if (s.start <= rel) shot = s;
    return shot;
  }

  cropX(shot, rel) {
    const p = this.plan, kf = shot.keyframes;
    let cx = p.source[0] / 2;
    if (kf.length) {
      if (rel <= kf[0][0]) cx = kf[0][1];
      else if (rel >= kf[kf.length - 1][0]) cx = kf[kf.length - 1][1];
      else for (let i = 1; i < kf.length; i++) {
        if (rel <= kf[i][0]) {
          const [t0, c0] = kf[i - 1], [t1, c1] = kf[i];
          cx = t1 > t0 ? c0 + (c1 - c0) * (rel - t0) / (t1 - t0) : c1;
          break;
        }
      }
    }
    return clamp(Math.round(cx - p.crop_w / 2), 0, p.source[0] - p.crop_w);
  }

  draw() {
    const { ctx, canvas, video } = this;
    const W = canvas.width, H = canvas.height;
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
    if (video.readyState < 2) return;
    const vw = video.videoWidth, vh = video.videoHeight;
    const rel = this.plan ? video.currentTime - this.plan.clip_start : 0;
    const shot = this.shotAt(rel);
    const layout = shot ? shot.layout : "fit";
    this.shotTag.textContent = { track: "얼굴 추적", split: "2인 분할", fit: "전체 화면" }[layout] || "";
    this.canvas.classList.toggle("draggable", layout === "track");

    if (layout === "track") {
      const sx = this.cropX(shot, rel);
      const sw = this.plan.crop_w * (vw / this.plan.source[0]);
      ctx.drawImage(video, sx * vw / this.plan.source[0], 0, sw, vh, 0, 0, W, H);
    } else if (layout === "split" && shot.split) {
      const k = vw / this.plan.source[0];
      const [a, b] = shot.split;
      ctx.drawImage(video, a[0] * k, a[1] * k, a[2] * k, a[3] * k, 0, 0, W, H / 2);
      ctx.drawImage(video, b[0] * k, b[1] * k, b[2] * k, b[3] * k, 0, H / 2, W, H / 2);
      ctx.fillStyle = "#000"; ctx.fillRect(0, H / 2 - 1.5, W, 3);
    } else {
      const s = Math.max(W / vw, H / vh);
      ctx.filter = "blur(14px) brightness(0.55)";
      ctx.drawImage(video, (W - vw * s) / 2, (H - vh * s) / 2, vw * s, vh * s);
      ctx.filter = "none";
      const fh = Math.min(H, W * vh / vw), fw = fh < H ? W : H * vw / vh;
      ctx.drawImage(video, (W - fw) / 2, (H - fh) / 2, fw, fh);
    }
    this.drawCaptions(video.currentTime, layout);
  }

  captionY(layout) {
    const st = this.style;
    if (st.position !== "auto") return POSITIONS[st.position];
    if (layout === "split") return OUT_H / 2;
    if (layout === "fit" && this.plan) {
      const fh = Math.min(OUT_H, Math.floor(OUT_W * this.plan.source[1] / this.plan.source[0]));
      return Math.min(OUT_H - 380, OUT_H / 2 + Math.floor(fh / 2) + 170);
    }
    return Math.floor(OUT_H * 0.7);
  }

  drawCaptions(t, layout) {
    const st = this.style;
    if (!st || !this.clip) return;
    const scale = this.el.clientHeight / OUT_H;
    let pi = -1;
    for (let i = 0; i < this.pages.length; i++) {
      const p = this.pages[i];
      const next = this.pages[i + 1] ? this.pages[i + 1][0].start : this.clip.end;
      const end = Math.min(this.clip.end, next, p[p.length - 1].end + 0.6);
      if (t >= Math.max(this.clip.start, p[0].start) && t < end) { pi = i; break; }
    }
    const page = this.pages[pi] || [];
    let cur = -1;
    page.forEach((w, i) => { if (t >= w.start) cur = i; });
    if (cur < 0 && page.length) cur = 0;
    const sweep = st.mode === "sweep" && cur >= 0 ? clamp((t - page[cur].start) / Math.max(0.05, (page[cur + 1]?.start ?? page[cur].end) - page[cur].start), 0, 1) : 0;
    const key = [pi, cur, layout, scale.toFixed(4), this.showTitle, this.clip.title, st.mode === "sweep" ? sweep.toFixed(2) : ""].join("|");
    if (key === this._capKey) return;
    const pageChanged = this._capKey.split("|")[0] !== String(pi);
    this._capKey = key;

    const layer = this.capLayer;
    layer.replaceChildren();
    if (this.showTitle && this.clip.title && st.mode === "sticker") {
      layer.append(h("div", { class: "cap-title", style: { top: `${220 * scale}px`, padding: `0 ${140 * scale}px` } },
        h("span", { class: "stk-title", style: {
          fontSize: `${st.title_size * scale * ASS_EM}px`, color: "#" + st.title_color, "--accent": "#" + st.title_box_color,
          padding: `${18 * scale}px ${32 * scale}px`, borderRadius: `${26 * scale}px`,
          fontFamily: `"${st.font}", "Noto Sans KR", sans-serif`,
        } }, this.clip.title)));
    } else if (this.showTitle && this.clip.title && st.mode === "signature") {
      layer.append(h("div", { class: "cap-title", style: { top: `${220 * scale}px`, padding: `0 ${130 * scale}px` } },
        h("span", { class: "sig-title", style: {
          fontSize: `${st.title_size * scale * ASS_EM}px`, color: "#" + st.title_color,
          padding: `${20 * scale}px ${34 * scale}px ${20 * scale}px ${46 * scale}px`, borderRadius: `${22 * scale}px`,
          borderLeft: `${12 * scale}px solid #${st.title_box_color}`, fontFamily: `"${st.font}", "Noto Sans KR", sans-serif`,
        } }, this.clip.title)));
    } else if (this.showTitle && this.clip.title) {
      layer.append(h("div", { class: "cap-title", style: { top: `${230 * scale}px`, padding: `0 ${90 * scale}px` } },
        h("span", { style: {
          fontSize: `${st.title_size * scale * ASS_EM}px`, color: "#" + st.title_color, background: "#" + st.title_box_color,
          padding: `${4 * scale}px ${16 * scale}px`, fontFamily: `"${st.font}", "Noto Sans KR", sans-serif`,
          boxDecorationBreak: "clone", webkitBoxDecorationBreak: "clone", lineHeight: 1.45,
        } }, this.clip.title)));
    }
    if (!page.length) return;

    // outline as a ring of hard shadows (libass draws the border outside the glyph; CSS stroke eats into it)
    const r = st.box ? 0 : st.outline * scale;
    const shadows = [];
    if (r > 0) for (let k = 0; k < 16; k++) {
      const a = (k / 16) * Math.PI * 2;
      shadows.push(`${(Math.cos(a) * r).toFixed(2)}px ${(Math.sin(a) * r).toFixed(2)}px 0 #${st.outline_color}`);
    }
    if (st.blur) shadows.push(`0 0 ${st.blur * 3 * scale}px #${st.outline_color}`, `0 0 ${st.blur * 6 * scale}px #${st.outline_color}`);
    if (st.shadow && !st.box) shadows.push(`${st.shadow * scale * 1.5}px ${st.shadow * scale * 1.5}px 0 rgba(0,0,0,.55)`);
    const kws = this.clip.keywords || [];
    const line = h("div", {
      class: "cap-line" + (st.pop_in && pageChanged ? " pop" : "") + (st.mode === "signature" && pageChanged ? " rise" : ""),
      style: {
        top: `${this.captionY(layout) * scale}px`, padding: `0 ${80 * scale}px`,
        fontFamily: `"${st.font}", "Noto Sans KR", sans-serif`, fontSize: `${st.size * scale * ASS_EM}px`,
        fontWeight: st.bold ? 700 : 400, color: "#" + st.color,
        textShadow: shadows.join(",") || "none",
        wordSpacing: st.mode === "sticker" ? "0.23em" : "normal", // the renderer uses a double space here
      },
    });
    const inner = h("span", {
      style: st.box ? {
        background: rgba(st.box_color, st.box_alpha), padding: `${st.outline * scale * 0.6}px ${st.outline * scale}px`,
        boxDecorationBreak: "clone", webkitBoxDecorationBreak: "clone", lineHeight: 1.5,
      } : {},
    });
    page.forEach((w, i) => {
      const isCur = i === cur;
      const css = {};
      let text = st.uppercase ? w.text.toUpperCase() : w.text;
      if (st.mode === "reveal" && i > cur) css.visibility = "hidden";
      if (isCur && st.highlight && (st.mode === "karaoke" || st.mode === "reveal")) css.color = "#" + st.highlight;
      else if (st.keyword_color && isKeyword(w.text, kws) && st.mode !== "sweep" && !((st.mode === "signature" || st.mode === "sticker") && isCur)) css.color = "#" + st.keyword_color;
      if (st.mode === "sweep" && st.highlight) {
        if (i < cur) css.color = "#" + st.highlight;
        else if (isCur) {
          const p = (sweep * 100).toFixed(1);
          Object.assign(css, { backgroundImage: `linear-gradient(90deg, #${st.highlight} ${p}%, #${st.color} ${p}%)`,
            webkitBackgroundClip: "text", backgroundClip: "text", color: "transparent" });
        }
      }
      if (isCur && st.highlight_scale !== 100 && st.mode !== "sweep" && st.mode !== "static" && st.mode !== "single") css.transform = `scale(${st.highlight_scale / 100})`;
      if (isCur && st.mode === "box") {
        const pad = Math.max(8, Math.floor(st.size * 0.14)) * scale;
        Object.assign(css, { background: "#" + st.word_box_color, padding: `0 ${pad}px`, margin: `0 ${-pad * 0.4}px` });
      }
      const attrs = { class: "w", style: css };
      if (isCur && st.mode === "sticker") {
        attrs.class = "w stk";
        Object.assign(css, { "--fill": "#" + st.word_box_color, "--accent": "#" + (st.highlight || "FF3D6E"),
          "--tilt": `${STICKER_TILTS[(pi * 3 + i) % STICKER_TILTS.length]}deg` });
      }
      if (isCur && st.mode === "signature") {
        attrs.class = "w mk";
        css["--mk"] = "#" + st.word_box_color;
        css["--tilt"] = `${TILTS[(pi * 3 + i) % TILTS.length]}deg`;
      }
      inner.append(h("span", attrs, text));
      if (i < page.length - 1) inner.append(" ");
    });
    line.append(inner);
    layer.append(line);
  }

  _bindDrag() {
    const c = this.canvas;
    c.addEventListener("pointerdown", e => {
      if (!this.plan) return;
      const rel = this.video.currentTime - this.plan.clip_start;
      const shot = this.shotAt(rel);
      if (!shot || shot.layout !== "track") return;
      c.setPointerCapture(e.pointerId);
      this._drag = { x: e.clientX, cx0: this.cropX(shot, rel) + this.plan.crop_w / 2, shot };
    });
    c.addEventListener("pointermove", e => {
      if (!this._drag) return;
      const { shot, cx0, x } = this._drag;
      const p = this.plan, half = p.crop_w / 2;
      const cx = clamp(cx0 - (e.clientX - x) * (p.crop_w / c.clientWidth), half, p.source[0] - half);
      shot.keyframes = [[shot.start, cx], [shot.end, cx]];
      this._drag.cx = cx;
    });
    const end = () => {
      if (this._drag && this._drag.cx !== undefined && this.onCropChange) this.onCropChange(this._drag.shot.start, Math.round(this._drag.cx));
      this._drag = null;
    };
    c.addEventListener("pointerup", end);
    c.addEventListener("pointercancel", end);
  }
}
