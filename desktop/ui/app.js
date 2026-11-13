"use strict";
// RR Trading One Control Centre. All dynamic text is set with textContent (never innerHTML).

const invoke = (cmd, args) => window.__TAURI__.core.invoke(cmd, args);
const $ = (id) => document.getElementById(id);

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}

function kv(dl, pairs) {
  dl.replaceChildren();
  for (const [k, v] of pairs) {
    dl.append(el("dt", k), el("dd", v));
  }
}

function badge(text, kind) {
  return el("span", text, "badge " + kind);
}

const LABELS = {
  start_all: "Start All", stop_all: "Stop All", restart_all: "Restart All",
  start_backend: "Start Backend", stop_backend: "Stop Backend",
  start_mt5_connector: "Start MT5 Connector", stop_mt5_connector: "Stop MT5 Connector",
  start_learner: "Start Learner", stop_learner: "Stop Learner",
  start_trading_worker: "Start Trading Worker", stop_trading_worker: "Stop Trading Worker",
  system_health: "System Health", recovery: "Recovery",
};

function describeStep(s) {
  const names = (xs) => xs.join(", ");
  switch (s.step) {
    case "check_prerequisites": return "Check prerequisites (Windows, Docker Desktop, WSL2, disk, ports)";
    case "start_services": return "Start: " + names(s.services);
    case "stop_services": return "Stop (in order): " + names(s.services);
    case "run_migrations": return "Back up database, then apply pending migrations";
    case "verify_health": return "Verify health (real checks)";
    case "restart_unhealthy": return "Restart services that failed health checks (never the trading worker)";
    default: return s.step;
  }
}

function showPlan(text) {
  $("plan").textContent = text;
}

async function runAction(id, confirmation) {
  try {
    const preview = await invoke("plan_action", { action: id, confirmation: confirmation ?? null });
    const lines = [`${LABELS[id] || id}: plan`, ...preview.steps.map((s, i) => `  ${i + 1}. ${describeStep(s)}`)];
    const out = await invoke("run_action", { action: id, confirmation: confirmation ?? null });
    lines.push("", out.message);
    showPlan(lines.join("\n"));
    if (out.health) renderHealth(out.health);
  } catch (e) {
    showPlan(String(e));
  }
}

function renderHealth(results) {
  const body = $("health");
  body.replaceChildren();
  for (const r of results) {
    const tr = el("tr");
    tr.append(el("td", r.target === "engine" ? "Engine API + PostgreSQL + Redis" : "Web interface"),
      el("td", r.state.replace("_", " "), "st-" + r.state), el("td", r.detail),
      el("td", r.latency_ms == null ? "—" : (r.latency_ms < 1 ? "<1 ms" : r.latency_ms + " ms")));
    body.append(tr);
  }
}

async function checkHealth() {
  $("btn-health").disabled = true;
  try {
    renderHealth(await invoke("check_health"));
  } catch (e) {
    const tr = el("tr");
    const td = el("td", String(e), "muted");
    td.colSpan = 4;
    tr.append(td);
    $("health").replaceChildren(tr);
  } finally {
    $("btn-health").disabled = false;
  }
}

async function init() {
  const o = await invoke("get_overview");
  $("version").textContent = "v" + o.version;

  const s = o.initial_safety_state;
  const b = $("badges");
  b.replaceChildren(
    badge(o.settings.operating_mode === "local" ? "LOCAL WINDOWS MODE" : "REMOTE ENGINE MODE", "ok"),
    badge(s.account_mode, "ok"),
    badge(s.auto_trading ? "AUTO TRADING ON" : "AUTO TRADING OFF", s.auto_trading ? "bad" : "ok"),
    badge(s.trading_worker_running ? "WORKER RUNNING" : "WORKER STOPPED", s.trading_worker_running ? "warn" : "ok"),
    badge(s.live_locked ? "LIVE LOCKED" : "LIVE UNLOCKED", s.live_locked ? "ok" : "bad"),
  );
  kv($("safety"), [
    ["Account mode", s.account_mode],
    ["Auto trading", s.auto_trading ? "ON" : "OFF"],
    ["Trading worker", s.trading_worker_running ? "RUNNING" : "STOPPED"],
    ["LIVE trading", s.live_locked ? "LOCKED (admin approval + typed confirmation required in the engine)" : "UNLOCKED"],
  ]);

  const banner = $("banner");
  const problems = [o.paths_error && "Directories: " + o.paths_error, o.settings_error && "Settings: " + o.settings_error
    + " (defaults in use; the file was not modified)"].filter(Boolean);
  if (problems.length) {
    banner.textContent = problems.join(" — ");
    banner.hidden = false;
    banner.classList.add("bad");
  }

  const svc = $("services");
  svc.replaceChildren();
  for (const x of o.services) {
    const tr = el("tr");
    tr.append(el("td", x.label), el("td", x.runtime === "compose" ? "Docker (local)" : "Windows user session (native)"));
    svc.append(tr);
  }

  $("supervisor-note").textContent = o.supervisor_available
    ? "Every action runs a fixed, allowlisted plan."
    : `Every action runs a fixed, allowlisted plan. Service control is delivered in Phase ${o.supervisor_phase}; until then actions show and validate their plan without starting or stopping anything.`;

  const acts = $("actions");
  acts.replaceChildren();
  for (const a of o.actions) {
    const btn = el("button", LABELS[a.id] || a.id, a.requires_confirmation ? "danger" : "");
    btn.type = "button";
    btn.addEventListener("click", () => {
      if (a.requires_confirmation) {
        $("confirm").hidden = false;
        $("phrase").value = "";
        $("phrase").focus();
      } else {
        runAction(a.id);
      }
    });
    acts.append(btn);
  }
  $("phrase-text").textContent = o.worker_confirmation_phrase;
  $("btn-confirm").addEventListener("click", () => {
    const v = $("phrase").value;
    $("confirm").hidden = true;
    runAction("start_trading_worker", v);
  });
  $("btn-cancel").addEventListener("click", () => { $("confirm").hidden = true; });

  const p = o.paths;
  kv($("paths"), [
    ["Engine address", o.endpoint ? o.endpoint.base_url : "—"],
    ["Installation", p ? p.install_dir : "—"],
    ["Application data", p ? p.data_dir : "—"],
    ["User settings", p ? p.settings_dir : "—"],
    ["Database & cache", "Docker named volumes (kept on upgrade and uninstall)"],
    ["Secrets", "Windows Credential Manager (never in the installation folder)"],
  ]);

  $("btn-health").addEventListener("click", checkHealth);
  $("btn-console").addEventListener("click", async () => {
    try {
      await invoke("open_console");
    } catch (e) {
      showPlan(String(e));
    }
  });
  checkHealth();
}

window.addEventListener("DOMContentLoaded", () => {
  init().catch((e) => {
    const banner = $("banner");
    banner.textContent = "Failed to initialise: " + e;
    banner.hidden = false;
    banner.classList.add("bad");
  });
});
