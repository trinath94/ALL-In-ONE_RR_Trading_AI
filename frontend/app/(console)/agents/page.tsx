"use client";
import { useState } from "react";
import { PageTitle } from "@/components/Shell";
import { Badge, ErrorLine, Panel, Stat, Table } from "@/components/ui";
import { api, fmt } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const KIND: Record<string, any> = { deterministic: "cyan", llm: "violet", hybrid: "warn" };
const STATUS: Record<string, any> = { ok: "neon", partial: "warn", error: "bear", skipped: "dim" };

/** The 21 specialist agents under one orchestrator: what each is, what it may do, and what it actually did. */
export default function Agents() {
  const { data, error } = useApi<any[]>("/api/agents", 20000);
  const { data: cycles } = useApi<any[]>("/api/agents/cycles", 20000);
  const [sel, setSel] = useState<any>(null);
  const [runs, setRuns] = useState<any[] | null>(null);
  const [cycle, setCycle] = useState<any>(null);
  const [cycleRuns, setCycleRuns] = useState<any[] | null>(null);
  async function choose(a: any) { setSel(a); setRuns(null); setRuns(await api(`/api/agents/runs?agent_id=${a.id}&limit=50`)); }
  async function openCycle(c: any) { setCycle(c); setCycleRuns(null); setCycleRuns(await api(`/api/agents/runs?cycle_id=${c.cycle_id}`)); }
  const active = (data || []).filter((a) => a.last_run).length;
  const runs24 = (data || []).reduce((n, a) => n + Object.values(a.runs_24h).reduce((x: number, y: any) => x + y, 0), 0);
  const errs24 = (data || []).reduce((n, a) => n + a.runs_24h.error + a.runs_24h.partial, 0);
  return (
    <div>
      <PageTitle sub="Specialist agents coordinated by one orchestrator, not independent bots. Each run is recorded in a typed contract (inputs, output, timing, provider/model, evidence, errors). Only Trade Execution and Trade Management touch the broker, and only through the Risk Engine.">
        Agent Control Room</PageTitle>
      <ErrorLine error={error} />
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        <Stat label="Agents" value={(data || []).length} sub="closed registry" />
        <Stat label="Have run" value={active} sub="at least once (run log)" />
        <Stat label="Runs (24 h)" value={runs24} />
        <Stat label="Errors (24 h)" value={errs24} tone={errs24 ? "down" : "plain"} />
      </div>
      <div className="grid gap-3 xl:grid-cols-[1fr_440px]">
        <Panel title="Agents">
          <Table rows={data} onRow={choose} rowKey={(a) => a.id}
            cols={[{ k: "name", h: "Agent" }, { k: "kind", h: "Kind", r: (a) => <Badge tone={KIND[a.kind]}>{a.kind}</Badge> },
              { k: "runs_in", h: "Runs in", r: (a) => <span className="text-[10px] text-dim">{a.runs_in}</span> },
              { k: "execution_path", h: "Broker", r: (a) => a.execution_path ? <Badge tone="bear">execution</Badge> : <span className="text-dim">read-only</span> },
              { k: "runs", h: "Runs 24 h", sort: false, r: (a) => `${a.runs_24h.ok + a.runs_24h.partial + a.runs_24h.error + a.runs_24h.skipped}${a.runs_24h.error ? ` (${a.runs_24h.error} err)` : ""}` },
              { k: "last", h: "Last run", sort: false, r: (a) => a.last_run
                ? <span><Badge tone={STATUS[a.last_run.status]}>{a.last_run.status}</Badge> <span className="text-dim">{fmt.time(a.last_run.completed_at)}</span></span>
                : <span className="text-dim">not run yet</span> }]} />
        </Panel>
        {sel && (
          <Panel title={sel.name}>
            <div className="grid grid-cols-[110px_1fr] gap-1 text-[11px]">
              <div className="text-dim">ID / version</div><div>{sel.id} · v{sel.version}</div>
              <div className="text-dim">Kind</div><div><Badge tone={KIND[sel.kind]}>{sel.kind}</Badge></div>
              <div className="text-dim">Implemented by</div><div className="font-mono text-[10px]">{sel.implementation}</div>
              <div className="text-dim">Output schema</div><div>{sel.output_schema}</div>
              <div className="text-dim">Runs in</div><div>{sel.runs_in}</div>
              <div className="text-dim">Authority</div><div>{sel.authority}</div>
            </div>
            <div className="mt-3 text-[10px] uppercase text-dim">Recent runs</div>
            {runs === null ? <div className="text-[11px] text-dim">loading…</div> : runs.length === 0
              ? <div className="text-[11px] text-dim">No runs recorded. Agents are recorded only when they actually do work.</div>
              : runs.slice(0, 15).map((r) => (
                <div key={r.id} className="border-b border-line py-1 text-[11px]">
                  <Badge tone={STATUS[r.status]}>{r.status}</Badge> {fmt.time(r.completed_at)} · {r.task} · {r.duration_ms == null ? "timing shared" : r.duration_ms < 1 ? "<1 ms" : `${Math.round(r.duration_ms)} ms`}
                  {r.provider && <span> · {r.provider}/{r.model}</span>}
                  <div className="text-dim">{Object.entries(r.output).slice(0, 6).map(([k, v]) => `${k}: ${v}`).join(" · ") || "—"}</div>
                  {r.errors.map((e: string) => <div key={e} className="text-bear">{e}</div>)}
                </div>))}
          </Panel>
        )}
      </div>
      <Panel title="Orchestrated cycles" className="mt-3">
        <Table rows={cycles} empty="No cycles yet. The analysis service records one each time it analyses newly closed bars." onRow={openCycle} rowKey={(c) => c.id}
          cols={[{ k: "completed_at", h: "Completed", r: (c) => fmt.time(c.completed_at) }, { k: "task", h: "Task" },
            { k: "status", h: "Status", r: (c) => <Badge tone={STATUS[c.status]}>{c.status}</Badge> },
            { k: "agents", h: "Agents", sort: false, r: (c) => c.input_refs.agents.length },
            { k: "analysed", h: "Analysed", sort: false, r: (c) => c.output.analysed },
            { k: "candidates", h: "AI candidates", sort: false, r: (c) => c.output.candidates },
            { k: "duration_ms", h: "Duration", r: (c) => `${Math.round(c.duration_ms)} ms` }]} />
        {cycle && cycleRuns && (
          <div className="mt-2">
            <div className="text-[10px] uppercase text-dim">Cycle {cycle.cycle_id.slice(0, 8)}</div>
            <Table rows={cycleRuns.filter((r) => r.agent_id !== "master_orchestrator")} rowKey={(r) => r.id}
              cols={[{ k: "agent_id", h: "Agent" }, { k: "status", h: "Status", r: (r) => <Badge tone={STATUS[r.status]}>{r.status}</Badge> },
                { k: "units", h: "Units", sort: false, r: (r) => r.input_refs.units ?? "—" },
                { k: "duration_ms", h: "Time", r: (r) => r.duration_ms == null ? "shared" : r.duration_ms < 1 ? "<1 ms" : `${Math.round(r.duration_ms)} ms` },
                { k: "output", h: "Output", sort: false, r: (r) => <span className="block max-w-[520px] truncate text-dim">{Object.entries(r.output).map(([k, v]) => `${k}: ${v}`).join(" · ")}</span> },
                { k: "evidence", h: "Evidence", sort: false, r: (r) => <span className="block max-w-[260px] truncate text-dim">{Object.values(r.evidence).join("; ")}</span> }]} />
          </div>)}
      </Panel>
    </div>
  );
}
