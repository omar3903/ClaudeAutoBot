/* The bottom panel: open positions, active orders (see orders.js), trade history,
   P/L summary, the watchlist, and the trade-record drawer. */
import {
  $, $$, SECTOR_SHORT, VENUE_SHORT, api, escapeHtml, fmtTime, markStale, num, parseDate, pct, plural, positionList, post,
  sectorTag, sideBadge, tfLabel, usd, fmtDay, fmtClock, fmtWhen,
} from "./util.js";
import { S, on, refreshState, serverNow } from "./state.js";
import { closeDrawer, drawerOpen, openDrawer, openModal, toast, toastResult } from "./ui.js";
import { stratLabel } from "./strategies.js";
import { loadWatchlist } from "./watchlist.js";
import { loadOrders } from "./orders.js";
import { loadPairs } from "./pairs.js";
import { watchPrice } from "./price.js";

const LOADERS = {
  open: loadOpen, orders: () => loadOrders(true), history: loadHistory, stats: loadStats, watchlist: loadWatchlist,
  pairs: loadPairs,
};

export const tabVisible = name => !$("#tab-" + name).classList.contains("hidden");
const loadedAt = {};           // tab -> when it last drew from a good answer, for the "couldn't refresh" line
const isLive = () => S.state.mode === "live";
const hereVenue = () => (S.state.venue || {}).trading_on || "paper";

/* ---------- open positions ---------- */
export async function loadOpen() {
  if (S.stopped) return;
  let trades;
  try { ({ trades } = await api("/api/trades?status=OPEN")); } catch (e) { markStale($("#tab-open"), loadedAt.open, e); return; }
  loadedAt.open = new Date();
  $("#tab-open-count").textContent = trades.length ? ` · ${trades.length}` : "";   // as Active orders counts its own
  const here = hereVenue(), el = $("#tab-open");
  $("#btn-exit-all").classList.toggle("hidden", !trades.some(t => (t.broker || "paper") === here));
  if (!trades.length) { el.innerHTML = `<p class="muted pad">No open positions.</p>` + untrackedHTML(); wireUntracked(el); return; }
  el.innerHTML = exposureHTML(trades) +
    `<table><thead><tr><th data-term="symbol">Symbol</th><th data-term="side">Side</th><th data-term="tf">Type</th><th data-term="strategy_col">Strategy</th>
    <th title="The order that opened the position">Order in</th>
    <th class="num" data-term="qty">Qty</th><th class="num" data-term="entry">Entry</th><th class="num" data-term="mark">Mark</th>
    <th class="num" data-term="unrealized">Unrealized</th><th class="num" data-term="r_now">R&nbsp;now</th>
    <th class="num" data-term="stop">Stop</th><th class="num" data-term="target">Target</th><th data-term="protection">Protection</th>
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
      <p class="muted">The open-trade records${r.recorded ? ` (${num(r.recorded, 0)} shares)` : ""} are left alone.</p>
      <p class="muted">Last trade <span data-price></span></p>`,
    okText: "Exit shares", okClass: "danger",
    onOk: async () => { toastResult(await post(`/api/positions/untracked/${encodeURIComponent(symbol)}/close`)); loadOpen(); refreshState(); },
  });
  watchPrice($("#modal-body [data-price]"), symbol);      // roughly what the market order will get
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

/* What stands ready to close a position, as a chip. Each open trade comes with its `protection`
   (Engine.open_positions): the stop and target orders the executor placed at the broker and follows - the
   ones the exit manager counts on - and whether this venue rests them at all. Green: they rest at the
   broker, working on its prices even while the app is closed. Amber: the app watches the price and sends
   the exit itself, only while it runs (the simulator). Red: the venue rests stops but this position has
   none yet. A day trade adds its time stop. */
function protectionCell(t, parked) {
  if (t.pair_id) return `<span class="muted small">the pair desk</span>`;
  if (parked) return `<span class="muted small">paused</span>`;
  const exiting = ((S.orders || {}).orders || []).find(o => o.purpose === "exit" && o.trade_id === t.id);
  if (exiting) return `<span class="badge warn" title="An exit order is working at the broker">exit working · ${escapeHtml(exiting.reason || exiting.order_type || "")}</span>`;
  const { native, stop, target } = t.protection || {};
  const chip = stop
    ? `<span class="badge good" title="${escapeHtml(`A good-till-cancelled stop order rests at the broker at ${num(stop.stop_price)} for ${plural(stop.qty, "share")}`
      + (target ? `, and a limit order at the target, ${num(target.limit_price)} for ${plural(target.qty, "share")}, in one group with it: when one fills the broker shrinks the other, so they can never both fill for the whole position. Both work even while the app is closed.`
        : ` - it protects the position even while the app is closed. The app works the target itself.`))}">${target ? "stop + target" : "stop"} at the broker</span>`
    : native
      ? `<span class="badge bad" title="No stop order rests at the broker for this position: the app watches the price and sends the exit itself, but only while it's running. It places one as soon as it safely can - an order is never rested for shares the broker doesn't show. The log says why it hasn't">no stop at the broker yet</span>`
      : `<span class="badge warn" title="No stop order rests at the broker: the app watches the price and sends the exit itself, while it's running">watched by the app</span>`;
  const late = timeStop(t);
  return chip + (late ? `<br>${late}` : "");
}

/* A day trade's time stop (exit_manager.intraday_time_stop): once its setup's window has passed - its
   overwatch_at - the exit manager closes it unless it's working, its stop at break-even or better; and every
   day trade is flat before the close. Neither applies with Auto exit off. The minutes are counted on the
   server's clock each time the tab redraws (every snapshot), which is often enough for whole minutes. */
const TIME_STOP_WARN_MIN = 5;                          // amber in its last 5 minutes

function timeStop(t) {
  if (t.timeframe !== "INTRADAY" || !t.managed_exit) return "";
  const rules = S.state.exit_manager || {}, at = t.overwatch_at ? parseDate(t.overwatch_at).getTime() : NaN;
  // the flatten comes first when the window runs to the close
  const flatAt = Date.parse((S.state.market || {}).regular_close || "") - (rules.flatten_intraday_before_close_min || 0) * 6e4;
  if (!rules.intraday_time_stop || isNaN(at) || at >= flatAt) return `<span class="muted small">flat before the close</span>`;
  const stop = t.stop_price || t.initial_stop_price;
  if (stop && (t.side === "SHORT" ? stop <= t.entry_price : stop >= t.entry_price))
    return `<span class="muted small" title="Its stop is at break-even or better, so its setup's window passing doesn't close it: it keeps its trailing stop until the flatten">working · flat before the close</span>`;
  const min = Math.ceil((at - serverNow()) / 6e4);
  const why = escapeHtml(`Its setup's window ends at ${fmtClock(t.overwatch_at)}: the exit manager then closes it unless it's working - its stop at break-even or better (exit_manager.intraday_time_stop). Every day trade is flat before the close.`);
  return min > 0
    ? `<span class="countdown small ${min <= TIME_STOP_WARN_MIN ? "warn" : ""}" title="${why}">out in ${min} min unless working</span>`
    : `<span class="countdown small warn" title="${why}">window passed · closing</span>`;
}

/* Where the trade stands at the mark in R, the risk it was opened with: (mark − entry) ÷ (entry − the original
   stop), as the exit manager measures it for the break-even and the trail. None for a pair leg (its stop is
   the pair's), a parked position or one with no mark. */
function rNow(t, pos, parked) {
  const first = t.initial_stop_price || t.stop_price, risk = first ? Math.abs(t.entry_price - first) : 0;
  if (t.pair_id || parked || pos.market_price == null || !risk) return null;
  return { r: (pos.market_price - t.entry_price) * (t.side === "SHORT" ? -1 : 1) / risk, first, risk };
}

function openRow(t, here) {
  const venue = t.broker || "paper", parked = venue !== here;
  const pos = (S.state.positions || []).find(x => x.symbol === t.symbol) || {};
  // this record's own open P/L - the broker's figure covers every share of the stock, recorded or not
  const upl = parked || pos.market_price == null ? null
    : (pos.market_price - t.entry_price) * Math.abs(t.quantity) * (t.side === "SHORT" ? -1 : 1);
  const moved = t.initial_stop_price != null && Math.abs((t.stop_price ?? 0) - t.initial_stop_price) > 0.01;
  const stopCell = moved ? `<span title="moved from ${num(t.initial_stop_price)}">${num(t.stop_price)} ▲</span>` : num(t.stop_price);
  const rn = rNow(t, pos, parked), rules = S.state.exit_manager || {};
  const rCell = !rn ? `<td class="num muted">–</td>`
    : `<td class="num ${rn.r >= 0 ? "pl-pos" : "pl-neg"}" title="${escapeHtml(`(mark − entry) ÷ ${num(rn.risk)} a share, the risk it was opened with (entry to the original stop, ${num(rn.first)})`
      + (rules.breakeven_at_r ? `. At +${rules.breakeven_at_r}R the stop moves to lock a small profit` : "")
      + (rules.trail_start_r ? `; from +${rules.trail_start_r}R it trails` : ""))}">${rn.r >= 0 ? "+" : ""}${rn.r.toFixed(2)}R</td>`;
  const status = t.time_status || "on_track";
  const barCls = status === "overdue" ? "bad" : status === "aging" ? "warn" : "ok";
  // when the setup usually exits, when it is due a look, and - for a swing trade - the day the time stop closes it
  const swing = t.timeframe === "SWING", when = swing ? fmtDay : fmtClock;
  const maxDays = (S.state.exit_manager || {}).max_swing_hold_days;
  const last = swing && maxDays && t.entry_time ? new Date(new Date(/[zZ]|[+-]\d\d:\d\d$/.test(t.entry_time) ? t.entry_time : t.entry_time + "Z").getTime() + maxDays * 864e5) : null;
  const usual = t.expected_exit_at ? new Date(/[zZ]|[+-]\d\d:\d\d$/.test(t.expected_exit_at) ? t.expected_exit_at : t.expected_exit_at + "Z") : null;
  const byTimeStop = !!(last && usual && last < usual);           // the time stop comes before the setup's usual exit
  const expectTitle = [t.expected_exit_at ? `This setup usually exits by ${when(t.expected_exit_at)}` : "",
    t.overwatch_at ? `it is flagged for a look after ${when(t.overwatch_at)}` : "",
    last ? `the time stop closes it on ${fmtDay(last.toISOString())} at the latest (${maxDays} days)` : (swing ? "" : "day trades are flat before the close")]
    .filter(Boolean).join("; ");
  const timeCell = `<div class="timecell" title="${escapeHtml(expectTitle)}">
    <span>${t.held_label || "–"}</span>
    ${t.expected_exit_at ? `<span class="muted small">exit by ${byTimeStop ? fmtDay(last.toISOString()) + " (time stop)" : when(t.expected_exit_at)}</span>` : ""}
    <span class="timebar"><i class="${barCls}" style="width:${Math.min(100, t.time_used_pct ?? 0)}%"></i></span>
    ${status === "overdue" ? '<span class="badge bad">⏰ overdue</span>' : status === "aging" ? '<span class="badge warn">aging</span>' : ""}</div>`;
  const parkedTag = parked
    ? ` <span class="badge warn" title="Opened on another platform. Its automatic exits pause until you switch back to it.">on ${VENUE_SHORT[venue] || escapeHtml(venue)}</span>` : "";
  return `<tr class="clickable-row ${status === "overdue" ? "row-overdue" : ""}" data-record="${escapeHtml(t.id)}">
    <td class="sym">${escapeHtml(t.symbol)} ${sectorTag(t.sector)}${parkedTag}${t.pair_id
      ? ' <span class="badge" title="One leg of a pair trade - the pair desk closes both legs together (Pairs tab)">pair</span>' : ""}</td><td>${sideBadge(t.side)}</td>
    <td class="tf">${t.timeframe ? tfLabel(t.timeframe) : "–"}</td>
    <td>${stratLabel(t.strategy)}</td>
    <td class="muted small">${t.order_type || "—"}${t.order_session === "EXTENDED" ? " · ext" : ""}</td>
    <td class="num">${num(t.quantity, 0)}${t.initial_quantity && Math.abs(t.initial_quantity - t.quantity) > 1e-9
      ? ` <span class="muted" title="part of the position was taken off at the first target">of ${num(t.initial_quantity, 0)}</span>` : ""}</td>
    <td class="num">${num(t.entry_price)}</td>
    <td class="num"${pos.price_at ? ` title="As of ${escapeHtml(fmtWhen(pos.price_at))} - the app's own latest price, pre-market and after-hours included (the exits act on regular-hours prices)"` : ""}>${parked ? "–" : num(pos.market_price)}</td>
    <td class="num ${upl >= 0 ? "pl-pos" : "pl-neg"}">${usd(upl)}${t.banked_pl
      ? ` <span class="muted" title="realized on the part already taken off">+${usd(t.banked_pl)} banked</span>` : ""}</td>
    ${rCell}
    <td class="num">${stopCell}</td>
    <td class="num">${num(t.target_price)}${t.target2_price
      ? ` <span class="muted" title="part comes off at the first target, the rest runs to the second">→ ${num(t.target2_price)}</span>` : ""}</td>
    <td class="protection">${protectionCell(t, parked)}</td>
    <td>${timeCell}</td>
    <td class="num muted">${usd(t.mfe)} / ${usd(t.mae == null ? null : -t.mae)}</td>
    <td class="no-row-click"><label class="switch lockable"><input type="checkbox" data-managed="${escapeHtml(t.id)}" ${t.managed_exit ? "checked" : ""} ${t.pair_id ? "disabled" : ""}><span></span></label></td>
    <td class="no-row-click"><button class="danger mini" data-exit="${escapeHtml(t.id)}" ${parked
      ? `disabled title="Switch back to ${VENUE_SHORT[venue] || escapeHtml(venue)} to exit this"`
      : exitsSending.has(t.id) ? `disabled title="${EXIT_SENDING_TIP}"`
        : 'title="Exit this position at the market"'}>${exitsSending.has(t.id) ? "Sending…" : "Exit"}</button></td></tr>`;
}

/* Exits sent from here and not answered yet, by trade id: the position's Exit buttons say so and stay off until
   the answer - the table redraws on every snapshot meanwhile, and keeps them so. A second click would only be
   refused (an exit already working), but it shouldn't look possible. */
const exitsSending = new Set();
const EXIT_SENDING_TIP = "The exit has gone to the app - waiting for its answer";

function markExit(id, sending) {
  if (sending) exitsSending.add(id); else exitsSending.delete(id);
  $$(`[data-exit="${CSS.escape(id)}"]`).forEach(b => {
    b.disabled = sending;
    b.textContent = sending ? "Sending…" : "Exit";
    b.title = sending ? EXIT_SENDING_TIP : "Exit this position at the market";
  });
  const rec = $("#rec-exit");
  if (rec && S.recordId === id) { rec.disabled = sending; rec.textContent = sending ? "Sending…" : "Exit position"; }
}

function confirmExit(t, fromRecord = false) {
  if (!t || exitsSending.has(t.id)) return;
  const venue = (S.state.venue || {}).trading_on_label || "your broker";
  openModal({
    title: `Exit ${t.symbol}?`,
    bodyHTML: `${isLive() ? `<div class="warn-box">This sends a <b>real market order</b> to ${escapeHtml(venue)}.</div>` : ""}
      <p>${t.side === "SHORT" ? "Buy back" : "Sell"} ${num(Math.abs(t.quantity), 0)} ${escapeHtml(t.symbol)} at the market and close the position.</p>
      <p class="muted">Entered at ${num(t.entry_price)} · stop ${num(t.stop_price)} · target ${num(t.target_price)}.</p>
      <p class="muted">Last trade <span data-price></span></p>`,
    okText: "Exit position", okClass: "danger",
    onOk: async () => {
      markExit(t.id, true);
      const r = await post(`/api/trades/${t.id}/close`);
      markExit(t.id, false);
      if (r.ok) toast(`Exit sent for ${t.symbol}${r.status && r.status !== "FILLED" ? ` (${r.status.toLowerCase()})` : ""}`, "good");
      else toast(`Exit for ${t.symbol} failed: ${r.reason || ""}`, "bad");
      loadOpen(); refreshState();
      if (fromRecord && drawerOpen("record") && S.recordId === t.id) openRecord(t.id);
    },
  });
  watchPrice($("#modal-body [data-price]"), t.symbol);    // roughly what the market order will get
}

async function exitAll() {
  let trades;
  try { ({ trades } = await api("/api/trades?status=OPEN")); } catch (e) { toast(`Couldn't read the open positions - ${e.message}`, "bad"); return; }
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
  try { ({ trades } = await api("/api/trades?limit=200")); } catch (e) { markStale($("#tab-history"), loadedAt.history, e); return; }
  loadedAt.history = new Date();
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
  try { s = await api("/api/pnl"); } catch (e) { markStale($("#tab-stats"), loadedAt.stats, e); return; }
  loadedAt.stats = new Date();
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
  </div>` + byTypeHTML(s.by_type);
}

/* Closed trades by type: how many exited with a profit and how many with a loss. A pair counts once, both legs
   together. Open positions aren't counted. */
function byTypeHTML(bt) {
  if (!bt) return "";
  const rows = [["INTRADAY", "Day trades"], ["SWING", "Swing trades"], ["PAIRS", "Pairs"]].map(([key, label]) => {
    const r = bt[key] || {};
    return `<tr><td>${label}</td><td class="num">${r.closed || 0}</td><td class="num pl-pos">${r.profit || 0}</td>
      <td class="num pl-neg">${r.loss || 0}</td><td class="num">${r.even || 0}</td></tr>`;
  }).join("");
  return `<table><thead><tr><th>Closed trades by type</th><th class="num">Closed</th><th class="num">Profit</th>
    <th class="num">Loss</th><th class="num">Break-even</th></tr></thead><tbody>${rows}</tbody></table>`;
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
      ${open ? `<span>At the broker</span><span>${atBroker}</span><span>Market price</span><span data-price></span>`
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
    ${open ? `<div class="row-gap"><button class="danger" id="rec-exit" ${rec.on_current_venue && !exitsSending.has(t.id) ? "" : "disabled"}>${exitsSending.has(t.id) ? "Sending…" : "Exit position"}</button></div>
      <p class="muted small">If this position is closed or removed outside the app, or the paper account is reset, this open-trade record is
      deleted once the broker confirms the position is gone.</p>` : ""}`;
  const exitBtn = $("#rec-exit");
  if (exitBtn) exitBtn.onclick = () => confirmExit(t, true);
  if (open) watchPrice($("#drawer-body [data-price]"), t.symbol);   // while this record stays open
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
  on("orders", () => { if (tabVisible("open")) loadOpen(); });
  $$(".blotter .tab").forEach(tab => { tab.onclick = () => showTab(tab.dataset.tab); });
  $("#btn-exit-all").onclick = exitAll;
  on("state", untrackedChanged);
}
