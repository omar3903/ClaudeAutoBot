/* The chart behind a play: its recent candles, the entry, stop and target, and every
   way the trade can end (see engine/chart.py). Opened from the chart button on a play. The
   same window shows a report's movers (movers.js). */
import { $, api, escapeHtml, num } from "./util.js";
import { S } from "./state.js";

const ROUTE_CLASS = { target: "rt-target", stop: "rt-stop", breakeven: "rt-lock", trail: "rt-trail", time: "rt-time" };

export async function openChart(play) {
  const box = chartModal();
  box.dataset.play = play.id;
  const title = (S.strategies[play.strategy] || {}).title || play.strategy.replace(/_/g, " ");
  $(".chart-title", box).textContent = `${play.symbol} · ${play.side.toLowerCase()} · ${title}`;
  $(".chart-body", box).innerHTML = `<p class="muted">Loading the chart…</p>`;
  box.classList.remove("hidden");
  let d;
  try { d = await api(`/api/plays/${encodeURIComponent(play.id)}/chart`); } catch { d = { ok: false, reason: "The app isn't reachable." }; }
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

  const legend = d.routes.map(r => `<li class="${ROUTE_CLASS[r.key] || "rt-time"}"><i></i><b>${escapeHtml(r.label)}</b>`
    + `${r.r != null ? ` <span class="route-r">${r.r >= 0 ? "+" : ""}${num(r.r, 1)}R</span>` : ""} — ${escapeHtml(r.how)}</li>`).join("");

  return `<div class="muted small">${escapeHtml(d.bars)}${S.state.data && S.state.data.delayed ? " · IBKR prices are delayed" : ""}</div>
    <div class="chart-wrap"><svg class="play-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
      <defs>${markers}</defs>
      <rect class="ahead" x="${x0 + step / 2}" y="${top}" width="${W - right - x0 - step / 2}" height="${H - top - bottom}"/>
      ${bars}${lines}${paths}${text}${ticks}
    </svg></div>
    <h4>Ways the trade can end</h4>
    <ul class="routes">${legend}</ul>`;
}
