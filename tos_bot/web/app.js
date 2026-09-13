/* AutoTradeBot dashboard - vanilla JS, no build step.
   Whatever you change here (filters, strategies, routing, Autopilot) is saved on
   the server, applied to the bot straight away and pushed to every open tab. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const LOCAL_HEADER = { "X-ATB-Request": "1" };      // the server's same-machine guard expects it
const api = (path, opt) => fetch(path, opt).then(r => r.json());
const getLocal = path => api(path, { headers: LOCAL_HEADER });
const post = (path, body = {}) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json", ...LOCAL_HEADER },
  body: JSON.stringify(body),
}).catch(() => ({ ok: false, reason: "the app isn't reachable" }));

let STATE = {};
let PLAYS = [];
let SELECTED = null;
let DRAWER = null;               // what the side drawer shows: connections | strategies | record
let CONN = null;                 // last /api/setup payload while Connections is open
let STRATS = {};                 // strategy key -> catalog row (title, thesis, on/off, weight)
let RECORD_ID = null;            // trade shown in the record drawer
let STOPPED = false;             // the app has shut down
let REFRESH_TIMER = null;

/* ---------- formatting ---------- */
const usd = v => (v == null || isNaN(v)) ? "–" :
  (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
const num = (v, d = 2) => (v == null || isNaN(v)) ? "–" : Number(v).toFixed(d);
const pct = v => (v == null || isNaN(v)) ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(1) + "%";
const escapeHtml = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const shorten = (s, n) => s && s.length > n ? s.slice(0, n - 1) + "…" : s;
const pretty = key => (key || "").replace(/_/g, " ");
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const fmtTime = s => {
  if (!s) return "–";
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(s) ? s : s + "Z");     // the database stores UTC
  return isNaN(d) ? s : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};

const SECTOR_SHORT = {
  "Technology": "Tech", "Communication Services": "Comm", "Consumer Discretionary": "Cons Disc",
  "Consumer Staples": "Cons Stpl", "Healthcare": "Health", "Financials": "Financials",
  "Industrials": "Industr", "Energy": "Energy", "Utilities": "Utilities",
  "Materials": "Materials", "Real Estate": "Real Est",
};
const sectorTag = sec => sec
  ? `<span class="sector" data-term="sector" data-sector="${escapeHtml(sec)}">${escapeHtml(SECTOR_SHORT[sec] || sec)}</span>` : "";
const sideBadge = side => `<span class="side ${side}" data-term="${side === "SHORT" ? "short" : "long"}">${side}</span>`;
const tfLabel = tf => `<span data-term="${tf === "INTRADAY" ? "intraday" : "swing"}">${tf === "INTRADAY" ? "day" : "swing"}</span>`;
const stratLabel = key => `<span data-term="strategy" data-key="${escapeHtml(key)}">${escapeHtml((STRATS[key] || {}).title || pretty(key))}</span>`;
const VENUE_SHORT = { "paper": "simulator", "ibkr-paper": "IBKR paper", "ibkr-live": "IBKR live", "schwab": "Schwab" };

/* ---------- small per-browser preferences ---------- */
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

/* ---------- theme ---------- */
function syncThemeButton() {
  const dark = document.documentElement.dataset.theme !== "light";
  $("#btn-theme").title = dark ? "Switch to light mode" : "Switch to dark mode";
}
function applyTheme(t) { document.documentElement.dataset.theme = t; syncThemeButton(); }
$("#btn-theme").onclick = () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  store.set("atb-theme", next);
  applyTheme(next);
};
// other tabs of this browser follow straight away
window.addEventListener("storage", e => {
  if (e.key === "atb-theme" && (e.newValue === "light" || e.newValue === "dark")) applyTheme(e.newValue);
  if (e.key === "atb-hide-done") { $("#f-hide-done").checked = e.newValue === "1"; renderPlays(); }
});

/* ---------- what the terms mean (hover / focus) ---------- */
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
  tf: ["Timeframe", "day = intraday, closed the same session.\nswing = held for days to weeks."],
  entry: ["Entry", "The price the order aims to get in at."],
  stop: ["Stop", "Where the idea is proven wrong. Hitting it exits the trade, capping the loss near the $ risk shown."],
  target: ["Target", "The first profit objective. The exit manager may trail the stop past it instead of selling right at it."],
  rr: ["Reward : Risk", "Distance to the target divided by distance to the stop. 2.0 means a win pays twice what a stop-out costs."],
  qty: ["Quantity", "Shares sized so a stop-out loses about your per-trade risk budget, capped by buying power."],
  risk: ["$ Risk", "What you lose if the stop is hit: quantity × |entry − stop|, before slippage."],
  score: ["Score", "The rank: the setup's confidence and reward:risk, times its strategy weight, plus a bump for unusual volume or a gap."],
  mark: ["Mark", "The broker's current price for the position."],
  unrealized: ["Unrealized", "Open profit or loss at the current mark."],
  age: ["Age / Expected", "How long it's been held against how long this setup usually takes. Past the review time it's flagged for a look - the stop isn't touched."],
  mfe: ["MFE / MAE", "The best and worst open P/L seen while holding (max favourable / adverse excursion)."],
  auto_exit: ["Auto exit", "On: the exit manager handles the stop, target, break-even and trailing moves, and flattens day trades before the close. Off: you manage the exit."],
};

function termContent(el) {
  const k = el.dataset.term;
  if (k === "sector") {
    const s = el.dataset.sector || "Unknown";
    const sel = (STATE.filters || {}).sectors || [];
    return [s, `This play's sector. ${sel.length ? `Only scanning and trading: ${sel.join(", ")}.` : "Every sector is being scanned."} Change it with the Sectors button.`];
  }
  if (k === "strategy") {
    const s = STRATS[el.dataset.key];
    if (!s) return [pretty(el.dataset.key), "A trading setup. Open Strategies for the full playbook."];
    const how = `${s.timeframe === "INTRADAY" ? "Day trade" : "Swing"} · ${s.kind.toLowerCase()} · ${s.enabled ? `on, weight ${s.weight}` : "switched off"}`;
    return [s.title, `${s.thesis}\n\n${how}`];
  }
  return GLOSSARY[k] || null;
}

const tip = $("#tooltip");
let TIP_EL = null;
function showTipAt(x, y, title, text) {
  tip.innerHTML = `<div class="tt-title">${escapeHtml(title)}</div>${escapeHtml(text).replace(/\n/g, "<br>")}`;
  tip.classList.remove("hidden");
  placeTip(x, y);
}
function placeTip(x, y) {
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let left = x + pad, top = y + pad;
  if (left + w > innerWidth) left = Math.max(4, x - w - pad);
  if (top + h > innerHeight) top = Math.max(4, y - h - pad);
  tip.style.left = left + "px"; tip.style.top = top + "px";
}
function hideTip() { tip.classList.add("hidden"); }

document.addEventListener("mouseover", e => {
  const el = e.target.closest ? e.target.closest("[data-term]") : null;
  if (el === TIP_EL) return;
  TIP_EL = el;
  if (!el) return;                                 // a row's own tooltip takes over
  const c = termContent(el);
  if (c) showTipAt(e.clientX, e.clientY, c[0], c[1]); else hideTip();
});
document.addEventListener("mousemove", e => {
  if (TIP_EL && !tip.classList.contains("hidden")) placeTip(e.clientX, e.clientY);
});
document.addEventListener("mouseout", e => {
  if (TIP_EL && !(e.relatedTarget && TIP_EL.contains(e.relatedTarget))) { TIP_EL = null; hideTip(); }
});
document.addEventListener("focusin", e => {
  const el = e.target.closest ? e.target.closest("[data-term]") : null;
  if (!el) return;
  const c = termContent(el), r = el.getBoundingClientRect();
  if (c) showTipAt(r.left, r.bottom, c[0], c[1]);
});
document.addEventListener("focusout", () => { if (!TIP_EL) hideTip(); });

/* ---------- websocket ---------- */
function connect() {
  if (STOPPED) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = ev => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleEvent(msg.topic, msg.payload || {});
  };
  ws.onclose = () => { if (!STOPPED) setTimeout(connect, 2000); };
  ws.onerror = () => ws.close();
}

function handleEvent(topic, p) {
  switch (topic) {
    case "hello":
    case "account.snapshot":
    case "engine.started":
      STATE = p.state || p; renderTop(); if ($("#tab-open").offsetParent) loadOpen(); break;
    case "plays.updated":
      PLAYS = p.plays || []; renderPlays();
      if (p.scan && p.scan.scanned != null) $("#scan-meta").textContent =
        `scan: ${p.scan.prefiltered}/${p.scan.scanned} passed` +
        (p.scan.sector_skipped ? `, ${p.scan.sector_skipped} outside sectors` : "") +
        `, ${p.scan.n_plays} plays, ${p.scan.elapsed_s}s`;
      break;
    case "order.filled":
      toast(`Filled: ${p.symbol} entry x${p.qty} @ ${num(p.price)}`, "good");
      if (p.play) mergePlay(p.play);
      if (p.trade_id && SELECTED && PLAYS.find(x => x.id === SELECTED && x.trade_id === p.trade_id))
        selectPlay(SELECTED);
      loadOpen(); refreshState(); break;
    case "trade.closed":
    case "exit.triggered": {
      const t = p.trade || {};
      const how = p.reason ? ` [${p.reason}]` : "";
      toast(`${topic === "exit.triggered" ? "Auto-exit" : "Closed"} ${t.symbol}: ${usd(t.realized_pl)} (${pct(t.realized_pl_pct)})${how}`,
        (t.realized_pl || 0) >= 0 ? "good" : "bad");
      if (t.id && t.id === RECORD_ID && DRAWER === "record") openRecord(t.id);
      loadOpen(); loadHistory(); loadStats(); refreshState(); break;
    }
    case "trades.removed": {
      const gone = p.trades || [];
      toast(`Removed ${plural(gone.length, "open-trade record")} no longer held at ${p.venue_label || "the broker"}: ` +
        gone.map(t => t.symbol).join(", "), "warn");
      if (DRAWER === "record" && gone.some(t => t.id === RECORD_ID)) closeDrawer();
      loadOpen(); refreshState(); break;
    }
    case "exit.not_held":
      toast("Auto-exit skipped: " + (p.reason || "the broker doesn't show that position"), "warn"); break;
    case "exit.stop_moved":
      toast(`${p.symbol}: stop → ${num(p.new_stop)} (${num(p.r, 1)}R locked)`, "good");
      loadOpen(); break;
    case "trade.overdue":
      toast("⏰ " + (p.msg || `${p.symbol} exit is overdue`), p.winning ? "good" : "bad");
      loadOpen(); break;
    case "play.decided":
      if (p.play) mergePlay(p.play);
      if (p.decision === "approved" && p.result && !p.result.ok)
        toast("Order not sent: " + (p.result.reason || "rejected"), "bad");
      break;
    case "filters.updated":
      STATE.filters = p.filters; syncFilterControls(); renderPlays(); break;
    case "strategies.updated":
      indexStrategies(p.strategies);
      if (DRAWER === "strategies") renderStrategies();
      renderPlays(); break;
    case "quit.requested":
      openQuitDialog(p); break;
    case "quit.started":
    case "quit.progress":
      STATE.quit = p.quit; renderLock();
      if (topic === "quit.started") { closeModal(); loadOpen(); }
      break;
    case "quit.done":
      showShutdown(p.note); break;
    case "auth.reauth_required":
      toast(p.reason || "Schwab needs you to sign in again", "bad"); refreshState(); break;
    case "auth.reauth_ok":
      refreshState(); break;
    case "auth.schwab_login":
      showLoginStatus(p);
      if (p.state === "ok") { toast(p.message, "good"); if (connOpen()) openConnections(); }
      else if (p.state === "error") toast(p.message, "bad");
      break;
    case "broker.switched":
      STATE = p.state || STATE; renderTop(); loadOpen();
      if (p.mode !== p.prev)
        toast(p.mode === "live" ? "LIVE mode — orders are real now" : "Paper mode", p.mode === "live" ? "bad" : "good");
      break;
    case "broker.error":
      toast("Broker: " + (p.message || "error"), "bad"); break;
    case "autopilot.config":
      STATE.autopilot = p; renderAutopilot(); break;
    case "autopilot.entered":
      toast(`🤖 Autopilot entered ${p.side} ${p.symbol} x${p.qty} — ${pretty(p.strategy)} (${p.count_today} today)`, "good");
      loadOpen(); refreshState(); break;
    case "autopilot.would_enter":
      toast(`🤖 Autopilot (dry-run) would enter ${p.side} ${p.symbol} x${p.qty}`, "warn"); break;
    case "autopilot.blocked":
      toast("🤖 " + (p.reason || "Autopilot is blocked"), "warn"); break;
  }
}

function refreshState() {
  if (STOPPED) return;
  api("/api/state").then(s => { STATE = s; renderTop(); })
    .catch(() => { /* server restarting - the websocket reconnect catches up */ });
}

function mergePlay(row) {
  const i = PLAYS.findIndex(x => x.id === row.id);
  if (i >= 0) { PLAYS[i] = { ...PLAYS[i], ...row }; renderPlays(); }
}
const DONE = new Set(["ACCEPTED", "SUBMITTED", "WORKING", "PARTIAL", "FILLED", "ERROR"]);
const isDone = p => DONE.has(p.status);

/* ---------- top bar ---------- */
function renderTop() {
  const s = STATE || {};
  const v = s.venue || {};

  $$("#mode-switch .seg").forEach(b => {
    b.classList.toggle("active", b.dataset.mode === s.mode);
    b.title = b.dataset.mode === "paper"
      ? `Paper: ${(v.paper_platforms || {})[v.paper_platform] || "simulator"}`
      : `Live: ${(v.live_brokers || {})[v.live_broker] || "your broker"} — real orders`;
  });
  $("#btn-reset-paper").classList.toggle("hidden", v.trading_on !== "paper");

  const mk = s.market || {};
  const sess = mk.session || (s.market_open ? "REGULAR" : "CLOSED");
  setPill("#pill-market", mk.label ? shorten(mk.label, 42) : (s.market_open ? "market open" : "market closed"),
    sess === "REGULAR" ? "good" : sess === "CLOSED" ? "bad" : "warn");
  $("#pill-market").title = mk.label
    ? mk.label + (mk.next_holiday ? `\nNext holiday: ${mk.next_holiday.name} (${mk.next_holiday.date})` : "") : "";

  const src = (s.data_source || "?").replace("broker:", "");
  const delayed = v.market_data === "delayed";
  setPill("#pill-data", `data: ${src}${delayed && src !== "yfinance" ? " (delayed)" : ""}`,
    !s.data_is_real ? "bad" : delayed ? "warn" : "good");
  $("#pill-data").title = !s.data_is_real
    ? "Synthetic demo data — connect a broker or install yfinance"
    : delayed ? `Delayed ~15 min${src === "yfinance" ? " (free yfinance feed)" : " — no real-time market-data subscription"}`
      : `Real-time quotes and candles from ${src}`;

  // "armed" only means something in LIVE mode (the $2k floor)
  $("#pill-armed").classList.toggle("hidden", s.mode === "paper");
  if (s.mode !== "paper") setPill("#pill-armed", s.armed ? "armed" : "disarmed", s.armed ? "good" : "bad");

  const a = s.account || {};
  $("#a-equity").textContent = usd(a.equity);
  $("#a-bp").textContent = usd(a.buying_power);
  $("#a-cash").textContent = usd(a.cash);
  const dt = s.day_trades_5d ?? 0, lim = s.day_trade_limit ?? 3;
  const dtEl = $("#a-dt");
  dtEl.textContent = s.mode === "paper" ? `${dt}` : `${dt} / ${lim}`;
  dtEl.style.color = (s.mode !== "paper" && dt >= lim) ? "var(--short)"
    : (s.mode !== "paper" && dt >= lim - 1) ? "var(--warn)" : "var(--fg)";
  const today = (s.pnl || {}).realized_today ?? 0;
  const pe = $("#a-pnl-today"); pe.textContent = usd(today);
  pe.style.color = today > 0 ? "var(--long)" : today < 0 ? "var(--short)" : "var(--fg)";

  const c = s.connection || {};
  setPill("#pill-conn", c.label || "connection", c.cls);
  $("#pill-conn").title = `${c.detail || ""}\nClick to open Connections`;
  if (s.strategies_on != null) $("#btn-strategies").textContent = `Strategies · ${s.strategies_on}`;
  renderBanner(c);
  syncFilterControls();
  renderAutopilot();
  renderLock();
}

function renderBanner(c) {
  const b = $("#reauth-banner");
  if (!c.action || c.cls === "good") { b.classList.add("hidden"); return; }
  b.classList.remove("hidden");
  b.innerHTML = `<span>⚠ ${escapeHtml(c.detail || "The broker connection needs attention")}</span>
    ${c.action === "schwab_login" ? '<button id="banner-login">Sign in with Schwab</button>' : ""}
    <button class="ghost" id="banner-conn">Open Connections</button>`;
  const bl = $("#banner-login"); if (bl) bl.onclick = startSchwabLogin;
  $("#banner-conn").onclick = openConnections;
}

function renderAutopilot() {
  const ap = (STATE || {}).autopilot || {};
  const btn = $("#ap-toggle");
  if (!btn) return;
  const on = !!ap.enabled, eff = !!ap.effective;
  const fast = !!(STATE || {}).scan_fast, ivl = (STATE || {}).scan_interval_s;
  const tt = (ap.trade_types || []).map(t => t.toLowerCase() === "intraday" ? "day" : "swing").join("+");
  btn.textContent = on ? `Autopilot: ${tt || "on"}${ap.dry_run ? " · dry" : ""}${fast ? ` ⚡${ivl}s` : ""}` : "Autopilot: off";
  btn.classList.toggle("on", on && eff);
  btn.classList.toggle("armed-paper", on && !eff);       // wants to run but paper-gated in live
  const caps = `${ap.open_auto_positions ?? 0}/${ap.max_auto_positions ?? 0} open · ${ap.auto_trades_today ?? 0}/${ap.max_auto_trades_per_day ?? 0} today`;
  const scanline = fast ? ` Scanning every ~${ivl}s while the session is open.`
    : (on && eff ? ` Scanning every ${Math.round((ivl || 300) / 60)} min (fast cadence kicks in when the market opens).` : "");
  btn.title = on
    ? (eff ? `Autopilot is taking entries: ${tt || "?"}, ≥ ${ap.min_reward_risk}:1, ≥ conf ${ap.min_confidence}. ${caps}.${scanline} Exits are automatic. Click to turn off.`
      : (ap.blocked_note || "Autopilot is on but not routing (paper-only gate). Click to turn off."))
    : "Hands-off entry is OFF — you click every entry. Exits are automatic regardless. Click to turn on.";
  $("#autopilot-ctl").classList.toggle("live-warn", on && !eff);
}
function setPill(sel, text, cls) {
  const el = $(sel); el.textContent = text;
  el.className = "pill" + (el.id === "pill-conn" ? " clickable" : "") + (cls ? " " + cls : "");
}

/* ---------- quitting: the lock while positions close ---------- */
function renderLock() {
  const q = (STATE || {}).quit;
  document.body.classList.toggle("locked", !!q);
  $("#btn-quit").disabled = !!q;
  const b = $("#quit-banner");
  if (!q) { b.classList.add("hidden"); return; }
  const syms = (q.symbols || []).map(escapeHtml).join(", ");
  b.innerHTML = q.left
    ? `<span>⏻ Quitting — closing ${plural(q.left, "open position")}${syms ? ` (${syms})` : ""}. Nothing else can change until
       ${q.left === 1 ? "it's" : "they're all"} out; then the app ${q.reset_sim ? "resets the simulator and " : ""}shuts down.
       You can still exit positions yourself.</span>`
    : `<span>⏻ Quitting — all positions are closed, finishing up…</span>`;
  b.classList.remove("hidden");
}

$("#btn-quit").onclick = async () => {
  let pv;
  try { pv = await getLocal("/api/quit"); } catch { toast("The app isn't reachable", "bad"); return; }
  if (pv.detail) { toast(pv.detail, "bad"); return; }
  openQuitDialog(pv);
};

function posList(list) {
  return `<ul class="pos-list">${(list || []).map(t =>
    `<li>${escapeHtml(t.symbol)} — ${t.side === "SHORT" ? "short" : "long"} ${num(Math.abs(t.quantity), 0)} @ ${num(t.entry_price)}</li>`).join("")}</ul>`;
}

function openQuitDialog(pv) {
  if (pv.quitting) { refreshState(); toast("Already closing positions before quitting", "warn"); return; }
  const n = pv.left || 0;
  const parked = (pv.parked || []).length
    ? `<p class="muted">Not touched — held on another platform: ${pv.parked.map(t => `${escapeHtml(t.symbol)} (${VENUE_SHORT[t.venue] || escapeHtml(t.venue)})`).join(", ")}.</p>` : "";
  if (pv.paper) {
    openModal({
      title: "Quit AutoTradeBot?",
      bodyHTML: `${n ? `<p>Every open paper position on ${escapeHtml(pv.venue_label)} is closed first:</p>${posList(pv.positions)}` : "<p>No open paper positions.</p>"}
        <p>${pv.resets_simulator ? `Then the simulator is reset to $${Number(pv.reset_cash || 0).toLocaleString()} and the app shuts down.` : "Then the app shuts down."}</p>
        ${n ? '<p class="muted">Until the last position is out, nothing else can be changed.</p>' : ""}${parked}`,
      okText: n ? "Close all & quit" : "Quit", okClass: "danger",
      onOk: () => sendQuit(true),
    });
  } else if (n) {
    openModal({
      title: "Quit with LIVE positions open?",
      bodyHTML: `<div class="warn-box">${plural(n, "live position")} open on <b>${escapeHtml(pv.venue_label)}</b>.</div>${posList(pv.positions)}
        <p><b>Exit all &amp; quit</b> sends a market order for each and shuts down once they've all closed. Nothing else can change meanwhile.</p>
        <p><b>Cancel</b> keeps them open and the app running, so their stops and targets stay managed.</p>${parked}`,
      okText: "Exit all & quit", okClass: "danger", cancelText: "Cancel",
      onOk: () => sendQuit(true),
    });
  } else {
    openModal({
      title: "Quit AutoTradeBot?",
      bodyHTML: `<p>No open live positions — the app shuts down.</p>${parked}`,
      okText: "Quit", okClass: "danger",
      onOk: () => sendQuit(true),
    });
  }
}

async function sendQuit(closeAll) {
  const r = await post("/api/quit", { close_all: closeAll });
  toastResult(r);
  if (r.quit) { STATE.quit = r.quit; renderLock(); }
}

function showShutdown(note) {
  if (STOPPED) return;
  STOPPED = true;
  clearInterval(REFRESH_TIMER);
  closeModal(); closeDrawer(); hideTip();
  const d = document.createElement("div");
  d.className = "shutdown";
  d.innerHTML = `<div class="card"><h2>AutoTradeBot has shut down</h2>
    <p>${escapeHtml(note || "")}</p>
    <p class="muted">You can close this tab. Start it again with <code>python run.py</code>.</p></div>`;
  document.body.appendChild(d);
}

/* ---------- filters: what the bot scans for and may trade ---------- */
const FILTER_BOXES = {
  "f-long": ["sides", "LONG"], "f-short": ["sides", "SHORT"],
  "f-intraday": ["timeframes", "INTRADAY"], "f-swing": ["timeframes", "SWING"],
};

function syncFilterControls() {
  const f = STATE.filters;
  if (f) for (const [id, [group, val]] of Object.entries(FILTER_BOXES)) $("#" + id).checked = (f[group] || []).includes(val);
  renderSectorsButton();
}

async function changeFilter(box) {
  const [group] = FILTER_BOXES[box.id];
  const picked = Object.entries(FILTER_BOXES)
    .filter(([id, [g]]) => g === group && $("#" + id).checked).map(([, [, v]]) => v);
  if (!picked.length) {
    box.checked = true;
    toast(group === "sides" ? "Keep Long or Short switched on" : "Keep Intraday or Swing switched on", "warn");
    return;
  }
  const r = await post("/api/filters", { [group]: picked });
  if (!r.ok) { syncFilterControls(); toast("Filter not changed: " + (r.reason || ""), "bad"); return; }
  STATE.filters = r.filters; syncFilterControls(); renderPlays();
  toast(r.note + (r.rescanning ? " Rescanning for the new plays…" : ""), "good");
}
Object.keys(FILTER_BOXES).forEach(id => { $("#" + id).onchange = e => changeFilter(e.target); });

$("#f-hide-done").checked = store.get("atb-hide-done") === "1";
$("#f-hide-done").onchange = e => { store.set("atb-hide-done", e.target.checked ? "1" : "0"); renderPlays(); };

function renderSectorsButton() {
  const sel = (STATE.filters || {}).sectors || [];
  const b = $("#btn-sectors");
  b.textContent = !sel.length ? "Sectors: all" : sel.length === 1 ? `Sector: ${SECTOR_SHORT[sel[0]] || sel[0]}` : `Sectors: ${sel.length}`;
  b.classList.toggle("active-filter", sel.length > 0);
  b.title = sel.length ? "Only scanning and trading: " + sel.join(", ") : "Scanning and trading every sector — click to narrow it down";
}
$("#btn-sectors").onclick = async () => {
  let d;
  try { d = await api("/api/filters"); } catch { toast("The app isn't reachable", "bad"); return; }
  const all = d.all_sectors || [], selected = (d.filters || {}).sectors || [];
  const on = new Set(selected.length ? selected : all);
  openModal({
    title: "Sectors to scan and trade",
    bodyHTML: `<p class="muted">The scanner skips everything else, and plays outside these sectors can't be executed — by you or by Autopilot.</p>
      <div class="sector-grid">${all.map(s =>
        `<label><input type="checkbox" value="${escapeHtml(s)}" ${on.has(s) ? "checked" : ""}> ${escapeHtml(s)}</label>`).join("")}</div>
      <div class="row-gap"><button class="ghost mini" id="sec-all">Select all</button><button class="ghost mini" id="sec-none">Clear</button></div>`,
    okText: "Apply", okClass: "long",
    onOk: async () => {
      const picked = $$(".sector-grid input:checked").map(i => i.value);
      if (!picked.length) { toast("Pick at least one sector", "bad"); return; }
      const r = await post("/api/filters", { sectors: picked });
      if (!r.ok) { toast("Couldn't save: " + (r.reason || ""), "bad"); return; }
      STATE.filters = r.filters; renderSectorsButton(); renderPlays();
      toast(r.note + (r.rescanning ? " Rescanning…" : ""), "good");
    }
  });
  $("#sec-all").onclick = () => $$(".sector-grid input").forEach(i => { i.checked = true; });
  $("#sec-none").onclick = () => $$(".sector-grid input").forEach(i => { i.checked = false; });
};

/* ---------- plays table ---------- */
function filtered() {
  const f = STATE.filters || {};
  const sides = f.sides || ["LONG", "SHORT"], tfs = f.timeframes || ["INTRADAY", "SWING"];
  const hideDone = $("#f-hide-done").checked;
  return PLAYS.filter(p => sides.includes(p.side) && tfs.includes(p.timeframe) && (!hideDone || !isDone(p)));
}
function renderPlays() {
  const rows = filtered();
  $("#plays-count").textContent = rows.length ? `(${rows.length})` : "";
  $("#plays-empty").classList.toggle("hidden", rows.length > 0);
  const body = $("#plays-body");
  body.innerHTML = "";
  for (const p of rows) {
    const tr = document.createElement("tr");
    tr.dataset.id = p.id;
    const done = isDone(p), ap = p.autopilot || {};
    tr.classList.toggle("selected", p.id === SELECTED);
    tr.classList.toggle("done", done);
    tr.classList.toggle("ap-eligible", !!ap.eligible && !done);
    const apMark = ap.acted
      ? '<span class="ap-badge acted" data-term="autopilot">🤖</span>'
      : (ap.eligible && !done ? '<span class="ap-badge" data-term="autopilot">🤖</span>' : "");
    const last = done
      ? `<span class="badge ${p.status === "ERROR" ? "bad" : "good"}" data-term="executed">${p.status === "FILLED" ? "✓ executed" : p.status.toLowerCase()}</span>`
      : `<span class="info-dot">i</span>`;
    tr.innerHTML = `
      <td class="sym">${escapeHtml(p.symbol)} ${sectorTag(p.sector)}${apMark}${p.extended_hours_ok ? '<span class="ext" data-term="ext">ext</span>' : ""}</td>
      <td>${sideBadge(p.side)}</td>
      <td>${stratLabel(p.strategy)}</td>
      <td class="tf">${tfLabel(p.timeframe)}</td>
      <td class="num">${num(p.entry)}</td>
      <td class="num">${num(p.stop)}</td>
      <td class="num">${num((p.targets || [])[0])}</td>
      <td class="num">${num(p.reward_risk, 1)}</td>
      <td class="num">${p.suggested_qty || 0}</td>
      <td class="num">${usd(p.dollar_risk)}</td>
      <td class="num"><span class="score-bar"><i style="width:${Math.min(100, (p.score || 0) * 100)}%"></i></span></td>
      <td>${last}</td>`;
    tr.addEventListener("click", () => selectPlay(p.id));
    tr.addEventListener("mousemove", e => {
      if (e.target.closest("[data-term]")) return;      // the term's own explanation is showing
      showTipAt(e.clientX, e.clientY, ((STRATS[p.strategy] || {}).title || pretty(p.strategy)).toUpperCase(),
        (p.explanation || p.rationale || "").trim());
    });
    tr.addEventListener("mouseleave", hideTip);
    body.appendChild(tr);
  }
}

/* ---------- detail / confirm ---------- */
async function selectPlay(id) {
  SELECTED = id;
  $$("#plays-body tr").forEach(tr => tr.classList.toggle("selected", tr.dataset.id === id));
  $("#detail-empty").classList.add("hidden");
  const body = $("#detail-body");
  body.classList.remove("hidden");
  body.innerHTML = `<p class="muted">Assessing…</p>`;
  const a = await post(`/api/plays/${id}/assess`);
  if (SELECTED !== id) return;
  if (!a.ok) { body.innerHTML = `<p class="reasons">${escapeHtml(a.reason || "unavailable")}</p>`; return; }

  const p = a.play, op = a.order_preview, pdt = a.pdt || {}, em = (STATE.exit_manager || {});
  const brModeTxt = { native: "broker OCO (TP + SL)", managed: "auto exit manager", none: "none" }[op.bracket_mode] || op.bracket_mode;
  const exitLine = em.enabled
    ? `stop @ ${num(op.stop_loss)}, target @ ${num(op.take_profit)}, then break-even at ${num(em.breakeven_at_r, 1)}R`
    + (em.trail_start_r > 0 ? `, trail from ${num(em.trail_start_r, 1)}R (lock ${Math.round(em.trail_lock_ratio * 100)}%)` : "")
    + (p.timeframe === "INTRADAY" && em.flatten_intraday_before_close_min ? `, flatten ${em.flatten_intraday_before_close_min} min before the close` : "")
    : "OFF — you must close this manually";

  const executed = a.already_executed || isDone(p);
  const confirmBlock = executed
    ? `<div class="reasons">${p.status === "ERROR" ? "⚠ last attempt errored — dismiss and rescan" : "✓ Already executed" + (p.trade_id ? ` — trade <code>${escapeHtml(p.trade_id)}</code>` : "")}</div>
       <div class="confirm-row">
         ${p.trade_id ? `<button id="btn-goto-trade">Show trade record</button>` : ""}
         <button class="ghost" id="btn-reject">Dismiss</button>
       </div>`
    : `${a.reasons && a.reasons.length ? `<div class="reasons">⚠ ${a.reasons.map(escapeHtml).join("<br>")}</div>` : ""}
       <div class="confirm-row">
         <button class="${p.side === "LONG" ? "long" : "danger"} lockable" id="btn-approve" ${a.can_execute ? "" : "disabled"}>Execute &#10003; Yes</button>
         <button class="ghost" id="btn-reject">Dismiss</button>
       </div>`;

  body.innerHTML = `
    <h3>${escapeHtml(p.symbol)} ${sectorTag(p.sector)} ${sideBadge(p.side)}${executed ? ' <span class="badge good" data-term="executed">executed</span>' : ""}</h3>
    <div class="sub">${stratLabel(p.strategy)} · ${tfLabel(p.timeframe)} · conf ${num(p.confidence, 2)} · score ${num(p.score, 2)} · session ${a.session}</div>
    ${sparkSvg(p.evidence && p.evidence.spark, p)}
    <div class="explain">${escapeHtml(p.explanation || p.rationale)}</div>
    <div class="kv">
      <span data-term="entry">Entry</span><span>${num(p.entry)}</span>
      <span data-term="stop">Stop</span><span>${num(p.stop)} (${usd(-(Math.abs(p.entry - p.stop)))}/sh)</span>
      <span data-term="target">Target(s)</span><span>${(p.targets || []).map(t => num(t)).join(" → ")}</span>
      <span data-term="rr">Reward : Risk</span><span>${num(p.reward_risk, 1)} : 1</span>
    </div>
    ${renderEvidence(p.evidence || {})}
    <div class="order-card">
      <h4>Order that will be sent</h4>
      <div class="kv">
        <span>Routes to</span><span><b style="color:${(op.routes_to || "").includes("LIVE") ? "var(--short)" : "var(--accent)"}">${escapeHtml(op.routes_to)}</b></span>
        <span>Action</span><span>${op.side} ${op.qty} ${escapeHtml(p.symbol)}</span>
        <span>Order type</span><span><b>${op.order_type || "—"}</b> · ${op.session_label || ""}</span>
        ${op.limit_price != null ? `<span>Limit</span><span>${num(op.limit_price)}</span>` : ""}
        ${op.stop_price != null ? `<span>Stop trigger</span><span>${num(op.stop_price)}</span>` : ""}
        <span>Time in force</span><span>${op.tif || "DAY"}</span>
        <span>Protection</span><span>${brModeTxt}</span>
        <span>Est. cost</span><span>${usd(op.est_cost)}</span>
        <span data-term="risk">Est. risk</span><span>${usd(op.est_risk)}</span>
      </div>
      ${op.note ? `<div class="muted" style="margin:6px 0">${escapeHtml(op.note)}</div>` : ""}
      <div class="exit-box"><b>Automatic exit strategy:</b> ${exitLine}<br>
        <b>Expected hold:</b> ${op.expected_hold || "—"} — past that it's flagged for a manual look, the stop is untouched.</div>
      ${pdt.warnings && pdt.warnings.length ? `<ul class="warnings">${pdt.warnings.map(w => `<li>${escapeHtml(w)}</li>`).join("")}</ul>` : ""}
      <div class="kv"><span>Day trades used</span><span>${pdt.day_trades_used ?? "?"} (${pdt.day_trades_remaining ?? "?"} left)</span></div>
      ${confirmBlock}
    </div>`;
  const ap = $("#btn-approve"); if (ap) ap.onclick = () => approve(id);
  const rj = $("#btn-reject"); if (rj) rj.onclick = () => reject(id);
  const gt = $("#btn-goto-trade"); if (gt) gt.onclick = () => openRecord(p.trade_id);
}

function renderEvidence(ev) {
  const blocks = [];
  const sig = ev.signal;
  if (sig && sig.rows) {
    blocks.push(`<h4>Comps vs peers</h4><table class="ev-table">` +
      sig.rows.map(r => `<tr><td>${r.multiple}</td><td class="num">${r.target}</td>
        <td class="num">${r.peer_median}</td><td class="num">${r.gap_pct}%</td></tr>`).join("") +
      `</table><div class="muted">mean gap ${sig.mean_gap_pct}% → ${pretty(sig.verdict)}</div>`);
  }
  if (ev.dcf) {
    const d = ev.dcf, as = d.assumptions || {}, s = d.sensitivity || {};
    const disagree = d.disagreement_ratio && d.disagreement_ratio > 1
      ? `<div class="muted">exit-multiple vs perpetuity disagree ${d.disagreement_ratio}x — ${d.methods_agree ? "same direction, OK" : "conflicting → DCF treated as ambiguous"}</div>` : "";
    const sens = Object.keys(s).length
      ? `<div class="muted">sensitivity — g&nbsp;±1%: ${num(s["perp_g_-1pct"])} / ${num(s["perp_g_+1pct"])}; exit&nbsp;±20%: ${num(s["exit_mult_-20%"])} / ${num(s["exit_mult_+20%"])}</div>` : "";
    blocks.push(`<h4>DCF <span class="muted" style="font-weight:400">(UFCF: ${as.ufcf_method || "?"})</span></h4><div class="kv">
      <span>WACC</span><span>${(as.wacc * 100).toFixed(1)}%</span>
      <span>Cost of equity</span><span>${(as.cost_of_equity * 100).toFixed(1)}%</span>
      <span>Exit multiple</span><span>${as.exit_multiple}x</span>
      <span>Perpetuity g</span><span>${(as.perpetuity_growth * 100).toFixed(1)}%</span>
      <span>Price (multiple)</span><span>${num(d.price_multiple)}</span>
      <span>Price (perpetuity)</span><span>${num(d.price_perpetuity)}</span>
      <span>Blended fair value</span><span>${num(d.price_blended)} (${pct(d.upside_blended_pct)})</span></div>${disagree}${sens}`);
  }
  if (ev.football_field && ev.football_field.rows) {
    const f = ev.football_field;
    blocks.push(`<h4>Football field</h4><table class="ev-table">` +
      f.rows.map(r => `<tr><td>${r.method}</td><td class="num">${r.low}</td><td class="num">${r.high}</td></tr>`).join("") +
      `</table><div class="muted">band ${f.band_low}–${f.band_high}, fair value ${f.fair_value}</div>`);
  }
  const skip = new Set(["signal", "dcf", "football_field", "spark", "verdict", "peer_median", "target_multiples", "peers"]);
  const rest = Object.entries(ev).filter(([k]) => !skip.has(k));
  if (rest.length) {
    blocks.push(`<h4>Signal detail</h4><div class="kv">` +
      rest.map(([k, v]) => `<span>${escapeHtml(k)}</span><span>${typeof v === "object" ? "" : escapeHtml(v)}</span>`).join("") + `</div>`);
  }
  return blocks.join("");
}

function sparkSvg(arr, p) {
  if (!arr || arr.length < 3) return "";
  const w = 340, h = 44, lo = Math.min(...arr), hi = Math.max(...arr), rng = hi - lo || 1;
  const y = v => (h - (v - lo) / rng * h).toFixed(1);
  const pts = arr.map((v, i) => `${(i / (arr.length - 1) * w).toFixed(1)},${y(v)}`).join(" ");
  const line = (val, cls) => (val >= lo && val <= hi) ? `<line class="${cls}" x1="0" x2="${w}" y1="${y(val)}" y2="${y(val)}"/>` : "";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    ${line(p.entry, "ln-entry")}${line(p.stop, "ln-stop")}${line((p.targets || [])[0], "ln-target")}
    <polyline points="${pts}"/></svg>`;
}

async function approve(id) {
  const btn = $("#btn-approve"); if (btn) { btn.disabled = true; btn.textContent = "Sending…"; }
  const sym = (PLAYS.find(x => x.id === id) || {}).symbol || "";
  const r = await post(`/api/plays/${id}/approve`);
  if (r.ok) {
    const where = r.order_session === "EXTENDED" ? " (extended-hours limit)" : "";
    toast(`Order sent for ${sym}: ${r.order_type || ""} ${r.status || "ok"}${where}`, "good");
    mergePlay({ id, status: r.status === "FILLED" ? "FILLED" : "SUBMITTED", trade_id: r.trade_id || null });
    selectPlay(id);
    loadOpen();
  } else {
    toast("Not sent: " + (r.reason || "rejected"), "bad");
    if (r.already_executed) { mergePlay({ id, status: "FILLED", trade_id: r.trade_id || null }); selectPlay(id); }
    else if (btn) { btn.disabled = false; btn.innerHTML = "Execute &#10003; Yes"; }
  }
}
async function reject(id) {
  await post(`/api/plays/${id}/reject`);
  PLAYS = PLAYS.filter(p => p.id !== id); renderPlays();
  $("#detail-body").classList.add("hidden"); $("#detail-empty").classList.remove("hidden");
}

/* ---------- blotter ---------- */
$$(".tab").forEach(t => t.onclick = () => {
  $$(".tab").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  ["open", "history", "stats"].forEach(n => $("#tab-" + n).classList.toggle("hidden", n !== t.dataset.tab));
  ({ open: loadOpen, history: loadHistory, stats: loadStats })[t.dataset.tab]();
});

const isLive = () => STATE.mode === "live";
const hereVenue = () => (STATE.venue || {}).trading_on || "paper";

async function loadOpen() {
  if (STOPPED) return;
  let trades;
  try { ({ trades } = await api("/api/trades?status=OPEN")); } catch { return; }
  const pos = STATE.positions || [];
  const here = hereVenue();
  const el = $("#tab-open");
  $("#btn-exit-all").classList.toggle("hidden", !trades.some(t => (t.broker || "paper") === here));
  if (!trades.length) { el.innerHTML = `<p class="muted" style="padding:10px">No open positions.</p>`; return; }

  // sector concentration of the open book
  const byS = {};
  let tot = 0;
  trades.forEach(t => {
    const notl = Math.abs((t.quantity || 0) * (t.entry_price || 0));
    byS[t.sector || "Unknown"] = (byS[t.sector || "Unknown"] || 0) + notl; tot += notl;
  });
  const exp = tot ? Object.entries(byS).sort((a, b) => b[1] - a[1])
    .map(([s, v]) => `<b>${escapeHtml(SECTOR_SHORT[s] || s)}</b> ${Math.round(v / tot * 100)}%`).join(" · ") : "";

  el.innerHTML = (exp ? `<div class="exposure">Exposure: ${exp}</div>` : "") +
    `<table><thead><tr><th data-term="symbol">Symbol</th><th data-term="side">Side</th><th data-term="strategy_col">Strategy</th><th>Order</th>
    <th class="num" data-term="qty">Qty</th><th class="num" data-term="entry">Entry</th><th class="num" data-term="mark">Mark</th>
    <th class="num" data-term="unrealized">Unrealized</th><th class="num" data-term="stop">Stop</th><th class="num" data-term="target">Target</th>
    <th data-term="age">Age / Expected</th>
    <th class="num" data-term="mfe">MFE / MAE</th>
    <th data-term="auto_exit">Auto&nbsp;exit</th><th></th></tr></thead><tbody>${
    trades.map(t => {
      const venue = t.broker || "paper";
      const parked = venue !== here;
      const pp = pos.find(x => x.symbol === t.symbol) || {};
      const upl = parked ? null : pp.unrealized_pl;
      const moved = t.initial_stop_price != null && Math.abs((t.stop_price ?? 0) - t.initial_stop_price) > 0.01;
      const stopCell = moved ? `<span title="moved from ${num(t.initial_stop_price)}">${num(t.stop_price)} ▲</span>` : num(t.stop_price);
      const ts = t.time_status || "on_track";
      const barCls = ts === "overdue" ? "bad" : ts === "aging" ? "warn" : "ok";
      const timeCell = `<div class="timecell">
        <span>${t.held_label || "–"}</span>
        <span class="timebar"><i class="${barCls}" style="width:${Math.min(100, t.time_used_pct ?? 0)}%"></i></span>
        ${ts === "overdue" ? '<span class="badge bad">⏰ overdue</span>' : ts === "aging" ? '<span class="badge warn">aging</span>' : ""}</div>`;
      const parkedTag = parked
        ? ` <span class="badge warn" title="Opened on another platform. Its automatic exits pause until you switch back to it.">on ${VENUE_SHORT[venue] || escapeHtml(venue)}</span>` : "";
      return `<tr class="clickable-row ${ts === "overdue" ? "row-overdue" : ""}" data-record="${escapeHtml(t.id)}">
        <td class="sym">${escapeHtml(t.symbol)} ${sectorTag(t.sector)}${parkedTag}</td><td>${sideBadge(t.side)}</td>
        <td>${stratLabel(t.strategy)}</td>
        <td class="muted">${t.order_type || "—"}${t.order_session === "EXTENDED" ? " · ext" : ""}</td>
        <td class="num">${num(t.quantity, 0)}</td>
        <td class="num">${num(t.entry_price)}</td>
        <td class="num">${parked ? "–" : num(pp.market_price)}</td>
        <td class="num ${upl >= 0 ? "pl-pos" : "pl-neg"}">${usd(upl)}</td>
        <td class="num">${stopCell}</td>
        <td class="num">${num(t.target_price)}</td>
        <td>${timeCell}</td>
        <td class="num muted">${usd(t.mfe)} / ${usd(t.mae == null ? null : -t.mae)}</td>
        <td class="no-row-click"><label class="switch lockable"><input type="checkbox" data-managed="${escapeHtml(t.id)}" ${t.managed_exit ? "checked" : ""}><span></span></label></td>
        <td class="no-row-click"><button class="danger mini" data-exit="${escapeHtml(t.id)}" ${parked
          ? `disabled title="Switch back to ${VENUE_SHORT[venue] || escapeHtml(venue)} to exit this"` : 'title="Exit this position at the market"'}>Exit</button></td></tr>`;
    }).join("")}</tbody></table>`;

  const byId = Object.fromEntries(trades.map(t => [t.id, t]));
  $$("[data-exit]", el).forEach(b => b.onclick = () => confirmExit(byId[b.dataset.exit]));
  $$("[data-managed]", el).forEach(c => c.onchange = async () => {
    const r = await post(`/api/trades/${c.dataset.managed}/managed`, { on: c.checked });
    if (!r.ok) { c.checked = !c.checked; toast("Not changed: " + (r.reason || ""), "bad"); return; }
    toast(`Auto-exit ${c.checked ? "ON" : "OFF"} for that position`, c.checked ? "good" : "warn");
  });
  $$("tr[data-record]", el).forEach(tr => tr.onclick = e => {
    if (!e.target.closest(".no-row-click")) openRecord(tr.dataset.record);
  });
}

function confirmExit(t, fromRecord = false) {
  if (!t) return;
  const venue = (STATE.venue || {}).trading_on_label || "your broker";
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
      if (fromRecord && DRAWER === "record" && RECORD_ID === t.id) openRecord(t.id);
    }
  });
}

$("#btn-exit-all").onclick = async () => {
  let trades;
  try { ({ trades } = await api("/api/trades?status=OPEN")); } catch { toast("The app isn't reachable", "bad"); return; }
  const here = hereVenue();
  const mine = trades.filter(t => (t.broker || "paper") === here);
  if (!mine.length) { toast("No open positions to exit", "warn"); return; }
  const venue = (STATE.venue || {}).trading_on_label || "your broker";
  openModal({
    title: `Exit all ${plural(mine.length, "position")}?`,
    bodyHTML: `${isLive() ? `<div class="warn-box">This sends <b>real market orders</b> to ${escapeHtml(venue)}.</div>` : ""}
      ${posList(mine)}<p class="muted">Every close is sent at once, at the market.</p>`,
    okText: "Exit all", okClass: "danger",
    onOk: async () => { toastResult(await post("/api/trades/close-all")); loadOpen(); refreshState(); }
  });
};

async function loadHistory() {
  if (STOPPED) return;
  let trades;
  try { ({ trades } = await api("/api/trades?limit=200")); } catch { return; }
  const closed = trades.filter(t => t.status === "CLOSED");
  const el = $("#tab-history");
  if (!closed.length) { el.innerHTML = `<p class="muted" style="padding:10px">No closed trades yet.</p>`; return; }
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
  $$("tr[data-record]", el).forEach(tr => tr.onclick = () => openRecord(tr.dataset.record));
}
async function loadStats() {
  if (STOPPED) return;
  let s;
  try { s = await api("/api/pnl"); } catch { return; }
  const g = (label, val, cls) => `<div class="stat"><label>${label}</label><b class="${cls || ""}">${val}</b></div>`;
  const sign = v => v >= 0 ? "pl-pos" : "pl-neg";
  $("#tab-stats").innerHTML = `<div class="stat-grid">
    ${g("Realized today", usd(s.realized_today), sign(s.realized_today))}
    ${g("Realized week", usd(s.realized_week), sign(s.realized_week))}
    ${g("Realized total", usd(s.realized_total), sign(s.realized_total))}
    ${g("Closed trades", s.n_closed)}
    ${g("Win rate", num(s.win_rate, 1) + "%")}
    ${g("Avg win", usd(s.avg_win), "pl-pos")}
    ${g("Avg loss", usd(s.avg_loss), "pl-neg")}
    ${g("Profit factor", s.profit_factor ?? "–")}
    ${g("Expectancy / trade", usd(s.expectancy))}
    ${g("Best", usd(s.best), "pl-pos")}
    ${g("Worst", usd(s.worst), "pl-neg")}
  </div>`;
}

/* ---------- trade record (open or closed) ---------- */
async function openRecord(id) {
  if (!id) return;
  DRAWER = "record"; RECORD_ID = id; CONN = null;
  if ($("#drawer").classList.contains("hidden")) openDrawer("Trade record", `<p class="muted">Loading…</p>`, true);
  let rec;
  try {
    const res = await fetch(`/api/trades/${encodeURIComponent(id)}/record`);
    rec = await res.json();
    if (!res.ok) throw new Error(rec.detail || "Trade record not found.");
  } catch (e) {
    if (RECORD_ID === id) $("#drawer-body").innerHTML = `<p class="reasons">${escapeHtml(e.message || "Couldn't load the record.")}</p>`;
    return;
  }
  if (DRAWER === "record" && RECORD_ID === id) renderRecord(rec);
}

function orderSummary(req) {
  if (!req || typeof req !== "object") return "";
  const parts = [req.side, req.quantity, req.symbol, req.order_type].filter(v => v != null && v !== "");
  if (req.limit_price != null) parts.push(`@ ${num(req.limit_price)}`);
  if (req.stop_price != null) parts.push(`stop ${num(req.stop_price)}`);
  return parts.join(" ");
}

function renderRecord(rec) {
  const t = rec.trade, p = rec.play || {}, bp = rec.broker_position;
  const open = t.status === "OPEN";
  const moved = t.initial_stop_price != null && Math.abs((t.stop_price ?? 0) - t.initial_stop_price) > 0.01;
  $("#drawer-title").textContent = `${t.symbol} · ${open ? "open" : "closed"} trade`;
  const fills = rec.fills || [], orders = rec.orders || [];
  const atBroker = bp ? `${num(bp.quantity, 0)} @ ${num(bp.market_price)} · <span class="${bp.unrealized_pl >= 0 ? "pl-pos" : "pl-neg"}">${usd(bp.unrealized_pl)}</span>`
    : rec.on_current_venue ? "not reported yet" : `held on ${escapeHtml(rec.venue_label)} — switch to it to manage`;
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
  const ex = $("#rec-exit");
  if (ex) ex.onclick = () => confirmExit(t, true);
}

/* ---------- strategies: switch setups on/off, set their weight ---------- */
function indexStrategies(rows) {
  if (!rows) return;
  STRATS = {};
  rows.forEach(s => { STRATS[s.key] = s; });
  STATE.strategies_on = rows.filter(s => s.enabled).length;
  $("#btn-strategies").textContent = `Strategies · ${STATE.strategies_on}`;
}
async function loadStrategies() {
  const d = await api("/api/strategies");
  indexStrategies(d.strategies);
}

async function openStrategies() {
  DRAWER = "strategies"; CONN = null; RECORD_ID = null;
  openDrawer("Strategies", `<p class="muted">Loading…</p>`, true);
  try { await loadStrategies(); } catch {
    $("#drawer-body").innerHTML = `<p class="reasons">Couldn't load the strategies.</p>`; return;
  }
  if (DRAWER === "strategies") renderStrategies();
}

function stratCard(s) {
  return `<div class="strat ${s.enabled ? "" : "off"}">
    <div class="strat-head">
      <label class="switch lockable" title="${s.enabled ? "On — click to switch off" : "Off — click to switch on"}">
        <input type="checkbox" data-strat-toggle="${escapeHtml(s.key)}" ${s.enabled ? "checked" : ""}><span></span></label>
      <div><div class="meta">${s.timeframe === "INTRADAY" ? "day trade" : "swing"} · ${escapeHtml(s.kind.toLowerCase())}${s.customized ? " · changed from config" : ""}</div>
        <h4>${escapeHtml(s.title)}</h4></div>
      <label class="weight lockable" title="Scales this setup's score — 1 is normal">weight
        <input type="number" min="0.1" max="3" step="0.1" value="${s.weight}" data-strat-weight="${escapeHtml(s.key)}"></label>
    </div>
    <div class="thesis">${escapeHtml(s.thesis)}</div>
  </div>`;
}

function renderStrategies() {
  const rows = Object.values(STRATS);
  const groups = [
    ["Day trades", s => s.kind === "TECHNICAL" && s.timeframe === "INTRADAY"],
    ["Swing trades", s => s.kind === "TECHNICAL" && s.timeframe !== "INTRADAY"],
    ["Valuation (fundamental)", s => s.kind === "FUNDAMENTAL"],
  ];
  const on = rows.filter(s => s.enabled).length;
  $("#drawer-body").innerHTML = `
    <p class="muted">${on} of ${rows.length} setups on. A change applies to the next scan and to Autopilot straight away,
      shows up in every open tab, and is remembered. Plays from a setup you switch off leave the board.</p>
    ${groups.map(([name, test]) => {
      const g = rows.filter(test);
      return g.length ? `<div class="group-title">${name}</div>${g.map(stratCard).join("")}` : "";
    }).join("")}
    <div class="row-gap"><button class="ghost mini lockable" id="strat-reset">Reset to config.yaml</button></div>`;
  $$("[data-strat-toggle]").forEach(c => c.onchange = () => updateStrategy(c.dataset.stratToggle, { enabled: c.checked }));
  $$("[data-strat-weight]").forEach(i => i.onchange = () => {
    const w = parseFloat(i.value);
    if (!(w >= 0.1 && w <= 3)) { toast("Weight must be between 0.1 and 3", "bad"); renderStrategies(); return; }
    updateStrategy(i.dataset.stratWeight, { weight: w });
  });
  $("#strat-reset").onclick = async () => {
    const r = await post("/api/strategies/reset");
    toastResult(r);
    if (r.ok) { indexStrategies(r.strategies); renderStrategies(); }
  };
}

async function updateStrategy(key, body) {
  const r = await post(`/api/strategies/${encodeURIComponent(key)}`, body);
  if (r.ok) { indexStrategies(r.strategies); toast(r.note + (r.rescanning ? " Rescanning…" : ""), "good"); }
  else toast("Strategy not changed: " + (r.reason || ""), "bad");
  if (DRAWER === "strategies") renderStrategies();
}
$("#btn-strategies").onclick = openStrategies;

/* ---------- toasts, modal, drawer ---------- */
function toast(text, cls) {
  const d = document.createElement("div");
  d.className = "toast " + (cls || "");
  d.textContent = text;
  $("#toasts").appendChild(d);
  setTimeout(() => d.remove(), 6500);
}
const toastResult = r => toast(r.ok ? (r.note || "Done") : (r.reason || r.detail || "Failed"), r.ok ? "good" : "bad");

function openModal({ title, bodyHTML, okText = "Confirm", okClass = "danger", cancelText = "Cancel", onOk }) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = bodyHTML;
  const ok = $("#modal-ok");
  ok.textContent = okText;
  ok.className = okClass;
  ok.onclick = async () => { closeModal(); await onOk(); };
  $("#modal-cancel").textContent = cancelText;
  $("#modal-cancel").onclick = closeModal;
  $("#modal").onclick = e => { if (e.target.id === "modal") closeModal(); };
  $("#modal").classList.remove("hidden");
  setTimeout(() => { const i = $("#modal-body input"); if (i) i.focus(); }, 50);
}
function closeModal() { $("#modal").classList.add("hidden"); }

function openDrawer(title, html, wide = false) {
  $("#drawer-title").textContent = title;
  $("#drawer-body").innerHTML = html;
  $("#drawer-inner").classList.toggle("wide", wide);
  $("#drawer").classList.remove("hidden");
}
function closeDrawer() { $("#drawer").classList.add("hidden"); DRAWER = null; CONN = null; RECORD_ID = null; }
$("#drawer-close").onclick = closeDrawer;
$("#drawer").onclick = e => { if (e.target.id === "drawer") closeDrawer(); };
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (!$("#modal").classList.contains("hidden")) closeModal();
  else if (!$("#drawer").classList.contains("hidden")) closeDrawer();
});

function busy(btn, text) { if (!btn) return; btn.dataset.label = btn.textContent; btn.textContent = text; btn.disabled = true; }
function unbusy(btn) { if (!btn) return; btn.textContent = btn.dataset.label || btn.textContent; btn.disabled = false; }

/* ---------- connections: brokers, keys, sign-in ---------- */
const connOpen = () => DRAWER === "connections" && !$("#drawer").classList.contains("hidden");

async function openConnections() {
  if (!connOpen()) { DRAWER = "connections"; RECORD_ID = null; openDrawer("Connections", `<p class="muted">Loading…</p>`, true); }
  let d;
  try { d = await getLocal("/api/setup"); }
  catch { if (connOpen()) $("#drawer-body").innerHTML = `<p class="reasons">Couldn't load connection settings.</p>`; return; }
  if (!connOpen()) return;                         // closed or replaced while loading
  if (d.detail) { $("#drawer-body").innerHTML = `<p class="reasons">${escapeHtml(d.detail)}</p>`; return; }
  CONN = d;
  renderConnections();
}

function renderConnections() {
  const d = CONN, v = d.venue, ib = d.ibkr, sw = d.schwab;
  const tok = sw.token || {}, login = sw.login || {};
  const fieldsOf = g => d.fields.filter(f => f.group === g).map(fieldHTML).join("");
  const radios = (name, opts, cur) => Object.entries(opts).map(([k, label]) =>
    `<label class="radio"><input type="radio" name="${name}" value="${k}" ${k === cur ? "checked" : ""}> ${escapeHtml(label)}</label>`).join("");
  const port = (label, p, open) =>
    `<span class="badge ${open ? "good" : "bad"}">${label} port ${p}: ${open ? "listening" : "closed"}</span>`;
  const problems = [...(v.blockers || []), ...(v.live_blockers || [])];
  const tokCls = !tok.exists || tok.needs_reauth ? "bad" : tok.needs_rotation ? "warn" : "good";
  const steps = list => `<details><summary>Setup steps</summary><ol class="steps">${list.map(s => `<li>${escapeHtml(s)}</li>`).join("")}</ol></details>`;

  $("#drawer-body").innerHTML = `
    <section class="conn">
      <h4>Where orders go</h4>
      <div class="conn-now">Right now <b>${v.mode === "live" ? "LIVE" : "paper"}</b> orders go to <b>${escapeHtml(v.trading_on_label)}</b>.</div>
      <div class="conn-grid lockable">
        <div><span class="group-label">Paper trades on</span>${radios("paper_platform", v.paper_platforms, v.paper_platform)}</div>
        <div><span class="group-label">Live trades on</span>${radios("live_broker", v.live_brokers, v.live_broker)}</div>
      </div>
      ${problems.length ? `<div class="reasons">⚠ ${problems.map(escapeHtml).join("<br>")}</div>` : ""}
      <div class="row-gap"><button class="ghost mini" id="conn-reconnect">Reconnect</button>
        <span class="muted small">Switching is blocked while positions are open on the current platform.</span></div>
    </section>

    <section class="conn">
      <h4>Interactive Brokers <span class="muted">· no token — the running IB Gateway is the login</span></h4>
      <div class="status-line">${port("Paper", ib.ports.paper, ib.listening.paper)} ${port("Live", ib.ports.live, ib.listening.live)}
        ${ib.installed ? "" : '<span class="badge bad">ib_async not installed</span>'}</div>
      <form class="field-grid" id="form-ibkr" onsubmit="return false">${fieldsOf("ibkr")}</form>
      <div class="row-gap">
        <button class="mini" id="ibkr-save">Save</button>
        <button class="ghost mini" data-probe="paper">Test paper</button>
        <button class="ghost mini" data-probe="live">Test live</button>
      </div>
      <div class="result hidden" id="ibkr-result"></div>
      ${steps(ib.steps)}
    </section>

    <section class="conn">
      <h4>thinkorswim / Schwab <span class="muted">· sign in once a week</span></h4>
      <div class="status-line"><span class="badge ${tokCls}">${escapeHtml(tok.message || "")}</span>
        ${sw.installed ? "" : '<span class="badge bad">schwab-py not installed</span>'}</div>
      <form class="field-grid" id="form-schwab" onsubmit="return false">${fieldsOf("schwab")}</form>
      <div class="row-gap">
        <button class="mini" id="schwab-save">Save</button>
        <button class="long mini" id="schwab-login" ${sw.keys_set && sw.installed ? "" : "disabled"}
          title="${sw.keys_set ? "Opens Schwab's login in your browser" : "Save your app key and secret first"}">Sign in with Schwab</button>
      </div>
      <div class="result hidden" id="schwab-login-status"></div>
      ${steps(sw.steps)}
    </section>

    <p class="muted small">Keys are saved to <code>.env</code> on this computer and never shown again — only their last 4 characters.
      These settings can only be changed from this machine.</p>`;

  showLoginStatus(login);
  $$('input[name="paper_platform"],input[name="live_broker"]').forEach(r => r.onchange = saveRouting);
  $("#conn-reconnect").onclick = async e => {
    busy(e.target, "Reconnecting…");
    toastResult(await post("/api/setup/reconnect"));
    openConnections();
  };
  $("#ibkr-save").onclick = e => saveFields("form-ibkr", e.target);
  $("#schwab-save").onclick = e => saveFields("form-schwab", e.target);
  $$("[data-probe]").forEach(b => b.onclick = () => probeIbkr(b));
  $("#schwab-login").onclick = startSchwabLogin;
  $$(".field-clear").forEach(a => a.onclick = ev => {
    ev.preventDefault();
    const input = $(`[data-key="${a.dataset.clear}"]`);
    input.dataset.clear = "1"; input.value = ""; input.placeholder = "will be cleared when you save";
  });
}

function fieldHTML(f) {
  const id = `fld-${f.key}`;
  let input, orig;
  if (f.kind === "bool") {
    orig = ["1", "true", "yes", "on"].includes(String(f.value || f.default).toLowerCase()) ? "1" : "0";
    input = `<input type="checkbox" id="${id}" data-key="${f.key}" data-orig="${orig}" ${orig === "1" ? "checked" : ""}>`;
  } else if (f.kind === "choice") {
    orig = f.value || f.default;
    input = `<select id="${id}" data-key="${f.key}" data-orig="${escapeHtml(orig)}">${
      f.choices.map(c => `<option ${c === orig ? "selected" : ""}>${escapeHtml(c)}</option>`).join("")}</select>`;
  } else if (f.secret) {
    input = `<input type="password" id="${id}" data-key="${f.key}" autocomplete="new-password" spellcheck="false"
      placeholder="${f.set ? `saved ${escapeHtml(f.hint || "")} — type to replace` : "not set"}">`;
  } else {
    orig = f.value || "";
    input = `<input type="${f.kind === "int" ? "number" : "text"}" id="${id}" data-key="${f.key}" spellcheck="false"
      data-orig="${escapeHtml(orig)}" value="${escapeHtml(orig)}" placeholder="${escapeHtml(f.default || "")}">`;
  }
  const clear = f.secret && f.set ? ` <a href="#" class="field-clear" data-clear="${f.key}">clear</a>` : "";
  return `<label for="${id}">${escapeHtml(f.label)}${clear}</label>
    <div>${input}${f.help ? `<div class="help">${escapeHtml(f.help)}</div>` : ""}</div>`;
}

async function saveFields(formId, btn) {
  const values = {};
  $$(`#${formId} [data-key]`).forEach(i => {
    const k = i.dataset.key;
    if (i.type === "password") {
      if (i.dataset.clear === "1") values[k] = "";
      else if (i.value.trim()) values[k] = i.value.trim();
    } else if (i.type === "checkbox") {
      if ((i.checked ? "1" : "0") !== i.dataset.orig) values[k] = i.checked;
    } else if (i.value !== i.dataset.orig) {
      values[k] = i.value;
    }
  });
  if (!Object.keys(values).length) { toast("Nothing changed", "warn"); return; }
  busy(btn, "Saving…");
  const r = await post("/api/setup/secrets", { values });
  toastResult(r);
  if (r.ok) openConnections(); else unbusy(btn);
}

async function saveRouting() {
  const pick = name => ($(`input[name="${name}"]:checked`) || {}).value;
  toastResult(await post("/api/setup/brokers", { paper_platform: pick("paper_platform"), live_broker: pick("live_broker") }));
  openConnections();
}

async function probeIbkr(btn) {
  const out = $("#ibkr-result");
  busy(btn, "Testing…");
  out.className = "result";
  out.textContent = `Connecting to the ${btn.dataset.probe} Gateway (read-only)…`;
  const r = await post("/api/setup/ibkr/test", { account: btn.dataset.probe });
  unbusy(btn);
  out.className = "result " + (r.ok ? "good" : "bad");
  out.textContent = r.ok ? r.note : (r.reason || r.detail || "Test failed");
}

async function startSchwabLogin() {
  const r = await post("/api/setup/schwab/login");
  if (r.ok) toast("Opening Schwab's login in your browser…", "good");
  else toast("Schwab sign-in: " + (r.reason || r.detail || "failed"), "bad");
  showLoginStatus(r.ok ? r.login : { state: "error", message: r.reason || r.detail });
}
function showLoginStatus(login) {
  const el = $("#schwab-login-status");
  if (!el || !login) return;
  el.className = "result " + ({ error: "bad", ok: "good" }[login.state] || "");
  el.textContent = login.message || "";
  el.classList.toggle("hidden", !login.message);
}

$("#btn-connections").onclick = openConnections;
$("#pill-conn").onclick = openConnections;
$("#pill-conn").onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openConnections(); } };

/* ---------- paper / live toggle ---------- */
$$("#mode-switch .seg").forEach(b => b.onclick = async () => {
  const target = b.dataset.mode;
  if (target === (STATE.mode || "paper")) return;
  if (target === "paper") {
    const r = await post("/api/broker", { mode: "paper" });
    if (!r.ok) toast("Switch failed: " + (r.reason || ""), "bad");
    return;
  }
  const v = STATE.venue || {};
  const broker = (v.live_brokers || {})[v.live_broker] || "broker";
  openModal({
    title: "Switch to LIVE trading?",
    bodyHTML: `<div class="warn-box">Orders you approve will be sent to your <b>real ${escapeHtml(broker)} account</b> and use real money.</div>
      <p class="muted">Every order still needs your click (or Autopilot, if you've allowed it live). The
      $${((STATE.account || {}).min_start_equity || 2000).toLocaleString()} equity floor and the 3-day-trade PDT cap apply.
      Change the live broker under Connections.</p>`,
    okText: "Go Live", okClass: "danger",
    onOk: async () => {
      const r = await post("/api/broker", { mode: "live" });
      if (!r.ok) toast("Could not go live: " + [r.reason, ...(r.blockers || [])].filter(Boolean).join(" — "), "bad");
    }
  });
});

$("#btn-reset-paper").onclick = () => {
  const cur = (STATE.account && STATE.account.paper_start_cash) || 100000;
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
    }
  });
};

$("#btn-refresh").onclick = async () => {
  const b = $("#btn-refresh"); busy(b, "↻ …");
  const r = await post("/api/account/refresh");
  if (r.state) { STATE = r.state; renderTop(); }
  loadOpen();
  setTimeout(() => unbusy(b), 800);
};

$("#btn-scan").onclick = async () => {
  const b = $("#btn-scan");
  b.disabled = true;
  const r = await post("/api/scan/now");
  toast(r.ok ? "Scan queued" : "Not scanning: " + (r.reason || ""), r.ok ? "good" : "bad");
  setTimeout(() => { b.disabled = false; }, 4000);
};

/* ---------- autopilot (hands-off entry) ---------- */
async function postAutopilot(body) {
  const r = await post("/api/autopilot", body);
  if (r.autopilot) { STATE.autopilot = r.autopilot; renderAutopilot(); }
  if (r.note) toast(r.note, r.autopilot && r.autopilot.effective ? "good" : "warn");
  else if (!r.ok) toast("Autopilot: " + (r.reason || "update failed"), "bad");
  return r;
}
$("#ap-toggle").onclick = () => {
  const ap = STATE.autopilot || {};
  if (ap.enabled) return postAutopilot({ enabled: false });
  const live = STATE.mode === "live";
  openModal({
    title: live ? "Turn on Autopilot in LIVE mode?" : "Turn on Autopilot?",
    bodyHTML: `<div class="${live && !ap.allow_live ? "warn-box" : "muted"}">
        ${live && !ap.allow_live
      ? "Autopilot will arm for <b>paper only</b> — it will not route real orders until you set <code>autopilot.allow_live: true</code> in config/config.yaml."
      : "The bot will <b>place entries for you</b> when a play clears the gate. Exits are already automatic. It stays inside the per-day and position caps and the 2:1 minimum."}
      </div>
      <p class="muted">Trade types: <b>${(ap.trade_types || ["INTRADAY"]).map(t => t === "INTRADAY" ? "day" : "swing").join(", ")}</b> ·
      ≤ ${ap.max_auto_positions ?? 2} open · ≤ ${ap.max_auto_trades_per_day ?? 3}/day · ≥ conf ${ap.min_confidence ?? 0.62}.
      Change these with the ⚙ button.</p>`,
    okText: "Turn on", okClass: live && !ap.allow_live ? "danger" : "long",
    onOk: () => postAutopilot({ enabled: true })
  });
};
$("#ap-cfg").onclick = () => {
  const ap = STATE.autopilot || {};
  const has = t => (ap.trade_types || []).includes(t) ? "checked" : "";
  openModal({
    title: "Autopilot settings",
    bodyHTML: `<div class="ap-form">
      <label>Auto-take these trade types</label>
      <div class="ap-row">
        <label><input type="checkbox" id="ap-day" ${has("INTRADAY")}> Day trades</label>
        <label><input type="checkbox" id="ap-swing" ${has("SWING")}> Swing trades</label>
      </div>
      <label>Minimum confidence <b id="ap-conf-v">${ap.min_confidence ?? 0.62}</b></label>
      <input type="range" id="ap-conf" min="0.4" max="0.9" step="0.01" value="${ap.min_confidence ?? 0.62}">
      <label>Minimum reward : risk</label>
      <input type="number" id="ap-rr" min="1" max="10" step="0.5" value="${ap.min_reward_risk ?? 2}">
      <div class="ap-row">
        <span><label>Max open positions</label><input type="number" id="ap-maxpos" min="0" max="10" step="1" value="${ap.max_auto_positions ?? 2}"></span>
        <span><label>Max per day</label><input type="number" id="ap-maxday" min="0" max="20" step="1" value="${ap.max_auto_trades_per_day ?? 3}"></span>
      </div>
      <div class="ap-row">
        <span><label>Max per strategy</label><input type="number" id="ap-maxstrat" min="1" max="10" step="1" value="${ap.max_per_strategy ?? 2}"></span>
        <span><label>New per scan</label><input type="number" id="ap-maxcycle" min="1" max="10" step="1" value="${ap.max_new_per_cycle ?? 1}"></span>
      </div>
      <label><input type="checkbox" id="ap-cooldown" ${ap.cooldown_after_loss !== false ? "checked" : ""}> Cool off a ticker for the day after it stops out</label>
      <label><input type="checkbox" id="ap-dry" ${ap.dry_run ? "checked" : ""}> Dry run (log what it would do, place nothing)</label>
      <p class="muted">Live routing also needs <code>autopilot.allow_live: true</code> in config.yaml. The Long / Short, Intraday / Swing and
      Sectors filters and the Strategies panel apply to Autopilot too. Exits are automatic no matter what.</p>
    </div>`,
    okText: "Save", okClass: "long",
    onOk: async () => {
      const types = [];
      if ($("#ap-day").checked) types.push("INTRADAY");
      if ($("#ap-swing").checked) types.push("SWING");
      const int = sel => parseInt($(sel).value, 10);
      await postAutopilot({
        trade_types: types.length ? types : ["INTRADAY"],
        min_confidence: parseFloat($("#ap-conf").value),
        min_reward_risk: parseFloat($("#ap-rr").value),
        max_auto_positions: int("#ap-maxpos"),
        max_auto_trades_per_day: int("#ap-maxday"),
        max_per_strategy: int("#ap-maxstrat"),
        max_new_per_cycle: int("#ap-maxcycle"),
        cooldown_after_loss: $("#ap-cooldown").checked,
        dry_run: $("#ap-dry").checked
      });
    }
  });
  const cs = $("#ap-conf"); if (cs) cs.oninput = () => { $("#ap-conf-v").textContent = cs.value; };
};

/* ---------- boot ---------- */
syncThemeButton();
refreshState();
loadStrategies().then(renderPlays).catch(() => { /* names fall back to their keys */ });
api("/api/plays").then(d => { PLAYS = d.plays || []; renderPlays(); }).catch(() => { });
loadOpen();
connect();
REFRESH_TIMER = setInterval(refreshState, 15000);
