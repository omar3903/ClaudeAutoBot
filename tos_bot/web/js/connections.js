/* Connections: where orders go, and the IB Gateway settings. */
import { $, $$, escapeHtml, getLocal, post } from "./util.js";
import { busy, drawerOpen, openDrawer, toast, toastResult, unbusy } from "./ui.js";

export async function openConnections() {
  if (!drawerOpen("connections")) openDrawer("connections", "Connections", `<p class="muted">Loading…</p>`);
  let d;
  try { d = await getLocal("/api/setup"); } catch (e) {
    // a refusal (the same-machine guard's, say) is shown in the server's own words
    if (drawerOpen("connections")) $("#drawer-body").innerHTML = `<p class="reasons">${escapeHtml(e.status
      ? e.message : `Couldn't load the connection settings - ${e.message}.`)}</p>`;
    return;
  }
  if (!drawerOpen("connections")) return;                 // closed or replaced while loading
  render(d);
}

function render(d) {
  const v = d.venue, ib = d.ibkr;
  const platforms = Object.entries(v.paper_platforms).map(([key, label]) =>
    `<label class="radio"><input type="radio" name="paper_platform" value="${key}" ${key === v.paper_platform ? "checked" : ""}> ${escapeHtml(label)}</label>`).join("");
  const port = (label, p, open) => `<span class="badge ${open ? "good" : "bad"}">${label} port ${p}: ${open ? "listening" : "closed"}</span>`;
  const problems = [...(v.blockers || []), ...(v.live_blockers || [])];

  $("#drawer-body").innerHTML = `
    <section class="conn">
      <h4>Where orders go</h4>
      <div class="conn-now">Right now <b>${v.mode === "live" ? "LIVE" : "paper"}</b> orders go to <b>${escapeHtml(v.trading_on_label)}</b>.
        Live orders always go to your live IBKR account.</div>
      <div class="lockable"><span class="group-label">Paper trades on</span>${platforms}</div>
      ${problems.length ? `<div class="reasons">⚠ ${problems.map(escapeHtml).join("<br>")}</div>` : ""}
      <div class="row-gap"><button class="ghost mini" id="conn-reconnect">Reconnect</button>
        <span class="muted small">Switching is blocked while positions are open on the current platform.</span></div>
    </section>

    <section class="conn">
      <h4>Interactive Brokers <span class="muted">· the running IB Gateway is the login and the price feed</span></h4>
      <div class="status-line">${port("Paper", ib.ports.paper, ib.listening.paper)} ${port("Live", ib.ports.live, ib.listening.live)}
        ${ib.installed ? "" : '<span class="badge bad">ib_async not installed</span>'}</div>
      <form class="field-grid" id="form-ibkr" onsubmit="return false">${d.fields.map(fieldHTML).join("")}</form>
      <div class="row-gap">
        <button class="mini" id="ibkr-save">Save</button>
        <button class="ghost mini" data-probe="paper">Test paper</button>
        <button class="ghost mini" data-probe="live">Test live</button>
      </div>
      <div class="result hidden" id="ibkr-result"></div>
      <details><summary>Setup steps</summary><ol class="steps">${ib.steps.map(s => `<li>${escapeHtml(s)}</li>`).join("")}</ol></details>
    </section>

    <section class="conn">
      <h4>Company news <span class="muted">· optional, for the signals</span></h4>
      <p class="muted small">Insider trades and 8-K filings come from SEC, and headlines from IBKR, without any key.
        A free Finnhub key adds more company news.</p>
      <form class="field-grid" id="form-signals" onsubmit="return false">${(d.signal_fields || []).map(fieldHTML).join("")}</form>
      <div class="row-gap"><button class="mini" id="signals-save">Save</button></div>
    </section>

    <p class="muted small">Settings are saved to <code>.env</code> on this computer. The account id is never shown again — only its
      last 4 characters. These settings can only be changed from this machine.</p>`;

  $$('input[name="paper_platform"]').forEach(r => { r.onchange = () => savePlatform(r.value); });
  $("#conn-reconnect").onclick = async e => {
    busy(e.currentTarget, "Reconnecting…");
    toastResult(await post("/api/setup/reconnect"));
    openConnections();
  };
  $("#ibkr-save").onclick = e => saveFields(e.currentTarget);
  $("#signals-save").onclick = e => saveFields(e.currentTarget, "#form-signals");
  $$("[data-probe]").forEach(b => { b.onclick = () => probe(b); });
  $$(".field-clear").forEach(a => {
    a.onclick = ev => {
      ev.preventDefault();
      const input = $(`[data-key="${a.dataset.clear}"]`);
      input.dataset.clear = "1"; input.value = ""; input.placeholder = "will be cleared when you save";
    };
  });
}

function fieldHTML(f) {
  const id = `fld-${f.key}`;
  let input, orig;
  if (f.kind === "bool") {
    orig = ["1", "true", "yes", "on"].includes(String(f.value || f.default).toLowerCase()) ? "1" : "0";
    input = `<input type="checkbox" id="${id}" data-key="${f.key}" data-orig="${orig}" ${orig === "1" ? "checked" : ""}>`;
  } else if (f.kind === "choice") {
    orig = f.value || f.default;
    input = `<select id="${id}" data-key="${f.key}" data-orig="${escapeHtml(orig)}">${
      f.choices.map(c => `<option ${c === orig ? "selected" : ""}>${escapeHtml(c)}</option>`).join("")}</select>`;
  } else if (f.secret) {
    input = `<input type="password" id="${id}" data-key="${f.key}" autocomplete="new-password" spellcheck="false"
      placeholder="${f.set ? `saved ${escapeHtml(f.hint || "")} — type to replace` : "not set"}">`;
  } else {
    orig = f.value || "";
    input = `<input type="${f.kind === "int" ? "number" : "text"}" id="${id}" data-key="${f.key}" spellcheck="false"
      data-orig="${escapeHtml(orig)}" value="${escapeHtml(orig)}" placeholder="${escapeHtml(f.default || "")}">`;
  }
  const clear = f.secret && f.set ? ` <a href="#" class="field-clear" data-clear="${f.key}">clear</a>` : "";
  return `<label for="${id}">${escapeHtml(f.label)}${clear}</label>
    <div>${input}${f.help ? `<div class="help">${escapeHtml(f.help)}</div>` : ""}</div>`;
}

async function saveFields(btn, form = "#form-ibkr") {
  const values = {};
  $$(`${form} [data-key]`).forEach(i => {
    const key = i.dataset.key;
    if (i.type === "password") {
      if (i.dataset.clear === "1") values[key] = "";
      else if (i.value.trim()) values[key] = i.value.trim();
    } else if (i.type === "checkbox") {
      if ((i.checked ? "1" : "0") !== i.dataset.orig) values[key] = i.checked;
    } else if (i.value !== i.dataset.orig) {
      values[key] = i.value;
    }
  });
  if (!Object.keys(values).length) { toast("Nothing changed", "warn"); return; }
  busy(btn, "Saving…");
  const r = await post("/api/setup/secrets", { values });
  toastResult(r);
  if (r.ok) openConnections(); else unbusy(btn);
}

async function savePlatform(platform) {
  toastResult(await post("/api/setup/paper-platform", { paper_platform: platform }));
  openConnections();
}

async function probe(btn) {
  const out = $("#ibkr-result");
  busy(btn, "Testing…");
  out.className = "result";
  out.textContent = `Connecting to the ${btn.dataset.probe} Gateway (read-only)…`;
  const r = await post("/api/setup/ibkr/test", { account: btn.dataset.probe });
  unbusy(btn);
  out.className = "result " + (r.ok ? "good" : "bad");
  out.textContent = r.ok ? r.note : (r.reason || r.detail || "Test failed");
}
