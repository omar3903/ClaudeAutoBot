/* Helpers shared by every panel: DOM lookup, requests, formatting, labels. */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const LOCAL_HEADER = { "X-ATB-Request": "1" };      // the server's same-machine guard expects it

/** The server's answer - or an Error saying why there isn't one: the server's own detail or reason when it
    refused (e.status is the HTTP status, 404 for something gone, and e.body what it sent), or "the app isn't
    reachable" when nothing answered (e.status 0). A refusal used to come back as if it were the answer. */
export async function api(path, opt) {
  let r, body;
  try { r = await fetch(path, opt); } catch { throw Object.assign(new Error("the app isn't reachable"), { status: 0 }); }
  try { body = await r.json(); } catch { body = undefined; }         // not JSON: a crash page
  if (r.ok && body !== undefined) return body;
  const why = body && (typeof body.detail === "string" ? body.detail : body.reason);
  throw Object.assign(new Error(why || (r.ok ? "the app's answer couldn't be read" : `the app answered with an error (${r.status})`)),
    { status: r.status, body });
}
export const getLocal = path => api(path, { headers: LOCAL_HEADER });
/* A POST's refusal keeps the server's own answer ({ok: false, reason, ...}) - callers read its fields. */
export const post = (path, body = {}) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json", ...LOCAL_HEADER },
  body: JSON.stringify(body),
}).catch(e => ({ ok: false, reason: e.message, ...(e.body && typeof e.body === "object" ? e.body : {}) }));

/** A panel that couldn't be refreshed keeps what it showed, under a line saying so and when that came in -
    so a table isn't taken for current while the app is away. `since` is when the panel last had a good
    answer (null: never). Its next good render replaces the line along with the rest. */
export function markStale(el, since, e) {
  let line = el.querySelector(":scope > .stale-note");
  if (!line) {
    line = document.createElement("div");
    line.className = "stale-note";
    el.prepend(line);
  }
  line.textContent = (since ? `Couldn't refresh - showing data from ${fmtClock(since.toISOString())}` : "Couldn't load this")
    + ` (${e.message})`;
}

/* ---------- formatting ---------- */
const missing = v => v == null || isNaN(v);
export const usd = v => missing(v) ? "–" :
  (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
export const num = (v, d = 2) => missing(v) ? "–" : Number(v).toFixed(d);
export const pct = v => missing(v) ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(1) + "%";
export const count = v => missing(v) ? "–" : Number(v).toLocaleString();

/** An amount in a given currency, in the viewer's locale. */
export function money(v, ccy = "USD") {
  if (missing(v)) return "–";
  const digits = Math.abs(v) >= 1000 ? 0 : 2;
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency", currency: ccy || "USD", minimumFractionDigits: 0, maximumFractionDigits: digits,
    }).format(v);
  } catch { return `${Number(v).toLocaleString(undefined, { maximumFractionDigits: digits })} ${ccy}`; }
}

export const escapeHtml = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const shorten = (s, n) => s && s.length > n ? s.slice(0, n - 1) + "…" : s;
export const pretty = key => (key || "").replace(/_/g, " ");
export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

export const parseDate = s => new Date(/[zZ]|[+-]\d\d:\d\d$/.test(s) ? s : s + "Z");     // the database stores UTC
export function fmtTime(s) {
  if (!s) return "–";
  const d = parseDate(s);
  return isNaN(d) ? s : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
/** A day, short: "Tue, Sep 22". */
export function fmtDay(s) {
  if (!s) return "–";
  const d = parseDate(s);
  return isNaN(d) ? s : d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}
export function fmtClock(s) {
  if (!s) return "–";
  const d = parseDate(s);
  return isNaN(d) ? s : d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
/** A market time, shown in New York time like the scan settings. */
export function fmtEt(s) {
  if (!s) return "–";
  const d = parseDate(s);
  return isNaN(d) ? s : d.toLocaleString(undefined,
    { weekday: "short", hour: "2-digit", minute: "2-digit", timeZone: "America/New_York" }) + " ET";
}

/* ---------- small per-browser preferences ---------- */
export const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

/* ---------- labels ---------- */
export const SECTOR_SHORT = {
  "Technology": "Tech", "Communication Services": "Comm", "Consumer Discretionary": "Cons Disc",
  "Consumer Staples": "Cons Stpl", "Healthcare": "Health", "Financials": "Financials",
  "Industrials": "Industr", "Energy": "Energy", "Utilities": "Utilities",
  "Materials": "Materials", "Real Estate": "Real Est",
};
export const VENUE_SHORT = { "paper": "simulator", "ibkr-paper": "IBKR paper", "ibkr-live": "IBKR live" };

export const sectorTag = sec => sec
  ? `<span class="sector" data-term="sector" data-sector="${escapeHtml(sec)}">${escapeHtml(SECTOR_SHORT[sec] || sec)}</span>` : "";
export const sideBadge = side =>
  `<span class="side ${side}" data-term="${side === "SHORT" ? "short" : "long"}">${side}</span>`;
export const tfLabel = tf =>
  `<span data-term="${tf === "INTRADAY" ? "intraday" : "swing"}">${tf === "INTRADAY" ? "day" : "swing"}</span>`;

export function positionList(list) {
  return `<ul class="pos-list">${(list || []).map(t =>
    `<li>${escapeHtml(t.symbol)} — ${t.side === "SHORT" ? "short" : "long"} ${num(Math.abs(t.quantity), 0)} @ ${num(t.entry_price)}</li>`).join("")}</ul>`;
}
