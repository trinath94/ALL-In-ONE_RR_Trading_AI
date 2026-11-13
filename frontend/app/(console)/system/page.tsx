"use client";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, ErrorLine, Panel, Stat, Table } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";

export default function SystemPage() {
  const me = useMe();
  const live = useLive(["system_event"]);
  const { data: s, error } = useApi<any>("/api/system/status", 5000);
  const { data: ev } = useApi<any[]>("/api/system/events?limit=100", 0, live.tick);
  const { data: audit } = useApi<any[]>(can(me, "users.manage") ? "/api/system/audit?limit=100" : null);
  const h = s?.host;
  const svc = Object.entries(s?.services || {});
  return (
    <div>
      <PageTitle sub={s ? `Server time ${s.server_time} · API uptime ${fmt.dur(s.uptime_s)}` : ""}>System Monitoring</PageTitle>
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
