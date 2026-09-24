/* The top bar: paper / live, the market, data and connection pills, balances,
   trading capital, and the theme, refresh and paper-reset buttons. */
import { $, $$, api, escapeHtml, fmtTime, getLocal, money, num, post, shorten, store, usd } from "./util.js";
import { S, emit, on, refreshState, setState } from "./state.js";
import { busy, closeModal, openModal, toast, toastResult, unbusy } from "./ui.js";
import { openConnections } from "./connections.js";
import { loadOpen, showTab } from "./blotter.js";

function setPill(sel, text, cls) {
  const el = $(sel);
  el.textContent = text;
  el.className = "pill" + (el.id === "pill-conn" ? " clickable" : "") + (cls ? " " + cls : "");
}

function renderTop() {
  const s = S.state, v = s.venue || {};
  $$("#mode-switch .seg").forEach(b => {
    b.classList.toggle("active", b.dataset.mode === s.mode);
    b.title = b.dataset.mode === "paper"
      ? `Paper: ${(v.paper_platforms || {})[v.paper_platform] || "simulator"}`
      : "Live: your IBKR account — real orders";
  });
  $("#btn-reset-paper").classList.toggle("hidden", v.trading_on !== "paper");
  renderMarket(s.market || {}, s.market_open);
  renderData(s.data || {});
  renderRegime(s.regime);
  // "armed" only means something in LIVE mode (the equity floor)
  $("#pill-armed").classList.toggle("hidden", s.mode === "paper");
  if (s.mode !== "paper") setPill("#pill-armed", s.armed ? "armed" : "disarmed", s.armed ? "good" : "bad");
  renderArmed(s);
  renderBalances(s);
  renderCapital(s.capital);
  const c = s.connection || {};
  setPill("#pill-conn", c.label || "connection", c.cls);
  $("#pill-conn").title = `${c.detail || ""}\nClick to open Connections`;
  if (s.strategies_on != null) $("#btn-strategies").textContent = `Strategies · ${s.strategies_on}`;
  renderBanner(c);
  renderMismatches(s.mismatches || []);
}

/* LIVE and disarmed: every new entry - yours, Autopilot's, a pair's - is refused until the account is back over
   the equity floor. The engine checks it again only on a start, a switch of account or Refresh, so the banner
   says so. The reason is the engine's own (engine.disarmed); after a page load it is read from the snapshot. */
let disarmedWhy = "";

export function noteDisarmed(reason) {
  disarmedWhy = reason || "";
  S.state.armed = false;                    // until the next snapshot, which says the same
  renderArmed(S.state);
}

function renderArmed(s) {
  const b = $("#armed-banner"), show = s.mode === "live" && s.armed === false;
  if (s.armed) disarmedWhy = "";
  b.classList.toggle("hidden", !show);
  if (!show) return;
  const a = s.account, base = (a || {}).base || {};
  const why = disarmedWhy || (!a ? "the account hasn't been read yet"
    : !base.usd_per_base ? `no ${base.currency || "account-currency"}->USD exchange rate yet`
    : `equity was under the ${usd(a.min_start_equity)} live floor when it was last checked (${usd(a.equity)} now)`);
  b.innerHTML = `<span>⚠ LIVE, disarmed: ${escapeHtml(why)}. No new entry is sent until it re-arms - exits still run.
    ↻ Refresh checks it again.</span>`;
}

let mismatchKey = "";

function renderMismatches(list) {
  // positions a different size than their records add up to - shown until they agree again, each with its Fix. Drawn
  // again only when the list changes, so a snapshot arriving mid-click doesn't swap the button out from under it
  const b = $("#records-banner"), key = JSON.stringify(list.map(m => [m.symbol, m.note]));
  b.classList.toggle("hidden", !list.length);
  if (key === mismatchKey) return;
  mismatchKey = key;
  b.innerHTML = list.map(m => `<span>⚠ ${escapeHtml(m.note)}</span>
    <button class="mini" data-fix="${escapeHtml(m.symbol)}" title="See what the record and the account hold, and fix the record">Fix…</button>`).join("");
  $$("[data-fix]", b).forEach(btn => { btn.onclick = () => openFix(btn.dataset.fix); });
}

/* The Fix button on a share-count warning (engine.mismatch_preview / fix_mismatch): what the record and the account
   hold, what the broker executed that no record has booked, and - when the account holds fewer shares than the
   record, the same way round - the two ways to make them agree. Nothing changes without a click here; the counts
   shown go with it, and the engine refuses if they've changed since. */
let fixing = false;

async function openFix(symbol) {
  let m;
  try { m = await getLocal(`/api/positions/mismatch/${encodeURIComponent(symbol)}`); }
  catch (e) { toast(`Couldn't check ${symbol} - ${e.message}`, "bad"); return; }
  const { match = {}, close = {} } = m.actions || {};
  const title = `Fix ${m.symbol}: the record and the account disagree`;
  if (m.kind === "more") {
    openModal({ title, bodyHTML: fixPreviewHTML(m), okText: "Show the shares without a record", okClass: "long",
      cancelText: "Close", onOk: () => showTab("open") });
    return;
  }
  if (!match.ok) {
    openModal({ title, bodyHTML: fixPreviewHTML(m), okText: "OK", okClass: "ghost", cancelText: "Close", onOk: () => {} });
    return;
  }
  const verb = m.side === "SHORT" ? "Buy back" : "Sell";
  openModal({
    title, okText: "Match the record to the account", okClass: "long", onOk: () => sendFix(m, "match"),
    bodyHTML: fixPreviewHTML(m) + `<div class="row-gap"><button class="danger" id="fix-close" ${close.ok ? "" : "disabled"}>
        ${verb} what's left and close the record</button></div>
      ${close.ok ? "" : `<p class="reasons">${escapeHtml(close.reason)}</p>`}`,
  });
  $("#fix-close").onclick = () => { closeModal(); sendFix(m, "close"); };
}

function fixPreviewHTML(m) {
  const venue = escapeHtml(m.venue_label || "the broker"), b = m.booking, recs = m.records || [], ex = m.executions || [];
  const way = s => s === "SHORT" ? " short" : s === "LONG" ? " long" : "";
  const [verb, verbs] = m.side === "SHORT" ? ["Buy back", "buys back"] : ["Sell", "sells"];
  return `${m.live ? `<div class="warn-box">This is your <b>live</b> account: closing sends a <b>real market order</b>.</div>` : ""}
    <div class="kv">
      <span>The app's record${recs.length === 1 ? "" : "s"}</span><span>${num(m.recorded, 0)} shares${way(m.side)}</span>
      <span>Held in ${venue}</span><span>${num(m.held, 0)} shares${way(m.held_side)}</span>
      ${m.missing ? `<span>Missing from the account</span><span>${num(m.missing, 0)}</span>` : ""}
    </div>
    ${recs.length ? `<table class="rec-table"><thead><tr><th>Record</th><th class="num">Qty</th><th class="num">Entry</th>
      <th class="num">Stop</th></tr></thead><tbody>${recs.map(t => `<tr><td><code>${escapeHtml(t.id)}</code></td>
      <td class="num">${num(t.quantity, 0)}</td><td class="num">${num(t.entry_price)}</td><td class="num">${num(t.stop_price)}</td></tr>`).join("")}
      </tbody></table>` : ""}
    ${m.kind === "fewer" ? `<h4>What ${venue} executed that no record has booked</h4>
      ${ex.length ? `<table class="rec-table"><thead><tr><th>Time</th><th>Sent by</th><th class="num">Qty</th><th class="num">Price</th></tr></thead>
        <tbody>${ex.map(f => `<tr><td>${fmtTime(f.at)}</td><td>${escapeHtml(f.label)}</td><td class="num">${num(f.qty, 0)}</td>
        <td class="num">${num(f.price)}</td></tr>`).join("")}</tbody></table>`
        : `<p class="muted small">Nothing since the entry - IBKR keeps only today's executions.</p>`}` : ""}
    ${b ? `<p>The ${num(b.qty, 0)} missing shares are booked at <b>${num(b.price)}</b> (${escapeHtml(b.reason)}): ${escapeHtml(b.basis)}.</p>
      <p class="muted small"><b>Match the record</b> takes them off it: it then holds ${num(m.held, 0)}, as ${venue} does, and the stop
        at the broker is sized from it. <b>${verb} what's left</b> does the same, then ${verbs} the ${num(m.held, 0)} left at the
        market and closes the record.</p>` : ""}
    ${(m.actions || {}).match && !m.actions.match.ok ? `<p class="reasons">${escapeHtml(m.actions.match.reason)}</p>` : ""}`;
}

async function sendFix(m, action) {
  if (fixing) return;                                 // the answer to the last click hasn't come back yet
  fixing = true;
  let r;
  try { r = await post(`/api/positions/mismatch/${encodeURIComponent(m.symbol)}/fix`, { action, recorded: m.recorded, held: m.held }); }
  finally { fixing = false; }
  toastResult(r);
  loadOpen(); refreshState();
}

function renderMarket(mk, open) {
  const session = mk.session || (open ? "REGULAR" : "CLOSED");
  setPill("#pill-market", mk.label ? shorten(mk.label, 42) : (open ? "market open" : "market closed"),
    session === "REGULAR" ? "good" : session === "CLOSED" ? "bad" : "warn");
  $("#pill-market").title = mk.label
    ? mk.label + (mk.next_holiday ? `\nNext holiday: ${mk.next_holiday.name} (${mk.next_holiday.date})` : "") : "";
}

function renderRegime(r) {
  const el = $("#pill-regime");
  if (!el) return;
  if (!r) { el.classList.add("hidden"); return; }
  setPill("#pill-regime", `market: ${r.regime}`, r.regime === "turbulent" ? "warn" : "good");
  const leg = x => x ? `${(x.vol * 100).toFixed(2)}% a day, lasting ~${Math.round(x.days)} sessions` : "–";
  el.title = `Hamilton's Markov switching model on SPY's daily returns: a ${Math.round(r.p_turbulent * 100)}% chance the market ` +
    `is in its turbulent regime on ${r.for_session}.\nCalm: ${leg(r.calm)}. Turbulent: ${leg(r.turbulent)}.\n` +
    "Momentum setups are flagged while it's turbulent; Autopilot skips that flag once the replay shows it helps.";
}

function renderData(d) {
  setPill("#pill-data", d.connected ? `data: IBKR${d.delayed ? " (delayed)" : d.reason ? " (refused)" : ""}` : "data: none",
    !d.connected ? "bad" : d.delayed || d.reason ? "warn" : "good");
  $("#pill-data").title = !d.connected
    ? "No prices: IB Gateway isn't connected, so nothing is scanned and the simulator can't fill. Open Connections."
    : d.reason
      ? d.reason + (d.delayed ? " Until then scans use IBKR's candles, and prices for stops and targets come from the latest one-minute candle." : "")
      : d.delayed
        ? "Delayed data from IBKR: scans use IBKR's candles, and prices for stops and targets come from the latest one-minute candle."
        : "Real-time quotes and candles from IBKR";
}

function renderBalances(s) {
  // balances in the account's own currency; trades themselves are in US dollars
  const a = s.account || {}, inCcy = a.base || {};
  const ccy = inCcy.currency || "USD", foreign = ccy !== "USD";
  [["#a-equity", "equity"], ["#a-cash", "cash"], ["#a-bp", "buying_power"]].forEach(([sel, key]) => {
    $(sel).textContent = foreign ? money(inCcy[key], ccy) : usd(a[key]);
    $(sel).title = !foreign ? "" : inCcy.usd_per_base
      ? `≈ ${money(a[key], "USD")}. US stocks are sized in US dollars at 1 ${ccy} = ${num(inCcy.usd_per_base, 4)} USD.`
      : `No ${ccy}→USD exchange rate yet, so nothing can be sized.`;
  });
  const used = s.day_trades_5d ?? 0, limit = s.day_trade_limit ?? 3, live = s.mode !== "paper";
  const dt = $("#a-dt");
  dt.textContent = live ? `${used} / ${limit}` : `${used}`;
  dt.style.color = live && used >= limit ? "var(--short)" : live && used >= limit - 1 ? "var(--warn)" : "var(--fg)";
  const today = (s.pnl || {}).realized_today ?? 0, pnl = $("#a-pnl-today");
  pnl.textContent = foreign ? money(today, "USD") : usd(today);
  pnl.title = foreign ? "Trades are in US dollars" : "";
  pnl.style.color = today > 0 ? "var(--long)" : today < 0 ? "var(--short)" : "var(--fg)";
  renderOpenPL(s);
}

/* The header's Unrealized: the positions held now, each at its latest price against its average cost - in US
   dollars, like the trades. Drawn with the balances, and again by each streamed price (events.js). */
export function renderOpenPL(s = S.state) {
  const foreign = (((s.account || {}).base || {}).currency || "USD") !== "USD";
  const held = s.positions || [], open = held.reduce((sum, p) => sum + (p.unrealized_pl || 0), 0);
  const upl = $("#a-pnl-open");
  upl.textContent = held.length ? (foreign ? money(open, "USD") : usd(open)) : "–";
  upl.title = held.length
    ? `The ${held.length} position${held.length === 1 ? "" : "s"} held now, at the latest price against the average cost - since each was bought, not since today's open`
    : "No positions held";
  upl.style.color = open > 0 ? "var(--long)" : open < 0 ? "var(--short)" : "var(--fg)";
}

function renderBanner(c) {
  const b = $("#conn-banner");
  if (!c.action || c.cls === "good") { b.classList.add("hidden"); return; }
  b.classList.remove("hidden");
  b.innerHTML = `<span>⚠ ${escapeHtml(c.detail || "The IB Gateway connection needs attention")}</span>
    <button class="ghost" id="banner-conn">Open Connections</button>`;
  $("#banner-conn").onclick = openConnections;
}

/* ---------- trading capital: how much of the account the bot may use ---------- */
export function renderCapital(c) {
  const b = $("#btn-capital");
  b.classList.remove("limited", "clipped");
  if (!c) { b.textContent = "–"; b.title = "No account data yet"; return; }
  b.textContent = c.limit ? money(c.effective, c.currency) : "Whole account";
  b.classList.toggle("limited", !!c.limit && !c.clipped);
  b.classList.toggle("clipped", !!c.clipped);
  const sp = c.split;
  const split = sp && sp.on ? ` Day trades: up to ${money(sp.day.limit, c.currency)} (${sp.day.pct}%), swing trades: up to ${money(sp.swing.limit, c.currency)} (${sp.swing.pct}%).` : "";
  b.title = (c.limit
    ? `The bot uses ${money(c.effective, c.currency)} of this ${money(c.account_value, c.currency)} account` +
      (c.clipped ? ` (you set ${money(c.limit, c.currency)}, but the account is worth less now)` : "") +
      `. ${money(c.available, c.currency)} of it isn't invested.`
    : `The bot can use the whole ${money(c.account_value, c.currency)} account.`) + split + " Click to change.";
}

async function openCapital() {
  let d;
  try { d = await api("/api/capital"); } catch (e) { toast(`Couldn't read the trading capital - ${e.message}`, "bad"); return; }
  const c = d.capital;
  if (!c) { toast("No account data yet — connect IB Gateway first", "warn"); return; }
  const ccy = c.currency;
  const save = async amount => {
    const r = await post("/api/capital", { amount });
    toastResult(r);
    if (r.ok) { S.state.capital = r.capital; renderCapital(r.capital); emit("capital", r.capital); }
  };
  openModal({
    title: "Trading capital",
    bodyHTML: `<p>How much of the money in <b>${escapeHtml(c.venue_label)}</b> the bot may use. It's worth
        <b>${money(c.account_value, ccy)}</b> right now.</p>
      ${c.clipped ? `<div class="warn-box">You set ${money(c.limit, ccy)}, but the account is worth less now, so the bot uses ${money(c.effective, ccy)}.</div>` : ""}
      <p><label for="cap-amt">Amount (${escapeHtml(ccy)}) </label>
        <input type="number" id="cap-amt" min="1" max="${c.account_value}" step="1000" value="${Math.round(c.limit || c.account_value)}"></p>
      <p class="muted">Risk per trade and position-size limits are measured against this amount, and new positions only use
        what's left of it. It can't be more than the account holds, and your broker balance isn't touched.</p>
      ${ccy !== "USD" && c.usd_per_base ? `<p class="muted small">Your account is in ${escapeHtml(ccy)}. US stocks are sized in
        US dollars at 1 ${escapeHtml(ccy)} = ${num(c.usd_per_base, 4)} USD.</p>` : ""}
      <div class="row-gap"><button class="ghost mini" id="cap-all">Use the whole account</button></div>
      <p class="muted small">The day/swing split is set with the slider next to the Intraday and Swing filters.</p>`,
    okText: "Save", okClass: "long",
    onOk: () => {
      const amt = parseFloat($("#cap-amt").value);
      if (!(amt > 0)) { toast("Enter an amount above zero", "bad"); return; }
      if (amt > c.account_value) { toast(`That's more than the account holds (${money(c.account_value, ccy)})`, "bad"); return; }
      return save(amt);
    },
  });
  $("#cap-all").onclick = () => { closeModal(); save(null); };
}

/* ---------- paper / live ---------- */
async function switchMode(target) {
  if (target === (S.state.mode || "paper")) return;
  if (target === "paper") {
    const r = await post("/api/mode", { mode: "paper" });
    if (!r.ok) toast("Switch failed: " + (r.reason || ""), "bad");
    return;
  }
  openModal({
    title: "Switch to LIVE trading?",
    bodyHTML: `<div class="warn-box">Orders you approve will be sent to your <b>real IBKR account</b> and use real money.</div>
      <p class="muted">Every order still needs your click (or Autopilot, if you've allowed it live). The
      $${((S.state.account || {}).min_start_equity || 2000).toLocaleString()} equity floor and the 3-day-trade PDT cap apply.
      IB Gateway must be logged in to your live account on the live port.</p>`,
    okText: "Go Live", okClass: "danger",
    onOk: async () => {
      const r = await post("/api/mode", { mode: "live" });
      if (!r.ok) toast("Could not go live: " + [r.reason, ...(r.blockers || [])].filter(Boolean).join(" — "), "bad");
    },
  });
}

function resetPaper() {
  const cur = (S.state.account && S.state.account.paper_start_cash) || 100000;
  openModal({
    title: "Reset paper account",
    bodyHTML: `<p>Wipe the simulator's cash, positions and session P/L, and start again from:</p>
      <p><label>$ </label><input type="number" id="reset-amt" value="${cur}" min="1000" step="1000" /></p>
      <p class="muted">Closed-trade history is kept. The simulator's open positions are wiped, so their open-trade records are deleted.</p>`,
    okText: "Reset", okClass: "danger",
    onOk: async () => {
      const amt = parseFloat(($("#reset-amt") || {}).value) || cur;
      toastResult(await post("/api/paper/reset", { cash: amt }));
      loadOpen();
    },
  });
}

/* The Refresh button spins and stays disabled until the server answers (connecting to a Gateway that
   just came up can take a few seconds), then says what happened - so pressing it again does nothing
   and there is no need to. */
let refreshing = false;
async function refreshAccount(btn) {
  if (refreshing) return;
  refreshing = true;
  const started = Date.now();
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.classList.add("spinning");
  btn.innerHTML = `<span class="spin">&#8635;</span> ${S.state.connected ? "Refreshing…" : "Connecting…"}`;
  let r;
  try {
    r = await post("/api/account/refresh");
  } catch (e) {
    r = { ok: false, reason: "The app didn't answer - is it still running?" };
  }
  if (r.state) setState(r.state);
  loadOpen();
  const verdict = r.ok ? (r.warn ? "warn" : "good") : "bad";
  setTimeout(() => {                                  // long enough to be seen, even on a fast answer
    btn.innerHTML = label;
    btn.disabled = false;
    btn.classList.remove("spinning");
    btn.classList.add(`flash-${verdict}`);
    setTimeout(() => btn.classList.remove("flash-good", "flash-warn", "flash-bad"), 1500);
    refreshing = false;
  }, Math.max(0, 700 - (Date.now() - started)));
  toast(r.ok ? (r.note || "Refreshed") : (r.reason || r.detail || "Refresh failed"), verdict);
}

/* ---------- theme ---------- */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("#btn-theme").title = theme === "light" ? "Switch to dark mode" : "Switch to light mode";
}

export function initTopbar() {
  on("state", renderTop);
  applyTheme(document.documentElement.dataset.theme);
  $("#btn-theme").onclick = () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    store.set("atb-theme", next);
    applyTheme(next);
  };
  // other tabs of this browser follow straight away
  window.addEventListener("storage", e => {
    if (e.key === "atb-theme" && (e.newValue === "light" || e.newValue === "dark")) applyTheme(e.newValue);
  });
  $$("#mode-switch .seg").forEach(b => { b.onclick = () => switchMode(b.dataset.mode); });
  $("#btn-capital").onclick = openCapital;
  $("#btn-reset-paper").onclick = resetPaper;
  $("#btn-refresh").onclick = e => refreshAccount(e.currentTarget);
  $("#btn-connections").onclick = openConnections;
  $("#pill-conn").onclick = openConnections;
  $("#pill-conn").onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openConnections(); } };
}
