/* Shared by every tab: DOM building, the API client, formatting, the theme
 * toggle, and the hash router that shows one tab at a time.
 *
 * Routes are `#<tab>[/<path>...][?<query>]`, e.g. `#collection`,
 * `#doomscroll?buggy=Solaris`, `#doomscroll/roll/42`. A tab registers a
 * handler with onRoute(); it runs whenever the hash lands on that tab.
 */
"use strict";

const $ = (id) => document.getElementById(id);

function build(el, attrs, children) {
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  el.append(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  return el;
}

/** Build an element. Children are nodes or strings; never parsed as HTML. */
const h = (tag, attrs = {}, ...children) => build(document.createElement(tag), attrs, children);

/** Replace an element's children, skipping null/false the way h() does
    (the native replaceChildren would print them as text). */
const fill = (el, ...children) =>
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));

/** The same, for SVG elements. */
const svg = (tag, attrs = {}, ...children) =>
  build(document.createElementNS("http://www.w3.org/2000/svg", tag), attrs, children);

// ── formatting ────────────────────────────────────────────────────────────

const clock = (iso) =>
  new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

const dayLabel = (day, opts = { weekday: "long", month: "numeric", day: "numeric" }) =>
  new Date(`${day}T12:00:00`).toLocaleDateString([], opts);

const shortDay = (day) => dayLabel(day, { weekday: "short", month: "numeric", day: "numeric" });

function duration(sec) {
  const s = Math.round(sec || 0);
  const mm = Math.floor(s / 60), ss = String(s % 60).padStart(2, "0");
  return mm >= 60 ? `${Math.floor(mm / 60)}:${String(mm % 60).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

const metres = (m) => `${Math.round(m).toLocaleString()} m`;

/** Roll and segment times: seconds to a tenth. */
const secs = (s) => `${s.toFixed(1)} s`;

/** A signed gap, "+0.42 s" / "−0.31 s"; positive means slower. */
const gapText = (g, digits = 2) => `${g > 0 ? "+" : g < 0 ? "−" : "±"}${Math.abs(g).toFixed(digits)} s`;

/** Class for a gap: slower reads as bad, faster as ok. The sign carries it too. */
const gapClass = (g) => (g > 0.005 ? "slower" : g < -0.005 ? "faster" : "even");

const mph = (v) => `${v.toFixed(1)} mph`;

/** A number with a true minus sign for negatives. */
const signed = (v, digits = 0) => `${v < 0 ? "−" : ""}${Math.abs(v).toFixed(digits)}`;

// ── server ────────────────────────────────────────────────────────────────

async function api(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      cache: "no-store",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error("Couldn't reach the server");
  }
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("Signed out");
  }
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((d) => d.msg).join("; ")
      : payload.detail;
    const err = new Error(detail || `Server error ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return payload;
}

// ── theme ─────────────────────────────────────────────────────────────────

const isDark = () => document.documentElement.dataset.theme === "dark";

/** The value of a CSS custom property, for drawing on canvases. */
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

$("theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  root.dataset.theme = isDark() ? "light" : "dark";
  try { localStorage.setItem("apex-theme", root.dataset.theme); } catch {}
  window.dispatchEvent(new Event("themechange"));
});

// ── routing ───────────────────────────────────────────────────────────────

const routeHandlers = {};

function currentRoute() {
  const raw = location.hash.slice(1);
  const [pathPart, query = ""] = raw.split("?");
  const [tab, ...path] = pathPart.split("/").filter(Boolean);
  const known = tab && document.querySelector(`[data-panel="${CSS.escape(tab)}"]`);
  return { tab: known ? tab : "collection", path: known ? path : [], params: new URLSearchParams(query) };
}

/** Run `handler(route)` whenever the hash lands on `tab`. */
function onRoute(tab, handler) {
  routeHandlers[tab] = handler;
}

function route() {
  const r = currentRoute();
  document.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== r.tab; });
  document.querySelectorAll("[data-tab]").forEach((t) =>
    t.dataset.tab === r.tab ? t.setAttribute("aria-current", "page") : t.removeAttribute("aria-current"));
  routeHandlers[r.tab]?.(r);
}

window.addEventListener("hashchange", route);
// Every tab script has run by the time the document finishes parsing.
document.addEventListener("DOMContentLoaded", route);
