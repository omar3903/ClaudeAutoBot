/* Settings: when the scans run and how big the lists are, plus the scan status
   and buttons to scan now. */
import { $, $$, api, count, escapeHtml, fmtClock, plural, post } from "./util.js";
import { S, emit, on } from "./state.js";
import { busy, drawerOpen, openDrawer, toastResult, unbusy } from "./ui.js";
import { currentRun, kindLabel, nextFullScanText, progressHTML, requestScan } from "./scan.js";

async function openSettings() {
  openDrawer("settings", "Settings", `<p class="muted">Loading…</p>`);
  let scan;
  try { scan = await api("/api/settings"); } catch {
    $("#drawer-body").innerHTML = `<p class="reasons">Couldn't load the settings.</p>`;
    return;
  }
  if (drawerOpen("settings")) render(scan);
}

function numberField(key, label, help, [lo, hi], value) {
  return `<label for="set-${key}">${label}</label>
    <div><input type="number" id="set-${key}" data-key="${key}" min="${lo}" max="${hi}" step="1" value="${value}">
      <div class="help">${help} (${lo}–${hi})</div></div>`;
}

function render(scan) {
  const s = scan.settings, limits = scan.limits, [earliest, latest] = scan.full_scan_window;
  const [gapLo, gapHi] = scan.gap_check_window || ["08:00", "09:25"];
  $("#drawer-body").innerHTML = `
    <section class="conn">
      <h4>Scanning</h4>
      <p class="muted">Once a day before the open every US stock is ranked by how in play it is. The hottest become the day's
        <b data-term="hot">hot list</b>; the next best in each sector wait in a <b data-term="buffer">buffer</b>. During the
        session the hot list and a couple of buffer names per sector are rescanned on a cycle, so few IBKR requests are used.</p>
      <form class="field-grid" id="form-settings" onsubmit="return false">
        <label for="set-premarket_time">Full scan at (ET)</label>
        <div><input type="time" id="set-premarket_time" data-key="premarket_time" min="${earliest}" max="${latest}" step="300" value="${escapeHtml(s.premarket_time)}">
          <div class="help">Pre-market, ${earliest}–${latest} ET, so it's finished at least half an hour before the open.</div></div>
        <label for="set-gapper_time" data-term="gap">Gap check at (ET)</label>
        <div><input type="time" id="set-gapper_time" data-key="gapper_time" min="${gapLo}" max="${gapHi}" step="300" value="${escapeHtml(s.gapper_time || "09:15")}">
          <div class="help">Just before the open, ${gapLo}–${gapHi} ET: the hot list and buffer names' pre-market candles are read once, and
            the stocks gapping on volume take hot-list slots - Aziz's gappers watchlist. One request per name.</div></div>
        ${numberField("cycle_minutes", "Rescan every (min)", "How often the hot list and buffers are rescanned in the session", limits.cycle_minutes, s.cycle_minutes)}
        ${numberField("hot_list_size", "Hot list size", "Stocks rescanned every cycle", limits.hot_list_size, s.hot_list_size)}
        ${numberField("sector_queue_size", "Buffer per sector", "Candidates lined up in each sector", limits.sector_queue_size, s.sector_queue_size)}
        ${numberField("wide_minutes", "Wide scan every (min)", `Every liquid stock's 5-minute candles, one request each, and the setups on all of them - so a stock that heats up mid-session is seen. 0 = off, else at least ${scan.wide_minimum_minutes || 15}`, limits.wide_minutes || [0, 120], s.wide_minutes ?? 30)}
        ${numberField("wide_stocks", "Wide scan covers", "The hottest N of the full scan's liquid stocks; 0 = all of them (a few minutes per scan)", limits.wide_stocks || [0, 6000], s.wide_stocks ?? 0)}
      </form>
      <div class="row-gap"><button class="mini lockable" id="settings-save">Save</button></div>
    </section>

    <section class="conn">
      <h4>Scan status</h4>
      <div id="settings-status">${statusHTML(scan)}</div>
      <div class="row-gap">
        <button class="mini lockable" id="settings-full">Run full scan now</button>
        <button class="ghost mini lockable" id="settings-cycle">Rescan hot list now</button>
        <button class="ghost mini lockable" id="settings-gappers" title="Read the pre-market candles now (before the open)">Check gappers now</button>
        <button class="ghost mini lockable" id="settings-wide" title="Every liquid stock's 5-minute candles now - one request each, a few minutes">Scan every stock now</button>
      </div>
      <p class="muted small">A full scan re-ranks every stock and rebuilds today's hot list and buffers. Candles already downloaded
        are reused, so running it again later in the day is quick.</p>
    </section>`;
  $("#settings-save").onclick = e => save(e.currentTarget);
  $("#settings-full").onclick = e => requestScan("full", e.currentTarget);
  $("#settings-cycle").onclick = e => requestScan("cycle", e.currentTarget);
  $("#settings-gappers").onclick = e => requestScan("gappers", e.currentTarget);
  $("#settings-wide").onclick = e => requestScan("wide", e.currentTarget);
}

function summary(label, sum) {
  if (!sum) return `<span>${label}</span><span class="muted">not since the app started</span>`;
  const size = sum.kind === "full"
    ? `${count(sum.universe_size)} listed · ${count(sum.liquid)} liquid · hot list ${sum.hot.length}`
    : `${count(sum.scanned)} scanned`;
  return `<span>${label}</span><span>${fmtClock(sum.finished_at)} · ${size} · ${plural(sum.n_plays, "play")} · ${sum.elapsed_s}s</span>`;
}

function statusHTML(scan) {
  const run = currentRun();
  return `<div class="kv">
    <span>Now</span><span>${run ? progressHTML(run) : "idle"}</span>
    ${summary("Last full scan", scan.last_full)}
    ${summary("Last gap check", scan.last_gappers)}
    ${summary("Last wide scan", scan.last_wide)}
    ${summary(`Last ${scan.last_cycle ? kindLabel(scan.last_cycle.kind).toLowerCase() : "cycle"}`, scan.last_cycle)}
    <span>Watchlist for</span><span>${escapeHtml(scan.watchlist_session || "none yet")}</span>
    <span>Next full scan</span><span>${nextFullScanText(scan)}</span>
  </div>`;
}

async function save(btn) {
  const body = {};
  $$("#form-settings [data-key]").forEach(i => {
    body[i.dataset.key] = i.type === "time" ? i.value : parseInt(i.value, 10);
  });
  busy(btn, "Saving…");
  const r = await post("/api/settings", body);
  unbusy(btn);
  toastResult(r.ok && !r.note ? { ...r, note: "Saved." } : r);
  if (r.ok) { S.state.scan = r.scan; emit("scan"); render(r.scan); }
}

export function initSettings() {
  $("#btn-settings").onclick = openSettings;
  on("scan", () => {
    if (drawerOpen("settings") && $("#settings-status")) $("#settings-status").innerHTML = statusHTML(S.state.scan || {});
  });
}
