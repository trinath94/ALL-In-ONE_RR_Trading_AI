"use client";
import { Activity, AlertTriangle } from "lucide-react";
import { Badge, dirTone, Panel, Stat, Table } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const UP: Record<string, any> = { UP: "neon", RUNNING: "neon", HEALTHY: "neon", DEGRADED: "warn", DOWN: "bear", STOPPED: "dim", UNKNOWN: "warn" };
const SVC: [string, string][] = [["backend", "Backend"], ["database", "Database"], ["redis", "Redis"], ["mt5_terminal", "MT5 terminal"],
  ["broker_connection", "Broker connection"]];
const ROLE: [string, string][] = [["monitor", "Monitor"], ["analysis", "Analysis"], ["learner", "Learner"], ["trading_worker", "Trading worker"]];

/** Part 17 Command Centre: overall health, services, roles, counts, latest signals, recent executions, critical alerts.
 *  From /api/dashboard/overview: live state and recorded rows only. */
export default function SystemStrip() {
  const { data: o, error } = useApi<any>("/api/dashboard/overview", 15000);
  if (error) return <div className="mb-3 text-[11px] text-bear">System status unavailable: {error}</div>;
  if (!o) return null;
  const runs = o.agents.runs_last_hour || {};
  return (
    <div className="mb-3 space-y-3">
      <Panel title="System status" icon={<Activity size={12} />} right={<Badge tone={UP[o.overall_health]}>overall {o.overall_health.toLowerCase()}</Badge>}>
        <div className="grid grid-cols-2 gap-2 text-[11px] md:grid-cols-5">
          {SVC.map(([k, label]) => <div key={k} title={String(o.services[k]?.detail ?? "")}>
            <div className="text-dim">{label}</div><Badge tone={UP[o.services[k]?.status]}>{o.services[k]?.status}</Badge>
            <div className="truncate text-[10px] text-dim">{String(o.services[k]?.detail ?? "")}</div></div>)}
        </div>
        <div className="mt-2 grid grid-cols-2 gap-2 text-[11px] md:grid-cols-4">
          {ROLE.map(([k, label]) => <div key={k}><span className="text-dim">{label}: </span><Badge tone={UP[o.roles[k]?.status]}>{o.roles[k]?.status}</Badge></div>)}
        </div>
      </Panel>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-6">
        <Stat label="Account mode" value={o.account_mode} sub={o.startup_checks_passed ? "reconciled" : "reconciliation pending"} tone={o.account_mode === "LIVE" ? "down" : "plain"} />
        <Stat label="Auto trading" value={o.auto_trading ? "ON" : "OFF"} tone={o.auto_trading ? "up" : "plain"} />
        <Stat label="Symbols discovered" value={o.symbols_discovered} />
        <Stat label="Symbols scanned" value={o.symbols_scanning} sub="enabled for scanning" />
        <Stat label="Active strategies" value={o.active_strategies} sub={`enabled for ${o.account_mode}`} />
        <Stat label="Agents (1 h)" value={o.agents.active_last_hour} sub={`${runs.ok || 0} ok · ${runs.error || 0} errors`} tone={runs.error ? "warn" : "plain"} />
      </div>
      <div className="grid gap-3 xl:grid-cols-3">
        <Panel title="Critical alerts" icon={<AlertTriangle size={12} />} accent={o.critical_alerts.length ? "bear" : "cyan"}>
          <div className="mb-1 text-[10px] uppercase text-dim">Active now</div>
          {o.critical_alerts.length === 0 ? <div className="text-[11px] text-dim">None: no kill switch, risk lock, unresolved critical anomaly, service down or pending reconciliation.</div>
            : o.critical_alerts.map((a: any, i: number) => <div key={i} className="text-[11px]"><Badge tone="bear">{a.kind}</Badge> {a.detail} {a.at && <span className="text-dim">{fmt.time(a.at)}</span>}</div>)}
          <div className="mb-1 mt-2 text-[10px] uppercase text-dim">Recent critical events (24 h)</div>
          {(o.recent_critical_events || []).length === 0 ? <div className="text-[11px] text-dim">None.</div>
            : o.recent_critical_events.map((g: any) => <div key={g.kind} className="text-[11px]"><Badge tone="warn">{g.kind}</Badge> {g.count > 1 ? `${g.count}× · ` : ""}{g.detail} <span className="text-dim">latest {fmt.time(g.latest)}</span></div>)}
        </Panel>
        <Panel title="Latest signals">
          <Table rows={o.latest_signals} rowKey={(s) => s.id} empty="No signals yet."
            cols={[{ k: "at", h: "Time", r: (s) => fmt.time(s.at) }, { k: "symbol", h: "Symbol" }, { k: "timeframe", h: "TF" },
              { k: "decision", h: "Decision", r: (s) => <Badge tone={dirTone(s.decision)}>{s.decision}</Badge> }, { k: "grade", h: "Grade" },
              { k: "status", h: "Status" }]} />
        </Panel>
        <Panel title="Recent executions">
          <Table rows={o.recent_executions} rowKey={(e, i) => `${e.execution_id}${i}`} empty="No executions yet."
            cols={[{ k: "at", h: "Time", r: (e) => fmt.time(e.at) }, { k: "symbol", h: "Symbol" }, { k: "action", h: "Action" },
              { k: "volume", h: "Lots" }, { k: "status", h: "Status", r: (e) => <Badge tone={e.status === "CONFIRMED" ? "neon" : e.status === "REJECTED" || e.status === "FAILED" ? "bear" : "warn"}>{e.status}</Badge> },
              { k: "source", h: "Source" }]} />
        </Panel>
      </div>
    </div>
  );
}
