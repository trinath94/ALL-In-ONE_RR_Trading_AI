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

const OUTCOME = { ok: "OK     ", failed: "FAILED ", skipped: "SKIPPED" };
let SERVICES = [];
let running = false;

function composeName(id) {
  return id === "mt5_connector" ? null : id.replace(/_/g, "-");
}

function setBusy(on) {
  running = on;
  for (const b of $("actions").querySelectorAll("button")) b.disabled = on;
}

async function runAction(id, confirmation) {
  if (running) return;
  let preview;
  try {
    preview = await invoke("plan_action", { action: id, confirmation: confirmation ?? null });
  } catch (e) {
    showPlan(String(e));
    return;
  }
  const title = `${LABELS[id] || id}`;
  showPlan([`${title}: running…`, ...preview.steps.map((s, i) => `  ${i + 1}. ${describeStep(s)}`)].join("\n"));
  setBusy(true);
  try {
    const rep = await invoke("run_action", { action: id, confirmation: confirmation ?? null });
    const lines = [`${title}: ${rep.ok ? "completed" : "FAILED"}`];
    rep.steps.forEach((s, i) => {
      lines.push(`  ${i + 1}. [${OUTCOME[s.outcome] || s.outcome}] ${describeStep(s.step)} (${(s.duration_ms / 1000).toFixed(1)} s)`);
      if (s.detail) lines.push(`        ${s.detail}`);
    });
    showPlan(lines.join("\n"));
    renderServices(rep.services);
    if (rep.engine) renderHealth([rep.engine]);
  } catch (e) {
    showPlan(`${title}: ${e}`);
  } finally {
    setBusy(false);
  }
}

function renderServices(statuses) {
  const byName = new Map((statuses || []).map((x) => [x.Service, x]));
  const svc = $("services");
  svc.replaceChildren();
  for (const x of SERVICES) {
    const name = composeName(x.id);
    const st = name ? byName.get(name) : null;
    let state = "—", cls = "";
    if (x.id === "mt5_connector") state = "see MT5 Manager";
    else if (st) {
      state = st.State + (st.Health ? ` / ${st.Health}` : "") + (st.State === "exited" ? ` (exit ${st.ExitCode})` : "");
      cls = st.Health === "unhealthy" || st.State === "restarting" || (st.State === "exited" && st.ExitCode !== 0)
        ? "st-unreachable" : (st.State === "running" ? "st-healthy" : "");
    } else if (statuses) state = "not created";
    const tr = el("tr");
    tr.append(el("td", x.label), el("td", x.runtime === "compose" ? "Docker (local)" : "Windows user session (native)"),
      el("td", state, cls));
    svc.append(tr);
  }
}

async function refreshServices() {
  try {
    renderServices(await invoke("service_status"));
  } catch (e) {
    renderServices(null);
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

  SERVICES = o.services;
  renderServices(null);

  $("supervisor-note").textContent = o.supervisor_unavailable
    ? `Service control unavailable: ${o.supervisor_unavailable}`
    : "Every action runs a fixed, allowlisted plan of Docker Compose steps (no shell). The trading worker is only ever started by its own button.";

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

  $("btn-health").addEventListener("click", () => { checkHealth(); refreshServices(); });
  $("btn-console").addEventListener("click", async () => {
    try {
      await invoke("open_console");
    } catch (e) {
      showPlan(String(e));
    }
  });
  checkHealth();
  if (!o.supervisor_unavailable) refreshServices();
}

window.addEventListener("DOMContentLoaded", () => {
  init().catch((e) => {
    const banner = $("banner");
    banner.textContent = "Failed to initialise: " + e;
    banner.hidden = false;
    banner.classList.add("bad");
  });
});
