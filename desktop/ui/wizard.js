"use strict";
// First-run wizard (Phase 18, specification Part 18). Each step's state is read from the system, never stored as
// "done"; the card stays visible until every required step is complete. textContent only. Depends on app.js helpers.

const STEPS = [
  { id: "prereq", title: "Check Windows, Docker Desktop, WSL2, disk space and the local port", where: "System health" },
  { id: "engine", title: "Start the engine (PostgreSQL, Redis, backend, web interface)", where: "Service manager → Start All" },
  { id: "health", title: "Verify health", where: "System health → Check now" },
  { id: "admin", title: "Create the administrator account", where: "First-time setup" },
  { id: "mt5", title: "Detect the MT5 terminal and save its profile", where: "MT5 Manager → Detect terminals" },
  { id: "connector", title: "Link and start the MT5 connector", where: "MT5 Manager → Link connector, Start connector" },
  { id: "demo", title: "Register the DEMO account with the engine", where: "MT5 Manager → Use as engine DEMO account" },
  { id: "ai", title: "Set up an AI provider (optional)", where: "Trading Console → AI Studio", optional: true },
  { id: "backup", title: "Take a first backup", where: "Backup and Recovery → Back up now" },
];

async function safe(cmd, args) {
  try { return await invoke(cmd, args); } catch { return null; }
}

async function evaluateSteps() {
  // One at a time: the supervisor runs a single action at once (a concurrent call would be refused).
  const pre = await safe("prerequisites");
  const svc = await safe("service_status");
  const backups = await safe("backup_list");
  const [health, eng, mt5] = await Promise.all([safe("check_health"), safe("engine_status"), safe("mt5_overview")]);
  const running = (name) => (svc || []).some((x) => x.Service === name && x.State === "running");
  const st = {};
  st.prereq = pre ? pre.every((c) => c.ok) : null;
  st.engine = svc ? ["postgres", "redis", "api", "frontend", "gateway"].every(running) : null;
  st.health = health ? health.every((h) => h.state === "healthy") : null;
  st.admin = eng ? !!eng.admin_exists : null;
  st.mt5 = mt5 ? (mt5.profiles || []).length > 0 : null;
  st.connector = mt5 ? !!(mt5.status && mt5.status.running) && !!(eng && eng.connector && eng.connector.online) : null;
  st.demo = eng ? !!(eng.active_account && eng.active_account.mode === "DEMO" && eng.active_account.verified) : null;
  st.ai = eng ? !!eng.ai_configured : null;
  st.backup = backups ? backups.length > 0 : null;
  return st;
}

async function refreshWizard() {
  const st = await evaluateSteps();
  const list = $("wizard-steps");
  list.replaceChildren();
  let next = null;
  for (const [i, s] of STEPS.entries()) {
    const li = el("li");
    const done = st[s.id];
    li.appendChild(badge(done === true ? "done" : done === false ? (s.optional ? "optional" : "to do") : "unknown",
      done === true ? "ok" : done === false ? (s.optional ? "muted" : "warn") : "muted"));
    li.appendChild(el("span", ` ${i + 1}. ${s.title} `));
    li.appendChild(el("span", `(${s.where})`, "muted"));
    list.appendChild(li);
    if (!next && done !== true && !s.optional) next = s;
  }
  const required = STEPS.filter((s) => !s.optional);
  const complete = required.every((s) => st[s.id] === true);
  $("wizard-next").textContent = complete ? "Setup is complete. Open the Trading Console to continue."
    : `Next: ${next ? next.title + " — " + next.where : "check the steps above"}`;
  $("wizard").classList.toggle("done", complete);
}

function initWizard() {
  $("wizard-refresh").addEventListener("click", refreshWizard);
  refreshWizard();
  setInterval(refreshWizard, 15000);
}

window.addEventListener("DOMContentLoaded", initWizard);
