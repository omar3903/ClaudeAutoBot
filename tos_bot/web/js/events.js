/* The live feed: engine events over the WebSocket, applied to the dashboard. */
import { num, pct, plural, pretty, usd } from "./util.js";
import { S, emit, refreshState, setState } from "./state.js";
import { closeModal, drawerOpen, toast } from "./ui.js";
import { renderCapital } from "./topbar.js";
import { openQuitDialog, renderLock, showShutdown } from "./quit.js";
import { renderAutopilot } from "./autopilot.js";
import { onScanEvent } from "./scan.js";
import { mergePlay, selectPlay } from "./plays.js";
import { loadHistory, loadOpen, loadStats, openRecord, recordGone, tabVisible } from "./blotter.js";
import { renderWatchlist } from "./watchlist.js";
import { indexStrategies } from "./strategies.js";

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

function handle(topic, p) {
  switch (topic) {
    case "hello":
    case "account.snapshot":
    case "engine.started":
      setState(p.state || p);
      if (tabVisible("open")) loadOpen();
      break;
    case "plays.updated":
      S.plays = p.plays || [];
      emit("plays");
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
    case "order.failed":
      toast("⚠ " + p.msg, "bad");
      loadOpen(); refreshState();
      break;
    case "positions.mismatch":
      (p.mismatches || []).forEach(m => toast("⚠ " + m.note, "bad"));
      refreshState();
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
    case "strategies.updated":
      indexStrategies(p.strategies);
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

    case "autopilot.config":
      S.state.autopilot = p;
      renderAutopilot();
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
