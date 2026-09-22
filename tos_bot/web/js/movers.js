/* The movers part of a session's report (research/movers.py on the server): the market's biggest gainers
   and losers, why each one moved, what the bot made of it, and a chart of each with the bot's trades,
   the setups it offered and the news on the session's candles. */
import { $, $$, api, escapeHtml, num, shorten, usd } from "./util.js";
import { chartModal } from "./chart.js";
import { stratLabel } from "./strategies.js";
import { inR, pctOf, tone } from "./journal.js";

const CATALYST = {
  earnings: ["Earnings", "good"], filing: ["SEC filing", "warn"], analyst: ["Analyst", "accent"], news: ["News", "accent"],
  sector: ["Sector move", ""], none: ["No news found", "faint"], unchecked: ["News not read", "faint"],
};
const STATUS = {
  traded: ["Traded", "good"], sent: ["Sent, not filled", "accent"], offered: ["Offered, not taken", "warn"],
  watched: ["Watched, no setup", ""], missed: ["Not watched", "bad"], offline: ["App wasn't scanning", "faint"],
};
const NEWS = new Set(["earnings", "filing", "analyst", "news"]);

let side = "gainers";

const signed = (v, d = 1) => v == null ? "–" : `${v > 0 ? "+" : ""}${num(v, d)}%`;
const etTime = iso => iso ? new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }) : "–";
const etWhen = iso => iso ? new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }) + " ET" : "–";
const safeUrl = u => /^https:\/\//.test(u || "") ? u : "";

export function moversHTML(m, capture) {
  if (!m) return "";
  if (!m.ok) return `<section class="movers"><h4>Market movers</h4><p class="muted">${escapeHtml(m.note || "Not built yet.")}</p></section>`;
  const s = m.summary || {};
  const card = (label, value, note) => `<div><label>${label}</label><b>${value}</b>${note ? `<span class="muted small">${note}</span>` : ""}</div>`;
  return `<section class="movers">
    <div class="movers-head">
      <h4>Market movers</h4>
      <span class="muted small">the biggest moves among ${m.stocks.toLocaleString()} stocks the scanner could trade</span>
      <div class="seg-group">${["gainers", "losers"].map(k => `<button class="seg mini ${side === k ? "active" : ""}" data-side="${k}">
        ${k === "gainers" ? "Gainers" : "Losers"} · ${m[k].length}</button>`).join("")}</div>
    </div>
    ${m.stale ? `<p class="small warn-text">${escapeHtml(m.stale_note || "Built before this rebuild, which couldn't refresh them.")}</p>` : ""}
    <div class="journal-cards movers-score">
      ${card("Traded", `${s.traded} of ${s.movers}`, s.traded ? inR(s.traded_r) : "")}
      ${s.sent ? card("Sent, not filled", s.sent, "an entry went out, no trade came of it") : ""}
      ${card("Offered, not taken", s.offered, s.offered_r != null ? `would have made ${inR(s.offered_r)}` : "")}
      ${card("On the morning watchlist", `${s.in_watchlist} of ${s.movers}`, `${s.on_hot_list} on the hot list`)}
      ${card("Moved at the open", `${s.before_open} of ${s.movers}`, `${s.news_before_open} with news before the bell`)}
      ${capture ? card(`Last ${capture.sessions} session${capture.sessions === 1 ? "" : "s"}`, `${pctOf(capture.in_watchlist)} watched`,
        `${pctOf(capture.traded)} traded · ${pctOf(capture.before_open)} moved at the open`) : ""}
    </div>
    <ul class="journal-lessons">${(m.lessons || []).map(l => `<li>${escapeHtml(l)}</li>`).join("")}</ul>
    ${m.news_note ? `<p class="muted small">${escapeHtml(m.news_note)}</p>` : ""}
    <div class="table-scroll"><table class="ev-table movers-table">
      <thead><tr><th>Stock</th><th class="num">Move</th><th>Why</th><th>The bot</th><th></th></tr></thead>
      <tbody id="movers-rows">${rowsHTML(m[side])}</tbody>
    </table></div>
    <p class="muted small">Click a row for the whole story; 📈 for the charts.</p>
  </section>`;
}

function rowsHTML(rows) {
  if (!rows.length) return `<tr><td colspan="5" class="muted">None this session.</td></tr>`;
  return rows.map((r, i) => {
    const [catLabel, catCls] = CATALYST[r.catalyst.kind] || [r.catalyst.kind, ""];
    const [stLabel, stCls] = STATUS[r.bot.status] || [r.bot.status, ""];
    const how = [r.before_open ? `gapped ${signed(r.gap_pct)}` : `${signed(r.session_pct)} intraday`,
      `${num(r.rvol, 1)}× vol`, r.extreme ? `20d ${r.extreme}` : ""].filter(Boolean).join(" · ");
    const result = r.bot.status === "traded" ? `${inR(r.bot.r)} · ${usd(r.bot.pl)}`
      : (r.bot.status === "offered" || r.bot.status === "sent") && r.bot.r != null ? `would have made ${inR(r.bot.r)}` : "";
    return `<tr class="mover" data-i="${i}" tabindex="0">
      <td><b>${escapeHtml(r.symbol)}</b><div class="muted small">${escapeHtml(r.sector || "–")}</div></td>
      <td class="num"><b class="${tone(r.change_pct)}">${signed(r.change_pct)}</b> <span class="muted small">$${num(r.close)}</span>
        <div class="muted small nowrap">${how}</div></td>
      <td class="why"><span class="badge ${catCls}">${catLabel}</span>${r.catalyst.before_open ? ` <span class="badge">before the open</span>` : ""}
        ${NEWS.has(r.catalyst.kind) ? `<div class="small">${escapeHtml(shorten(r.catalyst.label, 120))}</div>` : ""}</td>
      <td><span class="badge ${stCls}">${stLabel}</span> <span class="small ${tone(r.bot.r)}">${result}</span></td>
      <td class="row-tools"><button class="chart-btn" data-chart="${escapeHtml(r.symbol)}" title="Charts: the session and the days around it" aria-label="Chart">📈</button></td>
    </tr>
    <tr class="mover-detail hidden" data-detail="${i}"><td colspan="5">${detailHTML(r)}</td></tr>`;
  }).join("");
}

function detailHTML(r) {
  const b = r.bot;
  const stories = r.stories.length ? `<ul class="stories">${r.stories.map(s => {
    const url = safeUrl(s.url), headline = escapeHtml(s.headline);
    return `<li><span class="muted small">${escapeHtml(etWhen(s.at))}</span> <span class="badge ${(CATALYST[s.kind] || [])[1] || ""}">${escapeHtml((CATALYST[s.kind] || [s.kind])[0])}</span>
      ${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${headline}</a>` : headline}
      <span class="muted small">${escapeHtml(s.provider || s.source || "")}${s.sentiment != null ? ` · tone ${s.sentiment > 0 ? "+" : ""}${num(s.sentiment, 2)}` : ""}</span></li>`;
  }).join("")}</ul>` : `<p class="muted small">${r.catalyst.kind === "unchecked" ? "The news wasn't read for this session." : "No story about it in the feeds this account reads (IBKR, SEC 8-K filings" + " and Finnhub with a key)."}</p>`;
  const trades = b.trades.length ? `<table class="ev-table"><tr><th>Trade</th><th>Strategy</th><th>In → out (ET)</th><th class="num">Prices</th><th class="num">R</th><th class="num">P/L</th></tr>
    ${b.trades.map(t => `<tr><td>${escapeHtml(t.side.toLowerCase())} <span class="badge ${t.with_move ? "good" : "bad"}">${t.with_move ? "with the move" : "against it"}</span></td>
      <td>${stratLabel(t.strategy)}</td><td>${etTime(t.entry_time)} → ${t.exit_time ? etTime(t.exit_time) : "still open"}</td>
      <td class="num">${num(t.entry)} → ${t.exit != null ? num(t.exit) : "–"}</td><td class="num ${tone(t.r)}">${inR(t.r)}</td><td class="num ${tone(t.pl)}">${usd(t.pl)}</td></tr>`).join("")}</table>` : "";
  const plays = b.plays.length ? `<table class="ev-table"><tr><th>Setup offered</th><th>First seen (ET)</th><th class="num">Entry / stop</th><th>Status</th><th class="num">Taken as planned</th></tr>
    ${b.plays.map(p => `<tr><td>${stratLabel(p.strategy)} · ${escapeHtml(p.side.toLowerCase())} <span class="badge ${p.with_move ? "good" : "bad"}">${p.with_move ? "with the move" : "against it"}</span></td>
      <td>${etTime(p.seen_at)}</td><td class="num">${num(p.entry)} / ${num(p.stop)}</td>
      <td>${p.sent ? `<span class="badge accent">sent ${etTime(p.sent_at)}, not filled</span> <span class="muted small">${escapeHtml((p.status || "").toLowerCase())}</span>`
        : escapeHtml((p.status || "").toLowerCase())}</td>
      <td class="num ${tone(p.shadow_r)}">${p.shadow_r != null ? inR(p.shadow_r) : p.shadow_filled === false ? "wouldn't have filled" : "–"}</td></tr>`).join("")}</table>` : "";
  return `<div class="mover-story">
    <div><h5>How it moved</h5><ul>${r.reasons.map(x => `<li>${escapeHtml(x)}</li>`).join("")}</ul></div>
    <div><h5>The news</h5>${stories}</div>
    <div class="wide"><h5>What the bot made of it</h5><p>${escapeHtml(b.detail || "")}${b.watched && b.status !== "watched" ? ` <span class="muted">(${escapeHtml(b.watched)})</span>` : ""}</p>
      ${b.rank && b.status !== "missed" ? `<p class="muted small">The morning's ranking put it #${b.rank} of ${b.ranked}.</p>` : ""}${trades}${plays}</div>
  </div>`;
}

export function bindMovers(root, review) {
  const m = review.movers;
  if (!m || !m.ok) return;
  const body = $("#movers-rows", root);
  $$("[data-side]", root).forEach(btn => {
    btn.onclick = () => {
      side = btn.dataset.side;
      $$("[data-side]", root).forEach(x => x.classList.toggle("active", x === btn));
      body.innerHTML = rowsHTML(m[side]);
    };
  });
  const toggle = tr => {
    tr.classList.toggle("open");
    $(`[data-detail="${tr.dataset.i}"]`, body).classList.toggle("hidden");
  };
  body.onclick = e => {
    const chart = e.target.closest("[data-chart]");
    if (chart) { openMoverChart(review.session, chart.dataset.chart); return; }
    const tr = e.target.closest("tr.mover");
    if (tr && !e.target.closest("a")) toggle(tr);
  };
  body.onkeydown = e => {
    const tr = e.target.closest("tr.mover");
    if (tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(tr); }
  };
}

export async function openMoverChart(session, symbol) {
  const box = chartModal(), key = `${session}:${symbol}`;
  box.dataset.play = key;
  $(".chart-title", box).textContent = `${symbol} · ${session}`;
  $(".chart-body", box).innerHTML = `<p class="muted">Loading the charts…</p>`;
  box.classList.remove("hidden");
  let d;
  try { d = await api(`/api/journal/${encodeURIComponent(session)}/movers/${encodeURIComponent(symbol)}/chart`); } catch (e) { d = { ok: false, reason: `No chart - ${e.message}.` }; }
  if (box.dataset.play !== key || box.classList.contains("hidden")) return;
  $(".chart-body", box).innerHTML = d.ok ? moverChartHTML(d) : `<p class="reasons">${escapeHtml(d.reason || "No chart for this stock.")}</p>`;
}

function moverChartHTML(d) {
  const r = d.row, b = r.bot;
  const marks = [
    ...b.trades.flatMap(t => [
      { t: t.entry_time, v: t.entry, shape: t.side === "LONG" ? "up" : "down", cls: "mk-entry", title: `${t.side.toLowerCase()} entry at ${num(t.entry)} (${etTime(t.entry_time)} ET)` },
      t.exit_time ? { t: t.exit_time, v: t.exit, shape: t.side === "LONG" ? "down" : "up", cls: t.r > 0 ? "mk-win" : "mk-loss", title: `exit at ${num(t.exit)} (${etTime(t.exit_time)} ET): ${inR(t.r)}` } : null,
    ]).filter(Boolean),
    ...b.plays.map(p => ({ t: p.seen_at, v: p.entry, shape: "dot", cls: "mk-play",
      title: `offered: ${p.strategy.replace(/_/g, " ")} ${p.side.toLowerCase()} at ${num(p.entry)} (${etTime(p.seen_at)} ET)${p.shadow_r != null ? ` - taken as planned ${inR(p.shadow_r)}` : ""}` })),
  ];
  const during = r.stories.filter(s => !s.before_open);
  const early = r.stories.length - during.length;
  const intraday = d.intraday.length
    ? candlesSVG(d.intraday, {
      lines: [{ v: r.prev_close, cls: "lv-prev", text: `prev close ${num(r.prev_close)}` }],
      vlines: during.map(s => ({ t: s.at, cls: "vl-news", title: `${etTime(s.at)} ET - ${s.headline}` })),
      marks, clock: true,
    })
    : `<p class="muted">No 5-minute candles for this session${d.daily.length ? "" : " yet"} - they're downloaded with the report while IB Gateway is connected.</p>`;
  const at = d.daily.findIndex(k => k.t.slice(0, 10) === d.session);
  const daily = d.daily.length ? candlesSVG(d.daily, { band: at >= 0 ? at : null, height: 220 }) : "";
  return `<div class="muted small">${signed(r.change_pct)} · ${escapeHtml(r.catalyst.label)}${early ? ` · ${early} stor${early === 1 ? "y" : "ies"} before the open` : ""}</div>
    <h4>The session, 5-minute candles</h4>
    ${intraday}
    <ul class="chart-key small">
      <li><i class="mk-entry"></i>the bot's entries</li><li><i class="mk-win"></i><i class="mk-loss"></i>its exits</li>
      <li><i class="mk-play"></i>setups offered</li><li><i class="vl-news"></i>news during the session</li><li><i class="lv-prev"></i>the previous close</li>
    </ul>
    ${daily ? `<h4>Daily candles, the session shaded</h4>${daily}` : ""}`;
}

/** Candles as an SVG, with optional price lines, time lines, markers and one shaded candle. */
function candlesSVG(c, { lines = [], vlines = [], marks = [], band = null, height = 320, clock = false }) {
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
  const priceLines = lines.map(l => `<line class="lvl ${l.cls}" x1="${left}" x2="${W - right}" y1="${y(l.v)}" y2="${y(l.v)}"/>`
    + `<text class="lbl ${l.cls}" x="${W - right + 4}" y="${y(l.v) + 4}">${escapeHtml(l.text)}</text>`).join("");
  const timeLines = vlines.map(v => {
    const at = xAt(v.t);
    return at == null ? "" : `<line class="vline ${v.cls}" x1="${at}" x2="${at}" y1="${top}" y2="${H - bottom}"><title>${escapeHtml(v.title)}</title></line>`;
  }).join("");
  const markers = marks.map(m => {
    const at = xAt(m.t);
    if (at == null || m.v == null) return "";
    const py = y(m.v), s = 6;
    const shape = m.shape === "dot" ? `<circle cx="${at}" cy="${py}" r="4"/>`
      : m.shape === "up" ? `<path d="M ${at} ${py - s} L ${at + s} ${py + s} L ${at - s} ${py + s} z"/>`
        : `<path d="M ${at} ${py + s} L ${at + s} ${py - s} L ${at - s} ${py - s} z"/>`;
    return `<g class="mark ${m.cls}">${shape}<title>${escapeHtml(m.title)}</title></g>`;
  }).join("");
  const label = t => new Date(t).toLocaleString(undefined, clock
    ? { hour: "numeric", minute: "2-digit", timeZone: "America/New_York" } : { month: "short", day: "numeric" });
  const ticks = [...new Set([0, Math.floor(c.length / 2), c.length - 1])].map(i =>
    `<text class="tick" x="${x(i)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === c.length - 1 ? "end" : "middle"}">${escapeHtml(label(c[i].t))}</text>`).join("");
  return `<div class="chart-wrap"><svg class="play-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
    ${shade}${bars}${priceLines}${timeLines}${markers}${ticks}</svg></div>`;
}
