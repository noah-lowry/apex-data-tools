/* doomscROLL: a feed of analysed rolls, and a page per roll.
 *
 * #doomscroll[?buggy=&driver=&day=&all=1]   the feed (cards + leaderboard)
 * #doomscroll/roll/<id>                       one roll, in depth
 *
 * Everything is read-only and computed by the server (apex.analysis); this
 * file only lays it out. Comparing two rolls is done here: traces are aligned
 * segment by segment, so both rolls pass every gate at the same x and the
 * time-gap trace is exact at each gate.
 *
 * Colour: segments use the course's own colours (validated as a categorical
 * set; one dark-mode step below), speed on the map is a single-hue sequential
 * ramp, "this roll" is primary ink and the comparison is dashed secondary ink.
 */
"use strict";

// Dark-mode steps for course colours that fall outside the dark lightness band
// (checked with the dataviz palette validator against the dark surface).
const DARK_STEPS = { "#10b981": "#199e70" };
// Slow → fast. Validated as ordinal ramps against each theme's surface.
const SPEED_RAMP = {
  light: ["#e8935c", "#d9733a", "#c25824", "#9c4218", "#6c2b0f"],
  dark: ["#8a3a17", "#b4501f", "#d9692c", "#ef9056", "#f6b98c"],
};
const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

const doom = {
  course: null,       // /api/course, fetched once
  feedKey: null,      // the filter query the cards on screen were loaded for
  cursor: 0,
  nextCursor: null,
  loading: false,
  feedScroll: 0,      // where the feed was when a roll was opened
  page: null,         // { data, ref, refId, segment } for the open roll page
  cleanup: [],        // charts, maps and observers to dispose of when the page goes
};

const segColor = (hex) => (isDark() ? DARK_STEPS[hex.toLowerCase()] || hex : hex);
const segSpec = (id) => doom.course.segments.find((s) => s.id === id);
const segName = (id) => segSpec(id)?.name || id;

async function ensureCourse() {
  doom.course ??= await api("GET", "/api/course");
  return doom.course;
}

onRoute("doomscroll", async (r) => {
  try {
    await ensureCourse();
  } catch (err) {
    fill($("doom-cards"), h("p", { class: "empty-note" }, err.message));
    return;
  }
  if (r.path[0] === "roll" && r.path[1]) openRoll(Number(r.path[1]));
  else showFeed(r.params);
});

// ── feed ──────────────────────────────────────────────────────────────────

function feedQuery(params) {
  const q = new URLSearchParams();
  for (const k of ["buggy", "driver", "day"]) if (params.get(k)) q.set(k, params.get(k));
  if (params.get("all") === "1") q.set("all", "1");
  return q.toString();
}

function showFeed(params) {
  disposePage();
  $("doom-roll").hidden = true;
  $("doom-feed").hidden = false;
  const key = feedQuery(params);
  if (key === doom.feedKey) {
    // Coming back from a roll: the cards are still here; return to the same spot.
    requestAnimationFrame(() => window.scrollTo(0, doom.feedScroll));
    return;
  }
  doom.feedKey = key;
  doom.cursor = 0;
  doom.nextCursor = null;
  fill($("doom-cards"), );
  fill($("board"), );
  loadFeed();
}

async function loadFeed() {
  if (doom.loading) return;
  doom.loading = true;
  const key = doom.feedKey;
  const q = new URLSearchParams(key);
  if (q.get("all")) q.set("all", "true");
  q.set("cursor", doom.cursor);
  try {
    const data = await api("GET", `/api/feed?${q}`);
    if (key !== doom.feedKey) return;  // filters changed while this was loading
    if (doom.cursor === 0) {
      renderFilters(data.options, new URLSearchParams(key));
      renderBoard(data.leaderboard);
      $("doom-empty").hidden = data.total > 0;
    }
    $("doom-cards").append(...data.rolls.map(rollCard));
    doom.nextCursor = data.next_cursor;
    doom.cursor = data.next_cursor ?? doom.cursor;
    $("doom-more").hidden = data.next_cursor === null;
  } catch (err) {
    $("doom-cards").append(h("p", { class: "empty-note" }, `Couldn't load rolls: ${err.message}`));
  } finally {
    doom.loading = false;
  }
}

// Keep loading while the bottom of the feed is in view; the button is the fallback.
new IntersectionObserver((entries) => {
  if (entries.some((e) => e.isIntersecting) && doom.nextCursor !== null && !$("doom-feed").hidden) loadFeed();
}, { rootMargin: "600px" }).observe($("doom-more"));
$("doom-more").querySelector("button").addEventListener("click", loadFeed);

function renderFilters(options, current) {
  const form = $("doom-filters");
  const fill = (name, label, values, fmt = (v) => v) => {
    const select = form.elements[name];
    select.replaceChildren(
      h("option", { value: "" }, label),
      ...values.map((v) => h("option", { value: v }, fmt(v))),
    );
    select.value = current.get(name) || "";
    select.classList.toggle("empty", !select.value);
  };
  fill("buggy", "All buggies", options.buggies);
  fill("driver", "All drivers", options.drivers);
  fill("day", "All days", options.days, (d) => dayLabel(d, { weekday: "short", month: "short", day: "numeric", year: "numeric" }));
  form.elements.all.checked = current.get("all") === "1";
}

$("doom-filters").addEventListener("change", () => {
  const form = $("doom-filters");
  const q = new URLSearchParams();
  for (const k of ["buggy", "driver", "day"]) if (form.elements[k].value) q.set(k, form.elements[k].value);
  if (form.elements.all.checked) q.set("all", "1");
  location.hash = q.toString() ? `doomscroll?${q}` : "doomscroll";
});

// ── cards ─────────────────────────────────────────────────────────────────

const BADGES = { pb: "PB", fastest_day: "Fastest of the day", partial: "Partial", stopped: "Stopped" };

function rollCard(c) {
  const meta = [c.driver || "No driver", shortDay(c.day)].join(" · ");
  if (!c.has_data) {
    return h("a", { class: "card card-empty", href: `#doomscroll/roll/${c.id}` },
      h("span", { class: "card-title" }, c.title),
      h("span", { class: "card-meta" }, `${meta} · no data assigned`));
  }
  const gap = c.gap_sec !== null
    ? h("span", { class: `card-gap ${gapClass(c.gap_sec)}` }, gapText(c.gap_sec, 1))
    : null;
  return h("a", { class: "card", href: `#doomscroll/roll/${c.id}`, "aria-label": `${c.title}, ${secs(c.total_sec)}` },
    h("div", { class: "card-head" },
      h("div", { class: "card-name" },
        h("span", { class: "card-title" }, c.title),
        h("span", { class: "card-meta" }, meta)),
      h("div", { class: "card-total" },
        h("span", { class: "card-time num" }, c.finished ? secs(c.total_sec) : `${secs(c.total_sec)}*`),
        gap)),
    c.badges.length
      ? h("div", { class: "badges" }, c.badges.map((b) => h("span", { class: `badge badge-${b}` },
          b === "stopped" ? `Stopped ${Math.round(c.stopped_sec)} s` : BADGES[b])))
      : null,
    sparkline(c),
    splitBar(c),
    h("div", { class: "card-foot" },
      h("span", { class: "num" }, `${mph(c.max_mph)} top`),
      c.hill3_mph !== null ? h("span", { class: "num" }, `${mph(c.hill3_mph)} into Hill 3`) : h("span", { class: "warn" }, `lost after ${c.end_gate}`),
      c.reference ? h("span", {}, `vs ${c.reference.title.replace(/ \(.*\)$/, "")}`) : null),
    c.notes ? h("p", { class: "card-note" }, notePreview(c.notes)) : null,
  );
}

/** The notes on one line: headings like "Segment notes:" dropped, bullets joined. */
const notePreview = (notes) => notes.split("\n")
  .map((l) => l.trim().replace(/^- /, ""))
  .filter((l) => l && !/^segment notes:$/i.test(l))
  .join(" · ");

/** Speed over distance, coloured by segment. Fixed 0–35 mph so cards compare. */
function sparkline(c) {
  const W = 300, H = 56, top = Math.max(35, ...c.spark.map((p) => p[1]));
  const dMax = c.spark[c.spark.length - 1][0] || 1;
  const x = (d) => (d / dMax) * W;
  const y = (v) => H - 2 - (v / top) * (H - 6);
  const paths = c.segments.filter((s) => s.status === "ok").map((s) => {
    const pts = c.spark.filter(([d]) => d >= s.d_start - 0.05 && d <= s.d_end + 0.05);
    return svg("polyline", {
      points: pts.map(([d, v]) => `${x(d).toFixed(1)},${y(v).toFixed(1)}`).join(" "),
      stroke: segColor(segSpec(s.id).color),
      fill: "none", "stroke-width": 2, "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke",
    });
  });
  return svg("svg", { class: "spark", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" },
    svg("line", { x1: 0, x2: W, y1: y(0), y2: y(0), class: "spark-base", "vector-effect": "non-scaling-stroke" }),
    paths);
}

/** One cell per segment, as wide as its share of the roll, with its time and gap. */
function splitBar(c) {
  return h("div", { class: "splitbar" }, c.segments.map((s) => {
    const spec = segSpec(s.id);
    if (s.status !== "ok") {
      return h("div", { class: "split-cell missed", style: "flex-grow: 1", title: `${spec.name}: not measured` },
        h("i", {}), h("span", { class: "num" }, "—"));
    }
    return h("div", { class: "split-cell", style: `flex-grow: ${s.time_sec}`, title: spec.name },
      h("i", { style: `background: ${segColor(spec.color)}` }),
      h("span", { class: "num" }, s.time_sec.toFixed(1)),
      s.gap_sec !== null ? h("span", { class: `num ${gapClass(s.gap_sec)}` }, gapText(s.gap_sec, 1).replace(" s", "")) : null);
  }));
}

// ── leaderboard ───────────────────────────────────────────────────────────

const rollLink = (r, text) => h("a", { href: `#doomscroll/roll/${r.id}` }, text ?? r.title);

function renderBoard(board) {
  $("board-meta").textContent = board.fastest.length ? `fastest: ${secs(board.fastest[0].total_sec)}` : "";
  const panel = (title, ...body) => h("section", { class: "board-panel" }, h("h3", {}, title), ...body);
  const empty = h("p", { class: "empty-note" }, "No complete rolls yet.");

  fill($("board"), 
    panel("Fastest rolls", board.fastest.length
      ? h("ol", { class: "board-list" }, board.fastest.map((r) => h("li", {},
          rollLink(r), h("span", { class: "dim" }, r.driver || ""), h("span", { class: "num" }, secs(r.total_sec)))))
      : empty),
    panel("Segment records", h("ul", { class: "board-list" }, board.segments.map((s) => h("li", {},
      h("span", {}, h("i", { class: "swatch", style: `background: ${segColor(segSpec(s.id).color)}` }), segName(s.id)),
      s.best ? rollLink(s.best, `${s.best.buggy} · ${s.best.driver || "—"}`) : h("span", { class: "dim" }, "—"),
      h("span", { class: "num" }, s.best ? secs(s.best.time_sec) : ""))))),
    panel("By buggy", board.buggies.length
      ? h("div", { class: "scroller" }, h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "Buggy"), h("th", { class: "n" }, "Best"),
            h("th", { class: "n" }, "Median"), h("th", { class: "n" }, "Rolls"))),
          h("tbody", {}, board.buggies.map((b) => h("tr", {},
            h("td", {}, b.buggy),
            h("td", { class: "n" }, b.best ? rollLink(b.best, secs(b.best.total_sec)) : "—"),
            h("td", { class: "n" }, b.median_sec ? secs(b.median_sec) : "—"),
            h("td", { class: "n" }, b.rolls))))))
      : empty),
    panel("Today", board.today
      ? h("p", { class: "board-today" }, rollLink(board.today),
          h("span", { class: "num big-num" }, secs(board.today.total_sec)),
          h("span", { class: "dim" }, board.today.driver || ""))
      : h("p", { class: "empty-note" }, "No complete rolls today yet.")),
  );
}

// ── roll page ─────────────────────────────────────────────────────────────

function disposePage() {
  for (const fn of [...(doom.segCleanup?.splice(0) ?? []), ...doom.cleanup.splice(0)]) fn();
  if (doom.page) doom.page.marker = null;
}

async function openRoll(id) {
  if (!$("doom-feed").hidden) doom.feedScroll = window.scrollY;
  $("doom-feed").hidden = true;
  const view = $("doom-roll");
  view.hidden = false;
  disposePage();
  fill(view, backLink(), h("p", { class: "empty-note" }, "Loading…"));
  window.scrollTo(0, 0);
  try {
    const data = await api("GET", `/api/rolls/${id}/analysis`);
    const refId = data.reference?.id ?? null;
    const ref = refId ? await api("GET", `/api/rolls/${refId}/analysis`) : null;
    doom.page = { data, ref, refId, segment: doom.course.segments[2].id };  // the Chute first: where rolls differ most
    renderRoll();
  } catch (err) {
    fill(view, backLink(), h("p", { class: "empty-note" }, err.status === 404 ? "No such roll." : err.message));
  }
}

function backLink() {
  return h("a", { class: "back", href: doom.feedKey ? `#doomscroll?${doom.feedKey}` : "#doomscroll" }, "← All rolls");
}

async function setReference(refId) {
  const page = doom.page;
  page.refId = refId;
  page.ref = refId ? await api("GET", `/api/rolls/${refId}/analysis`) : null;
  const y = window.scrollY;
  renderRoll();
  window.scrollTo(0, y);
}

function renderRoll() {
  disposePage();
  const { data, ref } = doom.page;
  const roll = data.roll;
  const view = $("doom-roll");
  if (!data.analysis) {
    fill(view, backLink(), rollTitle(roll),
      h("p", { class: "empty-note" }, "No data is assigned to this roll, so there is nothing to analyse yet."));
    return;
  }
  const an = data.analysis;
  const refAn = ref?.analysis ?? null;
  const cmp = refAn ? { an: refAn, roll: ref.roll } : null;

  fill(view, 
    backLink(),
    rollTitle(roll),
    scoreboard(data, cmp),
    roll.notes ? block("Notes", null, h("p", { class: "notes roll-notes" }, roll.notes)) : null,
    compareBar(data),
    block("Splits", cmp ? `vs ${cmp.roll.title}` : "", splitsTable(data, cmp)),
    block("Speed", "mph over distance, aligned at every gate", chartBox("speed")),
    cmp ? block("Time gap", "where time was won and lost", chartBox("gap"),
      h("p", { class: "footnote" }, `Seconds behind ${cmp.roll.title} at each point. Rising means losing time there; falling means gaining it. Exact at every gate.`)) : null,
    block("Map", "coloured by speed", h("div", { class: "map", id: "roll-map" }), speedLegend(an), mapNote(cmp)),
    block("Segment", "pick one to dig in", segmentPicker(), h("div", { id: "seg-detail" })),
    block("Data quality", null, dataQuality(data)),
  );

  const axis = cmp ? cmp.an : an;
  drawSpeedChart(an, cmp, axis);
  if (cmp) drawGapChart(an, cmp.an);
  drawMap($("roll-map"), an, cmp?.an, null);
  renderSegment();
}

function rollTitle(roll) {
  return h("header", { class: "roll-title" },
    h("p", { class: "eyebrow" }, [roll.buggy, roll.driver || "No driver", dayLabel(roll.day), clock(roll.at_utc)].join(" · ")),
    h("h1", {}, roll.title));
}

function block(title, meta, ...content) {
  return h("section", { class: "block" },
    h("header", { class: "block-head" }, h("h2", {}, title), meta ? h("span", { class: "block-meta" }, meta) : null),
    ...content);
}

function scoreboard(data, cmp) {
  const an = data.analysis, t = data.stats.total;
  const stat = (label, value) => h("div", {}, h("dt", {}, label), h("dd", { class: "num" }, value));
  const ranks = [];
  if (t.rank) ranks.push(`#${t.rank} of ${t.n} ${data.roll.buggy} rolls`, `#${t.rank_day} of ${t.n_day} that day`);
  else if (!an.complete) ranks.push(an.finished ? "not ranked: a segment wasn't measured" : `partial: GPS lost after ${an.end_gate}`);
  if (an.stopped_sec >= 3) ranks.push(`stopped for ${Math.round(an.stopped_sec)} s on the course`);
  return h("section", { class: "scoreboard" },
    h("div", { class: "hero" },
      h("span", { class: "hero-time num" }, an.total_sec.toFixed(1), h("span", { class: "unit" }, "s")),
      cmp && an.complete && cmp.an.complete
        ? h("span", { class: `hero-gap ${gapClass(an.total_sec - cmp.an.total_sec)}` },
            `${gapText(an.total_sec - cmp.an.total_sec)} vs ${data.reference && cmp.roll.id === data.reference.id ? data.reference.label : "comparison"}`)
        : null,
      h("span", { class: "hero-ranks" }, ranks.join(" · "))),
    h("dl", { class: "stats" },
      stat("Average", mph(an.avg_mph)),
      stat("Top speed", mph(an.max_mph)),
      stat("Into Hill 3", an.hill3_mph !== null ? mph(an.hill3_mph) : "—"),
      stat("Distance", metres(an.dist_m)),
      stat("Buggy best", t.best_sec ? secs(t.best_sec) : "—")));
}

function compareBar(data) {
  const select = h("select", { class: "well", id: "compare-select" },
    h("option", { value: "" }, "No comparison"),
    data.candidates.map((c) => h("option", { value: c.id },
      `${c.title} · ${c.driver || "no driver"} · ${secs(c.total_sec)}${c.complete ? "" : " · partial"}${data.reference?.id === c.id ? ` (${data.reference.label})` : ""}`)));
  select.value = doom.page.refId ?? "";
  select.addEventListener("change", () => setReference(select.value ? Number(select.value) : null));
  return block("Compare with", data.candidates.length ? `${data.candidates.length} other ${data.roll.buggy} rolls` : "no other rolls for this buggy",
    h("span", { class: "select-wrap compare" }, select));
}

function splitsTable(data, cmp) {
  const an = data.analysis;
  const cell = (v, fmt, cls = "n") => h("td", { class: v === null || v === undefined ? `${cls} absent` : cls }, v === null || v === undefined ? "—" : fmt(v));
  const gapCell = (g) => g === null ? h("td", { class: "n absent" }, "—") : h("td", { class: `n ${gapClass(g)}` }, gapText(g));
  const rows = an.segments.map((s) => {
    const spec = segSpec(s.id), st = data.stats.segments[s.id];
    const r = cmp && cmp.an.segments.find((x) => x.id === s.id);
    const name = h("td", {}, h("button", { class: "seg-link", type: "button", onclick: () => pickSegment(s.id, true) },
      h("i", { class: "swatch", style: `background: ${segColor(spec.color)}` }), spec.name,
      spec.bad_gps ? h("span", { class: "tag", title: "GPS is known to be unreliable here" }, "bad GPS") : null));
    if (s.status !== "ok") return h("tr", { class: "missed" }, name, h("td", { class: "n absent", colspan: 9 }, "not measured"));
    return h("tr", {}, name,
      cell(s.time_sec, (v) => v.toFixed(2)),
      gapCell(r && r.status === "ok" ? s.time_sec - r.time_sec : null),
      cell(st.rank, (v) => `${v} / ${st.n}`),
      cell(s.entry_mph, (v) => v.toFixed(1)),
      cell(s.exit_mph, (v) => v.toFixed(1)),
      cell(s.min_mph, (v) => v.toFixed(1)),
      cell(s.max_mph, (v) => v.toFixed(1)),
      h("td", { class: `n ${s.delta_mph >= 0 ? "" : "dim"}` }, `${s.delta_mph >= 0 ? "+" : "−"}${Math.abs(s.delta_mph).toFixed(1)}`),
      cell(s.turn_deg, (v) => `${signed(v)}°`));
  });
  const total = h("tr", { class: "total" }, h("td", {}, "Whole roll"),
    h("td", { class: "n" }, an.total_sec.toFixed(2)),
    gapCell(cmp && an.complete && cmp.an.complete ? an.total_sec - cmp.an.total_sec : null),
    cell(data.stats.total.rank, (v) => `${v} / ${data.stats.total.n}`),
    cell(an.segments[0].entry_mph, (v) => v.toFixed(1)),
    cell(an.hill3_mph, (v) => v.toFixed(1)),
    h("td", { class: "n absent" }, ""), cell(an.max_mph, (v) => v.toFixed(1)), h("td", { class: "n absent" }, ""), h("td", { class: "n absent" }, ""));
  return h("div", { class: "scroller" }, h("table", { class: "splits-table" },
    h("thead", {}, h("tr", {},
      h("th", {}, "Segment"), h("th", { class: "n" }, "Time s"), h("th", { class: "n" }, cmp ? "vs comparison" : "vs —"),
      h("th", { class: "n", title: "Rank among this buggy's rolls" }, "Rank"),
      h("th", { class: "n" }, "Entry mph"), h("th", { class: "n" }, "Exit mph"), h("th", { class: "n" }, "Min"),
      h("th", { class: "n" }, "Max"), h("th", { class: "n", title: "Exit minus entry speed" }, "Δ mph"),
      h("th", { class: "n", title: "Change in heading" }, "Turn"))),
    h("tbody", {}, rows, total)));
}

// ── alignment ─────────────────────────────────────────────────────────────

/** x for each trace point on the axis roll's distance scale: within each
    segment, the point's fraction of the way through maps onto the axis roll's
    segment. Gates therefore land on the same x for both rolls. */
function alignX(an, axis) {
  return an.trace.t.map((t, i) => {
    const d = an.trace.d[i];
    const k = an.segments.findIndex((s) => s.status === "ok" && t <= s.t_end + 1e-6);
    const s = an.segments[k], a = axis.segments[k];
    if (!s || !a || a.status !== "ok") return d;
    const span = s.d_end - s.d_start;
    return a.d_start + (span > 0 ? (d - s.d_start) / span : 0) * (a.d_end - a.d_start);
  });
}

/** Linear interpolation of ys over increasing xs, null outside the range. */
function interp(xs, ys, x) {
  if (x < xs[0] - 1e-9 || x > xs[xs.length - 1] + 1e-9) return null;
  let lo = 0, hi = xs.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] <= x) lo = mid; else hi = mid;
  }
  const span = xs[hi] - xs[lo];
  return span > 0 ? ys[lo] + ((x - xs[lo]) / span) * (ys[hi] - ys[lo]) : ys[lo];
}

/** Two series on their own x values, merged onto one sorted x for uPlot. */
function mergeSeries(a, b) {
  const xs = [...new Set([...a.x, ...(b ? b.x : [])])].sort((p, q) => p - q);
  const ia = new Map(a.x.map((x, i) => [x, a.y[i]]));
  const ib = b ? new Map(b.x.map((x, i) => [x, b.y[i]])) : null;
  const out = [xs, xs.map((x) => ia.get(x) ?? null)];
  if (ib) out.push(xs.map((x) => ib.get(x) ?? null));
  return out;
}

// ── charts ────────────────────────────────────────────────────────────────

function chartBox(name) {
  return h("div", { class: "chart", id: `chart-${name}` });
}

/** Shared uPlot options: recessive grid and axes in theme ink, synced cursors. */
function baseOpts(el, height, { xLabel, yLabel }) {
  const ink = { text: cssVar("--text"), dim: cssVar("--dim"), faint: cssVar("--faint"), grid: cssVar("--line-soft"), axis: cssVar("--line") };
  const font = `11px ${cssVar("--f-mono") || "monospace"}`;
  const axis = (label) => ({
    stroke: ink.dim, font, labelFont: `12px ${cssVar("--f-sans") || "sans-serif"}`, label,
    grid: { stroke: ink.grid, width: 1 }, ticks: { stroke: ink.axis, width: 1, size: 4 },
  });
  return {
    ink,
    opts: {
      width: el.clientWidth || 600,
      height,
      cursor: { sync: { key: "roll" }, points: { size: 8 }, drag: { x: false, y: false } },
      legend: { live: true },
      scales: { x: { time: false } },
      axes: [axis(xLabel), { ...axis(yLabel), size: 52 }],
      series: [{ label: "m", value: (u, v) => (v === null ? "—" : `${Math.round(v)} m`) }],
    },
  };
}

/** Faint segment bands and gate lines behind a chart, named along the top. */
function segmentBands(axis, { only } = {}) {
  return (u) => {
    const ctx = u.ctx, { top, height } = u.bbox, dpr = devicePixelRatio || 1;
    ctx.save();
    ctx.font = `${11 * dpr}px ${cssVar("--f-sans") || "sans-serif"}`;
    ctx.textBaseline = "top";
    for (const s of axis.segments) {
      if (s.status !== "ok" || (only && s.id !== only)) continue;
      const spec = segSpec(s.id);
      const x0 = u.valToPos(s.d_start, "x", true), x1 = u.valToPos(s.d_end, "x", true);
      ctx.globalAlpha = 0.09;
      ctx.fillStyle = segColor(spec.color);
      ctx.fillRect(x0, top, x1 - x0, height);
      ctx.globalAlpha = 1;
      ctx.strokeStyle = cssVar("--line");
      ctx.lineWidth = dpr;
      ctx.beginPath(); ctx.moveTo(x1, top); ctx.lineTo(x1, top + height); ctx.stroke();
      if (x1 - x0 > 60 * dpr) {
        ctx.fillStyle = cssVar("--faint");
        ctx.fillText(spec.bad_gps ? `${spec.name} · bad GPS` : spec.name, x0 + 4 * dpr, top + 4 * dpr, x1 - x0 - 8 * dpr);
      }
    }
    ctx.restore();
  };
}

function mountChart(el, opts, data) {
  const u = new uPlot(opts, data, el);
  const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height: opts.height }));
  ro.observe(el);
  doom.cleanup.push(() => { ro.disconnect(); u.destroy(); });
  return u;
}

/** Legend value for a speed series: its sample, or interpolated where only the
    other series has a sample at the cursor's x. */
const speedAt = (series) => (u, v, si, idx) => {
  if (v === null && idx !== null && idx !== undefined) v = interp(series.x, series.y, u.data[0][idx]);
  return v === null || v === undefined ? "—" : `${v.toFixed(1)} mph`;
};

function drawSpeedChart(an, cmp, axis) {
  const el = $("chart-speed");
  const { ink, opts } = baseOpts(el, 260, { xLabel: "Distance along course (m)", yLabel: "mph" });
  const mine = { x: alignX(an, axis), y: an.trace.mph };
  const theirs = cmp ? { x: alignX(cmp.an, axis), y: cmp.an.trace.mph } : null;
  opts.series.push({ label: "This roll", stroke: ink.text, width: 2, spanGaps: true, value: speedAt(mine) });
  if (theirs) opts.series.push({ label: cmp.roll.title, stroke: ink.dim, width: 1.5, dash: [6, 4], spanGaps: true, value: speedAt(theirs) });
  opts.scales.y = { range: (u, lo, hi) => [0, Math.max(35, Math.ceil(hi / 5) * 5)] };
  opts.hooks = {
    drawClear: [segmentBands(axis)],
    setCursor: [(u) => moveMarker(u, mine.x)],
  };
  doom.page.markerX = mine.x;  // where this roll's points sit on the chart, for map hover
  doom.page.speedChart = mountChart(el, opts, mergeSeries(mine, theirs));
}

function drawGapChart(an, refAn) {
  const el = $("chart-gap");
  const { ink, opts } = baseOpts(el, 200, { xLabel: "Distance along course (m)", yLabel: "s behind" });
  const rx = alignX(an, refAn);
  const gaps = refAn.trace.d.map((x, i) => {
    const t = interp(rx, an.trace.t, x);
    return t === null ? null : +(t - refAn.trace.t[i]).toFixed(3);
  });
  opts.series.push({ label: "Gap", stroke: ink.text, width: 2, spanGaps: false, value: (u, v) => (v === null ? "—" : gapText(v)) });
  opts.scales.y = { range: (u, lo, hi) => { const m = Math.max(0.5, Math.abs(lo ?? 0), Math.abs(hi ?? 0)) * 1.15; return [-m, m]; } };
  opts.hooks = {
    drawClear: [segmentBands(refAn)],
    draw: [(u) => {  // zero line: level with the comparison
      const ctx = u.ctx, y = u.valToPos(0, "y", true);
      ctx.save(); ctx.strokeStyle = cssVar("--dim"); ctx.lineWidth = devicePixelRatio || 1;
      ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(u.bbox.left, y); ctx.lineTo(u.bbox.left + u.bbox.width, y); ctx.stroke();
      ctx.restore();
    }],
  };
  mountChart(el, opts, [refAn.trace.d, gaps]);
}

// ── map ───────────────────────────────────────────────────────────────────

function speedColor(v, lo, hi) {
  const ramp = SPEED_RAMP[isDark() ? "dark" : "light"];
  const f = hi > lo ? Math.min(1, Math.max(0, (v - lo) / (hi - lo))) : 0.5;
  return ramp[Math.min(ramp.length - 1, Math.floor(f * ramp.length))];
}

function speedLegend(an) {
  const lo = Math.min(...an.trace.mph), hi = Math.max(...an.trace.mph);
  const ramp = SPEED_RAMP[isDark() ? "dark" : "light"];
  return h("div", { class: "legend" },
    h("span", {}, h("span", { class: "ramp" }, ramp.map((c) => h("i", { style: `background: ${c}` }))),
      h("span", { class: "num" }, `${lo.toFixed(0)}–${hi.toFixed(0)} mph`), " this roll"),
    h("span", {}, h("i", { class: "line-key dashed" }), " comparison"),
    h("span", {}, h("i", { class: "line-key gate" }), " gates"));
}

function mapNote(cmp) {
  return h("p", { class: "footnote", id: "map-note" }, cmp ? "Hover a chart to follow along on the map." : "");
}

/** A Leaflet map with OpenStreetMap tiles, falling back to the drawn course
    (track, gates) on a plain background when tiles can't load. */
function makeMap(el) {
  const map = L.map(el, { zoomSnap: 0.25, scrollWheelZoom: false, attributionControl: true });
  const tiles = L.tileLayer(TILE_URL, { maxZoom: 19, attribution: "© OpenStreetMap contributors" });
  let loaded = 0, failed = 0;
  const offline = () => {
    if (map.hasLayer(tiles)) map.removeLayer(tiles);
    el.classList.add("offline");
    map.attributionControl.setPrefix("Offline · drawn course");
  };
  tiles.on("tileload", () => loaded++);
  tiles.on("tileerror", () => { if (++failed >= 4 && loaded === 0) offline(); });
  if (navigator.onLine === false) offline(); else tiles.addTo(map);
  const ro = new ResizeObserver(() => map.invalidateSize());
  ro.observe(el);
  doom.cleanup.push(() => { ro.disconnect(); map.remove(); });
  return map;
}

function drawMap(el, an, refAn, segId) {
  const map = makeMap(el);
  const range = (a) => (segId ? a.segments.find((s) => s.id === segId) : null);
  const slice = (a) => {
    const s = range(a);
    const idx = a.trace.t.map((t, i) => i).filter((i) => !s || (a.trace.t[i] >= s.t_start - 1e-6 && a.trace.t[i] <= s.t_end + 1e-6));
    return idx.map((i) => [a.trace.lat[i], a.trace.lon[i], a.trace.mph[i]]);
  };
  const pts = slice(an);
  if (refAn) {
    L.polyline(slice(refAn).map(([la, lo]) => [la, lo]), { color: cssVar("--dim"), weight: 2, dashArray: "6 6", interactive: false }).addTo(map);
  }
  const lo = Math.min(...an.trace.mph), hi = Math.max(...an.trace.mph);
  for (let i = 0; i + 1 < pts.length; i++) {
    L.polyline([pts[i].slice(0, 2), pts[i + 1].slice(0, 2)], {
      color: speedColor((pts[i][2] + pts[i + 1][2]) / 2, lo, hi), weight: 5, lineCap: "round", interactive: false,
    }).addTo(map);
  }
  for (const spec of doom.course.segments) {
    for (const g of [spec.start_gate, spec.end_gate]) {
      L.polyline(g.line, { color: cssVar("--text"), weight: 2, opacity: 0.55 }).bindTooltip(g.name, { sticky: true }).addTo(map);
    }
  }
  map.fitBounds(L.latLngBounds(pts.map((p) => p.slice(0, 2))).pad(segId ? 0.35 : 0.08));

  if (!segId) {
    const marker = L.circleMarker(pts[0].slice(0, 2), { radius: 6, color: cssVar("--bg"), weight: 2, fillColor: cssVar("--accent"), fillOpacity: 1, interactive: false });
    doom.page.marker = { map, marker, an };
    // Hovering the map moves the chart cursors to the nearest point of this roll.
    map.on("mousemove", (e) => {
      const u = doom.page.speedChart;
      if (!u) return;
      let best = 0, bestD = Infinity;
      an.trace.lat.forEach((la, i) => {
        const d = map.latLngToContainerPoint([la, an.trace.lon[i]]).distanceTo(e.containerPoint);
        if (d < bestD) { bestD = d; best = i; }
      });
      if (bestD > 40) return;
      const x = doom.page.markerX?.[best];
      if (x !== undefined) u.setCursor({ left: u.valToPos(x, "x"), top: u.valToPos(an.trace.mph[best], "y") });
    });
  }
  return map;
}

/** Follow the chart cursor with a marker on the main map. */
function moveMarker(u, xs) {
  const m = doom.page?.marker;
  if (!m) return;
  const idx = u.cursor.idx;
  if (idx === null || idx === undefined) { m.marker.remove(); return; }
  const x = u.data[0][idx];
  let best = 0;
  xs.forEach((v, i) => { if (Math.abs(v - x) < Math.abs(xs[best] - x)) best = i; });
  m.marker.setLatLng([m.an.trace.lat[best], m.an.trace.lon[best]]).addTo(m.map);
}

// ── segment deep dive ─────────────────────────────────────────────────────

function segmentPicker() {
  return h("div", { class: "seg-picker", role: "tablist" }, doom.course.segments.map((spec) =>
    h("button", {
      type: "button", role: "tab", class: "seg-chip", "data-seg": spec.id,
      "aria-selected": String(spec.id === doom.page.segment), onclick: () => pickSegment(spec.id),
    }, h("i", { class: "swatch", style: `background: ${segColor(spec.color)}` }), spec.name)));
}

function pickSegment(id, scroll = false) {
  doom.page.segment = id;
  document.querySelectorAll(".seg-chip").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.seg === id)));
  renderSegment();
  if (scroll) $("seg-detail").closest(".block").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderSegment() {
  const { data, ref } = doom.page;
  const id = doom.page.segment;
  const an = data.analysis, refAn = ref?.analysis ?? null;
  const spec = segSpec(id);
  const s = an.segments.find((x) => x.id === id);
  const r = refAn?.segments.find((x) => x.id === id);
  const st = data.stats.segments[id];
  const box = $("seg-detail");
  for (const fn of doom.segCleanup?.splice(0) ?? []) fn();
  doom.segCleanup = [];

  const intro = h("p", { class: "seg-desc" }, spec.description);
  if (s.status !== "ok") {
    fill(box, intro, h("p", { class: "empty-note" }, "This segment wasn't measured on this roll: one of its gates was not crossed."));
    return;
  }
  const vs = (mine, theirs, unit, betterLow) => {
    if (theirs === null || theirs === undefined) return null;
    const d = mine - theirs;
    const cls = Math.abs(d) < 0.005 ? "even" : (d > 0) === betterLow ? "slower" : "faster";
    return h("span", { class: `vs ${cls}` }, `${d > 0 ? "+" : d < 0 ? "−" : "±"}${Math.abs(d).toFixed(unit === "s" ? 2 : 1)}${unit === "s" ? " s" : ""}`);
  };
  const ok = r && r.status === "ok" ? r : null;
  const stat = (label, value, delta) => h("div", {}, h("dt", {}, label), h("dd", { class: "num" }, value, delta));
  const facts = h("dl", { class: "stats seg-stats" },
    stat("Time", secs(s.time_sec), vs(s.time_sec, ok?.time_sec, "s", true)),
    stat("Rank", `${st.rank} / ${st.n}`),
    stat("Entry", mph(s.entry_mph), vs(s.entry_mph, ok?.entry_mph, "mph", false)),
    stat("Exit", mph(s.exit_mph), vs(s.exit_mph, ok?.exit_mph, "mph", false)),
    stat("Slowest", mph(s.min_mph), vs(s.min_mph, ok?.min_mph, "mph", false)),
    stat("Fastest", mph(s.max_mph)),
    stat("Turn", `${signed(s.turn_deg)}°`),
    stat("Samples", `${s.samples}`));

  const chart = h("div", { class: "chart", id: "chart-seg" });
  const mapEl = h("div", { class: "map map-small" });
  fill(box, 
    intro,
    facts,
    spec.bad_gps ? h("p", { class: "notice-inline" }, "GPS is known to be unreliable in this segment (tree cover). Times at its gates hold; speeds inside it are rougher.") : null,
    s.samples < 8 ? h("p", { class: "notice-inline" }, `Only ${s.samples} samples fall inside this segment (one per second). The gate times are interpolated precisely; the shape of the speed curve between samples is not known.`) : null,
    h("div", { class: "seg-visuals" }, h("div", {}, h("h3", { class: "sub" }, "Speed through the segment"), chart), h("div", {}, h("h3", { class: "sub" }, "Line"), mapEl)),
    h("div", { class: "seg-visuals" },
      h("div", {}, h("h3", { class: "sub" }, `Every ${data.roll.buggy} time here`), segmentRanking(data, id, ok ? ref.roll.id : null)),
      h("div", {}, h("h3", { class: "sub" }, "Does entry speed pay off?"), entryScatter(data, id, ok ? ref.roll.id : null))),
  );

  // Zoomed speed chart: distance within the segment, aligned to the comparison's segment length.
  const axis = ok ? refAn : an;
  const axSeg = axis.segments.find((x) => x.id === id);
  const within = (a) => {
    const xs = alignX(a, axis), out = { x: [], y: [] };
    a.trace.t.forEach((t, i) => {
      const seg = a.segments.find((x) => x.id === id);
      if (t >= seg.t_start - 1e-6 && t <= seg.t_end + 1e-6) { out.x.push(+(xs[i] - axSeg.d_start).toFixed(2)); out.y.push(a.trace.mph[i]); }
    });
    return out;
  };
  const { ink, opts } = baseOpts(chart, 220, { xLabel: "Distance into segment (m)", yLabel: "mph" });
  const mineSeg = within(an), theirsSeg = ok ? within(refAn) : null;
  opts.cursor.sync = undefined;
  opts.series.push({ label: "This roll", stroke: ink.text, width: 2, spanGaps: true, value: speedAt(mineSeg), points: { show: true, size: 6 } });
  if (ok) opts.series.push({ label: ref.roll.title, stroke: ink.dim, width: 1.5, dash: [6, 4], spanGaps: true, value: speedAt(theirsSeg), points: { show: true, size: 5 } });
  const u = new uPlot(opts, mergeSeries(mineSeg, theirsSeg), chart);
  const ro = new ResizeObserver(() => u.setSize({ width: chart.clientWidth, height: 220 }));
  ro.observe(chart);
  doom.segCleanup.push(() => { ro.disconnect(); u.destroy(); });

  const saved = doom.cleanup.length;
  drawMap(mapEl, an, ok ? refAn : null, id);
  doom.segCleanup.push(...doom.cleanup.splice(saved));  // this map goes when the segment changes
}

/** The buggy's times in this segment, fastest first, as bars. */
function segmentRanking(data, id, refId) {
  const rows = data.peers.filter((p) => p.segments[id]).map((p) => ({ ...p, time: p.segments[id].time_sec }))
    .sort((a, b) => a.time - b.time);
  const best = rows[0]?.time ?? 0, worst = rows[rows.length - 1]?.time ?? 1;
  const shown = rows.length > 12 ? rows.filter((r, i) => i < 10 || r.id === data.roll.id || r.id === refId) : rows;
  return h("ol", { class: "ranking" }, shown.map((r) => {
    const frac = worst > best ? 0.25 + 0.75 * ((r.time - best) / (worst - best)) : 1;
    const me = r.id === data.roll.id;
    return h("li", { class: me ? "me" : r.id === refId ? "ref" : "" },
      h("span", { class: "rank num" }, `${rows.indexOf(r) + 1}`),
      h("a", { href: `#doomscroll/roll/${r.id}` }, `${r.title.replace(/^.* Roll /, "Roll ")}${r.driver ? ` · ${r.driver}` : ""}`),
      h("span", { class: "bar" }, h("i", { style: `width: ${(frac * 100).toFixed(1)}%` })),
      h("span", { class: "num" }, r.time.toFixed(2)));
  }));
}

/** Entry speed against segment time across the buggy's rolls. */
function entryScatter(data, id, refId) {
  const pts = data.peers.filter((p) => p.segments[id]).map((p) => ({ id: p.id, title: p.title, driver: p.driver, x: p.segments[id].entry_mph, y: p.segments[id].time_sec }));
  if (pts.length < 3) return h("p", { class: "empty-note" }, "Needs at least three rolls of this buggy through here.");
  const W = 360, H = 220, m = { l: 44, r: 10, t: 10, b: 34 };
  const ext = (vs) => { const lo = Math.min(...vs), hi = Math.max(...vs), pad = (hi - lo) * 0.1 || 0.5; return [lo - pad, hi + pad]; };
  const [x0, x1] = ext(pts.map((p) => p.x)), [y0, y1] = ext(pts.map((p) => p.y));
  const sx = (v) => m.l + ((v - x0) / (x1 - x0)) * (W - m.l - m.r);
  const sy = (v) => H - m.b - ((v - y0) / (y1 - y0)) * (H - m.t - m.b);
  const ticks = (lo, hi, n = 4) => Array.from({ length: n + 1 }, (_, i) => lo + ((hi - lo) * i) / n);
  const tip = h("div", { class: "tooltip", hidden: true });
  const order = [...pts].sort((a, b) => (a.id === data.roll.id) - (b.id === data.roll.id) || (a.id === refId) - (b.id === refId));
  const chart = svg("svg", { class: "scatter", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Entry speed against segment time" },
    ticks(y0, y1).map((v) => svg("g", {},
      svg("line", { x1: m.l, x2: W - m.r, y1: sy(v), y2: sy(v), class: "grid" }),
      svg("text", { x: m.l - 6, y: sy(v) + 3.5, class: "tick", "text-anchor": "end" }, v.toFixed(1)))),
    ticks(x0, x1).map((v) => svg("text", { x: sx(v), y: H - m.b + 14, class: "tick", "text-anchor": "middle" }, v.toFixed(1))),
    svg("text", { x: (m.l + W - m.r) / 2, y: H - 4, class: "axis-label", "text-anchor": "middle" }, "Entry speed (mph)"),
    svg("text", { x: 11, y: (m.t + H - m.b) / 2, class: "axis-label", "text-anchor": "middle", transform: `rotate(-90 11 ${(m.t + H - m.b) / 2})` }, "Segment time (s)"),
    order.map((p) => {
      const me = p.id === data.roll.id, isRef = p.id === refId;
      const dot = svg("circle", { cx: sx(p.x), cy: sy(p.y), r: me ? 6 : isRef ? 5 : 4, class: me ? "dot me" : isRef ? "dot ref" : "dot" });
      const hit = svg("circle", { cx: sx(p.x), cy: sy(p.y), r: 12, class: "hit" });
      hit.addEventListener("pointerenter", () => {
        tip.hidden = false;
        tip.replaceChildren(h("strong", {}, p.title), h("span", {}, `${p.driver || "No driver"} · in at ${mph(p.x)} · ${p.y.toFixed(2)} s`));
        tip.style.left = `${(sx(p.x) / W) * 100}%`;
        tip.style.top = `${(sy(p.y) / H) * 100}%`;
      });
      hit.addEventListener("pointerleave", () => { tip.hidden = true; });
      hit.addEventListener("click", () => { location.hash = `doomscroll/roll/${p.id}`; });
      return [dot, hit];
    }));
  return h("div", { class: "scatter-wrap" }, chart, tip,
    h("div", { class: "legend" }, h("span", {}, h("i", { class: "dot-key me" }), " this roll"),
      refId ? h("span", {}, h("i", { class: "dot-key ref" }), " comparison") : null,
      h("span", {}, h("i", { class: "dot-key" }), ` other ${data.roll.buggy} rolls`)));
}

// ── data quality ──────────────────────────────────────────────────────────

function dataQuality(data) {
  const an = data.analysis, f = data.file;
  const rows = [
    ["Recording", `${f.filename}${f.device_name ? ` · ${f.device_name.replace(/^Garmin /, "")} …${String(f.device_id).slice(-4)}` : ""}`],
    ["Samples in the roll", `${an.samples} (one per second; about one every ${(an.dist_m / Math.max(1, an.total_sec)).toFixed(0)} m at this pace)`],
    ["Per segment", an.segments.map((s) => `${segName(s.id)}: ${s.status === "ok" ? s.samples : "not measured"}`).join(" · ")],
    ["Bad-GPS segments", doom.course.segments.filter((s) => s.bad_gps).map((s) => s.name).join(", ") || "none"],
    ["Finish", an.finished ? `reached ${an.end_gate}` : `GPS lost after ${an.end_gate}; later segments not measured`],
  ];
  return h("div", {},
    h("dl", { class: "quality" }, rows.map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", {}, v)))),
    h("p", { class: "footnote" },
      "Speeds are the watch's Doppler readings, accurate to a few cm/s. Gate times are interpolated between samples at the exact crossing, so splits are precise even though positions are only good to a few metres. There is no altitude: these watches don't record it."));
}

// Charts and maps read theme colours when drawn, so redraw on a theme change.
window.addEventListener("themechange", () => {
  if (doom.page && !$("doom-roll").hidden) renderRoll();
  else if (!$("doom-feed").hidden && doom.feedKey !== null) { doom.feedKey = null; showFeed(currentRoute().params); }
});
