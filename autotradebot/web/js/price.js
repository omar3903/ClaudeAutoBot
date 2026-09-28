/* A stock's market price in a panel, popup or drawer about it - a play's panel and chart, its stock on the
   Signals page, an open trade's record, an exit's confirmation, a mover's chart, a pair's chart. Fetched as
   it opens and every 30 s while it stays open (GET /api/price/{symbol}, pre-market and after-hours
   included), and between those from each streamed price while the stock streams (prices.tick). Only shown:
   the exits and the entry checks read their own regular-hours prices on the server. */
import { api, escapeHtml, fmtWhen, isNewer, num } from "./util.js";
import { S } from "./state.js";

const EVERY_MS = 30000;

/** A price as the panels say it: "12.34 at 7:58 AM · pre-market" - the session only outside regular hours. */
const priceWords = d => `${num(d.price)} at ${fmtWhen(d.at)}${d.session === "regular" ? "" : ` · ${d.session}`}`;

/* The panels' prices still on show, so a streamed price reaches them between their fetches (priceTicks). */
const watchers = new Set();

/** Keeps `el`, an element the panel has just drawn, showing the price of `symbols` - one stock, or a pair's
    two - with `extra(answer)` after a stock's price where the panel adds to it (plain text). It stops once
    `el` has left the page or sits in a hidden panel - the panel closed, or drew itself again for another
    stock - and skips its turn while this tab is hidden or has lost the app. A price it couldn't get says why,
    quietly; one it already showed stays, its time saying how old it is. */
export function watchPrice(el, symbols, extra = () => "") {
  if (!el) return;
  const list = [].concat(symbols), lines = {};          // symbol -> its line as last drawn, and its price's time
  const alive = () => el.isConnected && !el.closest(".hidden") && !S.stopped;
  const draw = () => {
    el.innerHTML = list.map(s => (list.length > 1 ? `${escapeHtml(s)} ` : "") + (lines[s] ? lines[s].html : "…")).join("; ");
  };
  // a price shown gives way only to one from later - a fetch can answer with an older price than a stream sent
  const take = (s, d) => {
    if (lines[s] && lines[s].ok && isNewer(lines[s].at, d.at)) return false;
    lines[s] = { ok: true, at: d.at, html: escapeHtml(priceWords(d) + extra(d)) };
    return true;
  };
  const watcher = { list, alive, draw, take };
  watchers.add(watcher);
  el.textContent = "…";
  const tick = async () => {
    if (!alive()) { watchers.delete(watcher); return; }
    if (!document.hidden && !document.body.classList.contains("offline")) {
      const answers = await Promise.all(list.map(s => api(`/api/price/${encodeURIComponent(s)}`)
        .catch(e => ({ ok: false, reason: e.message }))));
      if (!alive()) { watchers.delete(watcher); return; }
      answers.forEach((d, i) => {
        if (d.ok) take(list[i], d);
        else if (!(lines[list[i]] || {}).ok) lines[list[i]] = { ok: false, html: `<span class="muted">${escapeHtml(d.reason || "No price.")}</span>` };
      });
      draw();
    }
    setTimeout(tick, EVERY_MS);
  };
  tick();
}

/** Streamed prices (events.js), {symbol: {price, at, session}} as /api/price answers: each panel showing one of
    those stocks takes it where it's newer than its line, and draws itself again. */
export function priceTicks(prices) {
  watchers.forEach(w => {
    if (!w.alive()) { watchers.delete(w); return; }
    if (w.list.filter(s => prices[s] && w.take(s, prices[s])).length) w.draw();
  });
}
