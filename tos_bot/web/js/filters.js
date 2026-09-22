/* Filters: what the bot scans for and may trade (saved on the server), plus the
   per-browser "hide executed" view option. */
import { $, $$, SECTOR_SHORT, api, escapeHtml, money, post, store } from "./util.js";
import { S, emit, on } from "./state.js";
import { openModal, toast } from "./ui.js";
import { splitSlots } from "./autopilot.js";

const BOXES = {
  "f-long": ["sides", "LONG"], "f-short": ["sides", "SHORT"],
  "f-intraday": ["timeframes", "INTRADAY"], "f-swing": ["timeframes", "SWING"],
};

// boxes whose change is on its way to the app: a snapshot arriving meanwhile still carries the old filters, and
// mustn't flick the box back before the answer - which sets it, or puts it back on a refusal
const sending = new Set();

function syncControls() {
  const f = S.state.filters;
  if (f) for (const [id, [group, value]] of Object.entries(BOXES)) {
    if (!sending.has(id)) $("#" + id).checked = (f[group] || []).includes(value);
  }
  renderSectorsButton();
}

async function changeFilter(box) {
  const [group] = BOXES[box.id];
  const picked = Object.entries(BOXES)
    .filter(([id, [g]]) => g === group && $("#" + id).checked).map(([, [, value]]) => value);
  if (!picked.length) {
    box.checked = true;
    toast(group === "sides" ? "Keep Long or Short switched on" : "Keep Intraday or Swing switched on", "warn");
    return;
  }
  sending.add(box.id);
  box.disabled = true;                                 // one change at a time
  const r = await post("/api/filters", { [group]: picked });
  sending.delete(box.id);
  box.disabled = false;
  if (!r.ok) { syncControls(); toast("Filter not changed: " + (r.reason || ""), "bad"); return; }
  S.state.filters = r.filters;
  emit("filters");
  toast(r.note + (r.rescanning ? " Rescanning for the new plays…" : ""), "good");
}

/* ---------- Pairs: whether Autopilot may enter pair trades (its PAIRS trade type) ---------- */
function syncPairs() {
  $("#f-pairs").checked = ((S.state.autopilot || {}).trade_types || []).includes("PAIRS");
}

async function changePairs(box) {
  // Autopilot's own day / swing boxes stay as they are: only PAIRS comes or goes. (It used to send day and
  // swing along, which silently ticked a kind the owner had unticked in Autopilot's settings.)
  const ap = S.state.autopilot || {};
  const own = (ap.own_trade_types || ap.trade_types || []).filter(t => t !== "PAIRS");
  const types = [...own, ...(box.checked ? ["PAIRS"] : [])];
  if (!types.length) { toast("Autopilot would take nothing at all - tick day or swing trades in its settings (⚙) first.", "bad"); syncPairs(); return; }
  const r = await post("/api/autopilot", { trade_types: types });
  if (!r.ok || !r.autopilot) { toast("Pairs not changed: " + (r.reason || "update failed"), "bad"); syncPairs(); return; }
  S.state.autopilot = r.autopilot;
  emit("autopilot");
  toast(box.checked ? "Autopilot may enter pair trades once the replay has proven them." : "Autopilot won't enter pair trades.", "good");
}

/* ---------- the day/swing split of the trading capital: only while both kinds are on ---------- */
let splitDragging = false;

function renderSplit() {
  const kinds = (S.state.filters || {}).timeframes || [], sp = (S.state.capital || {}).split;
  const both = kinds.includes("INTRADAY") && kinds.includes("SWING");
  $("#split-ctl").classList.toggle("hidden", !both || !sp);
  const warn = $("#split-warn"), notes = both && sp ? splitSlots() : { text: "", warnings: [] };
  warn.classList.toggle("hidden", !notes.warnings.length);
  warn.textContent = notes.warnings.length ? `⚠ ${notes.text}` : "";
  warn.title = notes.warnings.join("\n\n");
  if (!both || !sp || splitDragging) return;
  const pct = sp.set_pct ?? sp.day_pct;
  $("#split-range").value = pct;
  labelSplit(pct);
}

function labelSplit(pct) {
  const c = S.state.capital || {};
  $("#split-label").textContent = `${pct}% / ${100 - pct}%`;
  $("#split-range").title = `Day trades up to ${money(c.effective * pct / 100, c.currency)}, swing trades up to ${money(c.effective * (100 - pct) / 100, c.currency)}`;
}

async function saveSplit() {
  splitDragging = false;
  const r = await post("/api/capital/split", { day_pct: parseFloat($("#split-range").value) });
  if (!r.ok) { toast("Split not changed: " + (r.reason || ""), "bad"); renderSplit(); return; }
  S.state.capital = r.capital;
  emit("capital", r.capital);
  toast(r.note, "good");
}

function renderSectorsButton() {
  const sel = (S.state.filters || {}).sectors || [];
  const b = $("#btn-sectors");
  b.textContent = !sel.length ? "Sectors: all" : sel.length === 1 ? `Sector: ${SECTOR_SHORT[sel[0]] || sel[0]}` : `Sectors: ${sel.length}`;
  b.classList.toggle("active-filter", sel.length > 0);
  b.title = sel.length ? "Only scanning and trading: " + sel.join(", ") : "Scanning and trading every sector — click to narrow it down";
}

async function pickSectors() {
  let d;
  try { d = await api("/api/filters"); } catch (e) { toast(`Couldn't read the sectors - ${e.message}`, "bad"); return; }
  const all = d.all_sectors || [], selected = (d.filters || {}).sectors || [];
  const checked = new Set(selected.length ? selected : all);
  openModal({
    title: "Sectors to scan and trade",
    bodyHTML: `<p class="muted">The scanner skips everything else, and plays outside these sectors can't be executed — by you or by Autopilot.</p>
      <div class="sector-grid">${all.map(s =>
        `<label><input type="checkbox" value="${escapeHtml(s)}" ${checked.has(s) ? "checked" : ""}> ${escapeHtml(s)}</label>`).join("")}</div>
      <div class="row-gap"><button class="ghost mini" id="sec-all">Select all</button><button class="ghost mini" id="sec-none">Clear</button></div>`,
    okText: "Apply", okClass: "long",
    onOk: async () => {
      const picked = $$(".sector-grid input:checked").map(i => i.value);
      if (!picked.length) { toast("Pick at least one sector", "bad"); return; }
      const r = await post("/api/filters", { sectors: picked });
      if (!r.ok) { toast("Couldn't save: " + (r.reason || ""), "bad"); return; }
      S.state.filters = r.filters;
      emit("filters");
      toast(r.note + (r.rescanning ? " Rescanning…" : ""), "good");
    },
  });
  $("#sec-all").onclick = () => $$(".sector-grid input").forEach(i => { i.checked = true; });
  $("#sec-none").onclick = () => $$(".sector-grid input").forEach(i => { i.checked = false; });
}

export const hideExecuted = () => $("#f-hide-done").checked;
export const hideNoisy = () => $("#f-hide-noisy").checked;

// per-browser view options: checkbox id -> [storage key, on by default]
const VIEW_OPTIONS = { "f-hide-done": ["atb-hide-done", false], "f-hide-noisy": ["atb-hide-noisy", true] };

export function initFilters() {
  on("state", syncControls);
  on("filters", syncControls);
  on("state", renderSplit);
  on("state", syncPairs);
  on("autopilot", syncPairs);
  on("autopilot", renderSplit);
  $("#f-pairs").onchange = e => changePairs(e.target);
  on("filters", renderSplit);
  on("capital", renderSplit);
  $("#split-range").oninput = e => { splitDragging = true; labelSplit(parseFloat(e.target.value)); };
  $("#split-range").onchange = saveSplit;
  Object.keys(BOXES).forEach(id => { $("#" + id).onchange = e => changeFilter(e.target); });
  $("#btn-sectors").onclick = pickSectors;
  for (const [id, [key, byDefault]] of Object.entries(VIEW_OPTIONS)) {
    const saved = store.get(key);
    $("#" + id).checked = saved == null ? byDefault : saved === "1";
    $("#" + id).onchange = e => { store.set(key, e.target.checked ? "1" : "0"); emit("plays"); };
  }
  window.addEventListener("storage", e => {
    const id = Object.keys(VIEW_OPTIONS).find(k => VIEW_OPTIONS[k][0] === e.key);
    if (id) { $("#" + id).checked = e.newValue === "1"; emit("plays"); }
  });
}
