/* The Signals page, opened from the top bar (signals/ on the server): where the signals come from and
   whether each source is working, the stocks with unusual insider trading or news and what that does
   to their plays, every insider filing with how long after the trade it was filed, and the latest
   headlines with the tone FinBERT reads in them. */
import { $, $$, api, escapeHtml, fmtEt, num, post, usd } from "./util.js";
import { toastResult } from "./ui.js";
import { openConnections } from "./connections.js";
import { openStrategies } from "./strategies.js";
import { sheet, sheetOpen } from "./sheet.js";

let view = "stocks";
let open = null;                 // the stock whose story is showing

const KIND = { news: ["News", "accent"], analyst: ["Analyst", "accent"], filing: ["SEC filing", "warn"] };
const ROLE = { ceo_cfo: "top executive", officer: "officer", director: "director", ten_percent_owner: "10% owner", other: "insider" };
const signed = (v, d = 2) => v == null ? "–" : `${v > 0 ? "+" : ""}${num(v, d)}`;
const toneCls = v => v == null ? "" : v > 0.15 ? "gain" : v < -0.15 ? "loss" : "";
const safeUrl = u => /^https:\/\//.test(u || "") ? u : "";

export function initSignals() { $("#btn-signals").onclick = () => openSignals(); }

export function openSignals(symbol) {
  sheet("signals", "Signals").classList.remove("hidden");
  if (symbol) { view = "stocks"; open = symbol; }
  load();
}

/** The server finished a pass over the filings or the news. */
export function signalsUpdated() { if (sheetOpen("signals")) load(); }

function ago(iso) {
  if (!iso) return "not yet";
  const min = Math.round((Date.now() - Date.parse(iso)) / 60000);
  return min < 1 ? "just now" : min < 60 ? `${min} min ago` : min < 1440 ? `${Math.round(min / 60)} h ago` : fmtEt(iso);
}

async function load() {
  const body = $("#signals-body");
  let d;
  try { d = await api("/api/signals"); } catch { body.innerHTML = `<div class="empty">Couldn't load the signals.</div>`; return; }
  if (!sheetOpen("signals")) return;
  $("#signals-sub").textContent = "Insider trades and company news - they nudge the scores of plays on the same stocks";
  $("#signals-tools").innerHTML = `<button class="mini" id="signals-check" ${d.enabled && !d.checking ? "" : "disabled"}
    title="Read SEC's latest filings and the news now instead of waiting for the next pass">${d.checking ? "Checking…" : "Check now"}</button>`;
  $("#signals-check").onclick = async e => {
    e.currentTarget.disabled = true;
    toastResult(await post("/api/signals/check"));
    setTimeout(load, 1500);
  };
  const lists = { stocks: d.signals, filings: d.filings, news: d.headlines };
  body.innerHTML = `${sourcesHTML(d)}
    <div class="movers-head">
      <div class="seg-group">${[["stocks", "Stocks with signals"], ["filings", "Insider filings"], ["news", "Headlines"]].map(([k, label]) =>
        `<button class="seg mini ${view === k ? "active" : ""}" data-view="${k}">${label} · ${lists[k].length}</button>`).join("")}</div>
    </div>
    <div id="signals-view">${viewHTML(d)}</div>`;
  $$("[data-view]", body).forEach(b => { b.onclick = () => { view = b.dataset.view; load(); }; });
  $$("[data-open]", body).forEach(row => {
    row.onclick = e => { if (!e.target.closest("button, a")) openStock(row.dataset.open); };
    row.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openStock(row.dataset.open); } };
  });
  $$("[data-go]", body).forEach(b => { b.onclick = () => (b.dataset.go === "connections" ? openConnections() : openStrategies()); });
  if (open && view === "stocks") openStock(open, true);
}

function sourcesHTML(d) {
  const r = d.report || {}, ins = r.insiders || {}, news = r.news || {}, sent = d.sentiment || {}, lag = d.filing_delays;
  const card = (label, value, note, cls = "") => `<div><label>${label}</label><b class="${cls}">${value}</b><span class="muted small">${note}</span></div>`;
  const finbert = !sent.installed ? card("FinBERT", "Not installed", "headlines carry no tone", "loss")
    : sent.error ? card("FinBERT", "Couldn't load", escapeHtml(sent.error), "loss")
      : card("FinBERT", sent.loaded ? "Scoring" : "Ready", `${sent.scored} of ${sent.headlines} headlines scored (${d.days} days)`, "gain");
  const strategies = Object.entries(d.strategies || {}).map(([, s]) => `${escapeHtml(s.title)}: <b class="${s.enabled ? "gain" : ""}">${s.enabled ? "on" : "off"}</b>`).join(" · ");
  const errors = Object.entries(r.errors || {});
  return `${d.enabled ? "" : `<p class="reasons">The signals are off (signals.enabled in config.yaml) - nothing is read and no play is nudged.</p>`}
    <div class="journal-cards signal-sources">
      ${card("SEC insider filings", ago(ins.checked_at), `${ins.filings_read ?? 0} read last pass · every ${num(d.settings.insider_poll_minutes, 0)} min`)}
      ${card("News", ago(news.checked_at), `${news.watched ?? 0} stocks followed · ${news.new_stories ?? 0} new stories · every ${num(d.settings.news_poll_minutes, 0)} min`)}
      ${card("IBKR news feeds", d.ibkr_news ? "Connected" : "Not connected", d.ibkr_news ? "Briefing.com and analyst actions" : "they come back with IB Gateway", d.ibkr_news ? "gain" : "loss")}
      ${d.finnhub ? card("Finnhub", "Key saved", "company news from many sources", "gain")
        : `<div><label>Finnhub</label><b class="loss">No key</b><span class="muted small"><button class="link" data-go="connections">Add it in Connections</button></span></div>`}
      ${finbert}
      ${!d.finnhub ? card("Earnings calendar", "Needs the Finnhub key", "reports ahead of time, with estimates", "loss")
        : card("Earnings calendar", d.calendar.fetched_at ? ago(d.calendar.fetched_at) : "not read yet",
          `${d.calendar.reports.toLocaleString()} reports kept · every ${num(d.calendar.hours, 0)} h`)}
      ${lag ? card("Insiders filed", `${num(lag.median_days, lag.median_days % 1 ? 1 : 0)} business day${lag.median_days === 1 ? "" : "s"}`, `median after the trade · ${Math.round(lag.on_time * 100)}% within SEC's 2 days`) : ""}
    </div>
    ${errors.length ? `<ul class="warnings">${errors.map(([what, e]) => `<li>The last ${escapeHtml(what)} check failed: ${escapeHtml(e)}</li>`).join("")}</ul>` : ""}
    <p class="muted small">How they count: unusual insider buying adds up to ${signed(d.boosts.insider_buying)} to a long play's score and
      takes as much from a short; unusual selling takes up to ${num(d.boosts.insider_selling, 2)} from a long; the news moves a play by up to
      ${num(d.boosts.news, 2)} either way once ${d.boosts.min_headlines}+ headlines are scored. ${strategies}
      <button class="link" data-go="strategies">Strategies</button></p>`;
}

function viewHTML(d) {
  if (view === "filings") return filingsHTML(d.filings, d.filing_delays, d.days, true);
  if (view === "news") return headlinesHTML(d.headlines, true);
  if (!d.signals.length) return `<p class="muted">No stock has a signal right now. Unusual insider trading shows up here as SEC publishes the filings;
    news for the stocks held and on the hot list once it's read.</p>`;
  return `<div class="table-scroll"><table class="ev-table signals-table">
    <thead><tr><th>Stock</th><th>Insider buying</th><th>Insider selling</th><th>News tone</th><th>Material 8-K</th>
      <th class="num">On a long play</th><th class="num">On a short play</th></tr></thead>
    <tbody>${d.signals.map(s => `<tr class="mover ${open === s.symbol ? "open" : ""}" data-open="${escapeHtml(s.symbol)}" tabindex="0">
      <td><b>${escapeHtml(s.symbol)}</b></td>
      <td>${insiderCell(s.buying)}</td><td>${insiderCell(s.selling)}</td>
      <td>${s.news ? `<b class="${toneCls(s.news.score)}">${signed(s.news.score)}</b> <span class="muted small">${s.news.headlines} headline${s.news.headlines === 1 ? "" : "s"}</span>` : `<span class="muted">–</span>`}</td>
      <td class="small">${s.filings.length ? escapeHtml(s.filings[0].headline.replace(/^8-K: /, "")) : `<span class="muted">–</span>`}</td>
      <td class="num ${toneCls(s.effect.LONG.delta * 10)}">${s.effect.LONG.why.length ? signed(s.effect.LONG.delta, 3) : "–"}</td>
      <td class="num ${toneCls(s.effect.SHORT.delta * 10)}">${s.effect.SHORT.why.length ? signed(s.effect.SHORT.delta, 3) : "–"}</td>
    </tr><tr class="mover-detail ${open === s.symbol ? "" : "hidden"}" data-story="${escapeHtml(s.symbol)}"><td colspan="7"><p class="muted">Loading…</p></td></tr>`).join("")}</tbody>
  </table></div>`;
}

function insiderCell(sig) {
  if (!sig) return `<span class="muted">–</span>`;
  return `<span class="badge ${sig.unusual ? (sig.direction === "buying" ? "good" : "bad") : "faint"}">${sig.unusual ? "unusual" : "usual"} ${num(sig.score, 2)}</span>
    <div class="small">${usd(sig.value)} · ${sig.insiders} insider${sig.insiders === 1 ? "" : "s"} · ${escapeHtml(ROLE[sig.top_role] || sig.top_role || "")}</div>`;
}

async function openStock(symbol, keep = false) {
  const row = $(`[data-story="${CSS.escape(symbol)}"]`);
  if (!row) return;
  if (!keep && open === symbol && !row.classList.contains("hidden")) {
    row.classList.add("hidden");
    $(`[data-open="${CSS.escape(symbol)}"]`).classList.remove("open");
    open = null;
    return;
  }
  open = symbol;
  $$("[data-story]").forEach(r => r.classList.toggle("hidden", r !== row));
  $$("[data-open]").forEach(r => r.classList.toggle("open", r.dataset.open === symbol));
  let d;
  try { d = await api(`/api/signals/stock/${encodeURIComponent(symbol)}`); } catch { d = null; }
  if (open !== symbol) return;
  const cell = $("td", row);
  if (!d) { cell.innerHTML = `<p class="reasons">Couldn't load ${escapeHtml(symbol)}.</p>`; return; }
  const s = d.signals || {};
  const reasons = [s.buying, s.selling].filter(Boolean).map(sig => `<div><h5>Insider ${sig.direction}</h5><ul>${sig.reasons.map(x => `<li>${escapeHtml(x)}</li>`).join("")}</ul>
      <p class="muted small">${sig.trades} trade${sig.trades === 1 ? "" : "s"} from ${escapeHtml(sig.first_date)} to ${escapeHtml(sig.last_date)} at ${num(sig.avg_price)} on average</p></div>`).join("");
  const effect = ["LONG", "SHORT"].map(side => (s.effect || {})[side]).some(e => e && e.why.length)
    ? `<div class="wide"><h5>What it does to a play</h5><p>Long: ${escapeHtml((s.effect.LONG.why || []).join("; ") || "nothing")} · Short: ${escapeHtml((s.effect.SHORT.why || []).join("; ") || "nothing")}</p></div>` : "";
  const hour = e => ({ bmo: "before the open", amc: "after the close", dmh: "during the session" }[e.hour] || "");
  const earnings = (d.earnings || []).length ? `<div class="wide"><h5>Earnings</h5>${d.next_earnings
    ? `<p>Next report: <b>${escapeHtml(d.next_earnings.date)}</b> ${hour(d.next_earnings)}${d.next_earnings.eps_estimate != null ? ` · EPS estimate ${num(d.next_earnings.eps_estimate)}` : ""}</p>` : ""}
    <p class="muted small">${d.earnings.filter(e => e.eps_actual != null).map(e => `${escapeHtml(e.date)}: EPS ${num(e.eps_actual)} vs ${num(e.eps_estimate)} estimated${e.surprise != null ? ` (${e.surprise >= 0 ? "+" : ""}${Math.round(e.surprise * 100)}%)` : ""}`).join(" · ")}</p></div>` : "";
  cell.innerHTML = `<div class="mover-story">${reasons}${effect}${earnings}
    <div class="wide"><h5>Its insiders' filings over the past year</h5>${filingsHTML(d.filings, d.filing_delays, 365, false)}</div>
    <div class="wide"><h5>Its news, the last 30 days</h5>${headlinesHTML(d.headlines, false)}</div></div>`;
}

function filingsHTML(rows, lag, days, withStock) {
  if (!rows.length) return `<p class="muted">No insider filings in the last ${days} days.</p>`;
  const summary = lag ? `<p class="muted small">Insiders filed a median of ${num(lag.median_days, lag.median_days % 1 ? 1 : 0)} business day${lag.median_days === 1 ? "" : "s"} after trading
    (the longest ${lag.max_days}); ${Math.round(lag.on_time * 100)}% met SEC's two-business-day deadline. The app reads SEC's live feed
    of new filings every few minutes, so the wait is the insiders', not the app's.</p>` : "";
  return `${summary}<div class="table-scroll"><table class="ev-table">
    <thead><tr><th>Filed</th><th>Traded</th><th class="num">Days late</th>${withStock ? "<th>Stock</th>" : ""}<th>Insider</th><th></th>
      <th class="num">Shares</th><th class="num">Price</th><th class="num">Value</th><th>Notes</th></tr></thead>
    <tbody>${rows.map(f => {
      const late = businessDays(f.trade_date, f.filed);
      const notes = [f.planned ? "10b5-1 plan" : "", f.direct ? "" : "indirect", f.offering ? "offering" : ""].filter(Boolean).join(" · ");
      return `<tr><td>${escapeHtml(f.filed || "–")}</td><td>${escapeHtml(f.trade_date)}</td>
        <td class="num ${late > 2 ? "loss" : ""}">${late ?? "–"}</td>
        ${withStock ? `<td><button class="link" data-open-stock="${escapeHtml(f.symbol)}">${escapeHtml(f.symbol)}</button></td>` : ""}
        <td>${escapeHtml(f.owner_name)} <span class="muted small">${escapeHtml(f.title || ROLE[f.role] || f.role || "")}</span></td>
        <td><span class="badge ${f.code === "P" ? "good" : "bad"}">${f.code === "P" ? "buy" : "sell"}</span></td>
        <td class="num">${Math.round(f.shares).toLocaleString()}</td><td class="num">${num(f.price)}</td><td class="num">${usd(f.value)}</td>
        <td class="muted small">${escapeHtml(notes)}</td></tr>`;
    }).join("")}</tbody></table></div>`;
}

function headlinesHTML(rows, withStock) {
  if (!rows.length) return `<p class="muted">No headlines yet.</p>`;
  return `<ul class="stories">${rows.map(h => {
    const [label, cls] = KIND[h.kind] || [h.kind, ""], url = safeUrl(h.url);
    return `<li><span class="muted small">${escapeHtml(fmtEt(h.published_at))}</span>
      ${withStock ? `<button class="link" data-open-stock="${escapeHtml(h.symbol)}"><b>${escapeHtml(h.symbol)}</b></button>` : ""}
      <span class="badge ${cls}">${label}</span>
      ${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(h.headline)}</a>` : escapeHtml(h.headline)}
      <span class="muted small">${escapeHtml(h.provider || h.source)}</span>
      ${h.sentiment != null ? `<b class="small ${toneCls(h.sentiment)}" title="FinBERT: -1 negative to +1 positive">tone ${signed(h.sentiment)}</b>` : ""}</li>`;
  }).join("")}</ul>`;
}

function businessDays(from, to) {
  if (!from || !to) return null;
  let n = 0;
  for (let d = new Date(`${from}T12:00:00Z`), end = new Date(`${to}T12:00:00Z`); d < end; d.setUTCDate(d.getUTCDate() + 1)) {
    if (d.getUTCDay() % 6) n++;
  }
  return n;
}

document.addEventListener("click", e => {
  const go = e.target.closest("[data-open-stock]");
  if (go && sheetOpen("signals")) { view = "stocks"; open = go.dataset.openStock; load(); }
});
