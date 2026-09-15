/* AutoTradeBot dashboard - plain ES modules, no build step.
   Whatever you change here (filters, strategies, scan settings, routing,
   Autopilot) is saved on the server, applied to the bot straight away and
   pushed to every open tab. */
import { api } from "./util.js";
import { S, emit, refreshState } from "./state.js";
import { initUi } from "./ui.js";
import { initTooltips } from "./tooltips.js";
import { initTopbar } from "./topbar.js";
import { initQuit } from "./quit.js";
import { initAutopilot } from "./autopilot.js";
import { initFilters } from "./filters.js";
import { initScan } from "./scan.js";
import { initPlays } from "./plays.js";
import { initBlotter, loadOpen } from "./blotter.js";
import { initStrategies, loadStrategies } from "./strategies.js";
import { loadOrders } from "./orders.js";
import { initSettings } from "./settings.js";
import { connect } from "./events.js";

[initUi, initTooltips, initTopbar, initQuit, initAutopilot, initFilters, initScan, initPlays,
  initBlotter, initStrategies, initSettings].forEach(init => init());

refreshState();
loadStrategies().catch(() => { /* names fall back to their keys */ });
api("/api/plays").then(d => { S.plays = d.plays || []; emit("plays"); }).catch(() => { });
loadOpen();
loadOrders();
connect();
setInterval(refreshState, 15000);
