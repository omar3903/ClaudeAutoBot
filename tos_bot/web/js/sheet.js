/* A page over the dashboard (Reports, Signals): a card with a header, an optional side column and a
   scrolling body. It closes on its ✕, a click outside it, or Esc (unless a chart is open over it). */
import { $ } from "./util.js";

export const sheetOpen = id => { const box = $(`#${id}`); return !!box && !box.classList.contains("hidden"); };

export function sheet(id, title, { side = false } = {}) {
  let box = $(`#${id}`);
  if (box) return box;
  box = document.createElement("div");
  box.id = id;
  box.className = "sheet hidden";
  box.innerHTML = `<div class="sheet-card" role="dialog" aria-modal="true" aria-labelledby="${id}-title">
    <div class="sheet-head"><h3 id="${id}-title">${title}</h3><span class="muted small" id="${id}-sub"></span>
      <span class="sheet-tools" id="${id}-tools"></span>
      <button class="ghost mini" data-close title="Close (Esc)">✕</button></div>
    <div class="sheet-main ${side ? "with-side" : ""}">${side ? `<nav class="sheet-side" id="${id}-side"></nav>` : ""}
      <div class="sheet-body journal" id="${id}-body"></div></div>
  </div>`;
  document.body.appendChild(box);
  const close = () => box.classList.add("hidden");
  box.addEventListener("click", e => { if (e.target === box || e.target.closest("[data-close]")) close(); });
  document.addEventListener("keydown", e => {
    const chart = $("#chart-modal");
    if (e.key === "Escape" && sheetOpen(id) && (!chart || chart.classList.contains("hidden"))) close();
  });
  return box;
}
