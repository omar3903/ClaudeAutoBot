/* The journal half of a session's report (see reports.js): the trades and what they were taken on, the
   mistakes, the plays not taken and how they would have gone, the strategies' records, and the lessons. */
import { escapeHtml, fmtClock, num, usd } from "./util.js";
import { stratLabel } from "./strategies.js";

export const inR = v => v == null ? "–" : `${v >= 0 ? "+" : ""}${num(v, 2)}R`;
export const tone = v => v == null ? "" : v > 0 ? "gain" : v < 0 ? "loss" : "";
export const pctOf = v => v == null ? "–" : `${Math.round(v * 100)}%`;
const SEVERITY = { high: "bad", medium: "warn", info: "" };

function executionHTML(x) {
  if (!x || x.entry_latency_s == null) return "";
  const s = v => v == null ? "–" : `${num(v, 1)}s`;
  return `<h4>How the orders filled</h4><p class="muted">Typically ${s(x.entry_latency_s)} from the order going out to the fill coming back
    on the way in${x.exit_latency_s == null ? "" : `, ${s(x.exit_latency_s)} on the way out`}${x.slowest_entry_s ? ` (slowest entry ${num(x.slowest_entry_s, 0)}s)` : ""}.
    A stop or target resting at the broker isn't counted - it waits for the price, not for the broker.</p>`;
}

export function journalHTML(r) {
  return `<h4>What the bot learned</h4>
    <ul class="journal-lessons">${(r.lessons || []).map(l => `<li>${escapeHtml(l)}</li>`).join("")}</ul>
    ${mistakesHTML(r.mistakes || [])}
    ${openedHTML(r.opened || [])}
    ${tradesHTML(r.trades || [])}
    ${pairsHTML(r.pairs || [])}
    ${shadowsHTML(r.shadows || {})}
    ${strategiesHTML(r.strategies || [])}
    ${executionHTML(r.execution)}`;
}

function mistakesHTML(list) {
  if (!list.length) return `<h4>Mistakes</h4><p class="muted">None found.</p>`;
  return `<h4>Mistakes and things to watch</h4><table class="ev-table">
    <tr><th></th><th>Stock</th><th>Strategy</th><th class="num">R</th><th>What happened</th></tr>
    ${list.map(m => `<tr><td><span class="badge ${SEVERITY[m.severity] || ""}">${escapeHtml(m.severity)}</span></td>
      <td>${escapeHtml(m.symbol)}</td><td>${stratLabel(m.strategy)}</td><td class="num ${tone(m.r)}">${inR(m.r)}</td>
      <td>${escapeHtml(m.detail)}</td></tr>`).join("")}</table>`;
}

/* What a trade was taken on, in a few words - shared by the opened and the closed tables. */
function takenOn(t) {
  const at = (t.evidence || {}).at_entry || {}, ch = (t.evidence || {}).price_character, reg = at.market_regime || {};
  return [
    at.by ? `by ${escapeHtml(at.by)}` : "",
    at.unproven ? `<span title="${escapeHtml(String(at.unproven))}">practice - strategy not proven</span>` : "",
    at.confirmations ? `${at.confirmations} ${t.timeframe === "INTRADAY" && (at.settings || {}).confirm_on_new_candle ? "candle" : "scan"}${at.confirmations === 1 ? "" : "s"}` : "",
    (at.noise || []).length ? `flags: ${escapeHtml(at.noise.join(", "))}` : "no flags",
    ch ? escapeHtml(ch.character) : "",
    reg.regime ? `market ${escapeHtml(reg.regime)}` : "",
  ].filter(Boolean).join(" · ");
}

/* The positions opened this session. A session whose entries are all still open has no closed trades,
   but it isn't a session without trading: each row says where the position stood at the review. */
function openedHTML(rows) {
  if (!rows.length) return "";
  return `<h4>Positions opened</h4><table class="ev-table">
    <tr><th>Stock</th><th>Strategy</th><th>Type</th><th>In</th><th class="num">Entry</th><th class="num">Stop</th><th class="num">Target</th>
      <th class="num">At risk</th><th class="num" title="Still open: where it stood at the review, on the session's close. Closed the same session: its result.">Standing</th><th>Taken on</th></tr>
    ${rows.map(t => {
      const standing = t.still_open
        ? `<span class="${tone(t.open_r)}">${inR(t.open_r)}</span> <span class="muted">${t.open_pl == null ? "" : usd(t.open_pl)} · open</span>`
        : `<span class="${tone(t.r)}">${inR(t.r)}</span> <span class="muted">${usd(t.pl)} · ${escapeHtml(t.exit_reason || "closed")}</span>`;
      return `<tr><td>${escapeHtml(t.symbol)} <span class="muted">${escapeHtml(t.side)}</span></td><td>${stratLabel(t.strategy)}</td>
        <td>${t.timeframe === "INTRADAY" ? "day" : "swing"}</td><td>${escapeHtml(fmtClock(t.entry_time))}</td>
        <td class="num">${num(t.entry)} <span class="muted">×${num(t.quantity, 0)}</span></td><td class="num">${num(t.stop)}</td><td class="num">${num(t.target)}</td>
        <td class="num">${t.risk == null ? "–" : usd(t.risk)}</td><td class="num nowrap">${standing}</td><td class="muted wrap">${takenOn(t)}</td></tr>`;
    }).join("")}</table>`;
}

function tradesHTML(rows) {
  if (!rows.length) return "";
  return `<h4>Trades closed</h4><table class="ev-table">
    <tr><th>Stock</th><th>Strategy</th><th>In → out</th><th class="num">R</th><th class="num">Best</th><th class="num">P/L</th><th>Exit</th><th>Taken on</th></tr>
    ${rows.map(t => {
      const taken = takenOn(t);
      return `<tr><td>${escapeHtml(t.symbol)} <span class="muted">${escapeHtml(t.side)}</span></td><td>${stratLabel(t.strategy)}</td>
        <td>${escapeHtml(fmtClock(t.entry_time))} → ${escapeHtml(fmtClock(t.exit_time))}</td><td class="num ${tone(t.r)}">${inR(t.r)}</td><td class="num">${inR(t.mfe_r)}</td>
        <td class="num ${tone(t.pl)}">${usd(t.pl)}</td><td>${escapeHtml(t.exit_reason || "")}</td><td class="muted">${taken}</td></tr>`;
    }).join("")}</table>`;
}

function shadowsHTML(sh) {
  if (sh.note) return `<h4>Plays not taken</h4><p class="muted">${escapeHtml(sh.note)}</p>`;
  if (!sh.followed) return "";
  const s = sh.summary || {}, checks = sh.checks || {};
  const noise = Object.entries(sh.by_noise || {});
  const list = (rows, title) => rows && rows.length ? `<div class="muted">${title}: ${rows.map(x =>
    `${escapeHtml(x.symbol)} ${escapeHtml(x.side.toLowerCase())} (${escapeHtml(x.strategy)}) ${inR(x.r)}`).join(" · ")}</div>` : "";
  return `<h4>Plays not taken</h4>
    <p class="muted">${sh.followed} setup${sh.followed === 1 ? "" : "s"} followed on the session's candles as if taken; ${sh.filled} would have filled.
      ${s.trades ? `They would have averaged ${inR(s.expectancy_r)} (${inR(s.total_r)} in all, ${pctOf(s.win_rate)} winners).` : ""}
      ${checks.passed && checks.passed.trades ? ` Passing Autopilot's checks: ${inR(checks.passed.expectancy_r)} over ${checks.passed.trades}.` : ""}
      ${checks.turned_away && checks.turned_away.trades ? ` Turned away by them: ${inR(checks.turned_away.expectancy_r)} over ${checks.turned_away.trades}.` : ""}</p>
    ${noise.length ? `<table class="ev-table"><tr><th>Noise flag</th><th class="num">Flagged</th><th class="num">Their average</th><th class="num">The rest</th><th>Today</th></tr>
      ${noise.map(([flag, n]) => `<tr><td>${escapeHtml(flag === "all_checks" ? "any flag" : flag)}</td><td class="num">${n.flagged}</td>
        <td class="num">${inR(n.flagged_avg_r)}</td><td class="num">${inR(n.rest_avg_r)}</td><td>${escapeHtml(n.verdict)}</td></tr>`).join("")}</table>` : ""}
    ${list(sh.best_missed, "Best missed")}
    ${list(sh.worst_avoided, "Worst avoided")}`;
}

function strategiesHTML(rows) {
  if (!rows.length) return "";
  return `<h4>Strategies: real trades (last 20 sessions) against the replay</h4><table class="ev-table">
    <tr><th>Strategy</th><th class="num">Real trades</th><th class="num">Real a trade</th><th class="num">Replayed</th><th class="num">Replay a trade</th><th class="num">Held-out</th><th class="num">Weight</th><th></th></tr>
    ${rows.map(s => `<tr><td>${stratLabel(s.strategy)}</td><td class="num">${s.live_trades}</td><td class="num ${tone(s.live_r)}">${inR(s.live_r)}</td>
      <td class="num">${s.replay_trades}</td><td class="num ${tone(s.replay_r)}">${inR(s.replay_r)}</td><td class="num ${tone(s.held_out_r)}">${inR(s.held_out_r)}</td>
      <td class="num">×${num(s.evidence_weight, 2)}</td><td>${s.drifting ? `<span class="badge warn">worse than its replay</span>` : ""}</td></tr>`).join("")}</table>`;
}

function pairsHTML(rows) {
  if (!rows.length) return "";
  return `<h4>Pair trades</h4><table class="ev-table">
    <tr><th>Pair</th><th>Side</th><th class="num">z in → out</th><th>How</th><th class="num">P/L</th><th class="num">R</th></tr>
    ${rows.map(t => `<tr><td>${escapeHtml(t.pair)}</td><td>${t.side === "LONG_SPREAD" ? "long the spread" : "short the spread"}</td>
      <td class="num">${num(t.entry_z, 2)} → ${num(t.exit_z_at, 2)}</td><td>${escapeHtml(t.exit_reason || "")}</td>
      <td class="num ${tone(t.realized_pl)}">${usd(t.realized_pl)}</td><td class="num ${tone(t.r_multiple)}">${inR(t.r_multiple)}</td></tr>`).join("")}</table>`;
}
