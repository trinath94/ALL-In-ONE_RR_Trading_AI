"use strict";
// Live engine state, first-run administrator, DEMO account registration. textContent only.

function setBadges(s, live) {
  const st = s.state;
  const b = $("badges");
  const items = [
    badge("LOCAL WINDOWS MODE", "ok"),
    badge(st.active_mode, st.active_mode === "DEMO" ? "ok" : "bad"),
    badge(st.auto_trading ? "AUTO TRADING ON" : "AUTO TRADING OFF", st.auto_trading ? "bad" : "ok"),
    badge(s.roles.trading ? "WORKER RUNNING" : "WORKER STOPPED", s.roles.trading ? "warn" : "ok"),
    badge(s.live_accounts_approved ? "LIVE APPROVED" : "LIVE LOCKED", s.live_accounts_approved ? "bad" : "ok"),
  ];
  if (st.kill_switch) items.push(badge("KILL SWITCH", "bad"));
  if (st.risk_lock) items.push(badge("RISK LOCK", "bad"));
  b.replaceChildren(...items);
  b.title = live ? "Live values from the engine" : "";
}

function renderLiveSafety(s) {
  const st = s.state, a = s.active_account, c = s.connector;
  const on = (v) => (v ? "yes" : "no");
  kv($("safety"), [
    ["Account mode", st.active_mode],
    ["Auto trading", st.auto_trading ? "ON" : "OFF"],
    ["Trading worker", s.roles.trading ? "RUNNING" : "STOPPED"],
    ["Kill switch", st.kill_switch ? `ACTIVE ${st.kill_reason ? "(" + st.kill_reason + ")" : ""}` : "off"],
    ["Risk lock", st.risk_lock ? `LOCKED ${st.risk_lock_reason ? "(" + st.risk_lock_reason + ")" : ""}` : "off"],
    ["New entries paused", on(st.pause_new_entries)],
    ["Startup checks passed", on(st.startup_checks_passed)],
    ["Active account", a ? `${a.mode} ${a.login} @ ${a.server}${a.verified ? " (verified)" : ""}` : "none configured"],
    ["LIVE accounts approved", String(s.live_accounts_approved)],
    ["MT5 connector", c ? `${c.online ? "online" : "offline"} · ${c.account_type || "—"} ${c.login || ""} @ ${c.server || "—"}` : "not linked"],
    ["LIVE on this PC", c && c.live_authorized_on_pc ? "AUTHORISED" : "locked"],
    ["Engine roles", Object.entries(s.roles).map(([k, v]) => `${k} ${v ? "running" : "stopped"}`).join(" · ")],
    ["Symbols", `${s.symbols.discovered} discovered, ${s.symbols.scan_enabled} scan-enabled`],
  ]);
}

async function refreshEngine() {
  try {
    const s = await invoke("engine_status");
    $("safety-note").textContent = "Live values from the engine. They are enforced by the engine (RBAC, Risk Engine) and the "
      + "MT5 connector's LIVE lock; this window cannot change them.";
    renderLiveSafety(s);
    setBadges(s, true);
    $("setup").hidden = s.admin_exists;
  } catch (e) {
    $("safety-note").textContent = `Engine not reachable (${e}). Showing how RR Trading One ships; these are not live values.`;
    $("setup").hidden = true;
  }
}

function initEngine() {
  $("setup-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const user = $("s-user").value.trim(), p1 = $("s-pass").value, p2 = $("s-pass2").value;
    if (p1 !== p2) { $("setup-out").textContent = "The passwords do not match."; return; }
    try {
      const r = await invoke("engine_bootstrap_admin", { username: user, password: p1 });
      $("setup-out").textContent = `Administrator "${r.username}" created. Sign in with it in the Trading Console.`;
      await refreshEngine();
    } catch (e) {
      $("setup-out").textContent = String(e);
    } finally {
      $("s-pass").value = ""; $("s-pass2").value = "";
    }
  });
  $("mt5-use-demo").addEventListener("click", async () => {
    try {
      const r = await invoke("engine_use_demo_account");
      mt5Out(`Engine now uses DEMO account ${r.login} @ ${r.server}${r.created ? " (registered)" : ""}. `
        + "Auto trading stays OFF; the monitor re-runs its startup checks.");
      await refreshEngine();
    } catch (e) {
      mt5Out(String(e));
    }
  });
  refreshEngine();
  setInterval(refreshEngine, 5000);
}

window.addEventListener("DOMContentLoaded", initEngine);
