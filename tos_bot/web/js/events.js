/* The live feed: engine events over the WebSocket, applied to the dashboard. */
import { $, num, pct, plural, pretty, usd } from "./util.js";
import { S, emit, refreshState, setState } from "./state.js";
import { closeModal, drawerOpen, toast } from "./ui.js";
import { renderCapital } from "./topbar.js";
import { openQuitDialog, renderLock, showShutdown } from "./quit.js";
import { renderAutopilot } from "./autopilot.js";
import { onScanEvent } from "./scan.js";
import { mergePlay, selectPlay } from "./plays.js";
import { loadHistory, loadOpen, loadStats, openRecord, recordGone, tabVisible } from "./blotter.js";
import { renderWatchlist } from "./watchlist.js";
import { indexStrategies, onReplayEvent } from "./strategies.js";
import { loadOrders, ordersChanged } from "./orders.js";
import { addNotes } from "./notes.js";
import { reportsUpdated } from "./reports.js";
import { signalsUpdated } from "./signals.js";
import { loadPairs, showPairs } from "./pairs.js";

export function connect() {
  if (S.stopped) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = ev => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handle(msg.topic, msg.payload || {});
  };
  ws.onclose = () => { if (!S.stopped) setTimeout(connect, 2000); };
  ws.onerror = () => ws.close();
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

function handle(topic, p) {
  if (topic === "hello") loadNewScripts(p.web_build);
  switch (topic) {
    case "hello":
    case "account.snapshot":
    case "engine.started":
      setState(p.state || p);
      if (tabVisible("open")) loadOpen();
      if (topic === "hello") loadOrders();
      break;
    case "plays.updated":
      S.plays = p.plays || [];
      emit("plays");
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
    case "stop.short":
      toast(`${p.symbol}: the broker holds ${num(p.held, 0)} shares and the record ${num(p.record, 0)} - a stop protects the ${num(p.held, 0)} it holds`, "warn");
      break;
    case "stop.lost":
      toast(`⚠ ${p.symbol}: the stop order at the broker is gone (${p.reason}) - placing it again`, "bad");
      break;
    case "stop.failed":
      toast("⚠ Protective stop: " + (p.reason || "could not be placed"), "warn");
      break;
    case "exit.stop_moved":
      toast(`${p.symbol}: stop → ${num(p.new_stop)} (${num(p.r, 1)}R locked)`, "good");
      loadOpen();
      break;
    case "trade.overdue":
      toast("⏰ " + (p.msg || `${p.symbol} exit is overdue`), p.winning ? "good" : "bad");
      loadOpen();
      break;
    case "play.decided":
      if (p.play) mergePlay(p.play);
      if (p.decision === "approved" && p.result && !p.result.ok) toast("Order not sent: " + (p.result.reason || "rejected"), "bad");
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

    case "replay.progress":
    case "replay.completed":
    case "replay.failed":
      onReplayEvent(topic, p);
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
  }
}
