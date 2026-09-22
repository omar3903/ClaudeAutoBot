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
  autopilot: ["Autopilot", "A green bar at the left of a play and a bright robot: Autopilot would take it on its next pass - it passes every check and a cap has room for it. An amber bar: it passes the checks but a cap is full (the day's entries, the open positions, the day / swing slots, the setup's own) - hover the robot for which. A faded grey robot, while Autopilot is on: it won't take the play - hover it for the first check the play fails, in the words Autopilot's own gate uses. A dim robot: it has already acted on it. The play's detail panel says the same in one line. Exits are automatic either way."],
  last: ["Price", "The latest price the app holds for the stock - its last quote, or the close of its latest 5-minute candle - and how far that is past the entry in R (risk units). An entry is refused once the price has run more than 0.25R past it (execution.max_chase_r): the reward:risk the play was judged on is gone, so beyond that it's amber. The scans keep it current in the session; Refresh fetches it now for every play and position. On delayed data it's about 15 minutes old."],
  executed: ["Executed", "An order has gone out for this play. It won't be sent twice - the position is under Open positions."],
  hide_done: ["Hide executed", "Only changes this view: plays you've already sent drop out of the table. What the bot scans for isn't affected."],
  symbol: ["Symbol", "The ticker and its sector. Hover a row for the reasoning; click it for the numbers and the order."],
  side: ["Side", "LONG profits when the price rises, SHORT when it falls. Hover a badge for more."],
  strategy_col: ["Strategy", "The setup that found the play. Hover a name for how it works; switch setups on or off under Strategies."],
  record: ["Replay record", "The setup's record in the strategy replay, over the trades Autopilot would have taken of it: its average R a trade × how many trades. Green: proven by Autopilot's own test, the one the Strategies panel shows (enough trades, and an edge that clears its bar and holds up in the held-out sessions). Amber: not proven yet. Red: it loses on average. Grey: the replay has no trades from it yet. Click the play for what its replayed wins average against the R the play expects, and to switch the setup off."],
  noise: ["Noise flags", "Signs this is a bad moment for the setup: against the daily trend, the wrong side of VWAP, against today's gap, heavier volume against it, another setup pointing the other way, or too little expected value. Autopilot skips flagged plays; you can still take one. The strategy replay shows whether each check removes worse trades than it keeps."],
  hide_noisy: ["Hide noise", "Hide plays with noise flags from the list. They are still scanned, recorded and measured."],
  tf: ["Timeframe", "day = intraday, closed the same session.\nswing = held for days to weeks."],
  entry: ["Entry", "The price the order aims to get in at."],
  stop: ["Stop", "Where the idea is proven wrong. Hitting it exits the trade, capping the loss near the $ risk shown."],
  target: ["Target", "The first profit objective. The exit manager may trail the stop past it instead of selling right at it."],
  rr: ["Reward : Risk", "Distance to the target divided by distance to the stop. 2.0 means a win pays twice what a stop-out costs."],
  qty: ["Quantity", "Shares sized so a stop-out loses about your per-trade risk budget, capped by buying power."],
  risk: ["$ Risk", "What you lose if the stop is hit: quantity × |entry − stop|, before slippage."],
  score: ["Score", "The rank: what the play should make per dollar risked (its odds times the reward, less the odds it fails), times the setup's weight and the evidence weight from its record, plus a small bump for unusual volume, a gap (day trades) and enough range. Weights only reorder plays; they don't change a play."],
  mark: ["Mark", "The current price for the position: the one the exit manager acts on (it fetches one about every 20 seconds), or the broker's own mark when it has none. Refresh fetches it now."],
  unrealized: ["Unrealized", "This record's open profit or loss at the current mark: (mark − entry) × its shares. The broker's figure for all the shares it holds of the stock is in the trade record."],
  order_for: ["For", "entry: opens a position for a play.\nexit: closes an open position.\nbracket stop / target: attached to an entry.\nplaced outside the app: by hand or by another program - listed, but the app never changes or cancels it."],
  order_time_left: ["Time left", "A working entry's countdown. A day-trade entry not filled within 10 minutes (execution.entry_timeout_min) is cancelled rather than left to chase the price - amber in its last 2 minutes. Once part of an entry (day or swing) has filled, the rest is cancelled 30 seconds later (execution.partial_entry_wait_s) if it hasn't completed, so the shares bought get their stop at the broker: 'cut in 18 s'. cancelling: the app has asked the broker to cancel it and is waiting for the answer; what did fill is booked. Swing entries otherwise work for the day. The play's ⏳ shows the same, after the shares filled of the order."],
  order_status: ["Status", "accepted, not live yet: the broker holds it but hasn't sent it to the exchange - a regular-hours order placed before the open waits for 9:30, a stop waits for its price.\nworking: live at the exchange, waiting to fill.\npart filled: some shares are done, the rest are still working."],
  age: ["Age / Expected", "How long it's been held against how long this setup usually takes, and the day (the time, for a day trade) it is expected out by. Past the review time it's flagged for a look - the stop isn't touched. Hover a row for the review time and the day the time stop closes a swing trade."],
  mfe: ["MFE / MAE", "The best and worst open P/L seen while holding (max favourable / adverse excursion)."],
  auto_exit: ["Auto exit", "On: the exit manager handles the stop, target, break-even and trailing moves, and flattens day trades before the close. Off: you manage the exit."],
  pairs: ["Pairs", "Pair trades: two related stocks that usually move together - when their prices drift unusually far apart, buy the one that fell behind and short the one that ran ahead, and close both when the gap closes. Ticked, Autopilot may enter them (only once the replay has proven the pair rules, and at most pairs.max_new_per_day a day). The pairs being watched are always listed in the Pairs tab. They need shorting, so they don't work in a live cash account."],
  split: ["Day / swing split", "How much of the trading capital day trades and swing trades may each hold at once - pair trades count as swing trades. A trade that doesn't fit what's left of its share is made smaller; risk per trade is still measured against the whole trading capital. It shows while Intraday and Swing are both on; with only one on, that kind gets all of it."],
  capital: ["Trading capital", "How much of the account the bot may use. Risk per trade and position-size limits are measured against it, and new positions only use what's left of it. It can't be more than the account holds. Click the amount to change it."],
  untracked: ["Shares without a record", "The broker holds these shares, but no open-trade record in the app covers them: they were bought or sold outside the app, or an order filled and the app couldn't book it. Their exits aren't managed - no stop, no target. Exit closes them at the market; a record that later covers them makes the row go away."],
  hot: ["Hot list", "The day's most in-play stocks from the full scan, no more than a third of them from one sector. They're rescanned every cycle."],
  buffer: ["Sector buffers", "The next best candidates in each sector. Each cycle looks at a couple per sector: a hotter one is adopted into the hot list, a promising one is kept waiting for a slot, the rest are dropped.\nKept = waiting for a slot · Looked at = buffer names scanned today · Queued = not looked at yet."],
  heat: ["Heat", "How in play a stock is, 0 to 1.\nDaily heat (full scan): relative volume, yesterday's move against its ATR, a close near the day's high or low, volatility and dollar volume, ranked against every liquid stock.\nIntraday heat (each cycle): today's relative volume, the move so far against the ATR and the day's range - plus a bump when a setup fires."],
  decision: ["Buffer decisions", "What each cycle did with the buffer names it looked at: adopted into the hot list (replacing its coolest name), kept for later, or dropped. The pre-open gap check adopts the stocks gapping on pre-market volume the same way, and says what it saw."],
  gap: ["Pre-market gap", "How far the stock traded before the open from yesterday's close, from the gap check's one read of its pre-market candles (Settings → Gap check). A gap of 2% or more on 50,000+ pre-market shares makes it a gapper - Aziz's stocks in play - and it takes a hot-list slot from the coolest name that isn't gapping. Its pre-market high and low become the day's first levels."],
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
  if ((k === "autopilot" || k === "record") && el.dataset.why) {
    const [title, text] = GLOSSARY[k];
    return [title, `${el.dataset.why}.

${text}`];
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
