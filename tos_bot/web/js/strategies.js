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
const EXTRA_CHECK_LABELS = { unconfirmed: "seen in only one scan", all_checks: "all of them together" };
const inR = v => v == null ? "–" : `${v >= 0 ? "+" : ""}${num(v, 2)}R`;
const recordText = r => `${r.trades} trades · ${Math.round((r.win_rate || 0) * 100)}% wins · ${inR(r.expectancy_r)} a trade`;

async function loadReplay() {
  try { S.replay = await api("/api/replay"); } catch { S.replay = null; }
}

export async function onReplayEvent(topic, p) {
  if (topic === "replay.progress") {
    S.replay = { ...(S.replay || {}), running: true, progress: p };
  } else {
    if (topic === "replay.failed") toast("Replay failed: " + (p.reason || "unknown error"), "bad");
    else toast(`Replay finished: ${p.trades} simulated trades. Strategy records are updated.`, "good");
    await loadReplay();
  }
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
  const proven = !!auto && auto.trades >= (ap.min_replay_trades ?? 30) && auto.expectancy_r >= (ap.min_replay_expectancy_r ?? 0.05) && heldOk;
  const ev = (rp.evidence || {})[key];
  const weight = ev && Math.abs(ev.multiplier - 1) > 0.001 ? ` · evidence weight ×${num(ev.multiplier, 2)}` : "";
  return `<div class="record">Replay: ${recordText(all)}${auto && auto.trades ? ` · the ones Autopilot would take: ${recordText(auto)}` : ""}${held && held.trades ? ` · held-out sessions: ${recordText(held)}` : ""}${weight}
    <span class="badge ${proven ? "good" : "warn"}">${proven ? "proven" : "not proven"}</span></div>`;
}

function replayHTML() {
  const rp = S.replay || {};
  const labels = { ...((S.state.autopilot || {}).noise_labels || {}), ...EXTRA_CHECK_LABELS };
  const progress = rp.running && rp.progress
    ? `<span class="muted">${escapeHtml(rp.progress.stage)} ${rp.progress.done}/${rp.progress.total}…</span>` : "";
  const heldFrom = rp.held_out_from || {}, costs = rp.costs || {};
  const when = rp.ran_at
    ? `Last replay ${escapeHtml(new Date(rp.ran_at).toLocaleString())}: ${rp.trade_count} simulated trades — day-trade setups over the last ${rp.sessions} sessions, swing setups over ${rp.swing_sessions}.` +
      (heldFrom.INTRADAY || heldFrom.SWING ? ` Held out to test on: day trades from ${escapeHtml(heldFrom.INTRADAY || "–")}, swing trades from ${escapeHtml(heldFrom.SWING || "–")}.` : "") +
      (costs.commission_bps != null ? ` Costs: ${num(costs.slippage_bps, 1)} bps slippage on market fills, ${num(costs.commission_bps, 1)} bps commission on every fill.` : "")
    : "No replay yet. Autopilot only trades a strategy once the replay has proven it.";
  const learned = new Set(rp.learned_skips || []);
  const noise = rp.noise
    ? `<table class="ev-table replay-noise"><tr><th>Noise check</th><th class="num">Removes</th><th class="num">Their average</th><th class="num">Kept average</th><th>Verdict</th><th>Held-out sessions</th></tr>` +
      Object.entries(rp.noise).map(([check, n]) => `<tr><td>${escapeHtml(labels[check] || check)}${learned.has(check) ? ` <span class="badge good" title="It removed losers on every session and on the held-out ones, so Autopilot skips it">Autopilot skips it</span>` : ""}</td><td class="num">${n.removes}</td>
        <td class="num">${inR(n.removed_avg_r)}</td><td class="num">${inR(n.kept_avg_r)}</td><td>${escapeHtml(n.verdict)}</td><td>${escapeHtml((n.held_out || {}).verdict || "–")}</td></tr>`).join("") +
      `</table>`
    : "";
  return `<div class="replay-box">
    <div class="group-title">Replay</div>
    <p class="muted">${when} A noise check earns its place when the trades it removes did worse than the ones it keeps.</p>
    <div class="row-gap"><label title="60 gives most setups the 30+ trades a record needs, with the latest third held out">Day-trade sessions <input type="number" id="replay-sessions" min="5" max="120" step="1" value="${rp.sessions || (rp.defaults || {}).sessions || 60}"></label>
      <label title="Up to a year - as far back as the stored daily candles go">Swing sessions <input type="number" id="replay-swing" min="20" max="250" step="10" value="${rp.swing_sessions || (rp.defaults || {}).swing_sessions || 250}"></label>
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
      <label class="weight lockable" title="Scales this setup's score — 1 is normal">weight
        <input type="number" min="0.1" max="3" step="0.1" value="${s.weight}" data-strat-weight="${escapeHtml(s.key)}"></label>
    </div>
    <div class="thesis">${escapeHtml(s.thesis)}</div>
    ${recordLine(s.key)}
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
                                          swing_sessions: parseInt($("#replay-swing").value, 10) || 250 });
    toastResult(r);
    if (r.ok) {
      S.replay = { ...(S.replay || {}), running: true, progress: { stage: "starting", done: 0, total: 1 } };
      renderStrategies();
    }
  };
  $("#strat-reset").onclick = async () => {
    const r = await post("/api/strategies/reset");
    toastResult(r);
    if (r.ok) indexStrategies(r.strategies);
  };
}

async function updateStrategy(key, body) {
  const r = await post(`/api/strategies/${encodeURIComponent(key)}`, body);
  if (r.ok) { toast(r.note + (r.rescanning ? " Rescanning…" : ""), "good"); indexStrategies(r.strategies); return; }
  toast("Strategy not changed: " + (r.reason || ""), "bad");
  if (drawerOpen("strategies")) renderStrategies();
}

export function initStrategies() {
  $("#btn-strategies").onclick = openStrategies;
  on("strategies", () => { if (drawerOpen("strategies")) renderStrategies(); });
}
