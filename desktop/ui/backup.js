"use strict";
// Backup and Recovery (Phase 17). All dynamic text via textContent. Depends on helpers from app.js
// (invoke, $, el, describeStep, OUTCOME, runAction). Restoring needs the typed phrase, checked again in the supervisor.

const KIND_LABEL = { manual: "on request", "pre-migrate": "before a migration", "pre-restore": "before a restore",
  import: "from the Ubuntu server" };
let importId = null;
let restoreId = null;

function bkOut(text) {
  $("bk-out").textContent = text;
}

function size(bytes) {
  return bytes >= 1e6 ? (bytes / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(bytes / 1e3)) + " kB";
}

async function refreshBackups() {
  const body = $("backups");
  let list;
  try {
    list = await invoke("backup_list");
  } catch (e) {
    body.replaceChildren(el("tr"));
    body.firstChild.appendChild(el("td", String(e), "muted")).colSpan = 5;
    return;
  }
  body.replaceChildren();
  if (!list.length) {
    const tr = el("tr");
    tr.appendChild(el("td", "No backups yet. One is taken automatically before every migration.", "muted")).colSpan = 5;
    body.appendChild(tr);
    return;
  }
  for (const b of list) {
    const tr = el("tr");
    tr.appendChild(el("td", b.id));
    tr.appendChild(el("td", KIND_LABEL[b.kind] || b.kind));
    tr.appendChild(el("td", new Date(b.created * 1000).toLocaleString()));
    tr.appendChild(el("td", size(b.bytes)));
    const td = el("td");
    const btn = el("button", b.kind === "import" ? "Import…" : "Restore…");
    btn.type = "button";
    btn.addEventListener("click", () => {
      if (b.kind === "import") {
        importId = b.id;
        $("imp-id").textContent = b.id;
        $("imp-phrase").value = "";
        $("imp-confirm").hidden = false;
        return;
      }
      restoreId = b.id;
      $("bk-id").textContent = b.id;
      $("bk-phrase").value = "";
      $("bk-confirm").hidden = false;
      $("bk-phrase").focus();
    });
    td.appendChild(btn);
    tr.appendChild(td);
    body.appendChild(tr);
  }
}

async function restoreBackup() {
  const id = restoreId;
  const phrase = $("bk-phrase").value;
  $("bk-confirm").hidden = true;
  bkOut(`Restoring ${id}: running… (the engine is stopped during the restore)`);
  try {
    const rep = await invoke("backup_restore", { backup: id, confirmation: phrase });
    const lines = [`Restore ${id}: ${rep.ok ? "completed" : "FAILED"}`];
    rep.steps.forEach((s, i) => {
      lines.push(`  ${i + 1}. [${OUTCOME[s.outcome] || s.outcome}] ${describeStep(s.step)} (${(s.duration_ms / 1000).toFixed(1)} s)`);
      if (s.detail) lines.push(`        ${s.detail}`);
    });
    bkOut(lines.join("\n"));
  } catch (e) {
    bkOut(`Restore ${id}: ${e}`);
  }
  refreshBackups();
}

async function addImport() {
  const path = $("imp-path").value.trim();
  if (!path) return;
  try {
    importId = await invoke("backup_import_file", { path });
    $("imp-id").textContent = importId;
    $("imp-phrase").value = "";
    $("imp-confirm").hidden = false;
    bkOut(`Added ${importId}. Type the phrase to import it.`);
    refreshBackups();
  } catch (e) {
    bkOut(`Not added: ${e}`);
  }
}

async function runImport() {
  const id = importId;
  const phrase = $("imp-phrase").value;
  $("imp-confirm").hidden = true;
  bkOut(`Importing ${id}: running… (verification first; the engine is stopped during the restore)`);
  try {
    const rep = await invoke("backup_import", { backup: id, confirmation: phrase });
    const lines = [`Import ${id}: ${rep.ok ? "completed and verified" : "FAILED"}`];
    rep.steps.forEach((s, i) => {
      lines.push(`  ${i + 1}. [${OUTCOME[s.outcome] || s.outcome}] ${describeStep(s.step)} (${(s.duration_ms / 1000).toFixed(1)} s)`);
      if (s.detail) lines.push(`        ${s.detail}`);
    });
    bkOut(lines.join("\n"));
  } catch (e) {
    bkOut(`Import ${id}: ${e}`);
  }
  refreshBackups();
}

function initBackup() {
  $("bk-refresh").addEventListener("click", refreshBackups);
  $("bk-now").addEventListener("click", async () => {
    await runAction("backup_now");
    refreshBackups();
  });
  $("bk-go").addEventListener("click", restoreBackup);
  $("imp-add").addEventListener("click", addImport);
  $("imp-go").addEventListener("click", runImport);
  $("imp-cancel").addEventListener("click", () => { $("imp-confirm").hidden = true; });
  $("bk-cancel").addEventListener("click", () => { $("bk-confirm").hidden = true; restoreId = null; });
  refreshBackups();
}

window.addEventListener("DOMContentLoaded", initBackup);
