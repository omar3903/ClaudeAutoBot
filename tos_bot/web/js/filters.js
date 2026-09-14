/* Filters: what the bot scans for and may trade (saved on the server), plus the
   per-browser "hide executed" view option. */
import { $, $$, SECTOR_SHORT, api, escapeHtml, post, store } from "./util.js";
import { S, emit, on } from "./state.js";
import { openModal, toast } from "./ui.js";

const BOXES = {
  "f-long": ["sides", "LONG"], "f-short": ["sides", "SHORT"],
  "f-intraday": ["timeframes", "INTRADAY"], "f-swing": ["timeframes", "SWING"],
};

function syncControls() {
  const f = S.state.filters;
  if (f) for (const [id, [group, value]] of Object.entries(BOXES)) $("#" + id).checked = (f[group] || []).includes(value);
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
  const r = await post("/api/filters", { [group]: picked });
  if (!r.ok) { syncControls(); toast("Filter not changed: " + (r.reason || ""), "bad"); return; }
  S.state.filters = r.filters;
  emit("filters");
  toast(r.note + (r.rescanning ? " Rescanning for the new plays…" : ""), "good");
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
  try { d = await api("/api/filters"); } catch { toast("The app isn't reachable", "bad"); return; }
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

export function initFilters() {
  on("state", syncControls);
  on("filters", syncControls);
  Object.keys(BOXES).forEach(id => { $("#" + id).onchange = e => changeFilter(e.target); });
  $("#btn-sectors").onclick = pickSectors;
  $("#f-hide-done").checked = store.get("atb-hide-done") === "1";
  $("#f-hide-done").onchange = e => { store.set("atb-hide-done", e.target.checked ? "1" : "0"); emit("plays"); };
  window.addEventListener("storage", e => {
    if (e.key === "atb-hide-done") { $("#f-hide-done").checked = e.newValue === "1"; emit("plays"); }
  });
}
