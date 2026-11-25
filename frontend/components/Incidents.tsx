"use client";
import { AlertTriangle, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, fmt, post, put } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";
import { can, useMe } from "./Shell";
import { Badge, Btn, ErrorLine, Panel, Table } from "./ui";

const SEV: Record<string, any> = { critical: "bear", error: "bear", warning: "warn", info: "dim" };
const BORDER: Record<string, string> = { critical: "border-bear bg-bear/10", error: "border-bear/70 bg-bear/5",
  warning: "border-warn/70 bg-warn/5", info: "border-line bg-panel" };
const SEEN_KEY = "rr.incidents.seen";

function seen(): Record<string, number> {
  try { return JSON.parse(sessionStorage.getItem(SEEN_KEY) || "{}"); } catch { return {}; }
}
function markSeen(id: number, occ: number) {
  try { sessionStorage.setItem(SEEN_KEY, JSON.stringify({ ...seen(), [id]: occ })); } catch { /* private mode */ }
}

/** Popup alerts for new, re-opened and escalated incidents (warning and above). Critical ones stay until dismissed;
 *  others close after 20 s. Each popup explains the problem and the recommended fix. */
export function IncidentPopups() {
  const me = useMe();
  const live = useLive(["incident"]);
  const [items, setItems] = useState<any[]>([]);
  useEffect(() => {      // open errors and criticals not yet shown in this browser session
    api<any[]>("/api/incidents?status=open&limit=20").then((rows) => {
      const s = seen();
      const fresh = rows.filter((r) => ["critical", "error"].includes(r.severity) && r.status === "OPEN" && s[r.id] !== r.occurrences).slice(0, 3);
      if (fresh.length) setItems((cur) => [...fresh.map((r) => ({ ...r, event: "open" })), ...cur].slice(0, 4));
    }).catch(() => undefined);
  }, []);
  useEffect(() => {
    const e: any = live.last;
    if (!e || e.kind !== "incident") return;
    const d = e.data;
    if (d.event === "resolved") { setItems((cur) => cur.filter((x) => x.id !== d.id)); return; }
    setItems((cur) => [d, ...cur.filter((x) => x.id !== d.id)].slice(0, 4));
    if (d.severity !== "critical") setTimeout(() => setItems((cur) => cur.filter((x) => x !== d)), 20000);
  }, [live.tick]); // eslint-disable-line react-hooks/exhaustive-deps
  const close = (x: any) => { markSeen(x.id, x.occurrences); setItems((cur) => cur.filter((y) => y.id !== x.id)); };
  if (!items.length) return null;
  return (
    <div className="fixed right-3 top-14 z-50 flex w-[min(380px,calc(100vw-24px))] flex-col gap-2" role="status" aria-live="polite">
      {items.map((x) => (
        <div key={`${x.id}-${x.event}`} className={`rounded border p-2 text-[11px] shadow-lg ${BORDER[x.severity] || BORDER.info}`}>
          <div className="flex items-start gap-2">
            <AlertTriangle size={14} className={x.severity === "warning" ? "text-warn" : "text-bear"} />
            <div className="min-w-0 flex-1">
              <div className="font-bold">{x.title}{x.event === "reopened" ? " (again)" : x.event === "escalated" ? " — self-healing failed" : ""}</div>
              <div className="text-dim">{x.explanation}</div>
              <div className="mt-1"><span className="text-dim">What to do: </span>{x.recommended_fix}</div>
              <div className="mt-1 flex gap-2">
                <Link href="/system#incidents" className="text-cyan underline" onClick={() => close(x)}>Details</Link>
                {can(me, "trade.control") && <button type="button" className="text-cyan underline"
                  onClick={() => post(`/api/incidents/${x.id}/ack`).then(() => close(x)).catch(() => close(x))}>Acknowledge</button>}
              </div>
            </div>
            <button type="button" aria-label="Dismiss" onClick={() => close(x)}><X size={12} /></button>
          </div>
        </div>
      ))}
    </div>
  );
}

/** System Monitor: incidents with explanation, diagnosis, the recommended fix, the safe repair and its record. */
export function IncidentsPanel() {
  const me = useMe();
  const live = useLive(["incident"]);
  const [filter, setFilter] = useState("open");
  const { data: rows, reload } = useApi<any[]>(`/api/incidents?status=${filter}&limit=200`, 30000, live.tick);
  const { data: sum, reload: reloadSum } = useApi<any>("/api/incidents/summary", 30000, live.tick);
  const [sel, setSel] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const admin = can(me, "system.manage");
  async function run(fn: () => Promise<any>) {
    setBusy(true); setErr(null);
    try { const r = await fn(); if (r && r.id) setSel(r); reload(); reloadSum(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  const cur = sel && (rows || []).find((r) => r.id === sel.id) ? { ...sel, ...(rows || []).find((r) => r.id === sel.id) } : sel;
  return (
    <div id="incidents" className="mt-3 grid gap-3 xl:grid-cols-2">
      <Panel title={`Incidents${sum ? ` · ${Object.entries(sum.open_by_severity).map(([k, v]) => `${v} ${k}`).join(", ") || "none open"}` : ""}`}
        right={<div className="flex items-center gap-2 text-[11px]">
          <select className="bg-transparent" value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="open">open</option><option value="AUTO_RESOLVED">self-healed</option><option value="RESOLVED">resolved</option><option value="">all</option></select>
          {sum && <label className="flex items-center gap-1" title="Safe repairs only; never trading controls, orders or positions">
            <input type="checkbox" disabled={!admin || busy} checked={sum.auto_repair}
              onChange={(e) => run(() => put("/api/automation/policy", { auto_repair: e.target.checked }))} />auto-repair</label>}</div>}>
        <ErrorLine error={err} />
        <Table rows={rows} rowKey={(r) => r.id} empty="No incidents"
          cols={[{ k: "last_seen", h: "Last", r: (r) => fmt.time(r.last_seen) },
            { k: "severity", h: "Severity", r: (r) => <Badge tone={SEV[r.severity]}>{r.severity}</Badge> },
            { k: "title", h: "Incident", r: (r) => <button type="button" className="text-left underline-offset-2 hover:underline" onClick={() => setSel(r)}>{r.title}</button> },
            { k: "component", h: "Where" }, { k: "occurrences", h: "Count" },
            { k: "status", h: "Status", r: (r) => <Badge tone={r.status === "AUTO_RESOLVED" ? "neon" : r.status === "RESOLVED" ? "dim" : "warn"}>{r.status.replace("_", " ")}</Badge> }]} />
      </Panel>
      <Panel title={cur ? cur.title : "Incident details"}>
        {!cur ? <div className="text-[11px] text-dim">Select an incident.</div> : <div className="text-[11px]">
          <div className="text-dim">{cur.component} · {cur.category} · first {fmt.time(cur.first_seen)} · {cur.occurrences}× {cur.reopened ? `· reopened ${cur.reopened}×` : ""}</div>
          <div className="mt-1 break-words font-mono text-[10px]">{cur.message}</div>
          <div className="mt-2"><b>What happened.</b> {cur.explanation}</div>
          <div><b>Diagnosis.</b> {cur.diagnosis}</div>
          <div><b>Recommended fix.</b> {cur.recommended_fix}</div>
          {cur.repair_label && <div className="mt-1"><b>Safe repair:</b> {cur.repair_label}{!cur.auto_repair_allowed && " (manual only)"}</div>}
          {(cur.attempts || []).map((a: any, i: number) => <div key={i} className="text-dim">{fmt.time(a.at)} {a.label}: {a.ok ? "done" : "failed"}{a.verified === true ? ", verified" : a.verified === false ? ", not verified" : ""} — {a.detail} ({String(a.by)})</div>)}
          {cur.ai_diagnosis && <div className="mt-2 rounded border border-violet/40 p-1.5"><div className="text-[10px] uppercase text-violet">AI diagnostic advice ({cur.ai_diagnosis.provider}) — not executed</div>
            <div>{cur.ai_diagnosis.explanation}</div>
            <div className="text-dim">Likely causes: {cur.ai_diagnosis.likely_causes.join(" · ")}</div>
            <div className="text-dim">Steps: {cur.ai_diagnosis.recommended_steps.join(" · ")}</div></div>}
          {cur.resolution && <div className="mt-1 text-neon">{cur.resolution}</div>}
          <div className="mt-2 flex flex-wrap gap-1">
            {cur.status === "OPEN" && can(me, "trade.control") && <Btn small busy={busy} onClick={() => run(() => post(`/api/incidents/${cur.id}/ack`))}>Acknowledge</Btn>}
            {admin && cur.auto_repair_allowed && !["RESOLVED", "AUTO_RESOLVED"].includes(cur.status) && <Btn small tone="neon" busy={busy} onClick={() => run(() => post(`/api/incidents/${cur.id}/repair`))}>Run safe repair</Btn>}
            {admin && <Btn small tone="violet" busy={busy} onClick={() => run(() => post(`/api/incidents/${cur.id}/diagnose`))}>Ask AI to diagnose</Btn>}
            {admin && !["RESOLVED", "AUTO_RESOLVED"].includes(cur.status) && <Btn small tone="dim" busy={busy} onClick={() => run(() => post(`/api/incidents/${cur.id}/resolve`, { resolution: "resolved by an operator" }))}>Mark resolved</Btn>}
          </div></div>}
        {sum?.stats?.categories?.length > 0 && <div className="mt-3 text-[11px]">
          <div className="text-[10px] uppercase text-dim">Last {sum.stats.days} days</div>
          {sum.stats.categories.slice(0, 8).map((c: any) => <div key={c.category} className="flex gap-2"><span className="w-48 truncate">{c.title}</span>
            <span className="text-dim">{c.occurrences}× · open {c.open} · self-healed {c.auto_resolved}{c.mean_minutes_to_resolve != null ? ` · ${c.mean_minutes_to_resolve} min to resolve` : ""}{c.recurring ? ` · ${c.recurring} recurring` : ""}</span></div>)}
        </div>}
      </Panel>
    </div>
  );
}
