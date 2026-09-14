/* Helpers shared by every panel: DOM lookup, requests, formatting, labels. */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const LOCAL_HEADER = { "X-ATB-Request": "1" };      // the server's same-machine guard expects it
export const api = (path, opt) => fetch(path, opt).then(r => r.json());
export const getLocal = path => api(path, { headers: LOCAL_HEADER });
export const post = (path, body = {}) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json", ...LOCAL_HEADER },
  body: JSON.stringify(body),
}).catch(() => ({ ok: false, reason: "the app isn't reachable" }));

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

const parseDate = s => new Date(/[zZ]|[+-]\d\d:\d\d$/.test(s) ? s : s + "Z");     // the database stores UTC
export function fmtTime(s) {
  if (!s) return "–";
  const d = parseDate(s);
  return isNaN(d) ? s : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
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
