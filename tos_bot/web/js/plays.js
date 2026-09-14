/* The plays table, and the detail panel: why, the numbers, and the order. */
import { $, $$, escapeHtml, num, pct, pretty, post, sectorTag, sideBadge, tfLabel, usd } from "./util.js";
import { S, on } from "./state.js";
import { toast } from "./ui.js";
import { hideTip, showTipAt } from "./tooltips.js";
import { stratLabel } from "./strategies.js";
import { hideExecuted } from "./filters.js";
import { loadOpen, openRecord } from "./blotter.js";

const DONE = new Set(["ACCEPTED", "SUBMITTED", "WORKING", "PARTIAL", "FILLED", "ERROR"]);
const isDone = p => DONE.has(p.status);

export function mergePlay(row) {
  const i = S.plays.findIndex(x => x.id === row.id);
  if (i >= 0) { S.plays[i] = { ...S.plays[i], ...row }; renderPlays(); }
}

function visiblePlays() {
  const f = S.state.filters || {};
  const sides = f.sides || ["LONG", "SHORT"], tfs = f.timeframes || ["INTRADAY", "SWING"];
  return S.plays.filter(p => sides.includes(p.side) && tfs.includes(p.timeframe) && (!hideExecuted() || !isDone(p)));
}

function emptyText() {
  const s = (S.state.scan || {}).settings;
  return s
    ? `No plays yet. The full scan runs at <b>${escapeHtml(s.premarket_time)} ET</b>, and in the session the hot list is rescanned every <b>${s.cycle_minutes} min</b> — or hit <b>Scan now</b>.`
    : "No plays yet — hit <b>Scan now</b>.";
}

export function renderPlays() {
  const rows = visiblePlays();
  $("#plays-count").textContent = rows.length ? `(${rows.length})` : "";
  $("#plays-empty").innerHTML = emptyText();
  $("#plays-empty").classList.toggle("hidden", rows.length > 0);
  const body = $("#plays-body");
  body.innerHTML = "";
  for (const p of rows) body.appendChild(playRow(p));
}

function playRow(p) {
  const tr = document.createElement("tr");
  const done = isDone(p), ap = p.autopilot || {};
  tr.dataset.id = p.id;
  tr.classList.toggle("selected", p.id === S.selected);
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
    showTipAt(e.clientX, e.clientY, ((S.strategies[p.strategy] || {}).title || pretty(p.strategy)).toUpperCase(),
      (p.explanation || p.rationale || "").trim());
  });
  tr.addEventListener("mouseleave", hideTip);
  return tr;
}

/* ---------- detail / confirm ---------- */
export async function selectPlay(id) {
  S.selected = id;
  $$("#plays-body tr").forEach(tr => tr.classList.toggle("selected", tr.dataset.id === id));
  $("#detail-empty").classList.add("hidden");
  const body = $("#detail-body");
  body.classList.remove("hidden");
  body.innerHTML = `<p class="muted">Assessing…</p>`;
  const a = await post(`/api/plays/${id}/assess`);
  if (S.selected !== id) return;
  if (!a.ok) { body.innerHTML = `<p class="reasons">${escapeHtml(a.reason || "unavailable")}</p>`; return; }
  body.innerHTML = detailHTML(a);
  const approveBtn = $("#btn-approve"); if (approveBtn) approveBtn.onclick = () => approve(id);
  const rejectBtn = $("#btn-reject"); if (rejectBtn) rejectBtn.onclick = () => reject(id);
  const recordBtn = $("#btn-goto-trade"); if (recordBtn) recordBtn.onclick = () => openRecord(a.play.trade_id);
}

function detailHTML(a) {
  const p = a.play, op = a.order_preview, pdt = a.pdt || {}, em = S.state.exit_manager || {};
  const protection = { native: "broker OCO (TP + SL)", managed: "auto exit manager", none: "none" }[op.bracket_mode] || op.bracket_mode;
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
  return `
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
    ${evidenceHTML(p.evidence || {})}
    <div class="order-card">
      <h4>Order that will be sent</h4>
      <div class="kv">
        <span>Routes to</span><span><b style="color:${(op.routes_to || "").includes("LIVE") ? "var(--short)" : "var(--accent)"}">${escapeHtml(op.routes_to)}</b></span>
        <span>Action</span><span>${op.side} ${op.qty} ${escapeHtml(p.symbol)}</span>
        <span>Order type</span><span><b>${op.order_type || "—"}</b> · ${op.session_label || ""}</span>
        ${op.limit_price != null ? `<span>Limit</span><span>${num(op.limit_price)}</span>` : ""}
        ${op.stop_price != null ? `<span>Stop trigger</span><span>${num(op.stop_price)}</span>` : ""}
        <span>Time in force</span><span>${op.tif || "DAY"}</span>
        <span>Protection</span><span>${protection}</span>
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
}

function evidenceHTML(ev) {
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

function sparkSvg(values, p) {
  if (!values || values.length < 3) return "";
  const w = 340, h = 44, lo = Math.min(...values), hi = Math.max(...values), range = hi - lo || 1;
  const y = v => (h - (v - lo) / range * h).toFixed(1);
  const points = values.map((v, i) => `${(i / (values.length - 1) * w).toFixed(1)},${y(v)}`).join(" ");
  const line = (value, cls) => (value >= lo && value <= hi) ? `<line class="${cls}" x1="0" x2="${w}" y1="${y(value)}" y2="${y(value)}"/>` : "";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    ${line(p.entry, "ln-entry")}${line(p.stop, "ln-stop")}${line((p.targets || [])[0], "ln-target")}
    <polyline points="${points}"/></svg>`;
}

async function approve(id) {
  const btn = $("#btn-approve");
  if (btn) { btn.disabled = true; btn.textContent = "Sending…"; }
  const symbol = (S.plays.find(x => x.id === id) || {}).symbol || "";
  const r = await post(`/api/plays/${id}/approve`);
  if (r.ok) {
    const where = r.order_session === "EXTENDED" ? " (extended-hours limit)" : "";
    toast(`Order sent for ${symbol}: ${r.order_type || ""} ${r.status || "ok"}${where}`, "good");
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
  S.plays = S.plays.filter(p => p.id !== id);
  renderPlays();
  $("#detail-body").classList.add("hidden");
  $("#detail-empty").classList.remove("hidden");
}

export function initPlays() {
  on("plays", renderPlays);
  on("filters", renderPlays);
  on("strategies", renderPlays);
}
