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
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {} };
  if (form) opts.body = form;
  else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, opts);
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
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
  d.flush = (...args) => { clearTimeout(timer); fn(...args); };
  return d;
}

export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
