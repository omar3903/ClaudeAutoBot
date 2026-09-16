/* Autopilot: hands-off entry. Exits are automatic either way. */
import { $, $$, escapeHtml, post } from "./util.js";
import { S, emit, on } from "./state.js";
import { openModal, toast } from "./ui.js";

const typeName = t => ({ INTRADAY: "day", SWING: "swing", PAIRS: "pairs" })[t] || String(t).toLowerCase();

export function renderAutopilot() {
  const ap = S.state.autopilot || {}, scan = S.state.scan || {};
  const btn = $("#ap-toggle");
  const enabled = !!ap.enabled, eff = !!ap.effective;
  const fast = !!scan.fast, secs = scan.fast_cycle_seconds, mins = (scan.settings || {}).cycle_minutes;
  const types = (ap.trade_types || []).map(typeName).join("+");
  btn.textContent = enabled ? `Autopilot: ${types || "on"}${ap.dry_run ? " · dry" : ""}${ap.daily_loss_stop ? " · stopped today" : fast ? ` ⚡${secs}s` : ""}` : "Autopilot: off";
  btn.classList.toggle("on", enabled && eff);
  btn.classList.toggle("armed-paper", enabled && !eff);       // wants to run but paper-gated in live
  const caps = `${ap.open_auto_positions ?? 0}/${ap.max_auto_positions ?? 0} open · ${ap.auto_trades_today ?? 0}/${ap.max_auto_trades_per_day ?? 0} today`;
  const stopped = ap.daily_loss_stop ? ` Stopped for the day: today's closed trades have lost ${Math.abs(ap.realized_today || 0).toFixed(0)}, past the ${ap.max_daily_loss_pct}% daily limit.` : "";
  const cadence = fast ? ` The hot list is rescanned every ~${secs}s while the session is open.`
    : enabled && eff ? ` The hot list and buffers are rescanned every ${mins} min, and the hot list every ~${secs}s once the session opens.` : "";
  btn.title = enabled
    ? (eff ? `Autopilot is taking entries: ${types || "?"}, ≥ ${ap.min_reward_risk}:1, ≥ conf ${ap.min_confidence}. ${caps}.${stopped}${cadence} Exits are automatic. Click to turn off.`
      : (ap.blocked_note || "Autopilot is on but not routing (paper-only gate). Click to turn off."))
    : "Hands-off entry is OFF — you click every entry. Exits are automatic regardless. Click to turn on.";
  $("#autopilot-ctl").classList.toggle("live-warn", enabled && !eff);
}

async function postAutopilot(body) {
  const r = await post("/api/autopilot", body);
  if (r.autopilot) { S.state.autopilot = r.autopilot; renderAutopilot(); emit("autopilot"); }
  if (r.note) toast(r.note, r.autopilot && r.autopilot.effective ? "good" : "warn");
  else if (!r.ok) toast("Autopilot: " + (r.reason || "update failed"), "bad");
  return r;
}

function toggle() {
  const ap = S.state.autopilot || {};
  if (ap.enabled) return postAutopilot({ enabled: false });
  const live = S.state.mode === "live";
  openModal({
    title: live ? "Turn on Autopilot in LIVE mode?" : "Turn on Autopilot?",
    bodyHTML: `<div class="${live && !ap.allow_live ? "warn-box" : "muted"}">
        ${live && !ap.allow_live
      ? "Autopilot will arm for <b>paper only</b> — it will not route real orders until you set <code>autopilot.allow_live: true</code> in config/config.yaml."
      : "The bot will <b>place entries for you</b> when a play clears the gate. Exits are already automatic. It stays inside the per-day and position caps and the 2:1 minimum."}
      </div>
      <p class="muted">Trade types: <b>${(ap.trade_types || ["INTRADAY"]).map(typeName).join(", ")}</b> ·
      ≤ ${ap.max_auto_positions ?? 2} open · ≤ ${ap.max_auto_trades_per_day ?? 3}/day · ≥ conf ${ap.min_confidence ?? 0.62}.
      Change these with the ⚙ button.</p>`,
    okText: "Turn on", okClass: live && !ap.allow_live ? "danger" : "long",
    onOk: () => postAutopilot({ enabled: true }),
  });
}

function configure() {
  const ap = S.state.autopilot || {};
  openModal({
    title: "Autopilot settings",
    bodyHTML: `<div class="ap-form">
      <label>Auto-take these trade types</label>
      <p class="muted small">Autopilot takes what the <b>Intraday</b>, <b>Swing</b> and <b>Pairs</b> boxes above the plays switch on -
        now: <b>${(ap.trade_types || []).map(typeName).join(", ") || "none"}</b>.</p>
      <label>Minimum confidence <b id="ap-conf-v">${ap.min_confidence ?? 0.62}</b></label>
      <input type="range" id="ap-conf" min="0.4" max="0.9" step="0.01" value="${ap.min_confidence ?? 0.62}">
      <label>Minimum reward : risk</label>
      <input type="number" id="ap-rr" min="1" max="10" step="0.5" value="${ap.min_reward_risk ?? 2}">
      <div class="ap-row">
        <span><label>Max open positions</label><input type="number" id="ap-maxpos" min="0" max="10" step="1" value="${ap.max_auto_positions ?? 2}"></span>
        <span><label>Max per day</label><input type="number" id="ap-maxday" min="0" max="20" step="1" value="${ap.max_auto_trades_per_day ?? 3}"></span>
      </div>
      <div class="ap-row">
        <span><label>Max per strategy</label><input type="number" id="ap-maxstrat" min="1" max="10" step="1" value="${ap.max_per_strategy ?? 2}"></span>
        <span><label>New per scan</label><input type="number" id="ap-maxcycle" min="1" max="10" step="1" value="${ap.max_new_per_cycle ?? 1}"></span>
      </div>
      <div class="ap-row">
        <span><label title="Aziz's daily maximum loss: once today's closed trades have lost this share of equity, no more entries until tomorrow. 0 = off">Stop for the day after losing % of equity</label><input type="number" id="ap-dayloss" min="0" max="50" step="0.5" value="${ap.max_daily_loss_pct ?? 2}"></span>
      </div>
      <div class="ap-row">
        <span><label>Day trades: seen in scans in a row</label><input type="number" id="ap-confirm" min="1" max="10" step="1" value="${ap.min_confirmations ?? 2}"></span>
        <span><label>Max % of equity in positions</label><input type="number" id="ap-gross" min="10" max="400" step="5" value="${ap.max_gross_exposure_pct ?? 100}"></span>
      </div>
      <label data-term="noise">Skip plays flagged as noise</label>
      <div class="ap-noise">${Object.entries(ap.noise_labels || {}).map(([flag, label]) =>
        `<label><input type="checkbox" class="ap-noise-check" value="${escapeHtml(flag)}" ${(ap.skip_noise || []).includes(flag) ? "checked" : ""}> ${escapeHtml(label)}</label>`).join("")}</div>
      <label><input type="checkbox" id="ap-proven" ${ap.require_proven !== false ? "checked" : ""}> Only trade strategies the replay has proven (Strategies panel)</label>
      <label><input type="checkbox" id="ap-cooldown" ${ap.cooldown_after_loss !== false ? "checked" : ""}> Cool off a ticker for the day after it stops out</label>
      <label><input type="checkbox" id="ap-dry" ${ap.dry_run ? "checked" : ""}> Dry run (log what it would do, place nothing)</label>
      <p class="muted">Live routing also needs <code>autopilot.allow_live: true</code> in config.yaml. The Long / Short, Intraday / Swing and
      Sectors filters and the Strategies panel apply to Autopilot too. Exits are automatic no matter what.</p>
    </div>`,
    okText: "Save", okClass: "long",
    onOk: async () => {
      const types = ["INTRADAY", "SWING"];                      // day and swing follow the filters
      if ((ap.trade_types || []).includes("PAIRS")) types.push("PAIRS");     // pairs: the box next to them
      const int = sel => parseInt($(sel).value, 10);
      await postAutopilot({
        trade_types: types.length ? types : ["INTRADAY"],
        min_confidence: parseFloat($("#ap-conf").value),
        min_reward_risk: parseFloat($("#ap-rr").value),
        max_auto_positions: int("#ap-maxpos"),
        max_auto_trades_per_day: int("#ap-maxday"),
        max_per_strategy: int("#ap-maxstrat"),
        max_new_per_cycle: int("#ap-maxcycle"),
        min_confirmations: int("#ap-confirm"),
        max_gross_exposure_pct: parseFloat($("#ap-gross").value),
        max_daily_loss_pct: parseFloat($("#ap-dayloss").value),
        skip_noise: $$(".ap-noise-check").filter(c => c.checked).map(c => c.value),
        require_proven: $("#ap-proven").checked,
        cooldown_after_loss: $("#ap-cooldown").checked,
        dry_run: $("#ap-dry").checked,
      });
    },
  });
  const slider = $("#ap-conf");
  slider.oninput = () => { $("#ap-conf-v").textContent = slider.value; };
}

export function initAutopilot() {
  on("state", renderAutopilot);
  on("scan", renderAutopilot);
  $("#ap-toggle").onclick = toggle;
  $("#ap-cfg").onclick = configure;
}
