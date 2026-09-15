/* What the dashboard knows, shared by the panels, and a small change notifier
   so a panel can react to a change without importing the others. */
import { api } from "./util.js";

export const S = {
  state: {},          // the engine snapshot (/api/state and account.snapshot events)
  plays: [],
  strategies: {},     // strategy key -> catalog row (title, thesis, on/off, weight)
  selected: null,     // play shown in the detail panel
  drawer: null,       // what the side drawer shows: connections | strategies | settings | record
  recordId: null,     // trade shown in the record drawer
  scanRun: null,      // {kind, stage, done, total} while a scan runs
  orders: { orders: [], ok: false },   // what's working at the broker (/api/orders, orders.updated)
  notes: [],          // Autopilot notes: why plays joined or left the board (plays.changes)
  stopped: false,     // the app has shut down
};

const listeners = new Map();

/** Topics: state, plays, filters, strategies, scan, orders. */
export function on(topic, fn) {
  if (!listeners.has(topic)) listeners.set(topic, []);
  listeners.get(topic).push(fn);
}
export function emit(topic, payload) {
  (listeners.get(topic) || []).forEach(fn => fn(payload));
}

export function setState(snapshot) {
  S.state = snapshot || {};
  emit("state");
}
export async function refreshState() {
  if (S.stopped) return;
  try { setState(await api("/api/state")); } catch { /* server restarting - the websocket reconnect catches up */ }
}
