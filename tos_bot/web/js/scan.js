/* Scan status - what's running and what the last scans found - and asking for a scan. */
import { $, count, escapeHtml, fmtClock, fmtEt, plural, post } from "./util.js";
import { S, emit, on, refreshState } from "./state.js";
import { toast } from "./ui.js";

export const kindLabel = kind => ({ full: "Full scan", cycle: "Cycle", fast: "Hot-list cycle", gappers: "Gap check" }[kind] || "Scan");

let lastFailure = "";

export function onScanEvent(topic, p) {
  if (topic === "scan.started") {
    S.scanRun = { kind: p.kind, stage: "", done: 0, total: 0 };
  } else if (topic === "scan.progress") {
    S.scanRun = { kind: p.kind, stage: p.stage, done: p.done, total: p.total };
  } else {
    S.scanRun = null;
    if (topic === "scan.failed" && p.reason !== lastFailure) toast(`${kindLabel(p.kind)} didn't run: ${p.reason}`, "warn");
    lastFailure = topic === "scan.failed" ? p.reason : "";
    refreshState();
  }
  emit("scan");
}

/** The scan in progress - from events, or from the snapshot after a page load. */
export function currentRun() {
  const running = (S.state.scan || {}).running;
  return S.scanRun || (running ? { kind: running.kind, stage: "", done: 0, total: 0 } : null);
}

export function progressHTML(run) {
  if (!run) return "";
  const detail = run.total ? `${escapeHtml(run.stage)} ${count(run.done)} / ${count(run.total)}` : "running…";
  const bar = run.total ? `<span class="progress"><i style="width:${Math.round(run.done / run.total * 100)}%"></i></span>` : "";
  return `<span class="scan-run">${kindLabel(run.kind)}: ${detail}${bar}</span>`;
}

export function nextFullScanText(scan) {
  if (!scan.next_full_scan) return "–";
  return new Date(scan.next_full_scan) <= new Date() ? "due now" : fmtEt(scan.next_full_scan);
}

function renderScanMeta() {
  const scan = S.state.scan || {}, run = currentRun();
  if (run) { $("#scan-meta").innerHTML = progressHTML(run); return; }
  const last = [scan.last_cycle, scan.last_full].filter(Boolean)
    .sort((a, b) => (a.started_at < b.started_at ? 1 : -1))[0];
  const parts = [];
  if (last) parts.push(`${kindLabel(last.kind)} ${fmtClock(last.finished_at)}: ${plural(last.n_plays, "play")}, ${last.elapsed_s}s`);
  if (last && last.hot && last.hot.length) parts.push(`hot list ${last.hot.length}`);
  parts.push(`next full scan ${nextFullScanText(scan)}`);
  $("#scan-meta").textContent = parts.join(" · ");
}

export async function requestScan(kind, btn) {
  if (btn) btn.disabled = true;
  const r = await post("/api/scan", { kind });
  toast(r.ok ? r.note : "Not scanning: " + (r.reason || ""), r.ok ? "good" : "bad");
  if (btn) setTimeout(() => { btn.disabled = false; }, 3000);
}

export function initScan() {
  on("state", renderScanMeta);
  on("scan", renderScanMeta);
  $("#btn-scan").onclick = e => requestScan("cycle", e.currentTarget);
}
