/* The bottom panel: open positions, active orders (see orders.js), trade history,
   P/L summary, the watchlist, and the trade-record drawer. */
import {
  $, $$, SECTOR_SHORT, VENUE_SHORT, api, escapeHtml, fmtTime, num, pct, plural, positionList, post,
  sectorTag, sideBadge, tfLabel, usd,
} from "./util.js";
import { S, on, refreshState } from "./state.js";
import { closeDrawer, drawerOpen, openDrawer, openModal, toast, toastResult } from "./ui.js";
import { stratLabel } from "./strategies.js";
import { loadWatchlist } from "./watchlist.js";
import { loadOrders } from "./orders.js";
import { loadPairs } from "./pairs.js";

const LOADERS = {
  open: loadOpen, orders: () => loadOrders(true), history: loadHistory, stats: loadStats, watchlist: loadWatchlist,
  pairs: loadPairs,
};

export const tabVisible = name => !$("#tab-" + name).classList.contains("hidden");
const isLive = () => S.state.mode === "live";
const hereVenue = () => (S.state.venue || {}).trading_on || "paper";

/* ---------- open positions ---------- */
export async function loadOpen() {
  if (S.stopped) return;
  let trades;
  try { ({ trades } = await api("/api/trades?status=OPEN")); } catch { return; }
  const here = hereVenue(), el = $("#tab-open");
  $("#btn-exit-all").classList.toggle("hidden", !trades.some(t => (t.broker || "paper") === here));
  if (!trades.length) { el.innerHTML = `<p class="muted pad">No open positions.</p>` + untrackedHTML(); wireUntracked(el); return; }
  el.innerHTML = exposureHTML(trades) +
    `<table><thead><tr><th data-term="symbol">Symbol</th><th data-term="side">Side</th><th data-term="strategy_col">Strategy</th><th>Order</th>
    <th class="num" data-term="qty">Qty</th><th class="num" data-term="entry">Entry</th><th class="num" data-term="mark">Mark</th>
    <th class="num" data-term="unrealized">Unrealized</th><th class="num" data-term="stop">Stop</th><th class="num" data-term="target">Target</th>
    <th data-term="age">Age / Expected</th><th class="num" data-term="mfe">MFE / MAE</th>
    <th data-term="auto_exit">Auto&nbsp;exit</th><th></th></tr></thead><tbody>${trades.map(t => openRow(t, here)).join("")}</tbody></table>` +
    untrackedHTML();
  wireUntracked(el);

  const byId = Object.fromEntries(trades.map(t => [t.id, t]));
  $$("[data-exit]", el).forEach(b => { b.onclick = () => confirmExit(byId[b.dataset.exit]); });
  $$("[data-managed]", el).forEach(c => {
    c.onchange = async () => {
      const r = await post(`/api/trades/${c.dataset.managed}/managed`, { on: c.checked });
      if (!r.ok) { c.checked = !c.checked; toast("Not changed: " + (r.reason || ""), "bad"); return; }
      toast(`Auto-exit ${c.checked ? "ON" : "OFF"} for that position`, c.checked ? "good" : "warn");
    };
  });
  $$("tr[data-record]", el).forEach(tr => {
    tr.onclick = e => { if (!e.target.closest(".no-row-click")) openRecord(tr.dataset.record); };
  });
}

/* ---------- shares held at the broker with no open-trade record behind them ---------- */
let untrackedKey = "";

function untrackedHTML() {
  const rows = S.state.untracked || [];
  if (!rows.length) return "";
  const venue = (S.state.venue || {}).trading_on_label || "the broker";
  return `<div class="untracked"><h4 data-term="untracked">Shares without a record (${rows.length})</h4>
    <p class="muted small">${escapeHtml(venue)} holds these beyond what the open-trade records cover: opened or changed outside
      the app, or a fill it couldn't book. The app doesn't manage their exits. <b>Exit</b> closes them at the market.</p>
    <table><thead><tr><th data-term="symbol">Symbol</th><th data-term="side">Side</th><th class="num" data-term="qty">Qty</th>
      <th class="num">Recorded</th><th class="num">Avg price</th><th class="num" data-term="mark">Mark</th>
      <th class="num" data-term="unrealized">Unrealized</th><th></th></tr></thead><tbody>${rows.map(r => `<tr>
      <td class="sym">${escapeHtml(r.symbol)}</td><td>${sideBadge(r.side)}</td><td class="num">${num(r.qty, 0)}</td>
      <td class="num muted" title="shares the open-trade records cover, of all the shares held">${num(Math.abs(r.recorded), 0)} of ${num(Math.abs(r.held), 0)}</td><td class="num">${num(r.avg_price)}</td>
      <td class="num">${num(r.market_price)}</td>
      <td class="num ${(r.unrealized_pl || 0) >= 0 ? "pl-pos" : "pl-neg"}">${usd(r.unrealized_pl)}</td>
      <td><button class="danger mini" data-untracked="${escapeHtml(r.symbol)}" title="Close these shares at the market">Exit</button></td></tr>`).join("")}
    </tbody></table></div>`;
}

function wireUntracked(el) {
  $$("[data-untracked]", el).forEach(b => { b.onclick = () => confirmUntrackedExit(b.dataset.untracked); });
}

function confirmUntrackedExit(symbol) {
  const r = (S.state.untracked || []).find(x => x.symbol === symbol);
  if (!r) return;
  const venue = (S.state.venue || {}).trading_on_label || "your broker";
  openModal({
    title: `Exit ${symbol} (no record)?`,
    bodyHTML: `${isLive() ? `<div class="warn-box">This sends a <b>real market order</b> to ${escapeHtml(venue)}.</div>` : ""}
      <p>${r.side === "SHORT" ? "Buy back" : "Sell"} ${num(r.qty, 0)} ${escapeHtml(symbol)} at the market - the shares the app has no record for.</p>
      <p class="muted">The open-trade records${r.recorded ? ` (${num(r.recorded, 0)} shares)` : ""} are left alone.</p>`,
    okText: "Exit shares", okClass: "danger",
    onOk: async () => { toastResult(await post(`/api/positions/untracked/${encodeURIComponent(symbol)}/close`)); loadOpen(); refreshState(); },
  });
}

export function untrackedChanged() {
  const key = JSON.stringify((S.state.untracked || []).map(r => [r.symbol, r.qty]));
  if (key === untrackedKey) return;
  untrackedKey = key;
  if (tabVisible("open")) loadOpen();
}

function exposureHTML(trades) {
  // sector concentration of the open book
  const bySector = {};
  let total = 0;
  trades.forEach(t => {
    const notional = Math.abs((t.quantity || 0) * (t.entry_price || 0));
    bySector[t.sector || "Unknown"] = (bySector[t.sector || "Unknown"] || 0) + notional;
    total += notional;
  });
  if (!total) return "";
  const parts = Object.entries(bySector).sort((a, b) => b[1] - a[1])
    .map(([s, v]) => `<b>${escapeHtml(SECTOR_SHORT[s] || s)}</b> ${Math.round(v / total * 100)}%`).join(" · ");
  return `<div class="exposure">Exposure: ${parts}</div>`;
}

function openRow(t, here) {
  const venue = t.broker || "paper", parked = venue !== here;
  const pos = (S.state.positions || []).find(x => x.symbol === t.symbol) || {};
  // this record's own open P/L - the broker's figure covers every share of the stock, recorded or not
  const upl = parked || pos.market_price == null ? null
    : (pos.market_price - t.entry_price) * Math.abs(t.quantity) * (t.side === "SHORT" ? -1 : 1);
  const moved = t.initial_stop_price != null && Math.abs((t.stop_price ?? 0) - t.initial_stop_price) > 0.01;
  const stopCell = moved ? `<span title="moved from ${num(t.initial_stop_price)}">${num(t.stop_price)} ▲</span>` : num(t.stop_price);
  const status = t.time_status || "on_track";
  const barCls = status === "overdue" ? "bad" : status === "aging" ? "warn" : "ok";
  const timeCell = `<div class="timecell">
    <span>${t.held_label || "–"}</span>
    <span class="timebar"><i class="${barCls}" style="width:${Math.min(100, t.time_used_pct ?? 0)}%"></i></span>
    ${status === "overdue" ? '<span class="badge bad">⏰ overdue</span>' : status === "aging" ? '<span class="badge warn">aging</span>' : ""}</div>`;
  const parkedTag = parked
    ? ` <span class="badge warn" title="Opened on another platform. Its automatic exits pause until you switch back to it.">on ${VENUE_SHORT[venue] || escapeHtml(venue)}</span>` : "";
  return `<tr class="clickable-row ${status === "overdue" ? "row-overdue" : ""}" data-record="${escapeHtml(t.id)}">
    <td class="sym">${escapeHtml(t.symbol)} ${sectorTag(t.sector)}${parkedTag}${t.pair_id
      ? ' <span class="badge" title="One leg of a pair trade - the pair desk closes both legs together (Pairs tab)">pair</span>' : ""}</td><td>${sideBadge(t.side)}</td>
    <td>${stratLabel(t.strategy)}</td>
    <td class="muted">${t.order_type || "—"}${t.order_session === "EXTENDED" ? " · ext" : ""}</td>
    <td class="num">${num(t.quantity, 0)}</td>
    <td class="num">${num(t.entry_price)}</td>
    <td class="num">${parked ? "–" : num(pos.market_price)}</td>
    <td class="num ${upl >= 0 ? "pl-pos" : "pl-neg"}">${usd(upl)}</td>
    <td class="num">${stopCell}</td>
    <td class="num">${num(t.target_price)}</td>
    <td>${timeCell}</td>
    <td class="num muted">${usd(t.mfe)} / ${usd(t.mae == null ? null : -t.mae)}</td>
    <td class="no-row-click"><label class="switch lockable"><input type="checkbox" data-managed="${escapeHtml(t.id)}" ${t.managed_exit ? "checked" : ""} ${t.pair_id ? "disabled" : ""}><span></span></label></td>
    <td class="no-row-click"><button class="danger mini" data-exit="${escapeHtml(t.id)}" ${parked
      ? `disabled title="Switch back to ${VENUE_SHORT[venue] || escapeHtml(venue)} to exit this"` : 'title="Exit this position at the market"'}>Exit</button></td></tr>`;
}

function confirmExit(t, fromRecord = false) {
  if (!t) return;
  const venue = (S.state.venue || {}).trading_on_label || "your broker";
  openModal({
    title: `Exit ${t.symbol}?`,
    bodyHTML: `${isLive() ? `<div class="warn-box">This sends a <b>real market order</b> to ${escapeHtml(venue)}.</div>` : ""}
      <p>${t.side === "SHORT" ? "Buy back" : "Sell"} ${num(Math.abs(t.quantity), 0)} ${escapeHtml(t.symbol)} at the market and close the position.</p>
      <p class="muted">Entered at ${num(t.entry_price)} · stop ${num(t.stop_price)} · target ${num(t.target_price)}.</p>`,
    okText: "Exit position", okClass: "danger",
    onOk: async () => {
      const r = await post(`/api/trades/${t.id}/close`);
      if (r.ok) toast(`Exit sent for ${t.symbol}${r.status && r.status !== "FILLED" ? ` (${r.status.toLowerCase()})` : ""}`, "good");
      else toast(`Exit for ${t.symbol} failed: ${r.reason || ""}`, "bad");
      loadOpen(); refreshState();
      if (fromRecord && drawerOpen("record") && S.recordId === t.id) openRecord(t.id);
    },
  });
}

async function exitAll() {
  let trades;
  try { ({ trades } = await api("/api/trades?status=OPEN")); } catch { toast("The app isn't reachable", "bad"); return; }
  const mine = trades.filter(t => (t.broker || "paper") === hereVenue());
  if (!mine.length) { toast("No open positions to exit", "warn"); return; }
  const venue = (S.state.venue || {}).trading_on_label || "your broker";
  openModal({
    title: `Exit all ${plural(mine.length, "position")}?`,
    bodyHTML: `${isLive() ? `<div class="warn-box">This sends <b>real market orders</b> to ${escapeHtml(venue)}.</div>` : ""}
      ${positionList(mine)}<p class="muted">Every close is sent at once, at the market.</p>`,
    okText: "Exit all", okClass: "danger",
    onOk: async () => { toastResult(await post("/api/trades/close-all")); loadOpen(); refreshState(); },
  });
}

/* ---------- history and P/L ---------- */
export async function loadHistory() {
  if (S.stopped) return;
  let trades;
  try { ({ trades } = await api("/api/trades?limit=200")); } catch { return; }
  const closed = trades.filter(t => t.status === "CLOSED");
  const el = $("#tab-history");
  if (!closed.length) { el.innerHTML = `<p class="muted pad">No closed trades yet.</p>`; return; }
  el.innerHTML = `<table><thead><tr><th>Closed</th><th>Symbol</th><th>Side</th><th>Strategy</th>
    <th class="num">Entry</th><th class="num">Exit</th><th class="num">P/L</th><th class="num">P/L %</th>
    <th class="num">R</th><th>DT</th><th>Reason</th></tr></thead><tbody>${
    closed.map(t => `<tr class="clickable-row" data-record="${escapeHtml(t.id)}">
      <td>${(t.exit_time || "").slice(5, 16).replace("T", " ")}</td>
      <td class="sym">${escapeHtml(t.symbol)}</td><td>${sideBadge(t.side)}</td>
      <td>${stratLabel(t.strategy)}</td>
      <td class="num">${num(t.entry_price)}</td><td class="num">${num(t.exit_price)}</td>
      <td class="num ${t.realized_pl >= 0 ? "pl-pos" : "pl-neg"}">${usd(t.realized_pl)}</td>
      <td class="num ${t.realized_pl >= 0 ? "pl-pos" : "pl-neg"}">${pct(t.realized_pl_pct)}</td>
      <td class="num">${num(t.r_multiple, 2)}</td>
      <td>${t.is_day_trade ? "•" : ""}</td>
      <td class="muted">${escapeHtml(t.exit_reason || "")}</td></tr>`).join("")}</tbody></table>`;
  $$("tr[data-record]", el).forEach(tr => { tr.onclick = () => openRecord(tr.dataset.record); });
}

export async function loadStats() {
  if (S.stopped) return;
  let s;
  try { s = await api("/api/pnl"); } catch { return; }
  const stat = (label, value, cls) => `<div class="stat"><label>${label}</label><b class="${cls || ""}">${value}</b></div>`;
  const sign = v => v >= 0 ? "pl-pos" : "pl-neg";
  $("#tab-stats").innerHTML = `<div class="stat-grid">
    ${stat("Realized today", usd(s.realized_today), sign(s.realized_today))}
    ${stat("Realized week", usd(s.realized_week), sign(s.realized_week))}
    ${stat("Realized total", usd(s.realized_total), sign(s.realized_total))}
    ${stat("Closed trades", s.n_closed)}
    ${stat("Win rate", num(s.win_rate, 1) + "%")}
    ${stat("Avg win", usd(s.avg_win), "pl-pos")}
    ${stat("Avg loss", usd(s.avg_loss), "pl-neg")}
    ${stat("Profit factor", s.profit_factor ?? "–")}
    ${stat("Expectancy / trade", usd(s.expectancy))}
    ${stat("Best", usd(s.best), "pl-pos")}
    ${stat("Worst", usd(s.worst), "pl-neg")}
  </div>`;
}

/* ---------- trade record (open or closed) ---------- */
export async function openRecord(id) {
  if (!id) return;
  if (!drawerOpen("record")) openDrawer("record", "Trade record", `<p class="muted">Loading…</p>`);
  S.recordId = id;
  let rec;
  try {
    const res = await fetch(`/api/trades/${encodeURIComponent(id)}/record`);
    rec = await res.json();
    if (!res.ok) throw new Error(rec.detail || "Trade record not found.");
  } catch (e) {
    if (S.recordId === id) $("#drawer-body").innerHTML = `<p class="reasons">${escapeHtml(e.message || "Couldn't load the record.")}</p>`;
    return;
  }
  if (drawerOpen("record") && S.recordId === id) renderRecord(rec);
}

function orderSummary(req) {
  if (!req || typeof req !== "object") return "";
  const parts = [req.side, req.qty, req.symbol, req.type].filter(v => v != null && v !== "");
  if (req.limit != null) parts.push(`@ ${num(req.limit)}`);
  if (req.stop != null) parts.push(`stop ${num(req.stop)}`);
  return parts.join(" ");
}

function renderRecord(rec) {
  const t = rec.trade, p = rec.play || {}, bp = rec.broker_position, open = t.status === "OPEN";
  const moved = t.initial_stop_price != null && Math.abs((t.stop_price ?? 0) - t.initial_stop_price) > 0.01;
  const fills = rec.fills || [], orders = rec.orders || [];
  const atBroker = bp ? `${num(bp.quantity, 0)} @ ${num(bp.market_price)} · <span class="${bp.unrealized_pl >= 0 ? "pl-pos" : "pl-neg"}">${usd(bp.unrealized_pl)}</span>`
    : rec.on_current_venue ? "not reported yet" : `held on ${escapeHtml(rec.venue_label)} — switch to it to manage`;
  $("#drawer-title").textContent = `${t.symbol} · ${open ? "open" : "closed"} trade`;
  $("#drawer-body").innerHTML = `
    <div class="sub">${sideBadge(t.side)} ${t.timeframe ? tfLabel(t.timeframe) + " ·" : ""} ${stratLabel(t.strategy)} · ${escapeHtml(rec.venue_label)} ${sectorTag(t.sector)}</div>
    <div class="kv">
      <span>Status</span><span>${open ? "OPEN" : `CLOSED${t.exit_reason ? ` (${escapeHtml(t.exit_reason)})` : ""}`}</span>
      <span>Opened</span><span>${fmtTime(t.entry_time)}</span>
      ${open ? "" : `<span>Closed</span><span>${fmtTime(t.exit_time)}</span>`}
      <span>Quantity</span><span>${num(t.quantity, 0)}</span>
      <span>Entry</span><span>${num(t.entry_price)}</span>
      ${open ? "" : `<span>Exit</span><span>${num(t.exit_price)}</span>`}
      <span>Stop</span><span>${num(t.stop_price)}${moved ? ` (moved from ${num(t.initial_stop_price)})` : ""}</span>
      <span>Target</span><span>${num(t.target_price)}</span>
      ${open ? `<span>At the broker</span><span>${atBroker}</span>`
      : `<span>P/L</span><span class="${t.realized_pl >= 0 ? "pl-pos" : "pl-neg"}">${usd(t.realized_pl)} (${pct(t.realized_pl_pct)}) · ${num(t.r_multiple, 2)}R</span>`}
      <span>Auto exit</span><span>${t.managed_exit ? "on" : "off"}</span>
      <span>Trade id</span><span><code>${escapeHtml(t.id)}</code></span>
    </div>
    ${p.explanation || p.rationale ? `<h4>Why it was taken</h4><div class="explain">${escapeHtml(p.explanation || p.rationale)}</div>` : ""}
    <h4>Fills</h4>
    ${fills.length ? `<div class="table-wrap"><table class="rec-table"><thead><tr><th>Time</th><th>Leg</th><th>Side</th><th class="num">Qty</th><th class="num">Price</th></tr></thead><tbody>${
      fills.map(f => `<tr><td>${fmtTime(f.ts)}</td><td>${escapeHtml(f.leg)}</td><td>${escapeHtml(f.side)}</td>
        <td class="num">${num(f.quantity, 0)}</td><td class="num">${num(f.price)}</td></tr>`).join("")}</tbody></table></div>`
      : '<p class="muted">No fills stored.</p>'}
    <h4>Orders sent</h4>
    ${orders.length ? `<div class="table-wrap"><table class="rec-table"><thead><tr><th>Time</th><th>Action</th><th>Order</th><th>Result</th></tr></thead><tbody>${
      orders.map(o => `<tr><td>${fmtTime(o.ts)}</td><td>${escapeHtml(o.action)}</td><td>${escapeHtml(orderSummary(o.request))}</td>
        <td><span class="badge ${o.ok ? "good" : "bad"}">${o.ok ? "ok" : "failed"}</span> ${escapeHtml(o.message || "")}</td></tr>`).join("")}</tbody></table></div>`
      : '<p class="muted">No broker orders stored.</p>'}
    ${open ? `<div class="row-gap"><button class="danger" id="rec-exit" ${rec.on_current_venue ? "" : "disabled"}>Exit position</button></div>
      <p class="muted small">If this position is closed or removed outside the app, or the paper account is reset, this open-trade record is
      deleted once the broker confirms the position is gone.</p>` : ""}`;
  const exitBtn = $("#rec-exit");
  if (exitBtn) exitBtn.onclick = () => confirmExit(t, true);
}

export function recordGone(ids) {
  if (drawerOpen("record") && ids.includes(S.recordId)) closeDrawer();
}

export function showTab(name) {
  $$(".blotter .tab").forEach(x => x.classList.toggle("active", x.dataset.tab === name));
  Object.keys(LOADERS).forEach(key => $("#tab-" + key).classList.toggle("hidden", key !== name));
  LOADERS[name]();
}

export function initBlotter() {
  $$(".blotter .tab").forEach(tab => { tab.onclick = () => showTab(tab.dataset.tab); });
  $("#btn-exit-all").onclick = exitAll;
  on("state", untrackedChanged);
}
