/* tos-trader dashboard - vanilla JS, no build step. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = (p, opt) => fetch(p, opt).then(r => r.json());

let STATE = {};
let PLAYS = [];
let SELECTED = null;

/* ---------- formatting ---------- */
const usd = v => (v == null || isNaN(v)) ? "–" :
  (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
const num = (v, d = 2) => (v == null || isNaN(v)) ? "–" : Number(v).toFixed(d);
const pct = v => (v == null || isNaN(v)) ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(1) + "%";

/* ---------- websocket ---------- */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = ev => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleEvent(msg.topic, msg.payload || {});
  };
  ws.onclose = () => setTimeout(connect, 2000);
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
      if (p.scan) $("#scan-meta").textContent =
        `scan: ${p.scan.prefiltered}/${p.scan.scanned} passed, ${p.scan.n_plays} plays, ${p.scan.elapsed_s}s`;
      break;
    case "order.filled":
      toast(`Filled: ${p.symbol} entry x${p.qty} @ ${num(p.price)}`, "good");
      loadOpen(); refreshState(); break;
    case "trade.closed": {
      const t = p.trade || {};
      const cls = (t.realized_pl || 0) >= 0 ? "good" : "bad";
      toast(`Closed ${t.symbol}: ${usd(t.realized_pl)} (${pct(t.realized_pl_pct)})`, cls);
      loadOpen(); loadHistory(); loadStats(); refreshState(); break;
    }
    case "play.decided":
      if (p.decision === "approved" && p.result && !p.result.ok)
        toast("Order not sent: " + (p.result.reason || "rejected"), "bad");
      break;
    case "auth.reauth_required":
      showReauth(p); toast("Broker re-authentication required", "bad"); break;
    case "auth.reauth_ok":
      hideReauth(); toast("Broker re-authenticated", "good"); break;
    case "broker.error":
      toast("Broker: " + (p.message || "error"), "bad"); break;
  }
}

function refreshState() { api("/api/state").then(s => { STATE = s; renderTop(); }); }

/* ---------- top bar ---------- */
function renderTop() {
  const s = STATE || {};
  $("#mode-tag").textContent = s.mode || "suggest";
  setPill("#pill-broker", (s.broker || "?") + (s.connected ? " • live" : " • off"),
    s.connected ? "good" : "bad");
  setPill("#pill-market", s.market_open ? "market open" : "market closed", s.market_open ? "good" : "");
  setPill("#pill-armed", s.armed ? "armed" : "disarmed", s.armed ? "good" : "warn");

  const a = s.account || {};
  $("#a-equity").textContent = usd(a.equity);
  $("#a-bp").textContent = usd(a.buying_power);
  $("#a-cash").textContent = usd(a.cash);
  const dt = s.day_trades_5d ?? 0, lim = s.day_trade_limit ?? 3;
  const dtEl = $("#a-dt");
  dtEl.textContent = `${dt} / ${lim}`;
  dtEl.style.color = dt >= lim ? "var(--short)" : dt >= lim - 1 ? "var(--warn)" : "var(--fg)";
  const today = (s.pnl || {}).realized_today ?? 0;
  const pe = $("#a-pnl-today"); pe.textContent = usd(today);
  pe.style.color = today > 0 ? "var(--long)" : today < 0 ? "var(--short)" : "var(--fg)";

  const tok = s.token || {};
  setPill("#pill-token", "token: " + (tok.message ? shorten(tok.message, 26) : "ok"),
    tok.needs_reauth ? "bad" : tok.needs_rotation ? "warn" : "good");
  $("#pill-token").title = JSON.stringify(tok, null, 1);
  if (tok.needs_reauth) showReauth({ status: tok });
}
function setPill(sel, text, cls) {
  const el = $(sel); el.textContent = text;
  el.className = "pill" + (cls ? " " + cls : "");
}
const shorten = (s, n) => s && s.length > n ? s.slice(0, n - 1) + "…" : s;

/* ---------- plays table ---------- */
function filtered() {
  const fL = $("#f-long").checked, fS = $("#f-short").checked,
    fI = $("#f-intraday").checked, fW = $("#f-swing").checked;
  return PLAYS.filter(p =>
    (p.side === "LONG" ? fL : fS) &&
    (p.timeframe === "INTRADAY" ? fI : fW));
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
    if (p.id === SELECTED) tr.classList.add("selected");
    tr.innerHTML = `
      <td class="sym">${p.symbol}</td>
      <td><span class="side ${p.side}">${p.side}</span></td>
      <td>${p.strategy.replace(/_/g, " ")}</td>
      <td class="tf">${p.timeframe === "INTRADAY" ? "day" : "swing"}</td>
      <td class="num">${num(p.entry)}</td>
      <td class="num">${num(p.stop)}</td>
      <td class="num">${num((p.targets || [])[0])}</td>
      <td class="num">${num(p.reward_risk, 1)}</td>
      <td class="num">${p.suggested_qty || 0}</td>
      <td class="num">${usd(p.dollar_risk)}</td>
      <td class="num"><span class="score-bar"><i style="width:${Math.min(100, (p.score || 0) * 100)}%"></i></span></td>
      <td><span class="info-dot" data-tip="${encodeURIComponent(tipText(p))}">i</span></td>`;
    tr.addEventListener("click", () => selectPlay(p.id));
    body.appendChild(tr);
  }
  wireTips();
}
function tipText(p) {
  return (p.explanation || p.rationale || "").trim();
}

/* ---------- hover tooltip ---------- */
const tip = $("#tooltip");
function wireTips() {
  $$("#plays-body tr").forEach(tr => {
    tr.addEventListener("mousemove", e => {
      const p = PLAYS.find(x => x.id === tr.dataset.id);
      if (!p) return;
      showTip(e, p.strategy.replace(/_/g, " ").toUpperCase(), tipText(p));
    });
    tr.addEventListener("mouseleave", hideTip);
  });
}
function showTip(e, title, text) {
  tip.innerHTML = `<div class="tt-title">${title}</div>${escapeHtml(text)}`;
  tip.classList.remove("hidden");
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + w > innerWidth) x = e.clientX - w - pad;
  if (y + h > innerHeight) y = e.clientY - h - pad;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
function hideTip() { tip.classList.add("hidden"); }
const escapeHtml = s => (s || "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

/* ---------- detail / confirm ---------- */
async function selectPlay(id) {
  SELECTED = id;
  $$("#plays-body tr").forEach(tr => tr.classList.toggle("selected", tr.dataset.id === id));
  $("#detail-empty").classList.add("hidden");
  const body = $("#detail-body");
  body.classList.remove("hidden");
  body.innerHTML = `<p class="muted">Assessing…</p>`;
  let a;
  try { a = await api(`/api/plays/${id}/assess`, { method: "POST" }); }
  catch { body.innerHTML = `<p class="reasons">Could not assess play.</p>`; return; }
  if (!a.ok) { body.innerHTML = `<p class="reasons">${a.reason || "unavailable"}</p>`; return; }

  const p = a.play, op = a.order_preview, pdt = a.pdt || {};
  body.innerHTML = `
    <h3>${p.symbol} <span class="side ${p.side}">${p.side}</span></h3>
    <div class="sub">${p.strategy.replace(/_/g, " ")} · ${p.timeframe} · conf ${num(p.confidence, 2)} · score ${num(p.score, 2)}</div>
    ${sparkSvg(p.evidence && p.evidence.spark, p)}
    <div class="explain">${escapeHtml(p.explanation || p.rationale)}</div>
    <div class="kv">
      <span>Entry</span><span>${num(p.entry)}</span>
      <span>Stop</span><span>${num(p.stop)} (${usd(-(Math.abs(p.entry - p.stop)))}/sh)</span>
      <span>Target(s)</span><span>${(p.targets || []).map(t => num(t)).join(" → ")}</span>
      <span>Reward : Risk</span><span>${num(p.reward_risk, 1)} : 1</span>
    </div>
    ${renderEvidence(p.evidence || {})}
    <div class="order-card">
      <h4>Order preview</h4>
      <div class="kv">
        <span>Action</span><span>${op.side} ${op.qty} ${p.symbol}</span>
        <span>Type</span><span>${op.type} @ ~${num(op.limit_hint)}</span>
        <span>Bracket</span><span>${op.bracket ? `TP ${num(op.take_profit)} / SL ${num(op.stop_loss)}` : "none"}</span>
        <span>Est. cost</span><span>${usd(op.est_cost)}</span>
        <span>Est. risk</span><span>${usd(op.est_risk)}</span>
      </div>
      ${pdt.warnings && pdt.warnings.length ? `<ul class="warnings">${pdt.warnings.map(w => `<li>${escapeHtml(w)}</li>`).join("")}</ul>` : ""}
      <div class="kv">
        <span>Day trades used</span><span>${pdt.day_trades_used ?? "?"} (${pdt.day_trades_remaining ?? "?"} left)</span>
      </div>
      ${a.reasons && a.reasons.length ? `<div class="reasons">⚠ ${a.reasons.map(escapeHtml).join("<br>")}</div>` : ""}
      <div class="confirm-row">
        <button class="${p.side === "LONG" ? "long" : "danger"}" id="btn-approve" ${a.can_execute ? "" : "disabled"}>
          Execute &#10003; Yes</button>
        <button class="ghost" id="btn-reject">Dismiss</button>
      </div>
    </div>`;
  $("#btn-approve").onclick = () => approve(id);
  $("#btn-reject").onclick = () => reject(id);
}

function renderEvidence(ev) {
  const blocks = [];
  const sig = ev.signal;
  if (sig && sig.rows) {
    blocks.push(`<h4>Comps vs peers</h4><table class="ev-table">` +
      sig.rows.map(r => `<tr><td>${r.multiple}</td><td class="num">${r.target}</td>
        <td class="num">${r.peer_median}</td><td class="num">${r.gap_pct}%</td></tr>`).join("") +
      `</table><div class="muted">mean gap ${sig.mean_gap_pct}% → ${sig.verdict.replace(/_/g," ")}</div>`);
  }
  if (ev.dcf) {
    const d = ev.dcf, as = d.assumptions || {};
    blocks.push(`<h4>DCF</h4><div class="kv">
      <span>WACC</span><span>${(as.wacc * 100).toFixed(1)}%</span>
      <span>Cost of equity</span><span>${(as.cost_of_equity * 100).toFixed(1)}%</span>
      <span>Exit multiple</span><span>${as.exit_multiple}x</span>
      <span>Perpetuity g</span><span>${(as.perpetuity_growth * 100).toFixed(1)}%</span>
      <span>Price (multiple)</span><span>${num(d.price_multiple)}</span>
      <span>Price (perpetuity)</span><span>${num(d.price_perpetuity)}</span>
      <span>Blended fair value</span><span>${num(d.price_blended)} (${pct(d.upside_blended_pct)})</span></div>`);
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
      rest.map(([k, v]) => `<span>${k}</span><span>${typeof v === "object" ? "" : v}</span>`).join("") + `</div>`);
  }
  return blocks.join("");
}

function sparkSvg(arr, p) {
  if (!arr || arr.length < 3) return "";
  const w = 340, h = 44, lo = Math.min(...arr), hi = Math.max(...arr), rng = hi - lo || 1;
  const pts = arr.map((v, i) => `${(i / (arr.length - 1) * w).toFixed(1)},${(h - (v - lo) / rng * h).toFixed(1)}`).join(" ");
  const y = v => (h - (v - lo) / rng * h).toFixed(1);
  const line = (val, col) => (val >= lo && val <= hi)
    ? `<line x1="0" x2="${w}" y1="${y(val)}" y2="${y(val)}" stroke="${col}" stroke-dasharray="3 3" stroke-width="1"/>` : "";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    ${line(p.entry, "#4f9cff")}${line(p.stop, "#f85149")}${line((p.targets || [])[0], "#2ea043")}
    <polyline points="${pts}" fill="none" stroke="#8b98a9" stroke-width="1.4"/></svg>`;
}

async function approve(id) {
  const btn = $("#btn-approve"); if (btn) { btn.disabled = true; btn.textContent = "Sending…"; }
  const r = await api(`/api/plays/${id}/approve`, { method: "POST" });
  if (r.ok) {
    toast(`Order sent for ${(PLAYS.find(x => x.id === id) || {}).symbol || ""}: ${r.status || "ok"}`, "good");
    $("#detail-body").classList.add("hidden"); $("#detail-empty").classList.remove("hidden");
  } else {
    toast("Not sent: " + (r.reason || "rejected"), "bad");
    if (btn) { btn.disabled = false; btn.innerHTML = "Execute &#10003; Yes"; }
  }
}
async function reject(id) {
  await api(`/api/plays/${id}/reject`, { method: "POST" });
  PLAYS = PLAYS.filter(p => p.id !== id); renderPlays();
  $("#detail-body").classList.add("hidden"); $("#detail-empty").classList.remove("hidden");
}

/* ---------- blotter ---------- */
$$(".tab").forEach(t => t.onclick = () => {
  $$(".tab").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  ["open", "history", "stats"].forEach(n =>
    $("#tab-" + n).classList.toggle("hidden", n !== t.dataset.tab));
  if (t.dataset.tab === "open") loadOpen();
  if (t.dataset.tab === "history") loadHistory();
  if (t.dataset.tab === "stats") loadStats();
});

async function loadOpen() {
  const { trades } = await api("/api/trades?status=OPEN");
  const pos = (STATE.positions || []);
  const el = $("#tab-open");
  if (!trades.length) { el.innerHTML = `<p class="muted" style="padding:10px">No open positions.</p>`; return; }
  el.innerHTML = `<table><thead><tr><th>Symbol</th><th>Side</th><th>Strategy</th>
    <th class="num">Qty</th><th class="num">Entry</th><th class="num">Mark</th>
    <th class="num">Unrealized</th><th class="num">Stop</th><th class="num">Target</th><th></th></tr></thead><tbody>${
    trades.map(t => {
      const pp = pos.find(x => x.symbol === t.symbol) || {};
      const upl = pp.unrealized_pl;
      return `<tr>
        <td class="sym">${t.symbol}</td><td><span class="side ${t.side}">${t.side}</span></td>
        <td>${(t.strategy || "").replace(/_/g, " ")}</td>
        <td class="num">${num(t.quantity, 0)}</td>
        <td class="num">${num(t.entry_price)}</td>
        <td class="num">${num(pp.market_price)}</td>
        <td class="num ${upl >= 0 ? "pl-pos" : "pl-neg"}">${usd(upl)}</td>
        <td class="num">${num(t.stop_price)}</td>
        <td class="num">${num(t.target_price)}</td>
        <td><button class="danger" data-close="${t.id}">Close</button></td></tr>`;
    }).join("")}</tbody></table>`;
  $$("[data-close]", el).forEach(b => b.onclick = async () => {
    b.disabled = true;
    const r = await api(`/api/trades/${b.dataset.close}/close`, { method: "POST" });
    if (!r.ok) { toast("Close failed: " + (r.reason || ""), "bad"); b.disabled = false; }
  });
}
async function loadHistory() {
  const { trades } = await api("/api/trades?limit=200");
  const closed = trades.filter(t => t.status === "CLOSED");
  const el = $("#tab-history");
  if (!closed.length) { el.innerHTML = `<p class="muted" style="padding:10px">No closed trades yet.</p>`; return; }
  el.innerHTML = `<table><thead><tr><th>Closed</th><th>Symbol</th><th>Side</th><th>Strategy</th>
    <th class="num">Entry</th><th class="num">Exit</th><th class="num">P/L</th><th class="num">P/L %</th>
    <th class="num">R</th><th>DT</th><th>Reason</th></tr></thead><tbody>${
    closed.map(t => `<tr>
      <td>${(t.exit_time || "").slice(5, 16).replace("T", " ")}</td>
      <td class="sym">${t.symbol}</td><td><span class="side ${t.side}">${t.side}</span></td>
      <td>${(t.strategy || "").replace(/_/g, " ")}</td>
      <td class="num">${num(t.entry_price)}</td><td class="num">${num(t.exit_price)}</td>
      <td class="num ${t.realized_pl >= 0 ? "pl-pos" : "pl-neg"}">${usd(t.realized_pl)}</td>
      <td class="num ${t.realized_pl >= 0 ? "pl-pos" : "pl-neg"}">${pct(t.realized_pl_pct)}</td>
      <td class="num">${num(t.r_multiple, 2)}</td>
      <td>${t.is_day_trade ? "•" : ""}</td>
      <td class="muted">${t.exit_reason || ""}</td></tr>`).join("")}</tbody></table>`;
}
async function loadStats() {
  const s = await api("/api/pnl");
  const el = $("#tab-stats");
  const g = (label, val, cls) => `<div class="stat"><label>${label}</label><b class="${cls || ""}">${val}</b></div>`;
  el.innerHTML = `<div class="stat-grid">
    ${g("Realized today", usd(s.realized_today), s.realized_today >= 0 ? "pl-pos" : "pl-neg")}
    ${g("Realized week", usd(s.realized_week), s.realized_week >= 0 ? "pl-pos" : "pl-neg")}
    ${g("Realized total", usd(s.realized_total), s.realized_total >= 0 ? "pl-pos" : "pl-neg")}
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

/* ---------- misc UI ---------- */
function toast(text, cls) {
  const d = document.createElement("div");
  d.className = "toast " + (cls || "");
  d.textContent = text;
  $("#toasts").appendChild(d);
  setTimeout(() => d.remove(), 6000);
}
function showReauth(p) {
  const b = $("#reauth-banner");
  b.classList.remove("hidden");
  b.innerHTML = `<span>⚠ Broker token needs re-authentication ${
    p.status && p.status.message ? "— " + escapeHtml(p.status.message) : ""}.</span>
    <button id="btn-reauth">Re-authenticate</button>
    <span class="muted">or run <code>python scripts/authenticate.py</code></span>`;
  $("#btn-reauth").onclick = async () => {
    const r = await api("/api/auth/reauth", { method: "POST" });
    toast(r.note || "re-auth started", "good");
  };
}
function hideReauth() { $("#reauth-banner").classList.add("hidden"); }

$("#btn-scan").onclick = async () => {
  $("#btn-scan").disabled = true;
  await api("/api/scan/now", { method: "POST" });
  toast("Scan queued", "good");
  setTimeout(() => $("#btn-scan").disabled = false, 4000);
};
$$("#f-long,#f-short,#f-intraday,#f-swing").forEach(c => c.onchange = renderPlays);

$("#btn-strategies").onclick = async () => {
  const { strategies } = await api("/api/strategies");
  $("#drawer-body").innerHTML = strategies.map(s => `
    <div class="strat"><div class="meta">${s.kind} · ${s.timeframe}</div>
    <h4>${s.title}</h4><div>${escapeHtml(s.thesis)}</div></div>`).join("");
  $("#drawer").classList.remove("hidden");
};
$("#drawer-close").onclick = () => $("#drawer").classList.add("hidden");
$("#drawer").onclick = e => { if (e.target.id === "drawer") $("#drawer").classList.add("hidden"); };

/* ---------- boot ---------- */
api("/api/state").then(s => { STATE = s; renderTop(); });
api("/api/plays").then(d => { PLAYS = d.plays || []; renderPlays(); });
loadOpen();
connect();
setInterval(refreshState, 15000);
