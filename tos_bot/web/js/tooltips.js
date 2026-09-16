/* What the terms mean: hover or focus anything with a data-term attribute. */
import { $, escapeHtml, plural, pretty } from "./util.js";
import { S } from "./state.js";
import { orderLine, ordersFor } from "./orders.js";

const GLOSSARY = {
  long: ["Long", "Buy first, sell later. You profit when the price rises above your entry; the stop sits below it and caps the loss."],
  short: ["Short", "Sell borrowed shares first, buy them back later. You profit when the price falls; the stop sits above the entry. Losses grow if the price keeps rising, and it needs a margin account."],
  intraday: ["Intraday (day trade)", "Opened and closed in the same session - held minutes to hours and flattened before the close. Under $25k, a live margin account gets 3 day trades per 5 sessions (the PDT rule)."],
  swing: ["Swing", "Held for days to a few weeks to catch a bigger move. It carries overnight gap risk, but doesn't use up day trades."],
  ext: ["Extended hours", "Can also be entered pre-market (4:00-9:30) or after hours (16:00-20:00), with limit orders only. Thinner trading means wider spreads."],
  autopilot: ["Autopilot", "A bright robot means Autopilot will take this entry on its next pass; a dim one means it already has. Exits are automatic either way."],
  executed: ["Executed", "An order has gone out for this play. It won't be sent twice - the position is under Open positions."],
  hide_done: ["Hide executed", "Only changes this view: plays you've already sent drop out of the table. What the bot scans for isn't affected."],
  symbol: ["Symbol", "The ticker and its sector. Hover a row for the reasoning; click it for the numbers and the order."],
  side: ["Side", "LONG profits when the price rises, SHORT when it falls. Hover a badge for more."],
  strategy_col: ["Strategy", "The setup that found the play. Hover a name for how it works; switch setups on or off under Strategies."],
  noise: ["Noise flags", "Signs this is a bad moment for the setup: against the daily trend, the wrong side of VWAP, against today's gap, heavier volume against it, another setup pointing the other way, or too little expected value. Autopilot skips flagged plays; you can still take one. The strategy replay shows whether each check removes worse trades than it keeps."],
  hide_noisy: ["Hide noise", "Hide plays with noise flags from the list. They are still scanned, recorded and measured."],
  tf: ["Timeframe", "day = intraday, closed the same session.\nswing = held for days to weeks."],
  entry: ["Entry", "The price the order aims to get in at."],
  stop: ["Stop", "Where the idea is proven wrong. Hitting it exits the trade, capping the loss near the $ risk shown."],
  target: ["Target", "The first profit objective. The exit manager may trail the stop past it instead of selling right at it."],
  rr: ["Reward : Risk", "Distance to the target divided by distance to the stop. 2.0 means a win pays twice what a stop-out costs."],
  qty: ["Quantity", "Shares sized so a stop-out loses about your per-trade risk budget, capped by buying power."],
  risk: ["$ Risk", "What you lose if the stop is hit: quantity × |entry − stop|, before slippage."],
  score: ["Score", "The rank: the setup's confidence and reward:risk, times its strategy weight, plus a bump for unusual volume or a gap."],
  mark: ["Mark", "The broker's current price for the position."],
  unrealized: ["Unrealized", "This record's open profit or loss at the current mark: (mark − entry) × its shares. The broker's figure for all the shares it holds of the stock is in the trade record."],
  order_for: ["For", "entry: opens a position for a play.\nexit: closes an open position.\nbracket stop / target: attached to an entry.\nplaced outside the app: by hand or by another program - listed, but the app never changes or cancels it."],
  order_status: ["Status", "accepted, not live yet: the broker holds it but hasn't sent it to the exchange - a regular-hours order placed before the open waits for 9:30, a stop waits for its price.\nworking: live at the exchange, waiting to fill.\npart filled: some shares are done, the rest are still working."],
  age: ["Age / Expected", "How long it's been held against how long this setup usually takes. Past the review time it's flagged for a look - the stop isn't touched."],
  mfe: ["MFE / MAE", "The best and worst open P/L seen while holding (max favourable / adverse excursion)."],
  auto_exit: ["Auto exit", "On: the exit manager handles the stop, target, break-even and trailing moves, and flattens day trades before the close. Off: you manage the exit."],
  split: ["Day / swing split", "How much of the trading capital day trades and swing trades may each hold at once - pair trades count as swing trades. A trade that doesn't fit what's left of its share is made smaller; risk per trade is still measured against the whole trading capital. It shows while Intraday and Swing are both on; with only one on, that kind gets all of it."],
  capital: ["Trading capital", "How much of the account the bot may use. Risk per trade and position-size limits are measured against it, and new positions only use what's left of it. It can't be more than the account holds. Click the amount to change it."],
  hot: ["Hot list", "The day's most in-play stocks from the full scan, no more than a third of them from one sector. They're rescanned every cycle."],
  buffer: ["Sector buffers", "The next best candidates in each sector. Each cycle looks at a couple per sector: a hotter one is adopted into the hot list, a promising one is kept waiting for a slot, the rest are dropped.\nKept = waiting for a slot · Looked at = buffer names scanned today · Queued = not looked at yet."],
  heat: ["Heat", "How in play a stock is, 0 to 1.\nDaily heat (full scan): relative volume, yesterday's move against its ATR, a close near the day's high or low, volatility and dollar volume, ranked against every liquid stock.\nIntraday heat (each cycle): today's relative volume, the move so far against the ATR and the day's range - plus a bump when a setup fires."],
  decision: ["Buffer decisions", "What each cycle did with the buffer names it looked at: adopted into the hot list (replacing its coolest name), kept for later, or dropped."],
};

function termContent(el) {
  const k = el.dataset.term;
  if (k === "sector") {
    const s = el.dataset.sector || "Unknown";
    const sel = (S.state.filters || {}).sectors || [];
    return [s, `This play's sector. ${sel.length ? `Only scanning and trading: ${sel.join(", ")}.` : "Every sector is being scanned."} Change it with the Sectors button.`];
  }
  if (k === "active_order") {
    const list = ordersFor(el.dataset.symbol);
    return [`${plural(list.length, "order")} working on ${el.dataset.symbol}`,
      `${list.map(orderLine).join("\n") || "None any more."}\n\nClick to open Active orders.`];
  }
  if (k === "strategy") {
    const s = S.strategies[el.dataset.key];
    if (!s) return [pretty(el.dataset.key), "A trading setup. Open Strategies for the full playbook."];
    const how = `${s.timeframe === "INTRADAY" ? "Day trade" : "Swing"} · ${s.kind.toLowerCase()} · ${s.enabled ? `on, weight ${s.weight}` : "switched off"}`;
    return [s.title, `${s.thesis}\n\n${how}`];
  }
  return GLOSSARY[k] || null;
}

let tipFor = null;

export function showTipAt(x, y, title, text) {
  const tip = $("#tooltip");
  tip.innerHTML = `<div class="tt-title">${escapeHtml(title)}</div>${escapeHtml(text).replace(/\n/g, "<br>")}`;
  tip.classList.remove("hidden");
  placeTip(x, y);
}
function placeTip(x, y) {
  const tip = $("#tooltip"), pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let left = x + pad, top = y + pad;
  if (left + w > innerWidth) left = Math.max(4, x - w - pad);
  if (top + h > innerHeight) top = Math.max(4, y - h - pad);
  tip.style.left = left + "px"; tip.style.top = top + "px";
}
export function hideTip() { $("#tooltip").classList.add("hidden"); }

export function initTooltips() {
  document.addEventListener("mouseover", e => {
    const el = e.target.closest ? e.target.closest("[data-term]") : null;
    if (el === tipFor) return;
    tipFor = el;
    if (!el) return;                                 // a row's own tooltip takes over
    const c = termContent(el);
    if (c) showTipAt(e.clientX, e.clientY, c[0], c[1]); else hideTip();
  });
  document.addEventListener("mousemove", e => {
    if (tipFor && !$("#tooltip").classList.contains("hidden")) placeTip(e.clientX, e.clientY);
  });
  document.addEventListener("mouseout", e => {
    if (tipFor && !(e.relatedTarget && tipFor.contains(e.relatedTarget))) { tipFor = null; hideTip(); }
  });
  document.addEventListener("focusin", e => {
    const el = e.target.closest ? e.target.closest("[data-term]") : null;
    if (!el) return;
    const c = termContent(el), r = el.getBoundingClientRect();
    if (c) showTipAt(r.left, r.bottom, c[0], c[1]);
  });
  document.addEventListener("focusout", () => { if (!tipFor) hideTip(); });
}
