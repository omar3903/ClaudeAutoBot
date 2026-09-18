/* Active orders: every order still working at the broker and what it is for, and the
   ⏳ marker on plays whose stock has one. The engine re-checks the broker every few
   seconds and pushes orders.updated when anything changed. */
import { $, $$, api, count, escapeHtml, fmtClock, num, post, pretty } from "./util.js";
import { openModal, toastResult } from "./ui.js";
import { S, emit } from "./state.js";
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
  return [o.action, count(o.remaining), pretty(o.order_type || "order").toLowerCase(), ...prices].filter(Boolean).join(" ")
    + ` · ${PURPOSE[o.purpose] || o.purpose}`;
}

/** The marker a play gets when its stock has orders working. */
export function orderMark(symbol) {
  const n = ordersFor(symbol).length;
  return n ? `<span class="order-mark" data-term="active_order" data-symbol="${escapeHtml(symbol)}">⏳${n > 1 ? n : ""}</span>` : "";
}

export async function loadOrders(fresh = false) {
  if (S.stopped) return;
  try { S.orders = await api("/api/orders" + (fresh ? "?fresh=true" : "")); } catch { return; }
  ordersChanged();
}

export function ordersChanged() {
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
    <th data-term="strategy_col">Strategy</th><th>Order id</th></tr></thead><tbody>${rows.map(orderRow).join("")}</tbody></table>`;
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
    <td>${o.strategy ? stratLabel(o.strategy) : "–"}</td>
    <td class="muted"><code>${escapeHtml(o.order_id)}</code></td></tr>`;
}
