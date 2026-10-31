/* Collection tab: create roll entries, and attach collected watch data to them.
 *
 * The server is the only clock that matters: it timestamps and numbers rolls.
 * This page polls one endpoint and re-renders from it, so several phones and
 * the laptop running `apex collect-rolls` all converge on the same view. A
 * re-render never happens under an open <select>, which would close it.
 */
"use strict";

const POLL_MS = 10_000;
const TICK_MS = 30_000;
const NEW = "__new__";

const state = {
  scope: "today",        // "today" | "all"
  data: null,            // last /api/collection payload
  dataKey: "",           // its JSON, so identical polls skip the re-render
  clockOffset: 0,        // server clock minus this device's clock, in ms
  picks: new Map(),      // file id -> roll id the user chose but has not assigned yet
  renderDeferred: false,
};

const $ = (id) => document.getElementById(id);

/** Build an element. Children are nodes or strings; never parsed as HTML. */
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  el.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return el;
}

// ── formatting ────────────────────────────────────────────────────────────

const serverNow = () => Date.now() + state.clockOffset;

const clock = (iso) =>
  new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

const dayLabel = (day, opts = { weekday: "long", month: "numeric", day: "numeric" }) =>
  new Date(`${day}T12:00:00`).toLocaleDateString([], opts);

/** "12 minutes ago" today; the date for anything older. */
function relTime(iso, day) {
  if (state.data && day !== state.data.today) {
    return dayLabel(day, { month: "numeric", day: "numeric", year: "2-digit" });
  }
  const m = Math.max(0, Math.floor((serverNow() - Date.parse(iso)) / 60_000));
  if (m < 1) return "just now";
  if (m === 1) return "1 minute ago";
  if (m < 60) return `${m} minutes ago`;
  return `${Math.floor(m / 60)} h ${m % 60} min ago`;
}

function duration(sec) {
  const s = Math.round(sec || 0);
  const mm = Math.floor(s / 60), ss = String(s % 60).padStart(2, "0");
  return mm >= 60 ? `${Math.floor(mm / 60)}:${String(mm % 60).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

const metres = (m) => `${Math.round(m).toLocaleString()} m`;

/** Fact spans for a watch file, with problems called out rather than hidden. */
function fileFacts(f, { withName = true } = {}) {
  const facts = [];
  if (f.parse_error) facts.push(h("span", { class: "bad" }, "unreadable (raw kept)"));
  else if (!f.point_count) facts.push(h("span", { class: "warn" }, "no GPS"));
  else {
    facts.push(
      h("span", { class: "num" }, duration(f.duration_sec)),
      h("span", { class: "num" }, metres(f.total_dist_m)),
      h("span", { class: "num" }, `${f.max_speed_mph.toFixed(1)} mph max`),
    );
  }
  if (f.device_id) {
    facts.push(h("span", {}, `${(f.device_name || "watch").replace(/^Garmin /, "")} `, h("span", { class: "num" }, `…${String(f.device_id).slice(-4)}`)));
  }
  if (withName) facts.push(h("span", { class: "num" }, f.filename));
  return facts;
}

/** "1:21 PM · 9 minutes ago", with only the clock time set as a figure. */
const when = (iso, rel) =>
  h("span", { class: "item-when" }, h("span", { class: "num" }, clock(iso)), ` · ${rel}`);

const rollLabel = (r) => `${r.buggy} roll ${r.number} (${relTime(r.created_utc, r.day)})`;

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

async function refresh() {
  const sync = $("sync");
  try {
    const sentAt = Date.now();
    const data = await api("GET", `/api/collection?all=${state.scope === "all"}`);
    state.clockOffset = Date.parse(data.server_time_utc) - (sentAt + Date.now()) / 2;
    sync.textContent = `Live · ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
    sync.classList.remove("bad");

    const { server_time_utc, ...stable } = data;
    const key = JSON.stringify(stable);
    if (key === state.dataKey) return;
    state.dataKey = key;
    state.data = data;
    render();
  } catch (err) {
    sync.textContent = `${err.message} · retrying`;
    sync.classList.add("bad");
  }
}

// ── name pickers ──────────────────────────────────────────────────────────

/** A roster <select> whose "+ New …" option opens an inline name field. That
    option sits at the top and is always present, roster or no roster, so a name
    nobody has entered yet can always be typed in. */
class NamePicker {
  constructor(kind, { noneLabel }) {
    this.kind = kind;
    this.noneLabel = noneLabel;
    this.select = $(`roll-${kind}`);
    this.row = $(`roll-${kind}-new`);
    this.input = $(`roll-${kind}-name`);
    this.roster = [];     // names from the server, most recently used first
    this.added = [];      // typed here, not on the server's roster until a roll uses them
    this.names = null;    // what the <select> currently lists; null until first built
    this.previous = "";

    this.select.addEventListener("change", () => {
      if (this.select.value === NEW) this.openNew();
      else this.previous = this.select.value;
      this.sync();
    });
    this.select.addEventListener("blur", flushDeferredRender);
    this.row.querySelector("[data-confirm]").addEventListener("click", () => this.confirmNew());
    this.row.querySelector("[data-cancel]").addEventListener("click", () => this.closeNew());
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); this.confirmNew(); }
      if (e.key === "Escape") this.closeNew();
    });
    this.rebuild();  // usable before the first fetch, and offline
  }

  setNames(roster) {
    const lower = new Set(roster.map((n) => n.toLowerCase()));
    this.added = this.added.filter((n) => !lower.has(n.toLowerCase()));
    this.roster = roster;
    this.rebuild();
  }

  rebuild() {
    const all = [...this.added, ...this.roster];
    if (this.names !== null && all.join("\n") === this.names.join("\n")) return;
    this.names = all;

    const keep = this.select.value;
    this.select.replaceChildren(
      h("option", { value: "" }, this.noneLabel),
      h("option", { value: NEW }, `+ New ${this.kind}…`),
      ...all.map((n) => h("option", { value: n }, n)),
    );
    this.select.value = all.includes(keep) || keep === NEW ? keep : "";
    this.sync();
  }

  openNew() {
    this.row.hidden = false;
    this.input.value = "";
    this.input.focus();
  }

  closeNew() {
    this.row.hidden = true;
    this.select.value = this.previous;
    this.sync();
  }

  confirmNew() {
    const name = this.input.value.trim().replace(/\s+/g, " ");
    if (!name) { this.input.focus(); return; }
    const existing = this.names.find((n) => n.toLowerCase() === name.toLowerCase());
    if (!existing) {
      this.added.unshift(name);
      this.rebuild();
    }
    this.set(existing || name);
  }

  /** The chosen name, or a name typed into the open "new" field. */
  get value() {
    if (!this.row.hidden) return this.input.value.trim().replace(/\s+/g, " ") || null;
    const v = this.select.value;
    return v && v !== NEW ? v : null;
  }

  set(name) {
    this.row.hidden = true;
    this.select.value = name && this.names.includes(name) ? name : "";
    this.previous = this.select.value;
    this.sync();
    this.select.dispatchEvent(new Event("picked"));
  }

  sync() {
    this.select.classList.toggle("empty", this.select.value === "");
  }

  get busy() {
    return document.activeElement === this.select;
  }
}

const buggyPicker = new NamePicker("buggy", { noneLabel: "Choose buggy…" });
const driverPicker = new NamePicker("driver", { noneLabel: "No driver" });

/** Picking a buggy fills in whoever last drove it, which is usually right. */
function suggestDriver() {
  const buggy = buggyPicker.value;
  if (!buggy || !state.data || !driverPicker.row.hidden) return;
  const last = state.data.rolls.find(
    (r) => r.buggy.toLowerCase() === buggy.toLowerCase() && r.driver,
  );
  if (last) driverPicker.set(last.driver);
}
buggyPicker.select.addEventListener("change", suggestDriver);
buggyPicker.select.addEventListener("picked", suggestDriver);

// ── new roll ──────────────────────────────────────────────────────────────

$("roll-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const status = $("roll-status");
  const submit = $("roll-submit");
  const buggy = buggyPicker.value;
  if (!buggy) {
    status.textContent = "Choose a buggy first.";
    status.className = "status bad";
    buggyPicker.select.focus();
    return;
  }

  submit.disabled = true;
  status.textContent = "Saving…";
  status.className = "status";
  try {
    const roll = await api("POST", "/api/rolls", {
      buggy,
      driver: driverPicker.value,
      notes: $("roll-notes").value,
    });
    status.textContent = `Created ${roll.title}`;
    status.className = "status ok";
    buggyPicker.set("");
    driverPicker.set("");
    $("roll-notes").value = "";
    state.dataKey = "";
    await refresh();
  } catch (err) {
    status.textContent = `Not saved: ${err.message}.`;
    status.className = "status bad";
  } finally {
    submit.disabled = false;
  }
});

// ── assigning data ────────────────────────────────────────────────────────

/** The roll a file most likely belongs to: same buggy label, nearest in time,
    preferring rolls that have no data yet. Only a guess; nothing is sent
    until Assign is pressed. */
function suggestRoll(file, rolls) {
  if (!file.buggy) return "";
  const at = Date.parse(file.start_utc || file.uploaded_utc);
  const matches = rolls
    .filter((r) => r.buggy.toLowerCase() === file.buggy.toLowerCase() && r.day === file.day)
    .sort((a, b) =>
      (a.file_ids.length > 0) - (b.file_ids.length > 0) ||
      Math.abs(Date.parse(a.created_utc) - at) - Math.abs(Date.parse(b.created_utc) - at));
  return matches.length ? String(matches[0].id) : "";
}

function fileItem(file, rolls) {
  const status = h("span", { class: "status", role: "status" });
  const select = h("select", { class: "well", "aria-label": `Roll for ${file.filename}` },
    h("option", { value: "" }, rolls.length ? "Choose roll…" : "No rolls to assign to"),
    rolls.map((r) => h("option", { value: r.id },
      rollLabel(r) + (r.file_ids.length ? " · has data" : ""))),
  );
  select.value = state.picks.get(file.id) ?? suggestRoll(file, rolls);
  if (!select.value) select.value = "";
  select.classList.toggle("empty", !select.value);

  const button = h("button", { class: "btn-primary", type: "button", disabled: !select.value }, "Assign");
  select.addEventListener("change", () => {
    state.picks.set(file.id, select.value);
    select.classList.toggle("empty", !select.value);
    button.disabled = !select.value;
  });
  select.addEventListener("blur", flushDeferredRender);

  button.addEventListener("click", async () => {
    button.disabled = true;
    status.textContent = "Assigning…";
    status.className = "status";
    try {
      await api("PUT", `/api/files/${file.id}/roll`, { roll_id: Number(select.value) });
      state.picks.delete(file.id);
    } catch (err) {
      status.textContent = err.status === 409 ? "Already assigned by someone else." : err.message;
      status.className = "status bad";
    }
    state.dataKey = "";
    await refresh();
  });

  return h("li", { class: "item assign-row" },
    h("div", { class: "item-head" },
      h("span", { class: file.buggy ? "item-title" : "item-title faint" }, file.buggy || "No buggy label"),
      file.start_utc
        ? when(file.start_utc, relTime(file.start_utc, file.day))
        : h("span", { class: "item-when" }, "uploaded ", h("span", { class: "num" }, clock(file.uploaded_utc))),
    ),
    h("div", { class: "facts" }, fileFacts(file)),
    h("div", { class: "choice" }, h("span", { class: "select-wrap" }, select), button),
    status,
  );
}

function rollItem(roll, filesById) {
  const files = roll.file_ids.map((id) => filesById.get(id)).filter(Boolean);
  return h("li", { class: "item" },
    h("div", { class: "item-head" },
      h("span", { class: "item-title" }, roll.title),
      when(roll.created_utc, relTime(roll.created_utc, roll.day)),
    ),
    h("div", { class: "facts" }, h("span", {}, roll.driver ? `Driver: ${roll.driver}` : "No driver")),
    roll.notes ? h("p", { class: "notes" }, roll.notes) : null,
    files.length
      ? h("ul", { class: "attached" }, files.map((f) => h("li", {},
          h("div", { class: "facts" },
            f.start_utc ? h("span", { class: "num" }, clock(f.start_utc)) : null,
            fileFacts(f)),
          h("button", {
            class: "btn-quiet small", type: "button",
            onclick: async (e) => {
              e.currentTarget.disabled = true;
              try { await api("DELETE", `/api/files/${f.id}/roll`); }
              catch (err) { $("sync").textContent = err.message; }
              state.dataKey = "";
              await refresh();
            },
          }, "Unassign"))))
      : h("p", { class: "no-data" }, "No data assigned yet"),
  );
}

// ── render ────────────────────────────────────────────────────────────────

/** True while the user has a list <select> focused; replacing it would close it. */
function listBusy() {
  const el = document.activeElement;
  return el?.tagName === "SELECT" && ($("file-list").contains(el) || $("roll-list").contains(el));
}

function flushDeferredRender() {
  if (state.renderDeferred) setTimeout(render, 0);
}

function render() {
  const data = state.data;
  if (!data) return;
  if (listBusy() || buggyPicker.busy || driverPicker.busy) {
    state.renderDeferred = true;
    return;
  }
  state.renderDeferred = false;

  $("scope-day").textContent = state.scope === "all" ? "All days" : dayLabel(data.today);
  buggyPicker.setNames(data.roster.buggy);
  driverPicker.setNames(data.roster.driver);

  const unassigned = data.files.filter((f) => f.roll_id === null);
  for (const id of state.picks.keys()) {
    if (!unassigned.some((f) => f.id === id)) state.picks.delete(id);
  }
  $("file-list").replaceChildren(...unassigned.map((f) => fileItem(f, data.rolls)));
  $("file-empty").hidden = unassigned.length > 0;
  const meta = $("assign-meta");
  meta.textContent = unassigned.length ? `${unassigned.length} file${unassigned.length > 1 ? "s" : ""} to assign` : "";
  meta.classList.toggle("warn", unassigned.length > 0);

  const filesById = new Map(data.files.map((f) => [f.id, f]));
  $("roll-list").replaceChildren(...data.rolls.map((r) => rollItem(r, filesById)));
  $("roll-empty").hidden = data.rolls.length > 0;
  const missing = data.rolls.filter((r) => !r.file_ids.length).length;
  $("rolls-meta").textContent = data.rolls.length
    ? `${data.rolls.length} logged${missing ? ` · ${missing} without data` : ""}`
    : "";
}

// ── chrome ────────────────────────────────────────────────────────────────

document.querySelectorAll("[data-scope]").forEach((btn) =>
  btn.addEventListener("click", () => {
    state.scope = btn.dataset.scope;
    document.querySelectorAll("[data-scope]").forEach((b) =>
      b.setAttribute("aria-pressed", String(b === btn)));
    state.dataKey = "";
    refresh();
  }));

$("theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem("apex-theme", root.dataset.theme); } catch {}
});

/** Show the panel named by the hash; tabs other than Collection come later. */
function showTab() {
  const name = location.hash.slice(1) || "collection";
  const known = document.querySelector(`[data-panel="${CSS.escape(name)}"]`) ? name : "collection";
  document.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== known; });
  document.querySelectorAll("[data-tab]").forEach((t) =>
    t.dataset.tab === known ? t.setAttribute("aria-current", "page") : t.removeAttribute("aria-current"));
}
window.addEventListener("hashchange", showTab);
showTab();

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});
setInterval(() => { if (document.visibilityState === "visible") refresh(); }, POLL_MS);
setInterval(render, TICK_MS);  // keeps "n minutes ago" current
refresh();
