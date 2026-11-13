"use client";
import { BrainCircuit } from "lucide-react";
import { useApi, useLive } from "@/lib/hooks";
import { fmt } from "@/lib/api";
import { Badge, dirTone, Empty, Panel } from "./ui";

export const STATUS_TONE: Record<string, any> = { processing: "cyan", analysing: "cyan", evaluating: "violet", waiting: "dim",
  approved: "neon", rejected: "bear", error: "bear", IDLE: "dim" };

export default function AiPanel() {
  const live = useLive(["agent_activity", "signal"]);
  const { data: st } = useApi<any>("/api/agent/status", 15000, live.tick);
  const { data: feed } = useApi<any[]>("/api/agent/activity?limit=40", 0, live.tick);
  const { data: dec } = useApi<any[]>("/api/agent/decisions?limit=1", 30000, live.tick);
  const s = st?.stats_24h || {};
  const claude = st?.modules?.claude_agent;
  const latest = dec?.[0];
  const scan = st?.modules?.market_scanner;
  return (
    <Panel title="AI Intelligence" icon={<BrainCircuit size={12} />} accent="violet">
      <div className="space-y-3 text-[11px]">
        <div className="flex items-center justify-between">
          <span className="text-dim">Claude agent</span>
          {st && !st.claude_configured ? <Badge tone="warn">not configured</Badge> : <Badge tone={STATUS_TONE[claude?.status] || "dim"}>{claude?.status || "…"}</Badge>}
        </div>
        <div className="text-dim">{scan?.message || "Scanner has not run yet"}</div>
        <div className="grid grid-cols-3 gap-2 text-center">
          {[["Analyses", s.analyses], ["Signals", s.signals], ["AI approved", s.approved], ["AI rejected", s.rejected], ["Risk rejects", s.risk_rejections], ["Tokens in", s.claude_tokens_in]].map(([k, v]) => (
            <div key={k as string} className="rounded border border-line py-1"><div className="tabular text-sm text-text">{v ?? "—"}</div><div className="text-[9px] uppercase text-dim">{k}</div></div>
          ))}
        </div>
        <div>
          <div className="mb-1 text-[10px] uppercase tracking-widest text-cyan">Latest AI decision</div>
          {latest ? (
            <div className="rounded border border-line p-2">
              <div className="flex items-center gap-2"><Badge tone={dirTone(latest.decision === "NO_TRADE" ? null : latest.decision)}>{latest.decision}</Badge>
                <span>{latest.symbol}</span><span className="ml-auto text-dim">{fmt.time(latest.at)}</span></div>
              <p className="mt-1 line-clamp-4 text-dim">{latest.payload?.explanation}</p>
            </div>
          ) : <Empty>No Claude decisions recorded yet</Empty>}
        </div>
        <div>
          <div className="mb-1 text-[10px] uppercase tracking-widest text-cyan">Agent activity feed</div>
          <div className="max-h-72 space-y-0.5 overflow-y-auto font-mono text-[10.5px]">
            {feed?.length ? feed.map((e, i) => (
              <div key={i} className="flex gap-2"><span className="shrink-0 text-dim">{e.ts?.slice(11, 19)}</span>
                <span className={STATUS_TONE[e.status] === "neon" ? "shrink-0 text-neon" : STATUS_TONE[e.status] === "bear" ? "shrink-0 text-bear" : "shrink-0 text-cyan"}>[{e.module}]</span>
                <span className="text-text/80">{e.message}</span></div>
            )) : <Empty>No activity yet — events appear only when modules actually run.</Empty>}
          </div>
        </div>
      </div>
    </Panel>
  );
}
