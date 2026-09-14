/* Strategies: switch setups on or off and set their weight. */
import { $, $$, api, escapeHtml, post, pretty } from "./util.js";
import { S, emit, on } from "./state.js";
import { drawerOpen, openDrawer, toast, toastResult } from "./ui.js";

export const stratLabel = key =>
  `<span data-term="strategy" data-key="${escapeHtml(key)}">${escapeHtml((S.strategies[key] || {}).title || pretty(key))}</span>`;

export function indexStrategies(rows) {
  if (!rows) return;
  S.strategies = Object.fromEntries(rows.map(s => [s.key, s]));
  S.state.strategies_on = rows.filter(s => s.enabled).length;
  $("#btn-strategies").textContent = `Strategies · ${S.state.strategies_on}`;
  emit("strategies");
}

export async function loadStrategies() {
  const d = await api("/api/strategies");
  indexStrategies(d.strategies);
}

async function openStrategies() {
  openDrawer("strategies", "Strategies", `<p class="muted">Loading…</p>`);
  try { await loadStrategies(); } catch {
    $("#drawer-body").innerHTML = `<p class="reasons">Couldn't load the strategies.</p>`;
  }
}

function card(s) {
  return `<div class="strat ${s.enabled ? "" : "off"}">
    <div class="strat-head">
      <label class="switch lockable" title="${s.enabled ? "On — click to switch off" : "Off — click to switch on"}">
        <input type="checkbox" data-strat-toggle="${escapeHtml(s.key)}" ${s.enabled ? "checked" : ""}><span></span></label>
      <div><div class="meta">${s.timeframe === "INTRADAY" ? "day trade" : "swing"} · ${escapeHtml(s.kind.toLowerCase())}${s.customized ? " · changed from config" : ""}</div>
        <h4>${escapeHtml(s.title)}</h4></div>
      <label class="weight lockable" title="Scales this setup's score — 1 is normal">weight
        <input type="number" min="0.1" max="3" step="0.1" value="${s.weight}" data-strat-weight="${escapeHtml(s.key)}"></label>
    </div>
    <div class="thesis">${escapeHtml(s.thesis)}</div>
  </div>`;
}

function renderStrategies() {
  const rows = Object.values(S.strategies);
  const groups = [
    ["Day trades", s => s.kind === "TECHNICAL" && s.timeframe === "INTRADAY"],
    ["Swing trades", s => s.kind === "TECHNICAL" && s.timeframe !== "INTRADAY"],
    ["Valuation (fundamental)", s => s.kind === "FUNDAMENTAL"],
  ];
  $("#drawer-body").innerHTML = `
    <p class="muted">${rows.filter(s => s.enabled).length} of ${rows.length} setups on. A change applies to the next scan and to Autopilot
      straight away, shows up in every open tab, and is remembered. Plays from a setup you switch off leave the board.</p>
    ${groups.map(([name, test]) => {
      const group = rows.filter(test);
      return group.length ? `<div class="group-title">${name}</div>${group.map(card).join("")}` : "";
    }).join("")}
    <div class="row-gap"><button class="ghost mini lockable" id="strat-reset">Reset to config.yaml</button></div>`;
  $$("[data-strat-toggle]").forEach(c => { c.onchange = () => updateStrategy(c.dataset.stratToggle, { enabled: c.checked }); });
  $$("[data-strat-weight]").forEach(i => {
    i.onchange = () => {
      const w = parseFloat(i.value);
      if (!(w >= 0.1 && w <= 3)) { toast("Weight must be between 0.1 and 3", "bad"); renderStrategies(); return; }
      updateStrategy(i.dataset.stratWeight, { weight: w });
    };
  });
  $("#strat-reset").onclick = async () => {
    const r = await post("/api/strategies/reset");
    toastResult(r);
    if (r.ok) indexStrategies(r.strategies);
  };
}

async function updateStrategy(key, body) {
  const r = await post(`/api/strategies/${encodeURIComponent(key)}`, body);
  if (r.ok) { toast(r.note + (r.rescanning ? " Rescanning…" : ""), "good"); indexStrategies(r.strategies); return; }
  toast("Strategy not changed: " + (r.reason || ""), "bad");
  if (drawerOpen("strategies")) renderStrategies();
}

export function initStrategies() {
  $("#btn-strategies").onclick = openStrategies;
  on("strategies", () => { if (drawerOpen("strategies")) renderStrategies(); });
}
