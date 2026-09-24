/* The chart behind a play: its recent candles, the entry, stop and target, and every
   way the trade can end (see engine/chart.py). Opened from the chart button on a play. The
   same window shows a report's movers (movers.js), and the candle drawing here (candlesSVG)
   also draws the chart behind a trade record (tradeChartHTML, in the record's panel: blotter.js). */
import { $, api, escapeHtml, num } from "./util.js";
import { S } from "./state.js";
import { watchPrice } from "./price.js";

const ROUTE_CLASS = { target: "rt-target", stop: "rt-stop", breakeven: "rt-lock", trail: "rt-trail", time: "rt-time" };

export async function openChart(play) {
  const box = chartModal();
  box.dataset.play = play.id;
  const title = (S.strategies[play.strategy] || {}).title || play.strategy.replace(/_/g, " ");
  $(".chart-title", box).textContent = `${play.symbol} · ${play.side.toLowerCase()} · ${title}`;
  $(".chart-body", box).innerHTML = `<p class="muted">Loading the chart…</p>`;
  box.classList.remove("hidden");
  // the market price, kept fresh while this chart is open - a mover's chart puts its own line here
  $(".chart-price", box).innerHTML = `Market price <span data-price></span>`;
  watchPrice($(".chart-price [data-price]", box), play.symbol);
  let d;
  try { d = await api(`/api/plays/${encodeURIComponent(play.id)}/chart`); } catch (e) { d = { ok: false, reason: `No chart - ${e.message}.` }; }
  if (box.dataset.play !== play.id || box.classList.contains("hidden")) return;
  $(".chart-body", box).innerHTML = d.ok ? chartHTML(d) : `<p class="reasons">${escapeHtml(d.reason || "No chart for this play.")}</p>`;
}

export function chartModal() {
  let box = $("#chart-modal");
  if (box) return box;
  box = document.createElement("div");
  box.id = "chart-modal";
  box.className = "modal hidden";
  box.innerHTML = `<div class="modal-card chart-card" role="dialog" aria-modal="true">
    <div class="chart-head"><h3 class="chart-title"></h3><button class="ghost mini" data-close title="Close">✕</button></div>
    <div class="chart-price muted small"></div>
    <div class="chart-body"></div></div>`;
  document.body.appendChild(box);
  box.addEventListener("click", e => { if (e.target === box || e.target.closest("[data-close]")) box.classList.add("hidden"); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") box.classList.add("hidden"); });
  return box;
}

function chartHTML(d) {
  const c = d.candles;
  if (!c.length) return `<p class="reasons">No candles for ${escapeHtml(d.symbol)} yet - the chart fills in once IBKR sends prices.</p>`;
  const W = 860, H = 380, top = 14, bottom = 24, left = 8, right = 78, ahead = 170;
  const plotW = W - left - right - ahead;
  const priced = d.routes.filter(r => r.price != null);
  const values = [...c.flatMap(k => [k.h, k.l]), d.levels.entry, d.levels.stop, ...d.levels.targets,
    ...priced.flatMap(r => [r.price, r.trigger]).filter(v => v != null)];
  let lo = Math.min(...values), hi = Math.max(...values);
  const pad = (hi - lo) * 0.06 || hi * 0.01;
  lo -= pad; hi += pad;
  const y = v => top + (hi - v) / (hi - lo) * (H - top - bottom);
  const step = plotW / c.length, bodyW = Math.max(1, step * 0.65);
  const x = i => left + i * step + step / 2;

  const bars = c.map((k, i) => {
    const cls = k.c >= k.o ? "up" : "down", t = y(Math.max(k.o, k.c)), b = y(Math.min(k.o, k.c));
    return `<line class="wick ${cls}" x1="${x(i)}" x2="${x(i)}" y1="${y(k.h)}" y2="${y(k.l)}"/>`
      + `<rect class="body ${cls}" x="${x(i) - bodyW / 2}" y="${t}" width="${bodyW}" height="${Math.max(1, b - t)}"/>`;
  }).join("");

  const labels = [];
  const level = (v, cls, text) => {
    labels.push({ y: y(v), cls, text: `${text} ${num(v)}` });
    return `<line class="lvl ${cls}" x1="${left}" x2="${W - right}" y1="${y(v)}" y2="${y(v)}"/>`;
  };
  const lines = [level(d.levels.entry, "lv-entry", "entry"), level(d.levels.stop, "lv-stop", "stop"),
    ...d.levels.targets.slice(0, 1).map(t => level(t, "lv-target", "target"))].join("");

  // the routes: from the last close into the space on the right, via the price that triggers the stop move
  const x0 = x(c.length - 1), y0 = y(c[c.length - 1].c), x1 = W - right - 6;
  const paths = priced.map(r => {
    const cls = ROUTE_CLASS[r.key] || "rt-time";
    const via = r.trigger != null ? ` L ${x0 + ahead * 0.5} ${y(r.trigger)}` : "";
    labels.push({ y: y(r.price), cls, text: `${r.label} ${r.r >= 0 ? "+" : ""}${num(r.r, 1)}R` });
    return `<path class="route ${cls}" d="M ${x0} ${y0}${via} L ${x1} ${y(r.price)}" marker-end="url(#arrow-${r.key})"/>`;
  }).join("");
  const markers = priced.map(r => `<marker id="arrow-${r.key}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path class="route-head ${ROUTE_CLASS[r.key] || "rt-time"}" d="M 0 0 L 10 5 L 0 10 z"/></marker>`).join("");

  // keep the labels on the right from sitting on top of each other
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 12);
  const text = labels.map(l => `<text class="lbl ${l.cls}" x="${W - right + 4}" y="${l.y + 4}">${escapeHtml(l.text)}</text>`).join("");

  const intraday = d.timeframe === "INTRADAY";
  const when = t => new Date(t).toLocaleString(undefined, intraday ? { hour: "numeric", minute: "2-digit" } : { month: "short", day: "numeric" });
  const ticks = [0, Math.floor(c.length / 2), c.length - 1].map(i =>
    `<text class="tick" x="${x(i)}" y="${H - 6}" text-anchor="middle">${escapeHtml(when(c[i].t))}</text>`).join("");

  return `<div class="muted small">${escapeHtml(d.bars)}${S.state.data && S.state.data.delayed ? " · IBKR prices are delayed" : ""}</div>
    <div class="chart-wrap"><svg class="play-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
      <defs>${markers}</defs>
      <rect class="ahead" x="${x0 + step / 2}" y="${top}" width="${W - right - x0 - step / 2}" height="${H - top - bottom}"/>
      ${bars}${lines}${paths}${text}${ticks}
    </svg></div>
    <h4>Ways the trade can end</h4>${routesHTML(d.routes)}`;
}

/** The ways a trade can end, listed under its chart: each in its colour, with its R and how it happens. */
function routesHTML(routes) {
  return `<ul class="routes">${routes.map(r => `<li class="${ROUTE_CLASS[r.key] || "rt-time"}"><i></i><b>${escapeHtml(r.label)}</b>`
    + `${r.r != null ? ` <span class="route-r">${r.r >= 0 ? "+" : ""}${num(r.r, 1)}R</span>` : ""} — ${escapeHtml(r.how)}</li>`).join("")}</ul>`;
}

/** Candles as an SVG, with optional price lines, time lines, markers and one shaded candle. A line with
    `lighter` is drawn thinner and dimmer (a level that no longer applies); a mark of kind "best" is a
    small dot. Clock labels carry the weekday once the candles span more than one session. */
export function candlesSVG(c, { lines = [], vlines = [], marks = [], band = null, height = 320, clock = false }) {
  const W = 860, H = height, top = 12, bottom = 22, left = 8, right = 96;
  const plotW = W - left - right;
  const values = [...c.flatMap(k => [k.h, k.l]), ...lines.map(l => l.v), ...marks.map(m => m.v)].filter(v => v != null && isFinite(v));
  let lo = Math.min(...values), hi = Math.max(...values);
  const pad = (hi - lo) * 0.06 || hi * 0.01;
  lo -= pad; hi += pad;
  const y = v => top + (hi - v) / (hi - lo) * (H - top - bottom);
  const step = plotW / c.length, bodyW = Math.max(1, step * 0.65);
  const x = i => left + i * step + step / 2;
  const starts = c.map(k => Date.parse(k.t)), span = starts.length > 1 ? starts[1] - starts[0] : 0;
  const xAt = iso => {                 // the candle a moment falls in, or null outside the candles
    const t = Date.parse(iso);
    if (!isFinite(t) || t < starts[0] || t >= starts[starts.length - 1] + span) return null;
    let i = starts.length - 1;
    while (i > 0 && starts[i] > t) i--;
    return x(i);
  };
  const shade = band != null ? `<rect class="band" x="${x(band) - step / 2}" y="${top}" width="${step}" height="${H - top - bottom}"/>` : "";
  const bars = c.map((k, i) => {
    const cls = k.c >= k.o ? "up" : "down", t = y(Math.max(k.o, k.c)), bt = y(Math.min(k.o, k.c));
    return `<line class="wick ${cls}" x1="${x(i)}" x2="${x(i)}" y1="${y(k.h)}" y2="${y(k.l)}"/>`
      + `<rect class="body ${cls}" x="${x(i) - bodyW / 2}" y="${t}" width="${bodyW}" height="${Math.max(1, bt - t)}"/>`;
  }).join("");
  const priceLines = lines.map(l => {
    const cls = `${l.cls}${l.lighter ? " lv-initial" : ""}`;
    return `<line class="lvl ${cls}" x1="${left}" x2="${W - right}" y1="${y(l.v)}" y2="${y(l.v)}"/>`
      + `<text class="lbl ${cls}" x="${W - right + 4}" y="${y(l.v) + 4}">${escapeHtml(l.text)}</text>`;
  }).join("");
  const timeLines = vlines.map(v => {
    const at = xAt(v.t);
    return at == null ? "" : `<line class="vline ${v.cls}" x1="${at}" x2="${at}" y1="${top}" y2="${H - bottom}"><title>${escapeHtml(v.title)}</title></line>`;
  }).join("");
  const markers = marks.map(m => {
    const at = xAt(m.t);
    if (at == null || m.v == null) return "";
    const py = y(m.v), s = 6;
    const shape = m.kind === "best" ? `<circle cx="${at}" cy="${py}" r="3"/>`
      : m.shape === "dot" ? `<circle cx="${at}" cy="${py}" r="4"/>`
        : m.shape === "up" ? `<path d="M ${at} ${py - s} L ${at + s} ${py + s} L ${at - s} ${py + s} z"/>`
          : `<path d="M ${at} ${py + s} L ${at + s} ${py - s} L ${at - s} ${py - s} z"/>`;
    return `<g class="mark ${m.cls}">${shape}<title>${escapeHtml(m.title)}</title></g>`;
  }).join("");
  // the clock alone can't tell one session's 9:30 from the next's: more than one session on the chart, and
  // the weekday goes with it (the candles' times are New York's, so their dates are the sessions')
  const days = clock && new Set(c.map(k => k.t.slice(0, 10))).size > 1;
  const label = t => new Date(t).toLocaleString(undefined, clock
    ? { ...(days ? { weekday: "short" } : {}), hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }
    : { month: "short", day: "numeric" });
  const ticks = [...new Set([0, Math.floor(c.length / 2), c.length - 1])].map(i =>
    `<text class="tick" x="${x(i)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === c.length - 1 ? "end" : "middle"}">${escapeHtml(label(c[i].t))}</text>`).join("");
  return `<div class="chart-wrap"><svg class="play-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
    ${shade}${bars}${priceLines}${timeLines}${markers}${ticks}</svg></div>`;
}

/* ---- the chart behind a trade record (GET /api/trades/{id}/chart, drawn in the record's panel) ---- */
const etTime = iso => new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", timeZone: "America/New_York" });
const etWhen = iso => new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" });

/** The trade on its candles: the entry, stop and target lines (the first stop lighter once the stop has
    moved), where the bot got in, took part off and got out, the best point it saw, a line at the entry time
    so what led to the trade stands apart from what came after, and the ways it can still end. */
export function tradeChartHTML(d) {
  const c = d.candles, lv = d.levels, long = d.side === "LONG", open = d.status === "OPEN";
  if (!c.length) return `<p class="reasons">No candles for ${escapeHtml(d.symbol)} yet - the chart fills in once IBKR sends prices.</p>`;
  const clock = c.length > 1 && Date.parse(c[1].t) - Date.parse(c[0].t) < 864e5;   // 5-minute candles, not daily
  const when = clock ? etTime : etWhen;
  const moved = lv.initial_stop != null && lv.stop != null && Math.abs(lv.stop - lv.initial_stop) > 0.01;
  const lines = [
    { v: lv.entry, cls: "lv-entry", text: `entry ${num(lv.entry)}` },
    { v: lv.stop, cls: "lv-stop", text: `stop ${num(lv.stop)}` },
    moved ? { v: lv.initial_stop, cls: "lv-stop", lighter: true, text: `first stop ${num(lv.initial_stop)}` } : null,
    ...lv.targets.map((t, i) => ({ v: t, cls: "lv-target", text: `target${i ? " 2" : ""} ${num(t)}` })),
  ].filter(l => l && l.v != null);
  // the entry and the parts taken off point the way the shares went; the exit is green or red by its result
  const into = long ? "up" : "down", outOf = long ? "down" : "up";
  const shapeOf = { entry: [into, "mk-entry"], part: [outOf, "mk-part"], best: ["best", "mk-best"] };
  const marks = d.marks.map(m => {
    const [shape, cls] = m.kind === "exit" ? [outOf, m.r != null && m.r < 0 ? "mk-loss" : "mk-win"] : shapeOf[m.kind] || ["dot", "mk-play"];
    return { t: m.t, v: m.price, shape, kind: m.kind, cls, title: `${m.title} (${when(m.t)} ET)` };
  });
  const entry = d.marks.find(m => m.kind === "entry");
  const vlines = entry ? [{ t: entry.t, cls: "vl-entry", title: `entered ${when(entry.t)} ET` }] : [];
  const moves = (d.stop_moves || []).map(m =>
    `${m.t ? `${when(m.t)} ET → ` : "→ "}${num(m.price)}${m.r != null ? ` at ${m.r >= 0 ? "+" : ""}${num(m.r, 1)}R` : ""}`);
  return `<div class="muted small">${escapeHtml(d.bars)}${S.state.data && S.state.data.delayed ? " · IBKR prices are delayed" : ""}</div>
    ${candlesSVG(c, { lines, vlines, marks, height: 420, clock })}
    <ul class="chart-key small">
      <li><i class="mk-entry"></i>the entry</li><li><i class="mk-part"></i>part taken off</li>
      <li><i class="mk-win"></i><i class="mk-loss"></i>the exit</li><li><i class="mk-best"></i>the best point</li>
      <li><i class="vl-entry"></i>the entry time</li>
    </ul>
    ${moves.length ? `<p class="muted small">Stop moved: ${escapeHtml(moves.join(" · "))}</p>` : ""}
    ${d.routes.length ? `<h4>${open ? "Ways the trade can end" : "The ways it could have ended"}</h4>${routesHTML(d.routes)}` : ""}`;
}
