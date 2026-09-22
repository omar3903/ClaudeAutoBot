/* Active orders: every order still working at the broker and what it is for, and the
   ⏳ marker on plays whose stock has one - with a working entry's shares filled and its
   countdowns. The engine re-checks the broker every few seconds and pushes orders.updated
   when anything changed. */
import { $, $$, api, count, escapeHtml, fmtClock, markStale, num, post, pretty } from "./util.js";
import { openModal, toastResult } from "./ui.js";
import { S, emit, serverNow } from "./state.js";
import { openRecord, tabVisible } from "./blotter.js";
import { selectPlay } from "./plays.js";
import { stratLabel } from "./strategies.js";

const PURPOSE = {
  entry: "entry", exit: "exit", stop: "bracket stop", target: "bracket target",
  app: "sent by the app", outside: "placed outside the app",
};
const STATUS = { SUBMITTED: "accepted, not live yet", PENDING: "sending", WORKING: "working", PARTIAL: "part filled" };

export const ordersFor = symbol => (S.orders.orders || []).filter(o => o.symbol === symbol);

/** One order on one line, for tooltips: "BUY 100 limit @ 25.00 · entry", "SELL 100 stop @ 23.00 · bracket stop". */
export function orderLine(o) {
  const prices = [o.limit_price != null ? `@ ${num(o.limit_price)}` : "",
    o.stop_price != null ? `${o.limit_price != null ? "stop" : "@"} ${num(o.stop_price)}` : ""];
  const t = timeLeft(o);
  return [o.action, count(o.remaining), pretty(o.order_type || "order").toLowerCase(), ...prices].filter(Boolean).join(" ")
    + ` · ${PURPOSE[o.purpose] || o.purpose}` + (o.filled ? ` · ${count(o.filled)} of ${count(o.qty)} filled` : "")
    + (t ? ` · ${t.words}` : "");
}

/** The marker a play gets when its stock has orders working - and, for its own entry, the shares
    filled of the order and the time left: "⏳ 0/100 · 6:12". */
export function orderMark(p) {
  const list = ordersFor(p.symbol), n = list.length;
  if (!n) return "";
  const entry = list.find(o => o.purpose === "entry" && o.play_id === p.id), clock = entry ? countdown(entry) : "";
  return `<span class="order-mark" data-term="active_order" data-symbol="${escapeHtml(p.symbol)}">⏳${n > 1 ? n : ""}`
    + (entry ? ` ${count(entry.filled || 0)}/${count(entry.qty)}${clock ? ` · ${clock}` : ""}` : "") + "</span>";
}

/* A working entry's countdowns, from the times the server sent with it (Executor._entry_clock): to a day
   trade's time-out (execution.entry_timeout_min) and, once part of it has filled, to the rest being cut
   (execution.partial_entry_wait_s) - whichever comes first. They count on the server's clock, and the
   server sends no event each second: one ticker redraws the [data-countdown] nodes, and stops once none
   is counting. Past the time, the app calls the order off on its next order check. */
const WARN_S = 120;                                   // amber in the last 2 minutes
let ticker = null;

function timeLeft(o) {
  if (o.purpose !== "entry") return null;
  if (o.calling_off) return { text: "cancelling", cls: "warn", words: `being cancelled: ${o.calling_off}`, live: false };
  const cut = Date.parse(o.cut_at || ""), out = Date.parse(o.expires_at || "");
  if (isNaN(cut) && isNaN(out)) return null;
  const cutting = !isNaN(cut) && (isNaN(out) || cut <= out), s = Math.ceil(((cutting ? cut : out) - serverNow()) / 1000);
  if (s <= 0) return { text: "cancelling", cls: "warn", words: "time is up - it is being cancelled", live: false };
  if (cutting) return { text: `cut in ${s} s`, cls: "warn", live: true,
    words: `the rest is cancelled in ${s} s if it hasn't filled, so the shares bought get their stop` };
  const mmss = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  return { text: mmss, cls: s <= WARN_S ? "warn" : "", live: true,
    words: `cancelled in ${mmss} if it hasn't filled - a day-trade entry isn't left to chase the price` };
}

/** The countdown node for a working entry ("6:12", "cut in 18 s", "cancelling"), or "" when it has none. */
function countdown(o) {
  const t = timeLeft(o);
  if (!t) return "";
  if (t.live && !ticker) ticker = setInterval(tickCountdowns, 1000);
  return `<span class="countdown ${t.cls}" data-countdown="${escapeHtml(o.order_id)}">${t.text}</span>`;
}

function tickCountdowns() {
  const byId = Object.fromEntries((S.orders.orders || []).map(o => [o.order_id, o]));
  let live = 0;
  $$("[data-countdown]").forEach(el => {
    const o = byId[el.dataset.countdown], t = o && timeLeft(o);
    if (!t) return;
    if (el.textContent !== t.text) el.textContent = t.text;
    el.className = `countdown ${t.cls}`;
    if (t.live) live++;
  });
  if (!live) { clearInterval(ticker); ticker = null; }
}

let ordersAt = null;           // when the list last came in, by a request or a push

export async function loadOrders(fresh = false) {
  if (S.stopped) return;
  try { S.orders = await api("/api/orders" + (fresh ? "?fresh=true" : "")); } catch (e) {
    // the tab keeps the last list it heard of - a push may have brought it while the tab was hidden
    if (tabVisible("orders")) { if (ordersAt) renderOrders(); markStale($("#tab-orders"), ordersAt, e); }
    return;
  }
  ordersChanged();
}

export function ordersChanged() {
  ordersAt = new Date();
  const n = (S.orders.orders || []).length;
  $("#tab-orders-count").textContent = n ? ` · ${n}` : "";
  if (tabVisible("orders")) renderOrders();
  emit("orders");
}

function renderOrders() {
  const d = S.orders, rows = d.orders || [], el = $("#tab-orders"), where = escapeHtml(d.venue_label || "the broker");
  const note = d.ok
    ? `<div class="exposure">Working at <b>${where}</b>${d.checked_at ? ` · checked ${fmtClock(d.checked_at)}` : ""}.
       Orders placed outside the app are listed, but the app never changes or cancels them.</div>`
    : `<div class="warn-box">Can't ask ${where} for its orders right now - it isn't connected.${rows.length ? " These are the last ones seen." : ""}</div>`;
  if (!rows.length) {
    el.innerHTML = d.ok ? `<p class="muted pad">No orders working at ${where}.</p>` : note;
    return;
  }
  el.innerHTML = note + `<p><button class="danger mini" id="orders-cancel-all" title="Cancel every working order - entries, the app's exits, anything else on the account. The stops protecting open positions stay: they go when their position is closed.">Cancel working orders</button></p>
    <table class="orders-table"><thead><tr><th data-term="symbol">Symbol</th><th>Side</th>
    <th data-term="order_for">For</th><th>Type</th><th class="num">Qty</th><th class="num">Filled</th>
    <th class="num">Limit</th><th class="num">Stop</th><th>Time in force</th><th data-term="order_status">Status</th>
    <th data-term="order_time_left">Time left</th><th data-term="strategy_col">Strategy</th><th>Order id</th></tr></thead><tbody>${rows.map(orderRow).join("")}</tbody></table>`;
  $("#orders-cancel-all").onclick = () => {
    const stops = rows.filter(o => o.purpose === "stop").length, rest = rows.length - stops;
    openModal({
      title: "Cancel the working orders?",
      bodyHTML: `<p>${rest} working order${rest === 1 ? "" : "s"} will be cancelled: entries that haven't filled, the app's own exits, and anything
        else working on the account.</p><p class="muted">${stops ? `${stops} protective stop${stops === 1 ? "" : "s"} stay${stops === 1 ? "s" : ""} - ` : ""}A stop
        protecting an open position is never cancelled here: close the position and its stop goes with it.</p>`,
      okText: "Cancel the orders", okClass: "danger", cancelText: "Keep them",
      onOk: async () => { toastResult(await post("/api/orders/cancel-all", {})); loadOrders(true); },
    });
  };
  const byId = Object.fromEntries(rows.map(o => [o.order_id, o]));
  $$("tr[data-order]", el).forEach(tr => {
    const o = byId[tr.dataset.order];
    tr.onclick = () => { if (o.trade_id) openRecord(o.trade_id); else selectPlay(o.play_id); };
  });
}

function orderRow(o) {
  const linked = o.trade_id || (o.play_id && S.plays.some(p => p.id === o.play_id));
  const why = o.reason && o.reason !== o.purpose ? ` <span class="muted">(${escapeHtml(pretty(o.reason))})</span>` : "";
  return `<tr ${linked ? `class="clickable-row" data-order="${escapeHtml(o.order_id)}"` : ""} ${o.message ? `title="${escapeHtml(o.message)}"` : ""}>
    <td class="sym">${escapeHtml(o.symbol)}</td>
    <td>${o.action ? `<span class="side ${o.action === "BUY" ? "LONG" : "SHORT"}">${o.action}</span>` : "–"}</td>
    <td class="purpose">${escapeHtml(PURPOSE[o.purpose] || o.purpose)}${why}</td>
    <td class="muted">${escapeHtml(o.order_type ? pretty(o.order_type).toLowerCase() : "–")}</td>
    <td class="num">${count(o.qty)}</td>
    <td class="num">${o.filled ? count(o.filled) : "–"}</td>
    <td class="num">${num(o.limit_price)}</td>
    <td class="num">${num(o.stop_price)}</td>
    <td class="muted">${escapeHtml(o.tif || "–")}</td>
    <td><span class="badge ${o.status === "WORKING" ? "good" : "warn"}">${escapeHtml(STATUS[o.status] || String(o.status || "").toLowerCase())}</span></td>
    <td ${o.calling_off ? `title="${escapeHtml(o.calling_off)}"` : ""}>${countdown(o) || "–"}</td>
    <td>${o.strategy ? stratLabel(o.strategy) : "–"}</td>
    <td class="muted"><code>${escapeHtml(o.order_id)}</code></td></tr>`;
}
