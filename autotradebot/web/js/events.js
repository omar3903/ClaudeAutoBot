/* The live feed: engine events over the WebSocket, applied to the dashboard. */
import { $, count, fmtClock, isNewer, num, pct, plural, pretty, usd } from "./util.js";
import { S, emit, on, refreshState, setState } from "./state.js";
import { closeModal, drawerOpen, toast } from "./ui.js";
import { noteDisarmed, renderCapital, renderOpenPL } from "./topbar.js";
import { openQuitDialog, renderLock, showShutdown } from "./quit.js";
import { onDailyLoss, renderAutopilot } from "./autopilot.js";
import { onScanEvent } from "./scan.js";
import { mergePlay, renderPlays, selectPlay, tickPlays } from "./plays.js";
import { loadHistory, loadOpen, loadStats, openRecord, recordGone, tabVisible, tickOpen } from "./blotter.js";
import { renderWatchlist } from "./watchlist.js";
import { indexStrategies, onReplayEvent } from "./strategies.js";
import { loadOrders, ordersChanged } from "./orders.js";
import { addNotes } from "./notes.js";
import { reportsUpdated } from "./reports.js";
import { signalsUpdated } from "./signals.js";
import { loadPairs, showPairs } from "./pairs.js";
import { priceTicks } from "./price.js";

export function connect() {
  if (S.stopped) return;
  if (!linkTicker) linkTicker = setInterval(renderLink, 1000);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { S.live.open = true; S.live.downSince = null; renderLink(); };
  ws.onmessage = ev => {
    S.live.lastAt = Date.now();
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handle(msg.topic, msg.payload || {});
  };
  ws.onclose = () => {
    // a retry that fails again doesn't restart the count - the link has been down since it first dropped
    if (S.live.open) { S.live.open = false; S.live.downSince = Date.now(); renderLink(); }
    if (!S.stopped) setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
}

/* Whether this tab still hears the app. The socket closing is the only sign of an outage: a quiet spell (off
   hours a snapshot comes about every 30 s) isn't one, so the last message's age is only the pill's text. The
   banner and the disabled actions wait until it has been closed for 10 s - longer than a reconnect takes - and
   the shut-down screen wins over both. The hello a reconnect brings re-syncs the tables. */
const OFFLINE_AFTER_MS = 10000;
let linkTicker = null;

function ago(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 90 ? `${s} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`;
}

function renderLink() {
  const L = S.live, pill = $("#pill-live"), banner = $("#offline-banner"), now = Date.now();
  if (S.stopped) {
    clearInterval(linkTicker); linkTicker = null;
    document.body.classList.remove("offline");
    banner.classList.add("hidden");
    return;
  }
  const offline = !L.open && now - L.downSince >= OFFLINE_AFTER_MS;
  const text = L.open ? (L.lastAt ? `live · ${ago(now - L.lastAt)}` : "live")
    : L.lastAt ? `reconnecting · last update ${ago(now - L.lastAt)} ago` : "connecting…";
  const cls = "pill " + (L.open ? "good" : offline ? "bad" : "warn"), title = L.open
    ? "This tab hears the app's updates as they happen - the time is since the last one. Off hours one comes about every 30 s."
    : "This tab has lost its link to the app and tries again every 2 s. What it shows isn't updating meanwhile.";
  if (pill.textContent !== text) pill.textContent = text;
  if (pill.className !== cls) pill.className = cls;
  if (pill.title !== title) pill.title = title;
  document.body.classList.toggle("offline", offline);
  if (offline !== banner.classList.contains("hidden")) return;       // already showing, or already hidden
  banner.classList.toggle("hidden", !offline);
  if (offline) banner.innerHTML = `<span>⚠ No word from the app since ${fmtClock(new Date(L.lastAt || L.downSince).toISOString())}
    - it may be restarting or stopped, and this tab reconnects by itself. What's shown is from then and isn't updating, and the
    buttons that send orders or change settings are off until it's back.</span>`;
}

/* The dashboard's files as they were when this tab loaded them. After the app is updated and restarted the
   tab reconnects but would go on running its old scripts - so when the stamp has changed it loads the new
   ones, unless a dialog is open with something half-typed in it. */
let webBuild = null;
function loadNewScripts(build) {
  if (!build) return;
  if (webBuild === null) { webBuild = build; return; }
  if (build === webBuild) return;
  if ($("#modal") && !$("#modal").classList.contains("hidden")) {
    toast("The dashboard was updated - refresh the page to load it", "warn");
    return;
  }
  location.reload();
}

/* Streamed prices (prices.tick): at most one message a second while IBKR streams, with each stock whose price
   moved. They go into what the tab holds - the positions' marks and unrealized, the plays' prices - and straight
   into the cells that show them, in place: the header's Unrealized, the open positions' Mark, Unrealized and
   R now, the plays' Price and the panels' market price. A snapshot or a board push can carry an older price
   than a tick already shown, so the latest ticks are kept and applied again after one where they're newer.
   Hidden, the tab keeps the numbers and draws them once it's shown. */
let lastTicks = {};                  // symbol -> its latest {price, at, session}

function onPrices(prices) {
  Object.assign(lastTicks, prices);
  const held = markPositions(prices, true), draw = !document.hidden;
  tickPlays(prices, draw);
  if (!draw) return;
  if (held.length) { renderOpenPL(); tickOpen(held); }
  priceTicks(prices);
}

/* The positions held in `prices`' stocks at their newer price, and their unrealized worked out as the server's
   views.positions does. A tick just pushed also replaces the broker's own mark (a position with no price time);
   applied again after a snapshot, it leaves that mark - the app had no price of its own fresh enough. Returns
   the stocks it changed. */
function markPositions(prices, pushed) {
  const changed = [];
  (S.state.positions || []).forEach(pos => {
    const t = prices[pos.symbol];
    if (!t || !(pos.price_at ? isNewer(t.at, pos.price_at) : pushed)) return;
    pos.market_price = t.price;
    pos.price_at = t.at;
    pos.unrealized_pl = Math.round((t.price - pos.avg_price) * pos.qty * 100) / 100;
    changed.push(pos.symbol);
  });
  return changed;
}

export function initTicks() {
  // every snapshot, the 15 s refresh's too, keeps a tick newer than its marks
  on("state", () => { if (markPositions(lastTicks, false).length && !document.hidden) renderOpenPL(); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    renderOpenPL();
    tickOpen(Object.keys(lastTicks));
    renderPlays();                   // the rows whose price changed while hidden
    priceTicks(lastTicks);
  });
}

function handle(topic, p) {
  if (topic === "hello") loadNewScripts(p.web_build);
  switch (topic) {
    case "hello":
    case "account.snapshot":
    case "engine.started":
      if (topic === "hello") lastTicks = {};    // a new link: the app's own snapshot is the word from here
      setState(p.state || p);
      if (tabVisible("open")) loadOpen();
      if (topic === "hello") {                   // after a reconnect, the tab showing may be out of date
        loadOrders();
        if (tabVisible("history")) loadHistory();
        if (tabVisible("stats")) loadStats();
      }
      break;
    case "plays.updated":
      S.plays = p.plays || [];
      tickPlays(lastTicks, false);               // a tick newer than the board's price stays
      emit("plays");
      break;
    case "prices.tick":
      onPrices(p.prices || {});
      break;
    case "plays.changes":
      addNotes(p.notes);
      break;

    case "scan.started":
    case "scan.progress":
    case "scan.completed":
    case "scan.failed":
      onScanEvent(topic, p);
      break;
    case "watchlist.updated":
      if (p.scan) { S.state.scan = p.scan; emit("scan"); }
      if (tabVisible("watchlist")) renderWatchlist(p.watchlist, p.scan);
      break;
    case "settings.updated":
      S.state.scan = p.scan;
      emit("scan");
      break;

    case "order.filled":
      toast(`Filled: ${p.symbol} entry x${p.qty} @ ${num(p.price)}`, "good");
      if (p.play) mergePlay(p.play);
      if (p.trade_id && S.plays.some(x => x.id === S.selected && x.trade_id === p.trade_id)) selectPlay(S.selected);
      loadOpen(); refreshState();
      break;
    case "trade.closed":
    case "exit.triggered": {
      const t = p.trade || {};
      toast(`${topic === "exit.triggered" ? "Auto-exit" : "Closed"} ${t.symbol}: ${usd(t.realized_pl)} (${pct(t.realized_pl_pct)})${p.reason ? ` [${p.reason}]` : ""}`,
        (t.realized_pl || 0) >= 0 ? "good" : "bad");
      if (t.id && t.id === S.recordId && drawerOpen("record")) openRecord(t.id);
      loadOpen(); loadHistory(); loadStats(); refreshState();
      break;
    }
    case "trade.reduced":
    case "exit.scaled": {
      const t = p.trade || {};
      toast(`${t.symbol}: ${num(p.qty, 0)} of the position off at ${num(p.price || t.exit_price)} · ${usd(t.banked_pl)} banked · `
        + `stop → ${num(t.stop_price)}, target → ${num(t.target_price)}`, "good");
      if (t.id && t.id === S.recordId && drawerOpen("record")) openRecord(t.id);
      loadOpen(); refreshState();
      break;
    }
    case "trades.removed": {
      const gone = p.trades || [];
      toast(`Removed ${plural(gone.length, "open-trade record")} no longer held at ${p.venue_label || "the broker"}: ` +
        gone.map(t => t.symbol).join(", "), "warn");
      recordGone(gone.map(t => t.id));
      loadOpen(); refreshState();
      break;
    }
    case "exit.not_held":
      toast("Auto-exit skipped: " + (p.reason || "the broker doesn't show that position"), "warn");
      break;
    case "exit.failed":
      if (p.market_closed) {
        // no failed try: the exit waits for the regular session and goes out at its first pass
        toast("Auto-exit waits for the open: " + p.reason, "warn");
        break;
      }
      toast(`⚠ Auto-exit not sent (try ${p.attempt}, again in ${p.retry_in_s}s): ${p.reason}`, "bad");
      break;
    case "orders.updated":
      S.orders = p;
      ordersChanged();
      break;
    case "orders.adopted":
      toast(p.msg, p.cancelled && p.cancelled.length ? "bad" : "warn");
      loadOpen();
      break;
    case "order.failed":
      toast("⚠ " + p.msg, "bad");
      loadOpen(); refreshState();
      break;
    case "order.unbooked":
      // a fill the trade log couldn't save yet: the order stays followed and the next pass saves it
      toast("⚠ " + p.msg, "bad");
      break;
    case "positions.mismatch":
      (p.mismatches || []).forEach(m => toast("⚠ " + m.note, "bad"));
      refreshState();
      break;
    case "stop.placed":
      toast(`${p.symbol}: stop order resting at the broker @ ${num(p.stop_price)} for ${num(p.qty, 0)} shares`, "good");
      break;
    case "target.placed":
      toast(`${p.symbol}: target order resting at the broker @ ${num(p.limit_price)} for ${num(p.qty, 0)} shares - one fill shrinks the other`, "good");
      break;
    case "stop.missing":
      toast(`⚠ ${p.symbol} has had no stop at the broker for ${num(p.minutes, 1)} min - ${p.reason}. ${p.exit_held ? "Its exit waits too, until the broker's orders can be read." : "The app still exits it itself while it runs."}`, "bad");
      break;
    case "stop.lost":
      toast(`⚠ ${p.symbol}: the stop order at the broker is gone (${p.reason}) - placing it again`, "bad");
      break;
    case "stop.failed":
      toast("⚠ Protective stop: " + (p.reason || "could not be placed"), "warn");
      break;
    case "stop.moved":
      // the stop at the broker rests at its new price - exit.stop_moved has said what it locks, so no toast
      loadOrders();
      if (tabVisible("open")) loadOpen();
      break;
    case "exit.stop_moved": {
      // what the stop keeps if it's hit, then where the trade stood when it moved
      const signedR = v => `${v >= 0 ? "+" : ""}${num(v, 1)}R`;
      const now = p.r_now ?? p.r;
      toast(`${p.symbol}: stop → ${num(p.new_stop)}` + (p.locked_r == null ? "" : `, locks ${signedR(p.locked_r)}`)
        + (now == null ? "" : ` (the trade was at ${signedR(now)})`), "good");
      loadOpen();
      break;
    }
    case "trade.overdue":
      toast("⏰ " + (p.msg || `${p.symbol} exit is overdue`), p.winning ? "good" : "bad");
      loadOpen();
      break;
    case "play.decided":
      // the whole row, which says who sent it and when - an open detail panel follows it (plays.js followSelected)
      if (p.play) mergePlay(p.play);
      else if (p.decision === "rejected") mergePlay({ id: p.play_id, status: "REJECTED" });     // dismissed in another tab
      if (p.decision === "approved" && p.result && !p.result.ok) {
        if (p.result.sent_unknown) toast("⚠ Order not confirmed: " + (p.result.reason || "no answer from the broker in time"), "warn");
        else toast("Order not sent: " + (p.result.reason || "rejected"), "bad");
      }
      break;

    case "capital.updated":
      S.state.capital = p.capital;
      renderCapital(p.capital);
      emit("capital", p.capital);
      break;
    case "broker.disconnected":
      toast(p.note || "IB Gateway disconnected - reconnecting", "warn");
      refreshState();
      break;
    case "broker.reconnected":
      toast(p.note || "IB Gateway is back", "good");
      if (p.state) setState(p.state);
      loadOpen();
      break;
    case "broker.down":
      toast(p.note || "IB Gateway is still unreachable", "bad");
      break;
    case "broker.connected":
      toast(p.note || "Connected", "good");
      if (p.state) setState(p.state);
      loadOpen();
      break;
    case "engine.disarmed":
      noteDisarmed(p.reason);
      break;
    case "broker.switched":
      if (p.state) setState(p.state);
      loadOpen();
      if (p.mode !== p.prev) toast(p.mode === "live" ? "LIVE mode — orders are real now" : "Paper mode", p.mode === "live" ? "bad" : "good");
      break;
    case "filters.updated":
      S.state.filters = p.filters;
      emit("filters");
      break;
    case "pairs.updated":
      if (tabVisible("pairs")) showPairs(p);
      break;

    case "pairs.entered":
    case "pairs.opened":
    case "pairs.exiting":
    case "pairs.closed":
    case "pairs.failed":
    case "pairs.broken": {
      const what = {
        "pairs.entered": `Pair ${p.pair}: orders sent`, "pairs.opened": `Pair ${p.pair} is on`,
        "pairs.exiting": `Pair ${p.pair}: closing (${p.reason})`, "pairs.closed": `Pair ${p.pair} closed: ${usd(p.realized_pl)}`,
        "pairs.failed": `Pair ${p.pair} not entered: ${p.reason}`, "pairs.broken": `Pair ${p.pair}: the ${p.leg} leg was closed - closing the other`,
      }[topic];
      toast(what, topic === "pairs.failed" || topic === "pairs.broken" ? "bad" : topic === "pairs.closed" ? (p.realized_pl >= 0 ? "good" : "bad") : undefined);
      loadOpen();
      if (tabVisible("pairs")) loadPairs();
      break;
    }

    case "signals.updated":
      signalsUpdated();
      break;
    case "position.earnings_ahead":
      toast(p.note, "warn");
      break;

    case "journal.updated":
      reportsUpdated();
      toast(`The report on ${p.session} is in Reports${p.mistakes ? ` — ${p.mistakes} thing${p.mistakes === 1 ? "" : "s"} to learn from` : ""}.`);
      break;

    case "strategies.updated":
      indexStrategies(p.strategies);
      break;

    case "replay.started":
    case "replay.progress":
    case "replay.completed":
    case "replay.failed":
      onReplayEvent(topic, p);
      break;
    case "model.trained":
      toast(`The learned model was retrained on ${count(p.rows)} rows (${p.id}) - ${p.usable ? "usable" : "not usable yet, so it has no say"}.`);
      break;

    case "quit.requested":
      openQuitDialog(p);
      break;
    case "quit.started":
    case "quit.progress":
      S.state.quit = p.quit;
      renderLock();
      if (topic === "quit.started") { closeModal(); loadOpen(); }
      break;
    case "quit.done":
      showShutdown(p.note);
      break;
    case "quit.cancelled":
      S.state.quit = null;
      renderLock();
      toast(`Quit cancelled - ${p.left} position${p.left === 1 ? "" : "s"} stay open and managed`, "warn");
      refreshState();
      break;

    case "autopilot.config":
      S.state.autopilot = p;
      renderAutopilot();
      emit("autopilot");
      break;
    case "autopilot.entered":
      toast(`🤖 Autopilot entered ${p.side} ${p.symbol} x${p.qty} — ${pretty(p.strategy)} (${p.count_today} today)`, "good");
      loadOpen(); refreshState();
      break;
    case "autopilot.would_enter":
      toast(`🤖 Autopilot (dry-run) would enter ${p.side} ${p.symbol} x${p.qty}`, "warn");
      break;
    case "autopilot.blocked":
      toast("🤖 " + (p.reason || "Autopilot is blocked"), "warn");
      break;
    case "autopilot.daily_loss":
      onDailyLoss(p);
      refreshState();                             // the strip's reading says it too
      break;
    case "autopilot.skipped": {
      // it tried a play and the engine's assessment or the order was refused: a note, and the play's robot
      // keeps why (the next board push carries the same) - no toast
      const row = S.plays.find(x => x.id === p.play_id);
      if (row) mergePlay({ id: row.id, autopilot: { ...(row.autopilot || {}), acted: true, skipped: true, reason: p.reason } });
      addNotes([{ id: `skip_${p.play_id}_${Date.now()}`, at: new Date().toISOString(), kind: "skipped", play_id: p.play_id,
        symbol: p.symbol, side: row ? row.side : "", strategy: p.strategy, why: p.reason || "refused" }]);
      break;
    }
  }
}
