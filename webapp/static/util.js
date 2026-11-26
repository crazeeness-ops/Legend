// Small DOM / API helpers shared by the views.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") {
      for (const [prop, val] of Object.entries(v)) {
        if (prop.startsWith("--")) el.style.setProperty(prop, val); else el.style[prop] = val;
      }
    }
    else if (k.startsWith("on")) el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "value") el.value = v; // property, not attribute (textarea/select need it)
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  if (tag === "img") el.addEventListener("error", () => { el.style.visibility = "hidden"; }); // no broken-image icons
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: { "X-Requested-With": "legend" } };
  if (form) opts.body = form;
  else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, opts);
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    location.hash = "#/login";
    throw new Error("로그인이 필요합니다");
  }
  if (!res.ok) throw new Error((data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || res.statusText);
  return data;
}

export function toast(msg, isError = false) {
  const el = h("div", { class: "toast" + (isError ? " err" : "") }, msg);
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), isError ? 6000 : 2800);
}

export function fmt(t, decimals = 1) {
  if (!isFinite(t)) t = 0;
  const neg = t < 0;
  t = Math.abs(t);
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${neg ? "-" : ""}${m}:${s.toFixed(decimals).padStart(decimals ? 3 + decimals : 2, "0")}`;
}

export function debounce(fn, ms) {
  let timer;
  const d = (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
  d.flush = (...args) => { clearTimeout(timer); return fn(...args); };
  return d;
}

export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// multipart upload with progress (fetch can't report upload progress)
export function upload(url, form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("X-Requested-With", "legend");
    xhr.upload.onprogress = e => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch { /* not json */ }
      if (xhr.status >= 400) reject(new Error(typeof data.detail === "string" ? data.detail : "업로드 실패"));
      else resolve(data);
    };
    xhr.onerror = () => reject(new Error("네트워크 오류"));
    xhr.send(form);
  });
}

export const won = n => `${Math.round(n || 0).toLocaleString("ko-KR")}원`;
export const num = n => (n >= 10000 ? `${(n / 10000).toFixed(n >= 100000 ? 0 : 1)}만` : Number(n || 0).toLocaleString("ko-KR"));

export function modal(title, body, onClose) {
  const close = () => { overlay.remove(); onClose?.(); };
  const overlay = h("div", { class: "modal-bg", onclick: e => { if (e.target === overlay) close(); } },
    h("div", { class: "modal" }, h("div", { class: "row" }, h("h2", { class: "grow" }, title), h("button", { class: "ghost small", onclick: close }, "닫기")), body));
  document.body.append(overlay);
  return close;
}
