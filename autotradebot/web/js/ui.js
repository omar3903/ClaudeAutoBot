/* Toasts, the confirm modal and the side drawer. */
import { $ } from "./util.js";
import { S } from "./state.js";

export function toast(text, cls) {
  const d = document.createElement("div");
  d.className = "toast " + (cls || "");
  d.textContent = text;
  $("#toasts").appendChild(d);
  setTimeout(() => d.remove(), 6500);
}
export const toastResult = r =>
  toast(r.ok ? (r.note || "Done") : (r.reason || r.detail || "Failed"), r.ok ? "good" : "bad");

export function openModal({ title, bodyHTML, okText = "Confirm", okClass = "danger", cancelText = "Cancel", onOk }) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = bodyHTML;
  const ok = $("#modal-ok");
  ok.textContent = okText;
  ok.className = okClass;
  ok.onclick = async () => { closeModal(); await onOk(); };
  $("#modal-cancel").textContent = cancelText;
  $("#modal-cancel").onclick = closeModal;
  $("#modal").classList.remove("hidden");
  setTimeout(() => { const i = $("#modal-body input"); if (i) i.focus(); }, 50);
}
export function closeModal() { $("#modal").classList.add("hidden"); }

/** `size`: true for the wide drawer (the default), false for the narrow one, "panel" for the one a trade
    record fills - the whole window but a margin, its chart taking what the details leave. */
export function openDrawer(kind, title, html, size = true) {
  S.drawer = kind;
  $("#drawer-title").textContent = title;
  $("#drawer-body").innerHTML = html;
  $("#drawer-inner").classList.toggle("wide", size === true);
  $("#drawer-inner").classList.toggle("panel", size === "panel");
  $("#drawer").classList.remove("hidden");
}
export const drawerOpen = kind => S.drawer === kind && !$("#drawer").classList.contains("hidden");
export function closeDrawer() {
  $("#drawer").classList.add("hidden");
  S.drawer = null;
  S.recordId = null;
}

export function busy(btn, text) {
  if (!btn) return;
  btn.dataset.label = btn.textContent; btn.textContent = text; btn.disabled = true;
}
export function unbusy(btn) {
  if (!btn) return;
  btn.textContent = btn.dataset.label || btn.textContent; btn.disabled = false;
}

export function initUi() {
  $("#modal").onclick = e => { if (e.target.id === "modal") closeModal(); };
  $("#drawer-close").onclick = closeDrawer;
  $("#drawer").onclick = e => { if (e.target.id === "drawer") closeDrawer(); };
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    if (!$("#modal").classList.contains("hidden")) closeModal();
    else if (!$("#drawer").classList.contains("hidden")) closeDrawer();
  });
}
