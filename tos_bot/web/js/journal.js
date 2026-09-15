/* The Journal tab: each session's review - the trades and what they were taken on, the mistakes,
   the plays not taken and how they would have gone, the strategies' records, and the lessons. */
import { $, $$, api, escapeHtml, num, post, usd } from "./util.js";
import { toastResult } from "./ui.js";
import { stratLabel } from "./strategies.js";

let selected = null;

const inR = v => v == null ? "–" : `${v >= 0 ? "+" : ""}${num(v, 2)}R`;
const tone = v => v == null ? "" : v > 0 ? "gain" : v < 0 ? "loss" : "";
const pctOf = v => v == null ? "–" : `${Math.round(v * 100)}%`;
const time = iso => iso ? escapeHtml(new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })) : "–";
const SEVERITY = { high: "bad", medium: "warn", info: "" };

export async function loadJournal() {
  const box = $("#tab-journal");
  let state;
  try { state = await api("/api/journal"); } catch { box.innerHTML = `<div class="empty">Couldn't load the journal.</div>`; return; }
  const days = state.days || [];
  if (!days.some(d => d.session === selected)) selected = days.length ? days[0].session : null;
  box.innerHTML = `<div class="journal">
    <div class="journal-days">
      <button class="mini lockable" id="journal-build" title="Build the review of the last session now (it's rebuilt if it exists)">Review the last session</button>
      <div class="muted">${state.enabled ? `Written after every session at ${escapeHtml(state.review_at)} ET.` : "The automatic review is off (journal.enabled in config.yaml)."}</div>
      ${days.length ? days.map(d => `<button class="journal-day ${d.session === selected ? "active" : ""}" data-day="${escapeHtml(d.session)}">
          <b>${escapeHtml(d.session)}</b>
          <span class="${tone(d.total_r)}">${d.trades} trade${d.trades === 1 ? "" : "s"} · ${inR(d.total_r)} · ${usd(d.realized_pl)}</span>
          ${d.mistakes ? `<span class="badge warn">${d.mistakes} to learn from</span>` : ""}
        </button>`).join("") : `<div class="empty">No reviews yet.</div>`}
    </div>
    <div class="journal-review" id="journal-review">${selected ? `<p class="muted">Loading…</p>` : ""}</div>
  </div>`;
  $$("#tab-journal [data-day]").forEach(b => { b.onclick = () => { selected = b.dataset.day; loadJournal(); }; });
  $("#journal-build").onclick = async () => {
    const r = await post("/api/journal/review", {});
    toastResult(r);
    if (r.ok && r.review) { selected = r.review.session; loadJournal(); }
  };
  if (selected) showReview(selected);
}

async function showReview(day) {
  const box = $("#journal-review");
  let r;
  try { r = await api(`/api/journal/${encodeURIComponent(day)}`); } catch { r = null; }
  if (selected !== day || !$("#journal-review")) return;
  if (!r || !r.session) { box.innerHTML = `<div class="empty">Couldn't load the review.</div>`; return; }
  const d = r.day || {}, sh = r.shadows || {}, reg = r.regime;
  box.innerHTML = `
    <h4>${escapeHtml(r.session)}${reg ? ` <span class="badge ${reg.regime === "turbulent" ? "warn" : "good"}">market ${escapeHtml(reg.regime)}</span>` : ""}</h4>
    <div class="journal-cards">
      <div><label>Closed trades</label><b>${d.trades || 0}</b></div>
      <div><label>Winners</label><b>${pctOf(d.win_rate)}</b></div>
      <div><label>In all</label><b class="${tone(d.total_r)}">${inR(d.total_r)}</b></div>
      <div><label>A trade</label><b class="${tone(d.expectancy_r)}">${inR(d.expectancy_r)}</b></div>
      <div><label>Realized</label><b class="${tone(d.realized_pl)}">${usd(d.realized_pl)}</b></div>
      <div><label>Plays offered</label><b>${r.plays_offered || 0}</b></div>
    </div>
    <h4>Lessons</h4>
    <ul class="journal-lessons">${(r.lessons || []).map(l => `<li>${escapeHtml(l)}</li>`).join("")}</ul>
    ${mistakesHTML(r.mistakes || [])}
    ${tradesHTML(r.trades || [])}
    ${pairsHTML(r.pairs || [])}
    ${shadowsHTML(sh)}
    ${strategiesHTML(r.strategies || [])}`;
}

function mistakesHTML(list) {
  if (!list.length) return `<h4>Mistakes</h4><p class="muted">None found.</p>`;
  return `<h4>Mistakes and things to watch</h4><table class="ev-table">
    <tr><th></th><th>Stock</th><th>Strategy</th><th class="num">R</th><th>What happened</th></tr>
    ${list.map(m => `<tr><td><span class="badge ${SEVERITY[m.severity] || ""}">${escapeHtml(m.severity)}</span></td>
      <td>${escapeHtml(m.symbol)}</td><td>${stratLabel(m.strategy)}</td><td class="num ${tone(m.r)}">${inR(m.r)}</td>
      <td>${escapeHtml(m.detail)}</td></tr>`).join("")}</table>`;
}

function tradesHTML(rows) {
  if (!rows.length) return "";
  return `<h4>Trades</h4><table class="ev-table">
    <tr><th>Stock</th><th>Strategy</th><th>In → out</th><th class="num">R</th><th class="num">Best</th><th class="num">P/L</th><th>Exit</th><th>Taken on</th></tr>
    ${rows.map(t => {
      const at = (t.evidence || {}).at_entry || {}, ch = (t.evidence || {}).price_character, reg = at.market_regime || {};
      const taken = [
        at.by ? `by ${escapeHtml(at.by)}` : "",
        at.confirmations ? `${at.confirmations} scan${at.confirmations === 1 ? "" : "s"}` : "",
        (at.noise || []).length ? `flags: ${escapeHtml(at.noise.join(", "))}` : "no flags",
        ch ? escapeHtml(ch.character) : "",
        reg.regime ? `market ${escapeHtml(reg.regime)}` : "",
      ].filter(Boolean).join(" · ");
      return `<tr><td>${escapeHtml(t.symbol)} <span class="muted">${escapeHtml(t.side)}</span></td><td>${stratLabel(t.strategy)}</td>
        <td>${time(t.entry_time)} → ${time(t.exit_time)}</td><td class="num ${tone(t.r)}">${inR(t.r)}</td><td class="num">${inR(t.mfe_r)}</td>
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
