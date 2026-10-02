/* Strategies: switch setups on or off and set their weight. */
import { $, $$, api, escapeHtml, num, post, pretty } from "./util.js";
import { S, emit, on } from "./state.js";
import { drawerOpen, openDrawer, toast, toastResult } from "./ui.js";

export const stratLabel = key =>
  `<span data-term="strategy" data-key="${escapeHtml(key)}">${escapeHtml((S.strategies[key] || {}).title || pretty(key))}</span>`;

export function indexStrategies(rows) {
  if (!rows) return;
  S.strategies = Object.fromEntries(rows.map(s => [s.key, s]));
  S.state.strategies_on = rows.filter(s => s.enabled).length;
  $("#btn-strategies").textContent = `Strategies · ${S.state.strategies_on}`;
  emit("strategies");
}

export async function loadStrategies() {
  const d = await api("/api/strategies");
  indexStrategies(d.strategies);
}

export async function openStrategies() {
  openDrawer("strategies", "Strategies", `<p class="muted">Loading…</p>`);
  try {
    await Promise.all([loadStrategies(), loadReplay()]);
    if (drawerOpen("strategies")) renderStrategies();                // the list can arrive before the replay
  } catch {
    $("#drawer-body").innerHTML = `<p class="reasons">Couldn't load the strategies.</p>`;
  }
}

/* ---------- the replay: each setup's record on past candles, and what each noise check removes ---------- */
const EXTRA_CHECK_LABELS = { unconfirmed: "seen on only one candle", all_checks: "all of them together" };
const inR = v => v == null ? "–" : `${v >= 0 ? "+" : ""}${num(v, 2)}R`;
const recordText = r => `${r.trades} trades · ${Math.round((r.win_rate || 0) * 100)}% wins · ${inR(r.expectancy_r)} a trade`;
/* what the books ask of a record: Tharp's quality number, Aronson's odds that it is luck, Carver's cost share */
const judgedText = r => [
  r.sqn != null ? `quality ${num(r.sqn, 1)} (${escapeHtml(r.sqn_grade || "")})` : "",
  r.p_adjusted != null ? `could be luck: p ${num(r.p_adjusted, 2)} across ${r.setups_tested} setups` : "",
  r.edge_r != null && Math.abs(r.edge_r - r.expectancy_r) >= 0.01 ? `${inR(r.edge_r)} net of the stocks' own drift` : "",
  r.cost_share != null ? `costs take ${Math.round(r.cost_share * 100)}% of the edge` : "",
  r.drawdown ? `expect a ${num(Math.abs(r.drawdown.median_drawdown_r), 0)}R drawdown in 100 trades (${num(Math.abs(r.drawdown.p95_drawdown_r), 0)}R one time in twenty)` : "",
].filter(Boolean).join(" · ");

async function loadReplay() {
  try { S.replay = await api("/api/replay"); } catch { S.replay = null; }
}

/* The replay's events. "replay" is emitted when it starts or stops running, for the scan status beside the plays. */
export async function onReplayEvent(topic, p) {
  const was = !!(S.replay || {}).running;
  if (topic === "replay.started") {
    // the day's replay, started by itself after the morning's full scan
    if (!p.ok) { toast("The day's replay didn't start: " + (p.reason || "unknown reason"), "warn"); return; }
    S.replay = { ...(S.replay || {}), running: true, progress: { stage: "starting", done: 0, total: 1 } };
  } else if (topic === "replay.progress") {
    S.replay = { ...(S.replay || {}), running: true, progress: p };
  } else {
    if (topic === "replay.failed") toast("Replay failed: " + (p.reason || "unknown error"), "bad");
    else toast(`Replay finished: ${p.trades} simulated trades. Strategy records are updated.`, "good");
    await loadReplay();
    if (S.replay) S.replay.running = false;       // it has ended, whatever a read racing its last moment said
  }
  if (!!(S.replay || {}).running !== was) emit("replay");
  if (drawerOpen("strategies")) renderStrategies();
}

function recordLine(key) {
  const rp = S.replay || {};
  if (!rp.ran_at) return "";
  const all = ((rp.records || {}).all || {})[key], auto = ((rp.records || {}).autopilot || {})[key];
  if (!all || !all.trades) return `<div class="record muted">Replay: no trades from this setup.</div>`;
  const ap = S.state.autopilot || {};
  const held = auto && auto.out_of_sample;
  const heldOk = !held || (held.trades >= 10 && held.expectancy_r > 0);
  const missing = (rp.proof || {})[key];                     // the engine's own verdict, in words ("" = proven)
  const proven = missing != null ? missing === ""
    : !!auto && auto.trades >= (ap.min_replay_trades ?? 30) && auto.expectancy_r >= (ap.min_replay_expectancy_r ?? 0.05) && heldOk;
  const judged = judgedText(auto && auto.trades ? auto : all);
  return `<div class="record">Replay: ${recordText(all)}${auto && auto.trades ? ` · the ones Autopilot would take: ${recordText(auto)}` : ""}${held && held.trades ? ` · held-out sessions: ${recordText(held)}` : ""}
    <span class="badge ${proven ? "good" : "warn"}" title="${escapeHtml(missing || "")}">${proven ? "proven" : "not proven"}</span></div>
    ${judged ? `<div class="record muted">${judged}.</div>` : ""}`;
}

/* your weight × what the record says = the weight the ranking uses */
function weightLine(s) {
  const ev = ((S.replay || {}).evidence || {})[s.key];
  const mult = ev ? Number(ev.multiplier) : 1;
  const why = !ev || !(ev.replay_trades || ev.live_trades) ? "no record yet, so ×1"
    : Math.abs(mult - 1) < 0.001 ? `record too thin or mixed to tilt it${ev.note ? ` (${escapeHtml(ev.note)})` : ""}`
    : mult > 1 ? `the record raises it (${ev.replay_trades} replayed${ev.live_trades ? ` + ${ev.live_trades} real` : ""} trades)`
    : `the record lowers it (${ev.replay_trades} replayed${ev.live_trades ? ` + ${ev.live_trades} real` : ""} trades)`;
  return `<div class="record muted">Used in ranking: your ${num(s.weight, 1)} × record ${num(mult, 2)} = <b>${num(s.weight * mult, 2)}</b> — ${why}.</div>`;
}

function replayHTML() {
  const rp = S.replay || {};
  const labels = { ...((S.state.autopilot || {}).noise_labels || {}), ...EXTRA_CHECK_LABELS };
  const progress = rp.running && rp.progress
    ? `<span class="muted">${escapeHtml(rp.progress.stage)} ${rp.progress.done}/${rp.progress.total}…</span>` : "";
  const heldFrom = rp.held_out_from || {}, costs = rp.costs || {};
  const when = rp.ran_at
    ? `Last replay ${escapeHtml(new Date(rp.ran_at).toLocaleString())}: ${rp.trade_count} simulated trades — day-trade setups over the last ${rp.sessions} sessions` +
      (rp.intraday_stock_days
        ? ` on the stocks in play each morning (${rp.intraday_stock_days}${rp.intraday_requests_pending ? ` of ${rp.intraday_stock_days_in_play}` : ""} stock-days, ${rp.intraday_symbols} stocks${rp.intraday_requests_pending ? ` - IBKR serves past candles slowly, so ${rp.intraday_requests_pending} requests are left for the next replays` : ""}), entered on sight and, for Autopilot's record, after two bars in a row`
        : (rp.intraday_symbols ? ` on ${rp.intraday_symbols} stocks` : "")) +
      `, swing setups over ${rp.swing_sessions}.` +
      (heldFrom.INTRADAY || heldFrom.SWING ? ` Held out to test on: day trades from ${escapeHtml(heldFrom.INTRADAY || "–")}, swing trades from ${escapeHtml(heldFrom.SWING || "–")}.` : "") +
      (costs.commission_bps != null ? ` Costs: ${num(costs.slippage_bps, 1)} bps slippage on market fills, ${num(costs.commission_bps, 1)} bps commission on every fill.` : "")
    : "No replay yet. Autopilot only trades a strategy once the replay has proven it.";
  const learned = new Set(rp.learned_skips || []);
  const noise = rp.noise
    ? `<table class="ev-table replay-noise"><tr><th>Noise check</th><th class="num">Removes</th><th class="num">Their average</th><th class="num">Kept average</th><th>Verdict</th><th>Held-out sessions</th></tr>` +
      Object.entries(rp.noise).map(([check, n]) => `<tr><td>${escapeHtml(labels[check] || check)}${learned.has(check) ? ` <span class="badge good" title="The trades it removed averaged 0.05R or more worse over every session, two standard errors or more apart, and worse on the held-out sessions too, so Autopilot skips it">Autopilot skips it</span>` : ""}</td><td class="num">${n.removes}</td>
        <td class="num">${inR(n.removed_avg_r)}</td><td class="num">${inR(n.kept_avg_r)}</td><td${n.t != null ? ` title="The two averages are ${num(Math.abs(n.t), 1)} standard errors apart (Welch's t ${num(n.t, 2)}); Autopilot learns a skip from -2 or lower"` : ""}>${escapeHtml(n.verdict)}</td><td>${escapeHtml((n.held_out || {}).verdict || "–")}</td></tr>`).join("") +
      `</table>`
    : "";
  return `<div class="replay-box">
    <div class="group-title">Replay</div>
    <p class="muted">${when} A noise check earns its place when the trades it removes did worse than the ones it keeps.</p>
    <div class="row-gap"><label title="Each session is replayed on the stocks that were in play that morning - the scan's hottest as of the session before, and the day's biggest gaps (replay.day_stocks, replay.day_gappers). The first run downloads their 5-minute candles, about a request a stock-day; after that only new sessions. The latest third is held out">Day-trade sessions <input type="number" id="replay-sessions" min="5" max="120" step="1" value="${rp.sessions || (rp.defaults || {}).sessions || 60}"></label>
      <label title="As far back as the long daily history goes - three years by default (replay.daily_years). The first replay downloads it for the stocks it runs on; after that it costs nothing">Swing sessions <input type="number" id="replay-swing" min="20" max="1250" step="10" value="${Math.max(rp.swing_sessions || 0, (rp.defaults || {}).swing_sessions || 250)}"></label>
      <button class="mini lockable" id="replay-run" ${rp.running ? "disabled" : ""}>${rp.running ? "Replaying…" : "Run replay"}</button> ${progress}</div>
    ${noise}
  </div>`;
}

function card(s) {
  return `<div class="strat ${s.enabled ? "" : "off"}">
    <div class="strat-head">
      <label class="switch lockable" title="${s.enabled ? "On — click to switch off" : "Off — click to switch on"}">
        <input type="checkbox" data-strat-toggle="${escapeHtml(s.key)}" ${s.enabled ? "checked" : ""}><span></span></label>
      <div><div class="meta">${s.timeframe === "INTRADAY" ? "day trade" : "swing"} · ${escapeHtml(s.kind.toLowerCase())}${s.customized ? " · changed from config" : ""}</div>
        <h4>${escapeHtml(s.title)}</h4></div>
      <label class="weight lockable" title="Your weight. It only moves this setup's plays up or down the list against other setups' plays — it doesn't change whether a play is found, its odds, its stop or its target. Autopilot takes the top-ranked play first. 1 = normal; switch a setup off rather than weighting it 0.1.">weight
        <input type="number" min="0.1" max="3" step="0.1" value="${s.weight}" data-strat-weight="${escapeHtml(s.key)}"></label>
    </div>
    <div class="thesis">${escapeHtml(s.thesis)}</div>
    ${recordLine(s.key)}
    ${weightLine(s)}
  </div>`;
}

function renderStrategies() {
  const rows = Object.values(S.strategies);
  const groups = [
    ["Day trades", s => s.kind === "TECHNICAL" && s.timeframe === "INTRADAY"],
    ["Swing trades", s => s.kind === "TECHNICAL" && s.timeframe !== "INTRADAY"],
    ["Valuation (fundamental)", s => s.kind === "FUNDAMENTAL"],
  ];
  $("#drawer-body").innerHTML = `
    <p class="muted">${rows.filter(s => s.enabled).length} of ${rows.length} setups on. A change applies to the next scan and to Autopilot
      straight away, shows up in every open tab, and is remembered. Plays from a setup you switch off leave the board.</p>
    <div class="strat how-box">
      <h4>What the weight does, in plain words</h4>
      <div class="thesis">Every play gets a <b>score</b>: what it should make per dollar risked, from the setup's odds of paying
        and its reward against its risk. The weight multiplies that score, so it only moves a setup's plays <b>up or down the list</b>
        against other setups' plays. It doesn't change whether a setup fires, its odds, its stop or its target, and Autopilot's gates
        (confidence, reward:risk, noise flags, proof) never look at it. Autopilot does take the highest-scored play first, so the
        weight decides which of two plays it takes when both qualify.</div>
      <div class="thesis"><b>How to set it:</b> leave every weight at 1 unless you have a reason of your own. The bot already tilts each
        setup by its record: the replay and your real trades give it an <b>evidence weight</b> between ×0.5 and ×1.5, applied on top
        of yours, and a setup that lost money on the held-out sessions or in real trading is never raised. Each card shows the weight
        the ranking actually uses. A setup you don't want traded should be switched off, not weighted 0.1.</div>
    </div>
    ${replayHTML()}
    ${groups.map(([name, test]) => {
      const group = rows.filter(test);
      return group.length ? `<div class="group-title">${name}</div>${group.map(card).join("")}` : "";
    }).join("")}
    <div class="row-gap"><button class="ghost mini lockable" id="strat-reset">Reset to config.yaml</button></div>`;
  $$("[data-strat-toggle]").forEach(c => { c.onchange = () => updateStrategy(c.dataset.stratToggle, { enabled: c.checked }); });
  $$("[data-strat-weight]").forEach(i => {
    i.onchange = () => {
      const w = parseFloat(i.value);
      if (!(w >= 0.1 && w <= 3)) { toast("Weight must be between 0.1 and 3", "bad"); renderStrategies(); return; }
      updateStrategy(i.dataset.stratWeight, { weight: w });
    };
  });
  $("#replay-run").onclick = async () => {
    const r = await post("/api/replay", { sessions: parseInt($("#replay-sessions").value, 10) || 60,
                                          swing_sessions: parseInt($("#replay-swing").value, 10) || 700 });
    toastResult(r);
    if (r.ok) {
      S.replay = { ...(S.replay || {}), running: true, progress: { stage: "starting", done: 0, total: 1 } };
      emit("replay");
      renderStrategies();
    }
  };
  $("#strat-reset").onclick = async () => {
    const r = await post("/api/strategies/reset");
    toastResult(r);
    if (r.ok) indexStrategies(r.strategies);
  };
}

/* Also the play detail panel's "Switch this setup off" - one switch, wherever it's pressed. */
export async function updateStrategy(key, body) {
  const r = await post(`/api/strategies/${encodeURIComponent(key)}`, body);
  if (r.ok) { toast(r.note + (r.rescanning ? " Rescanning…" : ""), "good"); indexStrategies(r.strategies); return r; }
  toast("Strategy not changed: " + (r.reason || ""), "bad");
  if (drawerOpen("strategies")) renderStrategies();
  return r;
}

export function initStrategies() {
  $("#btn-strategies").onclick = openStrategies;
  on("strategies", () => { if (drawerOpen("strategies")) renderStrategies(); });
}
