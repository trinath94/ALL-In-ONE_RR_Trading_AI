"use strict";
// Security section (Phase 21): show the backup key (typed phrase), add another installation's key, rotate the access
// secrets (typed phrase). All dynamic text via textContent. Phrases are checked again by the supervisor.

function secOut(text) {
  $("sec-out").textContent = text;
}

async function showKey() {
  const phrase = $("key-phrase").value;
  $("key-phrase").value = "";
  try {
    const k = await invoke("backup_key_export", { confirmation: phrase });
    $("key-out").textContent = `${k.key}\n(key id ${k.id}) Store it offline. It is not shown again until you ask.`;
    setTimeout(() => { $("key-out").textContent = ""; }, 120000);   // do not leave it on screen
  } catch (e) {
    $("key-out").textContent = String(e);
  }
}

async function addKey() {
  const key = $("key-add").value;
  $("key-add").value = "";
  if (!key.trim()) return;
  try {
    const id = await invoke("backup_key_add", { key });
    secOut(`Backup key ${id} added. Backups encrypted with it can now be restored here.`);
    if (typeof refreshBackups === "function") refreshBackups();
  } catch (e) {
    secOut(`Not added: ${e}`);
  }
}

async function rotate() {
  const phrase = $("rot-phrase").value;
  $("rot-phrase").value = "";
  secOut("Rotating secrets: running… (the engine is stopped and started again; everyone is signed out)");
  try {
    const rep = await invoke("rotate_secrets", { confirmation: phrase });
    const lines = [`Rotate secrets: ${rep.ok ? "completed" : "FAILED"}`];
    rep.steps.forEach((s, i) => {
      lines.push(`  ${i + 1}. [${OUTCOME[s.outcome] || s.outcome}] ${describeStep(s.step)} (${(s.duration_ms / 1000).toFixed(1)} s)`);
      if (s.detail) lines.push(`        ${s.detail}`);
    });
    secOut(lines.join("\n"));
  } catch (e) {
    secOut(`Rotate secrets: ${e}`);
  }
}

window.addEventListener("DOMContentLoaded", () => {
  $("key-show").addEventListener("click", showKey);
  $("key-add-go").addEventListener("click", addKey);
  $("rot-go").addEventListener("click", rotate);
});
