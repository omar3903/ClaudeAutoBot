/* Autopilot notes: while Autopilot is on, why each play joined or left the board, with
   what Autopilot makes of a new one. Notes can be dismissed one by one or all at once. */
import { $, escapeHtml, fmtClock, pretty } from "./util.js";
import { S } from "./state.js";
import { selectPlay } from "./plays.js";

const MAX_NOTES = 100;

export function addNotes(notes) {
  if (!notes || !notes.length) return;
  S.notes = [...notes.slice().reverse(), ...S.notes].slice(0, MAX_NOTES);
  renderNotes();
}

export function renderNotes() {
  const list = S.notes;
  $("#ap-notes").classList.toggle("hidden", !list.length);
  $("#ap-notes-count").textContent = list.length ? `(${list.length})` : "";
  $("#ap-notes-list").innerHTML = list.map(n => {
    const title = (S.strategies[n.strategy] || {}).title || pretty(n.strategy);
    const onBoard = n.kind === "added" && S.plays.some(p => p.id === n.play_id);
    return `<div class="ap-note ${n.kind}${onBoard ? " clickable" : ""}" data-note="${escapeHtml(n.id)}">
      <button class="note-x" data-note-x="${escapeHtml(n.id)}" title="Dismiss this note">✕</button>
      <div class="note-head"><span class="badge ${n.kind === "added" ? "good" : "warn"}">${n.kind}</span>
        <b>${escapeHtml(n.symbol)}</b> ${escapeHtml(n.side.toLowerCase())} · ${escapeHtml(title)}
        <span class="muted small">${fmtClock(n.at)}</span></div>
      <div class="note-why">${escapeHtml(n.why)}</div>
      ${n.autopilot ? `<div class="note-ap muted small">🤖 ${escapeHtml(n.autopilot)}</div>` : ""}
    </div>`;
  }).join("");
}

export function initNotes() {
  $("#ap-notes-clear").onclick = () => { S.notes = []; renderNotes(); };
  $("#ap-notes-list").addEventListener("click", e => {
    const x = e.target.closest("[data-note-x]");
    if (x) {
      S.notes = S.notes.filter(n => n.id !== x.dataset.noteX);
      renderNotes();
      return;
    }
    const el = e.target.closest("[data-note]");
    const note = el && S.notes.find(n => n.id === el.dataset.note);
    if (note && note.kind === "added" && S.plays.some(p => p.id === note.play_id)) selectPlay(note.play_id);
  });
}
