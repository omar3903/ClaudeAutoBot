/* Autopilot: hands-off entry. Exits are automatic either way. */
import { $, $$, escapeHtml, money, num, plural, post, pretty, usd } from "./util.js";
import { S, emit, on } from "./state.js";
import { openModal, toast } from "./ui.js";

const typeName = t => ({ INTRADAY: "day", SWING: "swing", PAIRS: "pairs" })[t] || String(t).toLowerCase();

/* The day / swing split as Autopilot lives it: the positions and the day's entries each kind has of its
   share ("day 0/7 · swing 3/3"), and what the owner should know - a share kept for a kind Autopilot isn't
   taking, a kind holding more than its share. Empty while the split is off (one of the two filter boxes off). */
export function splitSlots() {
  const ap = S.state.autopilot || {}, slots = ap.slots, split = (S.state.capital || {}).split || {};
  if (!slots || !split.on) return { text: "", warnings: [] };
  const kinds = [["INTRADAY", "day", split.day || {}], ["SWING", "swing", split.swing || {}]];
  const text = kinds.map(([k, name]) => `${name} ${slots[k].open}/${slots[k].max}`).join(" · ");
  const c = S.state.capital || {}, warnings = [];
  for (const [k, name, part] of kinds) {
    const s = slots[k];
    if (part.pct > 0 && !s.taking && ap.enabled)
      warnings.push(`${part.pct}% of the trading capital and ${s.max} of ${ap.max_auto_positions} positions are kept for ${name} trades, which Autopilot isn't taking - ${(ap.own_trade_types || []).includes(k) ? `the ${name === "day" ? "Intraday" : "Swing"} box above the plays is off` : `${name} trades are unticked in its settings (⚙)`}. Tick them, or move the slider.`);
    if (part.over > 0)
      warnings.push(`${name} trades hold ${money(part.over, c.currency)} more than their ${part.pct}% share. Nothing is sold for it; they take no new entries until they are back under it.`);
    else if (s.open > s.max)
      warnings.push(`${name} trades hold ${s.open} positions and their share is ${s.max}: no new ${name} entries until some close.`);
  }
  return { text, warnings };
}

export function renderAutopilot() {
  const ap = S.state.autopilot || {}, scan = S.state.scan || {};
  const btn = $("#ap-toggle");
  const enabled = !!ap.enabled, eff = !!ap.effective;
  const fast = !!scan.fast, secs = scan.fast_cycle_seconds, mins = (scan.settings || {}).cycle_minutes;
  const types = (ap.trade_types || []).map(typeName).join("+");
  btn.textContent = enabled ? `Autopilot: ${types || "on"}${ap.dry_run ? " · dry" : ""}${ap.daily_loss_stop ? " · stopped today" : fast ? ` ⚡${secs}s` : ""}` : "Autopilot: off";
  btn.classList.toggle("on", enabled && eff);
  btn.classList.toggle("armed-paper", enabled && !eff);       // wants to run but paper-gated in live
  const split = splitSlots();
  const caps = `${ap.open_auto_positions ?? 0}/${ap.max_auto_positions ?? 0} open${split.text ? ` (${split.text})` : ""} · ${ap.auto_trades_today ?? 0}/${ap.max_auto_trades_per_day ?? 0} today`
    + ((ap.sent_today ?? 0) > (ap.auto_trades_today ?? 0) ? ` (${ap.sent_today}/${ap.sent_ceiling} orders sent)` : "");
  const stopped = !ap.daily_loss_stop ? ""
    : (ap.realized_today || 0) < 0 ? ` Stopped for the day: today's closed trades have lost ${Math.abs(ap.realized_today || 0).toFixed(0)}, past the ${ap.max_daily_loss_pct}% daily limit.`
    : ` Stopped for the day: today's realized gain fell from ${(ap.peak_realized || 0).toFixed(0)} to ${(ap.realized_today || 0).toFixed(0)}, giving back more than ${ap.max_giveback_pct}% of it.`;
  const cadence = fast ? ` The hot list is rescanned every ~${secs}s while the session is open.`
    : enabled && eff ? ` The hot list and buffers are rescanned every ${mins} min, and the hot list every ~${secs}s once the session opens.` : "";
  btn.title = enabled
    ? (eff ? `Autopilot is taking entries: ${types || "?"}, ≥ ${ap.min_reward_risk}:1, ≥ conf ${ap.min_confidence}. ${caps}.${split.warnings.length ? " ⚠ " + split.warnings.join(" ") : ""}${stopped}${cadence} Exits are automatic. Click to turn off.`
      : (ap.blocked_note || "Autopilot is on but not routing (paper-only gate). Click to turn off."))
    : "Hands-off entry is OFF — you click every entry. Exits are automatic regardless. Click to turn on.";
  $("#autopilot-ctl").classList.toggle("live-warn", enabled && !eff);
  renderStrip();
  renderLossBanner();
}

/* Stopped for the day by the daily loss limit or the give-back rule (autopilot.daily_loss): a red banner that
   stays until it's dismissed or the stop is lifted - a toast is gone in seconds. The reason is the engine's
   own, from the event; after a page load, from the strip's reading. */
let lossWhy = "", lossDismissed = false;

export function onDailyLoss(p) {
  lossWhy = p.reason || "";
  lossDismissed = false;
  S.state.autopilot = { ...(S.state.autopilot || {}), daily_loss_stop: true };   // until the next snapshot says so
  toast(`🤖 Autopilot stopped for the day: ${lossWhy || "the daily loss limit was reached"}`, "bad");
  renderAutopilot();
}

function renderLossBanner() {
  const ap = S.state.autopilot || {}, h = ap.headline || {}, b = $("#loss-banner");
  if (!ap.daily_loss_stop) { lossWhy = ""; lossDismissed = false; }        // lifted, or a new day: a new stop shows again
  const show = !!ap.enabled && !!ap.daily_loss_stop && !lossDismissed;
  b.classList.toggle("hidden", !show);
  if (!show) return;
  const why = lossWhy || (h.state === "stopped" ? h.text.replace(/^Stopped for the day: /, "") : "")
    || "the daily loss limit or the give-back rule was reached";
  if (b.dataset.why === why) return;                                        // the Dismiss button stays under the mouse
  b.dataset.why = why;
  b.innerHTML = `<span>🤖 Autopilot stopped for the day: ${escapeHtml(why)}. Positions already open keep their stops and
    automatic exits.</span> <button class="ghost mini" id="loss-dismiss" title="The strip under the header still says so">Dismiss</button>`;
  $("#loss-dismiss").onclick = () => { lossDismissed = true; renderLossBanner(); };
}

/* The strip under the header: what Autopilot is doing now and why (status().headline, in the server's
   words), how many plays on the board pass every check, a countdown while it paces its entries, and
   today's closed trades by setup. The countdown runs from the reading that carried it - a redraw for
   another reason doesn't restart it - and the ticker only runs while there is one. */
let stripHeadline = null, stripDue = 0, stripTimer = null;

function tickStrip() {
  const left = Math.ceil((stripDue - Date.now()) / 1000);
  $("#ap-strip-next").textContent = !stripDue ? "" : left > 0 ? `next entry in ${left}s` : "next entry on its next pass";
  if (left <= 0 && stripTimer) { clearInterval(stripTimer); stripTimer = null; }
}

function renderStrip() {
  const ap = S.state.autopilot || {}, h = ap.headline, strip = $("#ap-strip");
  strip.classList.toggle("hidden", !h);
  if (!h) return;
  strip.dataset.state = h.state;
  let text = h.text;
  if (h.state === "taking" || h.state === "pacing") {
    const tags = S.plays.map(p => p.autopilot || {}).filter(a => a.eligible && !a.acted);
    const room = tags.filter(a => !a.waiting).length, held = tags.length - room;
    text += room ? ` · ${plural(room, "play")} pass${room === 1 ? "es" : ""} every check` : " · no play passes every check right now";
    if (held) text += `, ${held} more wait${held === 1 ? "s" : ""} for room`;
  }
  const el = $("#ap-strip-text");
  if (el.textContent !== text) el.textContent = text;          // a live region: say it again only when it changes
  el.title = (ap.replay_losers || []).map(l => `${l.strategy}: ${l.why}`).join("\n");
  if (h !== stripHeadline) {                                  // a fresh reading from the server
    stripHeadline = h;
    stripDue = h.state === "pacing" && h.next_entry_in_s > 0 ? Date.now() + h.next_entry_in_s * 1000 : 0;
  }
  tickStrip();
  if (stripDue > Date.now() && !stripTimer) stripTimer = setInterval(tickStrip, 1000);
  $("#ap-strip-today").innerHTML = (ap.today || []).map(t =>
    `<span class="chip ${t.r > 0 ? "good" : t.r < 0 ? "bad" : ""}" title="${escapeHtml(`${pretty(t.strategy)} today: ${plural(t.closed, "trade")} closed, ${t.wins} won, ${t.r >= 0 ? "+" : ""}${num(t.r)}R, ${usd(t.pl)}`)}">${escapeHtml(pretty(t.strategy))} ${t.wins}/${t.closed} · ${t.r >= 0 ? "+" : ""}${num(t.r, 1)}R</span>`).join("");
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
      ≤ ${ap.max_auto_positions ?? 2} open · ≤ ${ap.max_auto_trades_per_day ?? 3}/day · ≥ conf ${ap.min_confidence ?? 0.5}.
      Change these with the ⚙ button.</p>`,
    okText: "Turn on", okClass: live && !ap.allow_live ? "danger" : "long",
    onOk: () => postAutopilot({ enabled: true }),
  });
}

function configure() {
  const ap = S.state.autopilot || {};
  const live = S.state.mode === "live";
  openModal({
    title: "Autopilot settings",
    bodyHTML: `<div class="ap-form">
      <label>Auto-take these trade types</label>
      <div class="row-gap">
        <label><input type="checkbox" id="ap-type-day" ${(ap.own_trade_types || ap.trade_types || []).includes("INTRADAY") ? "checked" : ""}> day trades</label>
        <label><input type="checkbox" id="ap-type-swing" ${(ap.own_trade_types || ap.trade_types || []).includes("SWING") ? "checked" : ""}> swing trades</label>
        <label><input type="checkbox" id="ap-type-pairs" ${(ap.own_trade_types || ap.trade_types || []).includes("PAIRS") ? "checked" : ""}> pairs</label>
      </div>
      <p class="muted small">The <b>Intraday</b> / <b>Swing</b> boxes above the plays say what is scanned and shown; these say what
        Autopilot may take of it. Untick day trades here to keep day plays on the board for the review without trading them.
        Taking now: <b>${(ap.trade_types || []).map(typeName).join(", ") || "none"}</b>.</p>
      <label title="Tick the setups Autopilot may take; none ticked = every setup. The others stay on the board for you to click - the Strategies panel is where a setup is switched off everywhere.">Only these setups <span class="muted">(none ticked = every setup)</span></label>
      <div class="ap-noise">${Object.values(S.strategies || {}).filter(s => s.kind !== "FUNDAMENTAL")
        .sort((a, b) => String(a.title || a.key).localeCompare(String(b.title || b.key))).map(s =>
        `<label><input type="checkbox" class="ap-strat-check" value="${escapeHtml(s.key)}" ${(ap.strategies || []).includes(s.key) ? "checked" : ""}> ${escapeHtml(s.title || pretty(s.key))}</label>`).join("")}</div>
      ${(() => { const sp = splitSlots(); return sp.text ? `<p class="muted small">Positions follow the day / swing split of the trading capital (the slider above the plays): <b>${escapeHtml(sp.text)}</b> of ${ap.max_auto_positions ?? 0}, and the day's entries the same way.</p>${sp.warnings.map(w => `<p class="small warn-text">⚠ ${escapeHtml(w)}</p>`).join("")}` : ""; })()}
      <label>Minimum confidence, day trades <b id="ap-conf-v">${ap.min_confidence ?? 0.5}</b></label>
      <input type="range" id="ap-conf" min="0.4" max="0.9" step="0.01" value="${ap.min_confidence ?? 0.5}">
      <label title="Swing setups state flat, modest confidences (0.55-0.58); the replay's proof is their real gate, so this only keeps out the weakest">Minimum confidence, swing trades <b id="ap-sconf-v">${ap.min_swing_confidence ?? 0.5}</b></label>
      <input type="range" id="ap-sconf" min="0.3" max="0.9" step="0.01" value="${ap.min_swing_confidence ?? 0.5}">
      <label>Minimum reward : risk</label>
      <input type="number" id="ap-rr" min="1" max="10" step="0.5" value="${ap.min_reward_risk ?? 2}">
      <div class="ap-row">
        <span><label>Max open positions</label><input type="number" id="ap-maxpos" min="0" max="10" step="1" value="${ap.max_auto_positions ?? 2}"></span>
        <span><label>Max per day</label><input type="number" id="ap-maxday" min="0" max="20" step="1" value="${ap.max_auto_trades_per_day ?? 3}"></span>
      </div>
      <div class="ap-row">
        <span><label>Max per strategy</label><input type="number" id="ap-maxstrat" min="1" max="10" step="1" value="${ap.max_per_strategy ?? 2}"></span>
        <span><label title="A scan cycle is a scan of the market: every 5 minutes for swing trades, every 60 seconds for day trades. The 15-second re-check of the board doesn't count as one.">New per scan cycle</label><input type="number" id="ap-maxcycle" min="1" max="10" step="1" value="${ap.max_new_per_cycle ?? 1}"></span>
      </div>
      <div class="ap-row">
        <span><label title="Aziz's daily maximum loss: once today's closed trades have lost this share of equity, no more entries until tomorrow. 0 = off">Stop for the day after losing % of equity</label><input type="number" id="ap-dayloss" min="0" max="50" step="0.5" value="${ap.max_daily_loss_pct ?? 2}"></span>
        <span><label title="Aziz's give-back rule: once the day's realized gain has fallen this far from its best, stop for the day and keep what's left. 0 = off">...or after giving back % of the day's gain</label><input type="number" id="ap-giveback" min="0" max="100" step="5" value="${ap.max_giveback_pct ?? 30}"></span>
      </div>
      <div class="ap-row">
        <span><label title="How many times in a row a day setup must show before Autopilot takes it - on new 5-minute candles while the box below is ticked, otherwise in scans">Day trades: seen in a row</label><input type="number" id="ap-confirm" min="1" max="10" step="1" value="${ap.min_confirmations ?? 2}"></span>
        <span><label title="Aziz keeps the last half hour for closing, and the exit manager flattens day trades 10 minutes before the bell - a new one this late has no time to work. 0 = off">Day trades: none in the last N minutes</label><input type="number" id="ap-close" min="0" max="120" step="5" value="${ap.min_minutes_to_close ?? 30}"></span>
        <span><label title="All positions together, as a share of the trading capital (the header button): with the whole account and margin that's the buying power IBKR allows, cash only the account's value. Under 100 keeps a buffer.">Max % of trading capital in positions</label><input type="number" id="ap-gross" min="10" max="100" step="5" value="${ap.max_gross_exposure_pct ?? 100}"></span>
      </div>
      <label title="The scans read one 5-minute candle several times over; the replay enters a day setup once it has shown on two candles running. Ticked, live counts the same way. A setup that fires on one candle (a reclaim, a flag) then never reaches two - set 1 above, or untick this, to practise those again. The replay models two at most."><input type="checkbox" id="ap-candles" ${ap.confirm_on_new_candle !== false ? "checked" : ""}> Count a day setup as seen again only on a new 5-minute candle, as the replay does</label>
      <label data-term="noise">Skip plays flagged as noise</label>
      <div class="ap-noise">${Object.entries(ap.noise_labels || {}).map(([flag, label]) =>
        `<label><input type="checkbox" class="ap-noise-check" value="${escapeHtml(flag)}" ${(ap.skip_noise || []).includes(flag) ? "checked" : ""}> ${escapeHtml(label)}</label>`).join("")}</div>
      <label title="With real money this is always on. On paper it is your choice: unticked, Autopilot practises the unproven setups too - at a quarter of the usual risk - and every trade is recorded with the settings it was taken on."><input type="checkbox" id="ap-proven" ${ap.require_proven !== false || live ? "checked" : ""} ${live ? "disabled" : ""}> Only trade strategies the replay has proven (Strategies panel)${live ? " - always on in Live" : (ap.require_proven === false ? " - off: unproven setups trade at practice size (a quarter of the risk)" : "")}</label>
      <label title="With the box above unticked Autopilot practises unproven setups; this still skips a setup with evidence it loses: its replay, the way Autopilot takes it, averages -${ap.replay_loser_r ?? 0.05}R a trade or worse over ${ap.min_replay_trades ?? 30} trades and over 10 in the held-out sessions, or its own trades average -0.30R or worse over 10. A replay that recovers lifts it by itself. With proof required it has nothing to add.">Skip setups that lose in the replay
        <select id="ap-losers">
          <option value="off" ${ap.skip_replay_losers === "off" ? "selected" : ""}>off - practise them too</option>
          <option value="day" ${(ap.skip_replay_losers || "day") === "day" ? "selected" : ""}>day trades</option>
          <option value="all" ${ap.skip_replay_losers === "all" ? "selected" : ""}>day and swing trades</option>
        </select></label>
      ${(ap.replay_losers || []).length ? `<p class="muted small">Skipped now: ${ap.replay_losers.map(l =>
        `<span title="${escapeHtml(l.why)}">${escapeHtml(l.strategy)}</span>`).join(", ")}</p>` : ""}
      <label title="The model learns, every night, the odds that a play pays from what happened to plays like it - live, not taken, and replayed. It only has a say while its own walk-forward test calls it usable.">The learned model
        <select id="ap-model">
          <option value="shadow" ${(ap.model_mode || "shadow") === "shadow" ? "selected" : ""}>shadow - log its odds, never act on them</option>
          <option value="gate" ${ap.model_mode === "gate" ? "selected" : ""}>gate - refuse plays it gives under ${Math.round((ap.model_min_p ?? 0.55) * 100)}%</option>
          <option value="size" ${ap.model_mode === "size" ? "selected" : ""}>size - gate, and risk more on better odds</option>
        </select></label>
      <p class="muted small">${ap.model ? `Model ${escapeHtml(ap.model.id)}: ${ap.model.rows} rows, ${ap.model.usable ? "<b>usable</b>" : "<b>not usable yet</b> - it has no say whatever is chosen here"}.` : "No model trained yet - the first is trained after a day's review once there are 500 rows."}</p>
      <label><input type="checkbox" id="ap-cooldown" ${ap.cooldown_after_loss !== false ? "checked" : ""}> Cool off a ticker for the day after it stops out</label>
      <label><input type="checkbox" id="ap-dry" ${ap.dry_run ? "checked" : ""}> Dry run (log what it would do, place nothing)</label>
      <p class="muted">Live routing also needs <code>autopilot.allow_live: true</code> in config.yaml. The Long / Short, Intraday / Swing and
      Sectors filters and the Strategies panel apply to Autopilot too. Exits are automatic no matter what.</p>
    </div>`,
    okText: "Save", okClass: "long",
    onOk: async () => {
      const types = [];
      if ($("#ap-type-day").checked) types.push("INTRADAY");
      if ($("#ap-type-swing").checked) types.push("SWING");
      if ($("#ap-type-pairs").checked) types.push("PAIRS");
      const int = sel => parseInt($(sel).value, 10);
      await postAutopilot({
        trade_types: types.length ? types : ["INTRADAY"],
        strategies: $$(".ap-strat-check").filter(c => c.checked).map(c => c.value),
        min_confidence: parseFloat($("#ap-conf").value),
        min_swing_confidence: parseFloat($("#ap-sconf").value),
        min_reward_risk: parseFloat($("#ap-rr").value),
        max_auto_positions: int("#ap-maxpos"),
        max_auto_trades_per_day: int("#ap-maxday"),
        max_per_strategy: int("#ap-maxstrat"),
        max_new_per_cycle: int("#ap-maxcycle"),
        min_confirmations: int("#ap-confirm"),
        confirm_on_new_candle: $("#ap-candles").checked,
        min_minutes_to_close: int("#ap-close"),
        max_gross_exposure_pct: parseFloat($("#ap-gross").value),
        max_daily_loss_pct: parseFloat($("#ap-dayloss").value),
        max_giveback_pct: parseFloat($("#ap-giveback").value),
        skip_noise: $$(".ap-noise-check").filter(c => c.checked).map(c => c.value),
        ...(live ? {} : { require_proven: $("#ap-proven").checked }),      // in Live the box is locked on; the paper choice is kept
        skip_replay_losers: $("#ap-losers").value,
        model_mode: $("#ap-model").value,
        cooldown_after_loss: $("#ap-cooldown").checked,
        dry_run: $("#ap-dry").checked,
      });
    },
  });
  const slider = $("#ap-conf");
  slider.oninput = () => { $("#ap-conf-v").textContent = slider.value; };
  const swing = $("#ap-sconf");
  swing.oninput = () => { $("#ap-sconf-v").textContent = swing.value; };
}

export function initAutopilot() {
  on("state", renderAutopilot);
  on("scan", renderAutopilot);
  on("filters", renderAutopilot);                 // what it takes follows the filter boxes,
  on("capital", renderAutopilot);                 // and its slots the day / swing split
  on("plays", renderStrip);                       // the strip counts the plays that pass every check
  $("#ap-toggle").onclick = toggle;
  $("#ap-cfg").onclick = configure;
}
