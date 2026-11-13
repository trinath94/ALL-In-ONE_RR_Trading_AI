"use strict";
// MT5 Manager. All dynamic text via textContent. Depends on helpers from app.js (invoke, $, el, kv, runAction).

function slug(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "profile";
}

function mt5Out(text) {
  $("mt5-out").textContent = text;
}

function yn(v) {
  return v === true ? "ON" : v === false ? "OFF" : "—";
}

function money(v, ccy) {
  return v == null ? "—" : `${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${ccy || ""}`.trim();
}

function renderMt5Status(st) {
  const d = (st && st.doc) || {};
  const a = d.account || {}, t = d.terminal || {}, e = d.engine || {}, m = d.mt5 || {}, p = d.profile || {};
  const hb = e.last_heartbeat ? new Date(e.last_heartbeat * (e.last_heartbeat < 1e12 ? 1000 : 1)).toLocaleTimeString() : "—";
  kv($("mt5-status"), [
    ["Connector", st ? `${st.running ? "running" : "not running"} (${st.state})` : "—"],
    ["Profile", p.name ? `${p.name} (expects ${p.expected_login})` : "—"],
    ["Terminal", t.path || "—"],
    ["Terminal connected to broker", yn(t.connected_to_broker)],
    ["Algo Trading", yn(t.algo_trading)],
    ["Broker", a.broker || "—"],
    ["Account", a.login || "—"],
    ["Server", a.server || "—"],
    ["Account mode", a.mode || "—"],
    ["Balance / Equity", `${money(a.balance, a.currency)} / ${money(a.equity, a.currency)}`],
    ["Free margin", money(a.free_margin, a.currency)],
    ["Leverage", a.leverage ? `1:${a.leverage}` : "—"],
    ["Engine link", `${e.state || "—"}${e.latency_ms != null ? ` · ${e.latency_ms} ms` : ""}`],
    ["Last heartbeat", hb],
    ["LIVE on this PC", d.live_authorized ? "AUTHORISED" : "locked"],
    ["PC emergency stop", d.local_kill_switch ? "ACTIVE" : "off"],
    ["Messages", [st && st.detail, m.detail, ...(d.warnings || [])].filter(Boolean).join(" · ") || "—"],
  ]);
}

function renderProfiles(o) {
  const body = $("mt5-profiles");
  body.replaceChildren();
  if (!o.profiles.length) {
    const tr = el("tr"), td = el("td", "No profiles yet. Add one below: choose the terminal and enter the account it is for.", "muted");
    td.colSpan = 6;
    tr.append(td);
    body.append(tr);
  }
  for (const p of o.profiles) {
    const tr = el("tr");
    const use = el("button", p.profile_id === o.active ? "Selected" : "Use", p.profile_id === o.active ? "primary" : "");
    use.type = "button";
    use.disabled = p.profile_id === o.active;
    use.addEventListener("click", async () => {
      try { await invoke("mt5_select_profile", { profileId: p.profile_id }); await refreshMt5(); } catch (e) { mt5Out(String(e)); }
    });
    const del = el("button", "Delete");
    del.type = "button";
    del.addEventListener("click", async () => {
      try { await invoke("mt5_delete_profile", { profileId: p.profile_id }); await refreshMt5(); } catch (e) { mt5Out(String(e)); }
    });
    const login = String(p.expected_login);
    tr.append(el("td").appendChild(use).parentNode, el("td", p.name), el("td", `${p.server}:${p.port}`),
      el("td", "•".repeat(Math.max(0, login.length - 3)) + login.slice(-3)), el("td", p.terminal_exe), el("td").appendChild(del).parentNode);
    body.append(tr);
  }
}

async function refreshMt5() {
  try {
    const o = await invoke("mt5_overview");
    if (!o.available) $("mt5-note").textContent = `MT5 connector unavailable: ${o.unavailable_reason}`;
    renderProfiles(o);
    renderMt5Status(o.status);
    for (const id of ["mt5-link", "mt5-start", "mt5-stop", "mt5-detect"]) $(id).disabled = !o.available;
  } catch (e) {
    mt5Out(String(e));
  }
}

function initMt5() {
  $("mt5-detect").addEventListener("click", async () => {
    mt5Out("Detecting MetaTrader 5 terminals…");
    try {
      const list = await invoke("mt5_detect");
      const dl = $("f-exes");
      dl.replaceChildren(...list.map((t) => Object.assign(el("option"), { value: t.exe })));
      mt5Out(list.length ? list.map((t) => `${t.running ? "running " : "stopped "} ${t.exe}`).join("\n")
        : "No MetaTrader 5 terminal found. Install it from your broker, log in, then detect again.");
    } catch (e) {
      mt5Out(String(e));
    }
  });
  $("mt5-link").addEventListener("click", async () => {
    mt5Out("Linking the connector to the local engine…");
    try {
      const r = await invoke("mt5_link");
      mt5Out(`Linked as agent ${r.agent_id}. Read-only credential issued: ${r.read_only_credential ? "yes" : "no"}.`);
    } catch (e) {
      mt5Out(String(e));
    }
  });
  $("mt5-start").addEventListener("click", () => runAction("start_mt5_connector").then(refreshMt5));
  $("mt5-stop").addEventListener("click", () => runAction("stop_mt5_connector").then(refreshMt5));
  $("mt5-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const name = $("f-name").value.trim();
    const profile = { profile_id: slug(name), name, terminal_exe: $("f-exe").value.trim(), server: $("f-server").value.trim(),
      port: Number($("f-port").value || 443), expected_login: Number($("f-login").value) };
    try {
      await invoke("mt5_save_profile", { profile });
      mt5Out(`Saved profile "${name}". Select it with "Use" to make it active.`);
      await refreshMt5();
    } catch (e) {
      mt5Out(String(e));
    }
  });
  refreshMt5();
  setInterval(refreshMt5, 3000);
}

window.addEventListener("DOMContentLoaded", initMt5);
