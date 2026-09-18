/* Quitting: the dialog, the lock while positions close, the shut-down screen. */
import { $, VENUE_SHORT, escapeHtml, getLocal, plural, positionList, post } from "./util.js";
import { S, on, refreshState } from "./state.js";
import { closeDrawer, closeModal, openModal, toast, toastResult } from "./ui.js";
import { hideTip } from "./tooltips.js";

export function renderLock() {
  const q = S.state.quit;
  document.body.classList.toggle("locked", !!q);
  $("#btn-quit").disabled = !!q;
  const b = $("#quit-banner");
  if (!q) { b.classList.add("hidden"); return; }
  const symbols = (q.symbols || []).map(escapeHtml).join(", ");
  b.innerHTML = q.left
    ? `<span>⏻ Quitting — closing ${plural(q.left, "open position")}${symbols ? ` (${symbols})` : ""}. Nothing else can change until
       ${q.left === 1 ? "it's" : "they're all"} out; then the app ${q.reset_sim ? "resets the simulator and " : ""}shuts down.
       You can still exit positions yourself.</span>`
    : `<span>⏻ Quitting — all positions are closed, finishing up…</span>`;
  b.classList.remove("hidden");
}

export function openQuitDialog(pv) {
  if (pv.quitting) { refreshState(); toast("Already closing positions before quitting", "warn"); return; }
  const n = pv.left || 0;
  const parked = (pv.parked || []).length
    ? `<p class="muted">Not touched — held on another platform: ${pv.parked.map(t =>
      `${escapeHtml(t.symbol)} (${VENUE_SHORT[t.venue] || escapeHtml(t.venue)})`).join(", ")}.</p>` : "";
  const keep = pv.keepable || [];
  if (keep.length) {
    const rest = n - keep.length;
    openModal({
      title: "Quit AutoTradeBot?",
      bodyHTML: `<p><b>${plural(keep.length, "swing position")}</b> on ${escapeHtml(pv.venue_label)} ${keep.length === 1 ? "has" : "have"} a stop order
        resting at the broker, so ${keep.length === 1 ? "it" : "they"} can stay open while the app is off:</p>${positionList(keep)}
        <p><button class="long" id="quit-keep">Keep ${keep.length === 1 ? "it" : "them"} open &amp; quit</button></p>
        <p class="muted">The broker's stops protect them until the app is back; it picks them up again when it starts. Targets and
        trailing are not worked while it is off.${rest > 0 ? ` The other ${plural(rest, "position")} (day trades, pair legs, or without a stop at the broker) ${rest === 1 ? "is" : "are"} closed first.` : ""}</p>
        <p><b>Close all &amp; quit</b> sends a market order for every position instead${pv.resets_simulator ? " and resets the simulator" : ""}.</p>${parked}`,
      okText: "Close all & quit", okClass: "danger", cancelText: "Cancel",
      onOk: () => sendQuit(true),
    });
    $("#quit-keep").onclick = () => { closeModal(); sendQuit(true, true); };
  } else if (pv.paper) {
    openModal({
      title: "Quit AutoTradeBot?",
      bodyHTML: `${n ? `<p>Every open paper position on ${escapeHtml(pv.venue_label)} is closed first:</p>${positionList(pv.positions)}` : "<p>No open paper positions.</p>"}
        <p>${pv.resets_simulator ? `Then the simulator is reset to $${Number(pv.reset_cash || 0).toLocaleString()} and the app shuts down.` : "Then the app shuts down."}</p>
        ${n ? '<p class="muted">Until the last position is out, nothing else can be changed.</p>' : ""}${parked}`,
      okText: n ? "Close all & quit" : "Quit", okClass: "danger",
      onOk: () => sendQuit(true),
    });
  } else if (n) {
    openModal({
      title: "Quit with LIVE positions open?",
      bodyHTML: `<div class="warn-box">${plural(n, "live position")} open on <b>${escapeHtml(pv.venue_label)}</b>.</div>${positionList(pv.positions)}
        <p><b>Exit all &amp; quit</b> sends a market order for each and shuts down once they've all closed. Nothing else can change meanwhile.</p>
        <p><b>Cancel</b> keeps them open and the app running, so their stops and targets stay managed.</p>${parked}`,
      okText: "Exit all & quit", okClass: "danger", cancelText: "Cancel",
      onOk: () => sendQuit(true),
    });
  } else {
    openModal({
      title: "Quit AutoTradeBot?",
      bodyHTML: `<p>No open live positions — the app shuts down.</p>${parked}`,
      okText: "Quit", okClass: "danger",
      onOk: () => sendQuit(true),
    });
  }
}

async function sendQuit(closeAll, keep = false) {
  const r = await post("/api/quit", { close_all: closeAll, keep });
  toastResult(r);
  if (r.quit) { S.state.quit = r.quit; renderLock(); }
}

export function showShutdown(note) {
  if (S.stopped) return;
  S.stopped = true;
  closeModal(); closeDrawer(); hideTip();
  const d = document.createElement("div");
  d.className = "shutdown";
  d.innerHTML = `<div class="card"><h2>AutoTradeBot has shut down</h2>
    <p>${escapeHtml(note || "")}</p>
    <p class="muted">You can close this tab. Start it again with <code>python run.py</code>.</p></div>`;
  document.body.appendChild(d);
}

export function initQuit() {
  on("state", renderLock);
  $("#btn-quit").onclick = async () => {
    let pv;
    try { pv = await getLocal("/api/quit"); } catch { toast("The app isn't reachable", "bad"); return; }
    if (pv.detail) { toast(pv.detail, "bad"); return; }
    openQuitDialog(pv);
  };
}
