"use client";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, ErrorLine, Panel, Stat, Table } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";
import { IncidentsPanel } from "@/components/Incidents";

const TONE: Record<string, any> = { OK: "neon", WARN: "warn", CRITICAL: "bear", UNKNOWN: "dim", NO_REPORT: "dim" };

/** Parts 27-30 Phase 7: data quality, API / logs / database / job backlog, model and strategy governance. */
function Reliability() {
  const { data: mon } = useApi<any>("/api/system/monitoring", 15000);
  const { data: dq } = useApi<any[]>("/api/data-quality", 30000);
  const { data: gov } = useApi<any>("/api/governance", 60000);
  const api = mon?.api;
  const bl = mon?.backlog;
  return (
    <>
      <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        <Stat label="API requests (15 min)" value={api?.requests ?? "—"} sub={api?.mean_latency_ms != null ? `mean ${api.mean_latency_ms} ms` : "no requests"} />
        <Stat label="Server errors (15 min)" value={api?.server_errors ?? "—"} tone={api?.server_errors ? "down" : "plain"} sub={api?.error_rate != null ? `${(api.error_rate * 100).toFixed(2)}%` : ""} />
        <Stat label="DB connections" value={mon ? `${mon.db_connections.total} / ${mon.db_connections.max}` : "—"} sub={mon ? Object.entries(mon.db_connections.by_state).map(([k, v]) => `${k} ${v}`).join(" · ") : ""} />
        <Stat label="Error logs (15 min)" value={mon ? Object.values(mon.log_errors_15m).reduce((a: number, b: any) => a + b, 0) : "—"}
          tone={mon && Object.values(mon.log_errors_15m).some((v: any) => v > 0) ? "warn" : "plain"}
          sub={mon ? Object.entries(mon.log_errors_15m).map(([k, v]) => `${k} ${v}`).join(" · ") || "none" : ""} />
      </div>
      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <Panel title="Job backlog">
          {!bl ? <div className="text-[11px] text-dim">loading…</div> : <>
            <div className="text-[11px]">Overdue learner jobs: {bl.learner_overdue.length ? bl.learner_overdue.map((o: any) => `${o.job} (${o.last_finished ? "last " + fmt.time(o.last_finished) : "never run"})`).join(", ") : <span className="text-neon">none</span>}</div>
            <div className="text-[11px]">Stuck jobs: {bl.learner_stuck.length ? <span className="text-bear">{bl.learner_stuck.map((o: any) => o.job).join(", ")}</span> : <span className="text-neon">none</span>}</div>
            <div className="text-[11px]">Last scan cycle: {bl.scanner_last_cycle ? `${bl.scanner_last_cycle.analysed} analysed, ${bl.scanner_last_cycle.deferred} deferred, ${bl.scanner_last_cycle.timed_out} timed out` : "—"}</div>
          </>}
        </Panel>
        <Panel title="Model governance and strategy suspension">
          <Table rows={gov?.models} empty="No production model." rowKey={(m) => `${m.model}${m.version}`}
            cols={[{ k: "model", h: "Model", r: (m) => `${m.model} v${m.version}` },
              { k: "healthy", h: "Health", r: (m) => <Badge tone={m.healthy ? "neon" : "bear"}>{m.healthy ? "healthy" : "degraded"}</Badge> },
              { k: "dataset_version", h: "Dataset", sort: false, r: (m) => <span className="font-mono text-[10px]">{m.dataset_version ? m.dataset_version.slice(0, 19) + "…" : "not recorded"}</span> },
              { k: "detail", h: "Detail", sort: false, r: (m) => <span className="text-dim">{m.detail}</span> }]} />
          <div className="mt-2 text-[10px] uppercase text-dim">Strategies recommended for suspension (your decision)</div>
          {gov?.strategy_suspensions?.length ? gov.strategy_suspensions.map((x: any) => <div key={x.strategy} className="text-[11px]">{x.strategy}: {x.reason}</div>)
            : <div className="text-[11px] text-dim">None: no strategy has 30+ paper/live trades with a profit factor below 0.8.</div>}
        </Panel>
      </div>
      <IncidentsPanel />
      <Panel title="Market data quality (latest analysis per symbol and timeframe)" className="mt-3">
        <Table rows={dq} empty="No analyses yet." rowKey={(d) => `${d.symbol}|${d.timeframe}`}
          cols={[{ k: "symbol", h: "Symbol" }, { k: "timeframe", h: "TF" },
            { k: "score", h: "Score", r: (d) => d.score == null ? "—" : <Badge tone={d.score >= 90 ? "neon" : d.score >= 70 ? "warn" : "bear"}>{d.score}</Badge> },
            { k: "problems", h: "Problems", sort: false, r: (d) => Object.keys(d.problems || {}).length ? Object.entries(d.problems).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(" · ") : <span className="text-dim">none</span> },
            { k: "quote_age_s", h: "Quote age", r: (d) => d.quote_age_s == null ? "—" : `${d.quote_age_s} s` },
            { k: "source", h: "Source", sort: false, r: (d) => <span className="text-dim">{d.source || d.note}</span> },
            { k: "analysed_at", h: "Analysed", r: (d) => fmt.time(d.analysed_at) }]} />
      </Panel>
    </>
  );
}

export default function SystemPage() {
  const me = useMe();
  const live = useLive(["system_event"]);
  const { data: s, error } = useApi<any>("/api/system/status", 5000);
  const { data: res } = useApi<any>("/api/system/resources", 15000);
  const { data: ev } = useApi<any[]>("/api/system/events?limit=100", 0, live.tick);
  const { data: audit } = useApi<any[]>(can(me, "users.manage") ? "/api/system/audit?limit=100" : null);
  const h = s?.host;
  const svc = Object.entries(s?.services || {});
  return (
    <div>
      <PageTitle sub={s ? `Server time ${s.server_time} · API uptime ${fmt.dur(s.uptime_s)}` : ""}>System Monitor</PageTitle>
      <ErrorLine error={error} />
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
        <Stat label="CPU" value={fmt.pctRaw(h?.cpu_pct, 0)} tone={h?.cpu_pct > 85 ? "down" : "plain"} sub={h ? `load ${h.load.map((x: number) => x.toFixed(2)).join(" ")}` : ""} />
        <Stat label="RAM" value={fmt.pctRaw(h?.mem_pct, 0)} sub={h ? `${h.mem_used_gb} / ${h.mem_total_gb} GB` : ""} tone={h?.mem_pct > 90 ? "down" : "plain"} />
        <Stat label="Disk" value={fmt.pctRaw(h?.disk_pct, 0)} sub={h ? `${h.disk_free_gb} GB free` : ""} tone={h?.disk_pct > 85 ? "down" : "plain"} />
        <Stat label="Network" value={h ? `${h.net_sent_mb}↑ ${h.net_recv_mb}↓` : "—"} sub="MB since boot" />
        <Stat label="Execution queue" value={s?.execution_queue ?? "—"} tone={s?.execution_queue ? "warn" : "plain"} />
        <Stat label="Exec latency 24h" value={s?.execution_latency_ms_24h ? `${s.execution_latency_ms_24h} ms` : "—"} />
        <Stat label="Errors 1h" value={s?.errors_1h ?? "—"} tone={s?.errors_1h ? "down" : "plain"} />
        <Stat label="WebSockets" value={s?.services?.websocket?.connections ?? "—"} />
      </div>
      <Panel title="Role resources (each against its own container limit)" right={res && <span className="text-[10px] text-dim">
        database {(res.database_bytes / 1e9).toFixed(2)} GB · data disk {res.disk.free_gb} GB free <Badge tone={TONE[res.disk.status]}>{res.disk.status}</Badge></span>}>
        <Table rows={res ? Object.entries(res.roles).map(([role, v]: any) => ({ role, ...v })) : []} rowKey={(r) => r.role} empty="Loading…"
          cols={[{ k: "role", h: "Role" },
            { k: "status", h: "Status", r: (r) => <Badge tone={TONE[r.status]}>{r.status === "NO_REPORT" ? "no report" : r.status}</Badge> },
            { k: "mem", h: "Memory", r: (r) => r.mem_bytes == null ? "—" : `${(r.mem_bytes / 2 ** 20).toFixed(0)} MB${r.mem_limit ? ` / ${(r.mem_limit / 2 ** 20).toFixed(0)} MB (${r.mem_pct}%)` : ""}` },
            { k: "cpu", h: "CPU", r: (r) => r.cpu_cores == null ? "—" : `${r.cpu_cores.toFixed(2)} cores${r.cpu_cap ? ` of ${r.cpu_cap} (${r.cpu_pct_of_cap}%)` : ""}` },
            { k: "reasons", h: "Notes", r: (r) => (r.reasons || []).join("; ") },
            { k: "at", h: "Reported", r: (r) => r.at ? fmt.time(new Date(r.at * 1000).toISOString()) : "—" }]} />
      </Panel>
      <Panel title="Services">
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {svc.map(([k, v]: any) => (
            <div key={k} className="rounded border border-line p-2 text-[11px]">
              <div className="flex items-center justify-between"><span className="uppercase tracking-wider">{k.replace(/_/g, " ")}</span>
                <Badge tone={v.status === "UP" ? "neon" : v.status === "DOWN" ? "bear" : "warn"}>{v.status}</Badge></div>
              {v.latency_ms !== undefined && <div className="text-dim">latency {v.latency_ms} ms</div>}
              <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap text-[10px] text-dim">{typeof v.detail === "string" ? v.detail : JSON.stringify(v.detail ?? {}, null, 1)}</pre>
            </div>))}
        </div>
      </Panel>
      <Reliability />
      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <Panel title="System events"><Table rows={ev} empty="No events" cols={[{ k: "at", h: "Time", r: (e) => fmt.time(e.at) }, { k: "component", h: "Component" },
          { k: "level", h: "Level", r: (e) => <Badge tone={e.level === "critical" || e.level === "error" ? "bear" : e.level === "warning" ? "warn" : "cyan"}>{e.level}</Badge> },
          { k: "message", h: "Message", r: (e) => <span className="block max-w-[420px] truncate">{e.message}</span> }]} /></Panel>
        {audit && <Panel title="Audit trail"><Table rows={audit} cols={[{ k: "at", h: "Time", r: (a) => fmt.time(a.at) }, { k: "user_id", h: "User" }, { k: "action", h: "Action" },
          { k: "target", h: "Target" }, { k: "ip", h: "IP" }]} /></Panel>}
      </div>
    </div>
  );
}
