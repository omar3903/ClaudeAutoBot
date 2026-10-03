/* The plays table, and the detail panel: why, the numbers, and the order. */
import { $, $$, api, escapeHtml, fmtClock, isNewer, num, pct, pretty, post, sectorTag, sideBadge, tfLabel, usd } from "./util.js";
import { S, on } from "./state.js";
import { openModal, toast } from "./ui.js";
import { hideTip, showTipAt } from "./tooltips.js";
import { stratLabel, updateStrategy } from "./strategies.js";
import { hideExecuted, hideNoisy } from "./filters.js";
import { loadOpen, openRecord, showTab } from "./blotter.js";
import { orderMark } from "./orders.js";
import { openChart } from "./chart.js";
import { openSignals } from "./signals.js";
import { watchPrice } from "./price.js";

const DONE = new Set(["ACCEPTED", "SUBMITTED", "WORKING", "PARTIAL", "FILLED", "ERROR"]);
const isDone = p => DONE.has(p.status);
const isNoisy = p => (p.noise || []).length > 0;
const noiseLabel = flag => ((S.state.autopilot || {}).noise_labels || {})[flag] || flag.replace(/_/g, " ");

/* Clicks waiting on the app, by play id: "sending" (Execute) or "dismissing". The screen shows the click at
   once - the row says it's sending, or is gone - and S.plays is never touched until the answer, so a refusal
   puts the row back exactly as it was, and a push arriving meanwhile can't undo the click on screen. */
const pending = new Map();

function setPending(id, what) {
  if (what) pending.set(id, what); else pending.delete(id);
  renderPlays();
}

export function mergePlay(row) {
  const i = S.plays.findIndex(x => x.id === row.id);
  if (i >= 0) { S.plays[i] = { ...S.plays[i], ...row }; renderPlays(); }
}

// a dismissed play stays on the app's board (its setup isn't offered again today), so the push still carries it
const dismissed = p => p.status === "REJECTED" || pending.get(p.id) === "dismissing";

function visiblePlays() {
  const f = S.state.filters || {};
  const sides = f.sides || ["LONG", "SHORT"], tfs = f.timeframes || ["INTRADAY", "SWING"];
  return S.plays.filter(p => !dismissed(p) && sides.includes(p.side) && tfs.includes(p.timeframe)
    && (!hideExecuted() || !isDone(p)) && (!hideNoisy() || isDone(p) || !isNoisy(p)));
}

function emptyText() {
  const s = (S.state.scan || {}).settings;
  return s
    ? `No plays yet. The full scan runs at <b>${escapeHtml(s.premarket_time)} ET</b>, and in the session the hot list is rescanned every <b>${s.cycle_minutes} min</b> — or hit <b>Scan now</b>.`
    : "No plays yet — hit <b>Scan now</b>.";
}

/* The rows on screen, by play id, each with its signature: its class and cells as last drawn. A push redraws
   only the rows whose signature changed and moves rows only when the order did, so a hover, the selected row
   and a click in flight survive a push that didn't touch them. */
const shown = new Map();
const sigOf = ({ cls, html }) => `${cls}|${html}`;

export function renderPlays() {
  const rows = visiblePlays(), offered = S.plays.filter(p => !dismissed(p)).length;
  $("#plays-count").textContent = rows.length ? `(${rows.length})` : "";
  $("#plays-empty").innerHTML = offered && !rows.length
    ? `All ${offered} plays are hidden by the view options above — untick <b>Hide noise</b> or <b>Hide executed</b> to see them.`
    : emptyText();
  $("#plays-empty").classList.toggle("hidden", rows.length > 0);
  const body = $("#plays-body"), ids = new Set(rows.map(p => p.id));
  for (const [id, r] of shown) if (!ids.has(id)) { r.tr.remove(); shown.delete(id); }
  rows.forEach((p, i) => {
    const row = playRow(p), sig = sigOf(row);
    let r = shown.get(p.id);
    if (!r || r.sig !== sig) {
      const tr = document.createElement("tr");
      tr.dataset.id = p.id;
      tr.className = row.cls;
      tr.innerHTML = row.html;
      if (r) r.tr.replaceWith(tr);
      shown.set(p.id, r = { tr, sig });
    }
    r.tr.classList.toggle("selected", p.id === S.selected);
    if (body.children[i] !== r.tr) body.insertBefore(r.tr, body.children[i] || null);
  });
  followSelected();
}

/* A streamed price (events.js): each play of those stocks takes it where it's newer than the price the play
   holds, and with `draw` its Price cell alone is drawn again - the row stays the same element, so a hover, the
   selected row and a click in flight survive - and the row's signature follows, so the next push doesn't draw
   it again for the price. A row that was due a redraw anyway keeps its old signature, and gets it. */
export function tickPlays(prices, draw) {
  for (const p of S.plays) {
    const t = prices[p.symbol];
    if (!t || !isNewer(t.at, p.last_at)) continue;
    const r = draw ? shown.get(p.id) : null, current = !!r && r.sig === sigOf(playRow(p));
    p.last_price = t.price;
    p.last_at = t.at;
    const cell = r && $('td[data-live="price"]', r.tr);
    if (!cell) continue;
    cell.outerHTML = priceCell(p);
    if (current) r.sig = sigOf(playRow(p));
  }
}

/** A play whose score the insider or news signals moved: click for its stock on the Signals page. */
function signalMark(p) {
  const ev = p.evidence || {}, delta = Number(ev.signal_nudge || 0);
  if (!ev.signal_reasons) return "";
  return `<span class="badge sig-mark ${delta > 0 ? "good" : delta < 0 ? "bad" : ""}" title="Signals ${delta > 0 ? "+" : ""}${delta.toFixed(3)}: ${escapeHtml(ev.signal_reasons)}">signal</span>`;
}

/* How far a price is past the play's entry in R (negative: short of it), or null when the play has no risk. */
function pastEntry(p, price) {
  const risk = Math.abs(p.entry - p.stop);
  return risk ? (p.side === "SHORT" ? -1 : 1) * (price - p.entry) / risk : null;
}
const pastEntryWords = r => `${Math.abs(r).toFixed(2)}R ${r >= 0 ? "past" : "short of"} the entry`;

/* The latest price the app holds for the stock, and how far it has run from the entry in R - an entry is
   refused a quarter of an R past it (execution.max_chase_r). The time it's from is in the tooltip. */
function priceCell(p) {
  if (p.last_price == null) return `<td class="num muted" data-live="price" title="No price yet - Refresh fetches one">–</td>`;
  const r = pastEntry(p, p.last_price);
  const at = p.last_at ? new Date(p.last_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  const tip = `${at ? `As of ${at}` : ""}${(S.state.data || {}).delayed ? " (delayed data)" : ""}${r == null ? "" : `. ${pastEntryWords(r)}`}`;
  const cls = r == null ? "" : r > 0.25 ? "warn-text" : "muted";
  return `<td class="num" data-live="price" title="${escapeHtml(tip)}">${num(p.last_price)}${r == null ? "" : ` <span class="small ${cls}">${r >= 0 ? "+" : ""}${r.toFixed(1)}R</span>`}</td>`;
}

/* What Autopilot makes of a play, in one line: the first of its checks the play fails (the gate's own
   words), that it tried the play and was refused, the cap it waits for, what its last pass said, or that
   it would take it. The robot's tooltip and the detail panel both show it. */
function apLine(ap) {
  if (ap.why_not) return `Won't take it: ${ap.why_not}`;
  if (ap.skipped) return `Tried it and was refused: ${ap.reason || "no reason given"} - not tried again today unless a setting changes`;
  if (ap.waiting) return `Waiting: ${ap.waiting}`;
  if (ap.reason) return `Last pass: ${ap.reason}`;
  return ap.acted ? "Has acted on it" : "Would take it on its next pass";
}

/* The setup's replayed record over the trades Autopilot would take (row.record): its average R a trade over
   how many trades. Green once proven, red while it loses, amber in between, grey with no replayed trades.
   The tooltip adds the held-out sessions and, when it isn't proven, why not in the gate's own words. */
const signedR = v => `${v >= 0 ? "+" : ""}${num(v, 2)}R`;
const recordSentence = rec => "replayed the way Autopilot takes it, " + (rec.trades
  ? `averaged ${signedR(rec.expectancy_r)} a trade over ${rec.trades} trades${rec.held_out_trades ? `, ${signedR(rec.held_out_r)} over the ${rec.held_out_trades} in the held-out sessions` : ""}`
  : "has no trades yet");

function recordChip(rec) {
  if (!rec) return "";
  const why = `This setup, ${recordSentence(rec)}. ${rec.proven ? "Proven" : `Not proven: ${rec.why || "no record"}`}`;
  const cls = !rec.trades ? "faint" : rec.proven ? "good" : rec.expectancy_r < 0 ? "bad" : "warn";
  return `<span class="badge ${cls} rec-chip" data-term="record" data-why="${escapeHtml(why)}">${rec.trades ? `${signedR(rec.expectancy_r)} ×${rec.trades}` : "no replay"}</span>`;
}

/* The record in the detail panel, with what the replayed wins average set beside the play's expected R -
   which counts a win at the full target (up to 4R, as the ranking does) - and the setup's off switch. */
function recordBlock(p) {
  const rec = p.record;
  if (!rec) return "";
  const exp = (p.evidence || {}).expected_r;
  const wins = !rec.trades || exp == null ? ""
    : `<div class="muted small">This play expects ${signedR(exp)}, counting a win at the full target (${signedR(Math.min(p.reward_risk || 0, 4))}); ${rec.win_rate ? `the replayed wins average ${signedR(rec.avg_win_r)}` : "none of the replayed trades won"}.</div>`;
  const enabled = (S.strategies[p.strategy] || {}).enabled !== false;
  return `<div class="rec-box">
      <div>The setup, ${escapeHtml(recordSentence(rec))}. <span class="badge ${rec.proven ? "good" : "warn"}" data-term="record">${rec.proven ? "proven" : "not proven"}</span></div>
      ${rec.proven ? "" : `<div class="muted small">${escapeHtml(rec.why || "")}</div>`}
      ${wins}
      ${enabled ? `<button class="ghost mini lockable" id="btn-strat-off" title="The Strategies panel's switch - it stays off until you switch it back on">Switch this setup off</button>` : ""}
    </div>`;
}

/* The Strategies panel's own switch, from a play: the setup stays off, through a restart, until it's switched
   back on there. Its plays leave the board, so the panel closes once it's off. */
function switchOff(p) {
  const name = (S.strategies[p.strategy] || {}).title || pretty(p.strategy);
  openModal({
    title: `Switch ${name} off?`,
    bodyHTML: `<p>The scans stop looking for this setup and its plays leave the board, for you and for Autopilot.</p>
      <p><b>It stays off until you switch it back on</b> under Strategies - the change is saved and kept through a restart.</p>
      <p class="muted">Positions it has open are left as they are, and their exits stay managed.</p>`,
    okText: "Switch off", okClass: "danger",
    onOk: async () => {
      const r = await updateStrategy(p.strategy, { enabled: false });
      if (r.ok && S.selected === p.id) {
        $("#detail-body").classList.add("hidden");
        $("#detail-empty").classList.remove("hidden");
      }
    },
  });
}

/** A play's row: its class (the selected row's is added by renderPlays) and its cells. */
function playRow(p) {
  // one this tab is sending shows as sent until the answer - a refusal puts it back
  const sending = pending.get(p.id) === "sending", done = isDone(p) || sending, ap = p.autopilot || {};
  // it tried the play and the engine's assessment or the order was refused: no bar, and a faded robot saying why
  const skipped = !!ap.skipped && !done;
  // a play it has acted on gets no bar either: it won't look at it again today, whatever the caps say
  const bar = ap.eligible && !skipped && !done && !ap.acted;
  const cls = [done && "done", bar && !ap.waiting && "ap-eligible", bar && ap.waiting && "ap-waiting"]
    .filter(Boolean).join(" ");
  // a play it won't take gets a faded robot too, only while Autopilot is on - off, every row would have one
  const refused = (!ap.eligible && !!ap.why_not || skipped) && !!(S.state.autopilot || {}).effective;
  // acted on and still on offer: the robot says why, as apLine does
  const apMark = ap.acted && !skipped
    ? `<span class="ap-badge acted" data-term="autopilot"${done ? "" : ` data-why="${escapeHtml(apLine(ap))}"`}>🤖</span>`
    : ((ap.eligible || refused) && !done
      ? `<span class="ap-badge${ap.waiting && !skipped ? " waiting" : ""}${refused ? " refused" : ""}" data-term="autopilot" data-why="${escapeHtml(apLine(ap))}">🤖</span>`
      : "");
  const last = sending ? `<span class="badge warn" title="Sent to the app - waiting for its answer">sending…</span>`
    : done
      ? `<span class="badge ${p.status === "ERROR" ? "bad" : "good"}" data-term="executed">${p.status === "FILLED" ? "✓ executed" : p.status.toLowerCase()}</span>`
      : `<span class="info-dot">i</span>`;
  const html = `
    <td class="sym">${escapeHtml(p.symbol)} ${sectorTag(p.sector)}${orderMark(p)}${signalMark(p)}${apMark}${p.extended_hours_ok ? '<span class="ext" data-term="ext">ext</span>' : ""}${isNoisy(p) && !done ? `<span class="badge warn" data-term="noise" title="${escapeHtml(p.noise.map(noiseLabel).join(", "))}">noisy</span>` : ""}</td>
    <td>${sideBadge(p.side)}</td>
    <td>${stratLabel(p.strategy)} ${recordChip(p.record)}</td>
    <td class="tf">${tfLabel(p.timeframe)}</td>
    <td class="num">${num(p.entry)}</td>
    ${priceCell(p)}
    <td class="num">${num(p.stop)}</td>
    <td class="num">${num((p.targets || [])[0])}</td>
    <td class="num">${num(p.reward_risk, 1)}</td>
    <td class="num">${p.suggested_qty || 0}</td>
    <td class="num">${usd(p.dollar_risk)}</td>
    <td class="num"><span class="score-bar"><i style="width:${Math.min(100, (p.score || 0) * 100)}%"></i></span></td>
    <td class="row-tools"><button class="chart-btn" title="Chart, and the ways this trade can end" aria-label="Chart">📈</button>${last}</td>`;
  return { cls, html };
}

/* A row's hover shows the play's full explanation. The board's push leaves that out - it's most of a play's
   size - so it's fetched whole (GET /api/plays/{id}) once the pointer has rested on the row, and kept while
   the play's entry, stop, targets and rationale stay the same. Until it comes, or once the play has left the
   board, the one-line rationale shows. */
const EXPLAIN_WAIT_MS = 250;
const explained = new Map();          // play id -> {stamp, text}
let hovered = null, hoverTimer = null, hoverAt = [0, 0];
const stamp = p => [p.entry, p.stop, (p.targets || []).join(","), p.rationale].join("|");
const rowTitle = p => ((S.strategies[p.strategy] || {}).title || pretty(p.strategy)).toUpperCase();

function rowTip(p) {
  const known = explained.get(p.id);
  return (p.explanation || (known && known.stamp === stamp(p) ? known.text : "") || p.rationale || "").trim();
}

function hoverRow(p, x, y) {
  hoverAt = [x, y];
  showTipAt(x, y, rowTitle(p), rowTip(p));
  if (hovered === p.id) return;                      // already fetched, or on its way, for this visit
  hovered = p.id;
  clearTimeout(hoverTimer);
  const known = explained.get(p.id);
  if (!p.explanation && !(known && known.stamp === stamp(p))) hoverTimer = setTimeout(() => explain(p.id), EXPLAIN_WAIT_MS);
}

async function explain(id) {
  let row;
  // the app isn't reachable, or failed: the rationale stands, and the next visit asks again
  try { row = await api(`/api/plays/${encodeURIComponent(id)}`); } catch (e) { if (e.status !== 404) return; row = null; }
  const p = S.plays.find(x => x.id === id);
  if (!p) return;
  // a play that has left the board answers 404: the rationale stands, and isn't asked again while it reads the same
  const whole = row && row.id === id ? row : null;
  explained.set(id, { stamp: stamp(whole || p), text: whole ? whole.explanation || "" : "" });
  if (explained.size > 200) explained.delete(explained.keys().next().value);      // the oldest first
  if (hovered === id) showTipAt(hoverAt[0], hoverAt[1], rowTitle(p), rowTip(p));
}

/** The play of the row an event happened in. */
function rowPlay(target) {
  const tr = target.closest("tr[data-id]");
  return tr ? S.plays.find(x => x.id === tr.dataset.id) : null;
}

/* ---------- detail / confirm ---------- */
export async function selectPlay(id) {
  S.selected = id;
  drawn = null;
  $$("#plays-body tr").forEach(tr => tr.classList.toggle("selected", tr.dataset.id === id));
  $("#detail-empty").classList.add("hidden");
  const body = $("#detail-body");
  body.classList.remove("hidden");
  body.innerHTML = `<p class="muted">Assessing…</p>`;
  const a = await post(`/api/plays/${id}/assess`);
  if (S.selected !== id) return;
  if (!a.ok) { body.innerHTML = `<p class="reasons">${escapeHtml(a.reason || "unavailable")}</p>`; return; }
  drawDetail(a);
}

/* The detail panel as last drawn: its play's assessment, and the status its row had then. A push or a decision
   that moves the play on - Autopilot sent it, its order filled or was refused - draws the panel again, so it
   never offers Execute for a play that has gone out (the app would refuse it as already sent). */
let drawn = null;                    // {id, a, seen}

function drawDetail(a) {
  const id = a.play.id, body = $("#detail-body");
  drawn = { id, a, seen: (S.plays.find(x => x.id === id) || a.play).status };
  $("#detail-empty").classList.add("hidden");
  body.classList.remove("hidden");
  body.innerHTML = detailHTML(a);
  const approveBtn = $("#btn-approve"); if (approveBtn) approveBtn.onclick = () => approve(id);
  const rejectBtn = $("#btn-reject"); if (rejectBtn) rejectBtn.onclick = () => reject(id);
  const recordBtn = $("#btn-goto-trade"); if (recordBtn) recordBtn.onclick = () => openRecord(a.play.trade_id);
  const offBtn = $("#btn-strat-off"); if (offBtn) offBtn.onclick = () => switchOff(a.play);
  // the market price, kept fresh while the panel shows this play - and against the plan until it's sent
  const sent = a.already_executed || isDone(a.play);
  watchPrice($("[data-price]", body), a.play.symbol, d => {
    const r = sent ? null : pastEntry(a.play, d.price);
    return r == null ? "" : ` · ${pastEntryWords(r)}`;
  });
  followSelected();                  // a push that came while it was being assessed
}

function hideDetail() {
  $("#detail-body").classList.add("hidden");
  $("#detail-empty").classList.remove("hidden");
}

/* Sent: drawn at once from the row over the assessment - play.decided brings the whole row, with who sent it and
   when - with no request. Back on offer after it had gone out (its order was refused): assessed afresh, as Execute
   needs. Dismissed in another tab: closed. A panel already showing the play as sent follows only the row's own
   moves, never a row that lags the assessment - that would step a filled play back, or assess it again and again
   until the next push. */
function followSelected() {
  if (!drawn || drawn.id !== S.selected || $("#detail-body").classList.contains("hidden")) return;
  const row = S.plays.find(x => x.id === drawn.id);
  if (!row) return;
  const was = drawn.a.play, moved = row.status !== drawn.seen;
  drawn.seen = row.status;
  if (row.status === "REJECTED") { if (moved) hideDetail(); }
  else if (isDone(row)) {
    if ((moved || !isDone(was)) && row.status !== was.status)
      drawDetail({ ...drawn.a, play: { ...was, ...row, evidence: { ...(was.evidence || {}), ...(row.evidence || {}) } } });
  } else if (moved && isDone(was)) selectPlay(row.id);
}

/* How many times in a row a day play has shown: on 5-minute candles when Autopilot counts those
   (a scan reading the same candle again isn't another sighting), otherwise in scans. */
function seenText(p) {
  const n = p.confirmations || 1;
  return (S.state.autopilot || {}).confirm_on_new_candle && (p.evidence || {}).bar_at
    ? `seen on ${n} candle${n === 1 ? "" : "s"} in a row` : `seen in ${n} scan${n === 1 ? "" : "s"} in a row`;
}

/* Who sent a play's order and when - from what it was taken on (evidence.at_entry, in the whole row: the
   assessment's, or the one play.decided brings) - and where the order stands. */
const STANDS = { ACCEPTED: "going out", SUBMITTED: "working", WORKING: "working", PARTIAL: "part filled", FILLED: "filled" };

function sentLine(p) {
  const at = (p.evidence || {}).at_entry || {};
  const who = at.by === "autopilot" || (!at.by && (p.autopilot || {}).acted) ? "Autopilot sent it"
    : at.by ? "Sent from the dashboard" : "Sent";
  return `✓ ${who}${at.at ? ` at ${fmtClock(at.at)}` : ""} · ${STANDS[p.status] || p.status.toLowerCase()}`
    + (p.trade_id ? ` — trade <code>${escapeHtml(p.trade_id)}</code>` : "");
}

function detailHTML(a) {
  const p = a.play, op = a.order_preview, pdt = a.pdt || {}, em = S.state.exit_manager || {};
  const protection = { native: "broker OCO (TP + SL)", managed: "auto exit manager", none: "none" }[op.bracket_mode] || op.bracket_mode;
  const exitLine = em.enabled
    ? `stop @ ${num(op.stop_loss)}, target @ ${num(op.take_profit)}, ${em.scale_out_pct && (p.targets || []).length > 1 ? `${num(em.scale_out_pct, 0)}% off at the first target (stop to the entry, the rest to ${num(p.targets[1])}), ` : ""}then break-even at ${num(em.breakeven_at_r, 1)}R`
    + (em.trail_start_r > 0 ? `, trail from ${num(em.trail_start_r, 1)}R (lock ${Math.round(em.trail_lock_ratio * 100)}%)` : "")
    + (p.timeframe === "INTRADAY" && em.flatten_intraday_before_close_min ? `, flatten ${em.flatten_intraday_before_close_min} min before the close` : "")
    : "OFF — you must close this manually";
  const executed = a.already_executed || isDone(p), sending = pending.get(p.id) === "sending";
  const confirmBlock = executed
    ? `<div class="reasons">${p.status === "ERROR" ? "⚠ the order errored — the setup isn't offered again today" : sentLine(p)}</div>
       <div class="confirm-row">
         ${p.trade_id ? `<button id="btn-goto-trade">Show trade record</button>` : ""}
       </div>`
    : `${a.reasons && a.reasons.length ? `<div class="reasons">⚠ ${a.reasons.map(escapeHtml).join("<br>")}</div>` : ""}
       <div class="confirm-row">
         <button class="${p.side === "LONG" ? "long" : "danger"} lockable" id="btn-approve" ${a.can_execute && !sending ? "" : "disabled"}>${sending ? "Sending…" : "Execute &#10003; Yes"}</button>
         <button class="ghost" id="btn-reject" ${sending ? "disabled" : ""}>Dismiss</button>
       </div>`;
  return `
    <h3>${escapeHtml(p.symbol)} ${sectorTag(p.sector)} ${sideBadge(p.side)}${executed ? ' <span class="badge good" data-term="executed">executed</span>' : ""}</h3>
    <div class="sub">${stratLabel(p.strategy)} · ${tfLabel(p.timeframe)} · conf ${num(p.confidence, 2)} · expected ${num((p.evidence || {}).expected_r, 2)}R · score ${num(p.score, 2)}${p.timeframe === "INTRADAY" ? ` · ${seenText(p)}` : ""} · session ${a.session}</div>
    ${p.autopilot && !executed ? `<div class="muted" style="margin:6px 0" data-term="autopilot">🤖 ${escapeHtml(apLine(p.autopilot))}</div>` : ""}
    ${recordBlock(p)}
    ${(a.noise || []).length && !executed ? `<div class="warn-box" data-term="noise">⚠ Probably noise right now: ${a.noise.map(escapeHtml).join(" · ")}. Autopilot won't take it; you still can.</div>` : ""}
    ${sparkSvg(p.evidence && p.evidence.spark, p)}
    <div class="explain">${escapeHtml(p.explanation || p.rationale)}</div>
    <div class="kv">
      <span>Market price</span><span data-price></span>
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
        ${op.caps && op.caps.length ? `<span>Size limited by</span><span>${escapeHtml(op.caps.join("; "))}</span>` : ""}
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
  const qc = ev.price_character, vf = ev.vol_forecast, mr = ev.market_regime, mm = ev.market_move, ne = ev.next_earnings;
  if (qc || vf || (mr && mr.p_turbulent != null) || ev.evidence_weight || mm || ne) {
    blocks.push(`<h4>Statistics</h4><div class="kv">` +
      (qc ? `<span>Price character</span><span>${escapeHtml(qc.character)} — Hurst ${num(qc.hurst, 2)}, variance ratio z ${num(qc.variance_ratio_z, 1)}${qc.half_life_bars ? `, half-life ${num(qc.half_life_bars, 0)} bars` : ""}</span>` : "") +
      (vf ? `<span>Tomorrow's volatility</span><span>${num(vf.vol * 100, 2)}% (${escapeHtml(vf.model)}, ${num(vf.ratio, 2)}× the last 60 days)</span>` : "") +
      (mr && mr.p_turbulent != null ? `<span>Market regime</span><span>${escapeHtml(mr.regime)} — P(turbulent) ${num(mr.p_turbulent, 2)}</span>` : "") +
      (ev.evidence_weight ? `<span>Evidence weight</span><span>×${num(ev.evidence_weight, 2)} from its replayed and real record</span>` : "") +
      (mm ? `<span>Move vs the market</span><span>${pct(mm.move_pct)} against ${pct(mm.expected_pct)} the market explains (β ${num(mm.beta, 2)}) - ${num(mm.z, 1)} usual moves of ${num(mm.usual_pct, 1)}%; news: ${mm.news == null ? "not being read" : mm.news ? `${mm.news} stor${mm.news === 1 ? "y" : "ies"}` : "none found"}</span>` : "") +
      (ne ? `<span>Next earnings</span><span>${escapeHtml(ne.date)} ${escapeHtml({ bmo: "before the open", amc: "after the close", dmh: "during the session" }[ne.hour] || "")} - ${ne.sessions === 0 ? "today" : ne.sessions === 1 ? "next session" : `in ${ne.sessions} sessions`}</span>` : "") +
      `</div>`);
  }
  const skip = new Set(["signal", "dcf", "football_field", "spark", "verdict", "peer_median", "target_multiples", "peers",
    "price_character", "vol_forecast", "market_regime", "evidence_weight", "at_entry", "hold_from_half_life",
    "market_move", "next_earnings", "as_confirmed", "last_look"]);
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

/* The panel drawn again from its last assessment, after a click the app refused - the button as it was. */
function restoreDetail(id) {
  if (S.selected === id && drawn && drawn.id === id) drawDetail(drawn.a);
}

async function approve(id) {
  if (pending.has(id)) return;                        // one click at a time
  const btn = $("#btn-approve"), dismiss = $("#btn-reject");
  if (btn) { btn.disabled = true; btn.textContent = "Sending…"; }
  if (dismiss) dismiss.disabled = true;
  setPending(id, "sending");                          // the row says so at once
  const symbol = (S.plays.find(x => x.id === id) || {}).symbol || "";
  const r = await post(`/api/plays/${id}/approve`);
  pending.delete(id);
  if (r.ok) {
    const where = r.order_session === "EXTENDED" ? " (extended-hours limit)" : "";
    // the shares sent: the last look re-sizes an entry it re-prices off the quote, so they can differ from the preview
    const shares = r.qty ? `${num(r.qty, 0)} shares, ` : "";
    toast(`Order sent for ${symbol}: ${shares}${r.order_type || ""} ${r.status || "ok"}${where}`, "good");
    mergePlay({ id, status: r.status === "FILLED" ? "FILLED" : "SUBMITTED", trade_id: r.trade_id || null });
    if (S.selected === id) selectPlay(id);            // another play picked meanwhile stays shown
    loadOpen();
  } else if (r.sent_unknown) {
    // IBKR didn't answer in time: the order may be working, and the app looks for it - the play counts as sent
    toast(`Order for ${symbol} not confirmed: ` + (r.reason || "no answer from the broker in time"), "warn");
    mergePlay({ id, status: "SUBMITTED", trade_id: null });
    if (S.selected === id) selectPlay(id);
    loadOpen();
  } else {
    toast("Not sent: " + (r.reason || "rejected"), "bad");
    if (r.already_executed) {
      mergePlay({ id, status: "FILLED", trade_id: r.trade_id || null });
      if (S.selected === id) selectPlay(id);
    } else { renderPlays(); restoreDetail(id); }      // the row and the panel back as they were
  }
}

async function reject(id) {
  if (pending.has(id)) return;
  setPending(id, "dismissing");                       // the row goes at once...
  hideDetail();
  const r = await post(`/api/plays/${id}/reject`);
  pending.delete(id);
  if (r && r.ok === false) {
    toast("Not dismissed: " + (r.reason || "refused"), "bad");
    renderPlays();                                    // ...and comes back as it was, with its panel
    restoreDetail(id);
    return;
  }
  mergePlay({ id, status: "REJECTED" });              // as the app's board now has it
}

export function initPlays() {
  // one set of listeners for every row, whichever rows a push redraws
  const body = $("#plays-body");
  body.addEventListener("click", e => {
    const p = rowPlay(e.target);
    if (!p) return;
    if (e.target.closest(".chart-btn")) openChart(p);
    else if (e.target.closest(".order-mark")) showTab("orders");
    else if (e.target.closest(".sig-mark")) openSignals(p.symbol);
    else selectPlay(p.id);
  });
  body.addEventListener("mousemove", e => {
    const p = rowPlay(e.target);
    if (!p || e.target.closest("[data-term]")) { hovered = null; return; }    // the term's own explanation is showing
    hoverRow(p, e.clientX, e.clientY);
  });
  body.addEventListener("mouseleave", () => { hovered = null; clearTimeout(hoverTimer); hideTip(); });
  on("plays", renderPlays);
  on("filters", renderPlays);
  on("strategies", renderPlays);
  on("orders", renderPlays);
}
