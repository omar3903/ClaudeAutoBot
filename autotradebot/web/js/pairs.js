/* The Pairs tab: the pair trades on, the pairs being watched and their spreads, and what the replay says. */
import { $, $$, api, escapeHtml, num, post, usd } from "./util.js";
import { S } from "./state.js";
import { openModal, toastResult } from "./ui.js";
import { loadOpen } from "./blotter.js";
import { watchPrice } from "./price.js";

const inR = v => v == null ? "–" : `${v >= 0 ? "+" : ""}${num(v, 2)}R`;
const SIDE = { LONG_SPREAD: "long the spread", SHORT_SPREAD: "short the spread" };

export async function loadPairs() {
  let d;
  try { d = await api("/api/pairs?live=true"); } catch { d = null; }
  if (!d || d.enabled === undefined) { $("#tab-pairs").innerHTML = `<div class="empty">Couldn't load the pairs.</div>`; return; }
  showPairs(d);
}

export function showPairs(d) {
  const box = $("#tab-pairs");
  if (!box || !d) return;
  if (!d.enabled) { box.innerHTML = `<div class="empty">Pairs trading is off (pairs.enabled in config.yaml).</div>`; return; }
  const w = d.window || {}, rec = d.record, ap = d.autopilot || {}, limits = d.limits || {};
  const held = rec && rec.out_of_sample && rec.out_of_sample.trades
    ? ` · held-out sessions ${inR(rec.out_of_sample.expectancy_r)} over ${rec.out_of_sample.trades}` : "";
  const record = rec && rec.trades
    ? `Replay: ${rec.trades} pair trades · ${Math.round((rec.win_rate || 0) * 100)}% winners · ${inR(rec.expectancy_r)} a trade${held}.`
    : "No replay record yet — Strategies → Run replay tests the pairs too.";
  const pilot = ap.on ? (ap.proof ? ` Autopilot won't trade pairs yet: ${escapeHtml(ap.proof)}.` : " Autopilot is trading pairs.") : "";
  box.innerHTML = `<div class="pairs">
    <p class="muted">Two stocks from one industry whose prices are cointegrated. When their spread strays past its band it's bought or
      sold — one stock long, the other short — and both legs are closed together back at the mean, at the stop, at the time stop, or at
      once if the pair loses ${num(limits.emergency_loss_r, 1)}× its planned risk. Entries and exits are decided in the last
      ${(w.minutes || [30])[0]} minutes of the session${w.open ? " — <b>that's now</b>" : ""}. ${escapeHtml(record)}${pilot}</p>
    ${tradesHTML(d.trades || [])}
    ${watchHTML(d.watch || [], w)}
    ${recentHTML(d.recent || [])}
  </div>`;
  $$("[data-pair-enter]", box).forEach(b => { b.onclick = () => confirmEnter((d.watch || []).find(r => r.id === b.dataset.pairEnter)); });
  $$("[data-pair-close]", box).forEach(b => { b.onclick = () => confirmClose((d.trades || []).find(t => t.id === b.dataset.pairClose)); });
  $$("[data-pair-chart]", box).forEach(b => { b.onclick = () => openChart(b.dataset.pairChart); });
}

function legs(t) {
  const long = t.side === "LONG_SPREAD";
  return `${long ? "+" : "−"}${num(t.qty_first, 0)} ${escapeHtml(t.first)} · ${long ? "−" : "+"}${num(t.qty_second, 0)} ${escapeHtml(t.second)}`;
}

function tradesHTML(rows) {
  if (!rows.length) return `<h4>Pair trades on</h4><p class="muted">None.</p>`;
  return `<h4>Pair trades on</h4><table class="ev-table">
    <tr><th>Pair</th><th>Side</th><th>Shares</th><th class="num">z at entry → now</th><th class="num">Open P/L</th><th class="num">R</th><th>Held</th><th>Status</th><th></th></tr>
    ${rows.map(t => `<tr><td><button class="ghost mini chart-btn" data-pair-chart="${escapeHtml(t.pair)}" title="Chart the spread">📈</button><b>${escapeHtml(t.pair)}</b></td>
      <td>${SIDE[t.side] || ""}</td><td>${legs(t)}</td>
      <td class="num">${num(t.entry_z, 2)} → ${num(t.z_now, 2)}</td><td class="num">${usd(t.unrealized_pl)}</td><td class="num">${inR(t.r_now)}</td>
      <td>${t.days_held != null ? `${num(t.days_held, 1)} of ${t.time_stop_days} sessions` : "–"}</td>
      <td>${escapeHtml((t.status || "").toLowerCase())}${t.exit_reason ? ` (${escapeHtml(t.exit_reason)})` : ""}</td>
      <td>${t.status === "OPEN" ? `<button class="danger mini" data-pair-close="${escapeHtml(t.id)}" title="Exit both legs at the market">Exit pair</button>` : ""}</td></tr>`).join("")}
  </table>`;
}

function zBar(z, band, stop) {
  if (z == null) return "–";
  const span = Math.max(stop || 4, Math.abs(z));
  const at = v => 50 + 50 * Math.max(-1, Math.min(1, v / span));
  return `<span class="zbar" title="z ${num(z, 2)} · band ±${num(band, 2)} · stop ±${num(stop, 2)}">
    <i class="band" style="left:${at(-band)}%;width:${at(band) - at(-band)}%"></i>
    <i class="mark ${Math.abs(z) >= band ? "hot" : ""}" style="left:${at(z)}%"></i></span> ${num(z, 2)}`;
}

function watchHTML(rows, w) {
  if (!rows.length) return `<h4>Pairs being watched</h4><p class="muted">No pairs yet — they're looked for after each full scan, among the watchlist's stocks.</p>`;
  return `<h4>Pairs being watched</h4><table class="ev-table">
    <tr><th>Pair</th><th>Industry</th><th class="num">Hedge</th><th class="num">Half-life</th><th class="num">Band / stop</th><th>Spread now</th><th>Signal</th><th>Out of sample</th><th></th></tr>
    ${rows.map(r => {
      const v = r.validation || {};
      const oos = v.stable === false ? "not a pair before" : v.trades ? `${v.trades} trades · ${inR(v.expectancy_r)}` : v.stable ? "no trades" : "–";
      return `<tr><td><button class="ghost mini chart-btn" data-pair-chart="${escapeHtml(r.id)}" title="Chart the spread">📈</button><b>${escapeHtml(r.first)}</b> / <b>${escapeHtml(r.second)}</b></td>
        <td class="muted">${escapeHtml(r.group)}</td><td class="num">${num(r.hedge, 2)}</td><td class="num">${num(r.half_life, 1)} d</td>
        <td class="num">±${num(r.entry_z, 2)} / ±${num(r.stop_z, 2)}</td>
        <td>${zBar(r.z, r.entry_z, r.stop_z)}${r.live ? "" : ` <span class="muted" title="From the last close — live prices are read during the session">close</span>`}</td>
        <td>${r.side ? `<span class="badge good">${SIDE[r.side]}</span>` : `<span class="muted">waiting</span>`}</td>
        <td class="muted" title="${escapeHtml(v.note || (v.from ? `fitted before ${v.from}, traded from then on` : ""))}">${escapeHtml(oos)}</td>
        <td>${r.side ? `<button class="mini lockable" data-pair-enter="${escapeHtml(r.id)}" ${w.regular ? "" : 'disabled title="Pairs are entered in the regular session"'}>Enter</button>` : ""}</td></tr>`;
    }).join("")}
  </table>`;
}

function recentHTML(rows) {
  if (!rows.length) return "";
  return `<h4>Recent pair trades</h4><table class="ev-table">
    <tr><th>Pair</th><th>Side</th><th>Closed</th><th>How</th><th class="num">P/L</th><th class="num">R</th></tr>
    ${rows.map(t => `<tr><td>${escapeHtml(t.pair)}</td><td>${SIDE[t.side] || ""}</td>
      <td class="muted">${t.closed_at ? escapeHtml(new Date(t.closed_at + (t.closed_at.endsWith("Z") ? "" : "Z")).toLocaleString()) : "–"}</td>
      <td>${t.status === "FAILED" ? `<span class="badge warn">not entered</span> <span class="muted">${escapeHtml(t.notes || "")}</span>` : escapeHtml(t.exit_reason || "")}</td>
      <td class="num">${usd(t.realized_pl)}</td><td class="num">${inR(t.r_multiple)}</td></tr>`).join("")}
  </table>`;
}

function confirmEnter(r) {
  if (!r) return;
  const long = r.side === "LONG_SPREAD", live = S.state.mode === "live";
  openModal({
    title: `Enter ${r.first} / ${r.second}?`,
    bodyHTML: `${live ? `<div class="warn-box">This sends <b>two real market orders</b>.</div>` : ""}
      <p>${long ? "Buy" : "Short"} <b>${escapeHtml(r.first)}</b> and ${long ? "short" : "buy"} <b>${escapeHtml(r.second)}</b> at the market —
        $${num(r.hedge, 2)} of ${escapeHtml(r.second)} for every $1 of ${escapeHtml(r.first)}. The spread is at z ${num(r.z, 2)}, past its ±${num(r.entry_z, 2)} band.</p>
      <p class="muted">Both legs are closed together back at the mean, at z ±${num(r.stop_z, 2)}, after ${r.time_stop_days} sessions, or at once if the pair loses
        twice its planned risk. The shares are sized so that reaching the stop costs about the usual risk per trade. If the second order can't go in,
        the first is closed again.</p>`,
    okText: "Enter pair", okClass: "long",
    onOk: async () => { const res = await post("/api/pairs/enter", { pair: r.id }); toastResult(res); loadPairs(); loadOpen(); },
  });
}

function confirmClose(t) {
  if (!t) return;
  openModal({
    title: `Exit ${t.pair}?`,
    bodyHTML: `${S.state.mode === "live" ? `<div class="warn-box">This sends <b>two real market orders</b>.</div>` : ""}
      <p>Close both legs at the market: ${legs(t)}.</p>`,
    okText: "Exit pair", okClass: "danger",
    onOk: async () => { const res = await post(`/api/pairs/trades/${encodeURIComponent(t.id)}/close`); toastResult(res); loadPairs(); loadOpen(); },
  });
}

async function openChart(pair) {
  let d;
  try { d = await api(`/api/pairs/chart?pair=${encodeURIComponent(pair)}`); } catch { d = null; }
  if (!d || !d.ok) { toastResult(d || { ok: false, reason: "Couldn't chart that pair." }); return; }
  openModal({ title: `${d.pair.first} / ${d.pair.second} — the spread`, okText: "Close", okClass: "ghost", onOk: () => { },
    bodyHTML: `<p class="muted small">Prices now <span data-price></span></p>${chartSVG(d)}` });
  watchPrice($("#modal-body [data-price]"), [d.pair.first, d.pair.second]);     // both legs, while the chart is open
}

function chartSVG(d) {
  const pts = d.points || [], m = d.pair, W = 640, H = 260, pad = 30;
  if (pts.length < 2) return `<p class="muted">Not enough history to chart.</p>`;
  const extreme = Math.max(m.stop_z + 0.5, ...pts.map(p => Math.abs(p.z)), Math.abs(d.live_z || 0));
  const x = i => pad + (W - 2 * pad) * i / (pts.length - (d.live_z != null ? 0 : 1));
  const y = z => H / 2 - (H / 2 - 14) * z / extreme;
  const line = (z, cls, label) => `<line class="${cls}" x1="${pad}" x2="${W - pad}" y1="${y(z)}" y2="${y(z)}"/><text x="${W - pad + 3}" y="${y(z) + 4}">${label}</text>`;
  const index = Object.fromEntries(pts.map((p, i) => [p.date, i]));
  const dateOf = s => s ? String(s).slice(0, 10) : null;
  const marks = (d.trades || []).map(t => {
    const i = index[dateOf(t.opened_at)], j = index[dateOf(t.closed_at)];
    return (i != null ? `<circle class="pc-in" cx="${x(i)}" cy="${y(pts[i].z)}" r="4"><title>entered ${escapeHtml(t.side)}</title></circle>` : "") +
      (j != null ? `<circle class="pc-out" cx="${x(j)}" cy="${y(pts[j].z)}" r="4"><title>closed ${inR(t.r)}</title></circle>` : "");
  }).join("");
  const poly = pts.map((p, i) => `${x(i).toFixed(1)},${y(p.z).toFixed(1)}`).join(" ");
  const live = d.live_z != null ? `<circle class="pc-live" cx="${x(pts.length)}" cy="${y(d.live_z)}" r="4"><title>now ${num(d.live_z, 2)}</title></circle>` : "";
  return `<svg class="pair-chart" viewBox="0 0 ${W + 30} ${H}" role="img" aria-label="z-score of the spread">
    ${line(0, "pc-mean", "mean")}${line(m.entry_z, "pc-band", `+${num(m.entry_z, 1)}`)}${line(-m.entry_z, "pc-band", `−${num(m.entry_z, 1)}`)}
    ${line(m.stop_z, "pc-stop", "stop")}${line(-m.stop_z, "pc-stop", "stop")}
    <polyline class="pc-z" points="${poly}"/>${marks}${live}
    <text x="${pad}" y="${H - 4}">${escapeHtml(pts[0].date)}</text><text x="${W - pad - 60}" y="${H - 4}">${escapeHtml(pts[pts.length - 1].date)}</text>
  </svg>
  <p class="muted">The spread ${escapeHtml(m.first)} − ${num(m.hedge, 2)} × ${escapeHtml(m.second)} (in log prices), in standard deviations from its
    ${m.lookback}-session average. Half-life ${num(m.half_life, 1)} sessions · Engle–Granger ${num(m.adf_stat, 2)} · correlation ${num(m.correlation, 2)}.</p>`;
}
