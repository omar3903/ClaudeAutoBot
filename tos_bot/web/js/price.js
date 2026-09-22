/* A stock's market price in a panel, popup or drawer about it - a play's panel and chart, its stock on the
   Signals page, an open trade's record, an exit's confirmation, a mover's chart, a pair's chart. Fetched as
   it opens and every 30 s while it stays open (GET /api/price/{symbol}, pre-market and after-hours
   included). Only shown: the exits and the entry checks read their own regular-hours prices on the server. */
import { api, escapeHtml, fmtWhen, num } from "./util.js";
import { S } from "./state.js";

const EVERY_MS = 30000;

/** A price as the panels say it: "12.34 at 7:58 AM · pre-market" - the session only outside regular hours. */
const priceWords = d => `${num(d.price)} at ${fmtWhen(d.at)}${d.session === "regular" ? "" : ` · ${d.session}`}`;

/** Keeps `el`, an element the panel has just drawn, showing the price of `symbols` - one stock, or a pair's
    two - with `extra(answer)` after a stock's price where the panel adds to it (plain text). It stops once
    `el` has left the page or sits in a hidden panel - the panel closed, or drew itself again for another
    stock - and skips its turn while this tab is hidden or has lost the app. A price it couldn't get says why,
    quietly; one it already showed stays, its time saying how old it is. */
export function watchPrice(el, symbols, extra = () => "") {
  if (!el) return;
  const list = [].concat(symbols), lines = {};          // symbol -> its line as last drawn
  const alive = () => el.isConnected && !el.closest(".hidden") && !S.stopped;
  el.textContent = "…";
  const tick = async () => {
    if (!alive()) return;
    if (!document.hidden && !document.body.classList.contains("offline")) {
      const answers = await Promise.all(list.map(s => api(`/api/price/${encodeURIComponent(s)}`)
        .catch(e => ({ ok: false, reason: e.message }))));
      if (!alive()) return;
      answers.forEach((d, i) => {
        if (d.ok) lines[list[i]] = { ok: true, html: escapeHtml(priceWords(d) + extra(d)) };
        else if (!(lines[list[i]] || {}).ok) lines[list[i]] = { ok: false, html: `<span class="muted">${escapeHtml(d.reason || "No price.")}</span>` };
      });
      el.innerHTML = list.map(s => (list.length > 1 ? `${escapeHtml(s)} ` : "") + lines[s].html).join("; ");
    }
    setTimeout(tick, EVERY_MS);
  };
  tick();
}
