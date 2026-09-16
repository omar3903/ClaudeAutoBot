/* The Watchlist tab: today's hot list, the sector buffers behind it, and what
   each cycle decided about the buffer names it looked at. */
import { $, SECTOR_SHORT, api, count, escapeHtml, fmtClock, num, sectorTag } from "./util.js";
import { S } from "./state.js";

const ACTION_CLASS = { adopted: "good", kept: "warn", dropped: "" };

export async function loadWatchlist() {
  if (S.stopped) return;
  let d;
  try { d = await api("/api/watchlist"); } catch { return; }
  renderWatchlist(d.watchlist, d.scan);
}

const heatBar = h => h == null ? '<span class="muted">–</span>'
  : `<span class="heat"><i style="width:${Math.min(100, Math.max(0, h) * 100)}%"></i></span> ${num(h, 2)}`;

export function renderWatchlist(wl, scan) {
  const el = $("#tab-watchlist");
  if (!wl) {
    const at = scan && scan.settings ? ` at ${escapeHtml(scan.settings.premarket_time)} ET` : "";
    el.innerHTML = `<p class="muted pad">No watchlist yet. The full scan builds it before the open${at} — or use
      Settings → Run full scan now once IB Gateway is connected.</p>`;
    return;
  }
  const gapCell = g => g == null ? '<span class="muted">–</span>'
    : `<span class="${g >= 0 ? "pl-pos" : "pl-neg"}">${g >= 0 ? "+" : ""}${num(g, 1)}%</span>`;
  const hot = wl.hot.map(c => `<tr><td class="sym">${escapeHtml(c.symbol)} ${sectorTag(c.sector)}</td>
    <td>${heatBar(c.daily_heat)}</td><td>${gapCell(c.gap_pct)}</td><td>${heatBar(c.heat)}</td></tr>`).join("");
  const sectors = wl.sectors.map(s => `<tr><td>${escapeHtml(s.sector)}</td>
    <td>${s.kept.map(c => `<span class="chip" title="intraday heat ${num(c.heat, 2)}">${escapeHtml(c.symbol)}</span>`).join(" ") || '<span class="muted">–</span>'}</td>
    <td class="num">${s.searched}</td><td class="num">${s.queued}</td></tr>`).join("");
  const decisions = wl.decisions.slice(0, 40).map(d => `<tr><td>${fmtClock(d.at)}</td>
    <td class="sym">${escapeHtml(d.symbol)}</td><td>${escapeHtml(SECTOR_SHORT[d.sector] || d.sector)}</td>
    <td><span class="badge ${ACTION_CLASS[d.action] || ""}">${escapeHtml(d.action)}</span>${d.replaced ? ` <span class="muted">for ${escapeHtml(d.replaced)}</span>` : ""}${d.note ? ` <span class="muted">· ${escapeHtml(d.note)}</span>` : ""}</td>
    <td class="num">${num(d.heat, 2)}</td></tr>`).join("");

  el.innerHTML = `
    <div class="wl-head">For the <b>${escapeHtml(wl.session)}</b> session · built ${fmtClock(wl.built_at)} from
      ${count(wl.universe)} listed stocks (${count(wl.liquid)} liquid), daily candles through ${escapeHtml(wl.bars_through)}</div>
    <div class="wl-grid">
      <div><h4 data-term="hot">Hot list (${wl.hot.length})</h4>
        <table><thead><tr><th>Symbol</th><th data-term="heat">Daily heat</th><th data-term="gap">Pre-mkt gap</th><th data-term="heat">Intraday heat</th></tr></thead><tbody>${hot}</tbody></table></div>
      <div><h4 data-term="buffer">Sector buffers</h4>
        <table><thead><tr><th>Sector</th><th>Kept</th><th class="num">Looked at</th><th class="num">Queued</th></tr></thead><tbody>${sectors}</tbody></table></div>
      <div><h4 data-term="decision">Buffer decisions</h4>${decisions
        ? `<table><thead><tr><th>Time</th><th>Symbol</th><th>Sector</th><th>Decision</th><th class="num">Heat</th></tr></thead><tbody>${decisions}</tbody></table>`
        : '<p class="muted">None yet — the first cycle of the session makes them.</p>'}</div>
    </div>`;
}
