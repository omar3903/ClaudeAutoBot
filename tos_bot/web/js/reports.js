/* The Reports page, opened from the top bar: one report per session. It starts with the market's
   biggest movers and what the bot made of them (movers.js), then the journal's review of the bot's
   own trading (journal.js). The dot on the button marks a report you haven't opened yet. */
import { $, $$, api, count, escapeHtml, post, store, usd } from "./util.js";
import { toastResult } from "./ui.js";
import { inR, journalHTML, pctOf, tone } from "./journal.js";
import { bindMovers, moversHTML } from "./movers.js";
import { sheet, sheetOpen } from "./sheet.js";

const SEEN = "reports.seen";
let selected = null;

export function initReports() {
  $("#btn-reports").onclick = () => openReports();
  refreshReportsDot();
}

export const reportsOpen = () => sheetOpen("reports");

const stamp = d => d ? `${d.session}|${d.created_at || ""}` : "";

/** Show the dot when the newest report (or its latest rebuild) hasn't been opened. */
export async function refreshReportsDot() {
  let state;
  try { state = await api("/api/journal?limit=1"); } catch { return; }
  const latest = (state.days || [])[0];
  $("#reports-dot").classList.toggle("hidden", !latest || stamp(latest) === store.get(SEEN));
}

/** Called when the server says a report was written or gained its movers. */
export function reportsUpdated() {
  if (reportsOpen()) loadDays();
  else refreshReportsDot();
}

export function openReports(day) {
  sheet("reports", "Reports", { side: true }).classList.remove("hidden");
  if (day) selected = day;
  loadDays();
}

async function loadDays() {
  let state;
  try { state = await api("/api/journal"); } catch { $("#reports-body").innerHTML = `<div class="empty">Couldn't load the reports.</div>`; return; }
  const days = state.days || [];
  if (!days.some(d => d.session === selected)) selected = days.length ? days[0].session : null;
  $("#reports-sub").textContent = state.enabled
    ? `Written after every session at ${state.review_at} ET, with the market's movers once every stock's candles are in`
    : "The daily report is off (journal.enabled in config.yaml)";
  $("#reports-side").innerHTML = `
    <button class="mini lockable" id="reports-build" title="Build the report on the last session now (it's rebuilt if it exists)">Rebuild the last session</button>
    ${days.length ? days.map(d => `<button class="journal-day ${d.session === selected ? "active" : ""}" data-day="${escapeHtml(d.session)}">
        <b>${escapeHtml(longDate(d.session, true))}</b>
        ${d.opened && !d.trades
          ? `<span class="${tone(d.open_r)}" title="Nothing closed this session; the positions opened stood here on the close">${d.opened} opened, still open · standing ${inR(d.open_r)}</span>`
          : `<span class="${tone(d.total_r)}">${d.opened ? `${d.opened} opened · ` : ""}${d.trades} closed · ${inR(d.total_r)} · ${usd(d.realized_pl)}</span>`}
        ${d.mistakes ? `<span class="badge warn">${d.mistakes} to learn from</span>` : ""}
      </button>`).join("") : `<div class="empty">No reports yet. The first is written after the next close.</div>`}`;
  $$("#reports-side [data-day]").forEach(b => { b.onclick = () => { selected = b.dataset.day; loadDays(); }; });
  $("#reports-build").onclick = async () => {
    const r = await post("/api/journal/review", {});
    toastResult(r);
    if (r.ok && r.review) { selected = r.review.session; loadDays(); }
  };
  if (!selected) { $("#reports-body").innerHTML = ""; return; }
  showReport(selected, days[0]);
}

async function showReport(day, latest) {
  const body = $("#reports-body");
  if (body.dataset.day !== day) body.innerHTML = `<p class="muted">Loading…</p>`;
  let r;
  try { r = await api(`/api/journal/${encodeURIComponent(day)}`); } catch { r = null; }
  if (selected !== day || !reportsOpen()) return;
  if (!r || !r.session) { body.innerHTML = `<div class="empty">Couldn't load the report.</div>`; return; }
  body.dataset.day = day;
  body.innerHTML = headerHTML(r) + moversHTML(r.movers, r.capture) + journalHTML(r);
  bindMovers(body, r);
  if (latest && latest.session === day) {
    store.set(SEEN, stamp(latest));
    $("#reports-dot").classList.add("hidden");
  }
}

function headerHTML(r) {
  const d = r.day || {}, reg = r.regime, m = r.movers && r.movers.ok ? r.movers : null, s = m ? m.summary : null;
  return `<h4 class="report-title">${escapeHtml(longDate(r.session))}
      ${reg ? `<span class="badge ${reg.regime === "turbulent" ? "warn" : "good"}">market ${escapeHtml(reg.regime)}</span>` : ""}</h4>
    <div class="journal-cards">
      ${m && m.market_pct != null ? `<div><label>The market (SPY)</label><b class="${tone(m.market_pct)}">${m.market_pct > 0 ? "+" : ""}${m.market_pct.toFixed(2)}%</b></div>` : ""}
      ${d.opened ? `<div><label>Positions opened</label><b>${d.opened}</b></div>` : ""}
      ${d.still_open ? `<div title="Where the positions opened this session and still open stood at the review, on the session's close"><label>Still open, standing at</label><b class="${tone(d.open_r)}">${inR(d.open_r)} <span class="muted">${usd(d.open_pl)}</span></b></div>` : ""}
      <div><label>Closed trades</label><b>${d.trades || 0}</b></div>
      <div><label>Winners</label><b>${pctOf(d.win_rate)}</b></div>
      <div><label>In all</label><b class="${tone(d.total_r)}">${inR(d.total_r)}</b></div>
      <div><label>Realized</label><b class="${tone(d.realized_pl)}">${usd(d.realized_pl)}</b></div>
      ${r.setups_offered == null
        ? `<div><label>Plays offered</label><b>${r.plays_offered || 0}</b></div>`
        : `<div title="${count(r.plays_offered)} play-log rows: a setup that leaves the board and comes back is logged again"><label>Setups offered</label><b>${count(r.setups_offered)}</b></div>`}
      ${s ? `<div><label>Top movers traded</label><b>${s.traded} of ${s.movers}</b></div>` : ""}
    </div>`;
}

function longDate(iso, short = false) {
  const d = new Date(`${iso}T12:00:00`);
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, short
    ? { weekday: "short", month: "short", day: "numeric" }
    : { weekday: "long", month: "long", day: "numeric", year: "numeric" });
}
