"use client";
import { motion } from "framer-motion";
import { Sparkles } from "lucide-react";
import { useState } from "react";
import AiPanel, { STATUS_TONE } from "@/components/AiPanel";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, Empty, ErrorLine, Panel, Table } from "@/components/ui";
import { fmt, post } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";

const NODES: [string, string][] = [
  ["market_scanner", "Market Scanner"], ["smc_engine", "SMC Engine"], ["ict_engine", "ICT Engine"],
  ["price_action_engine", "Price Action"], ["liquidity_engine", "Liquidity"], ["indicator_engine", "Indicators"],
  ["volume_intelligence", "Volume Intel"], ["quantitative_engine", "Quant Engine"], ["strategy_optimisation", "Optimisation"],
  ["risk_engine", "Risk Engine"], ["execution", "Trade Execution"],
];
const COLORS: Record<string, string> = { neon: "#39ff88", bear: "#ff3d5a", cyan: "#22e4ff", violet: "#b45cff", dim: "#3a4a54" };
// Strategy-family engines run inside the scanner pass; their state mirrors it.
const MIRROR: Record<string, string> = { smc_engine: "market_scanner", ict_engine: "market_scanner", price_action_engine: "market_scanner",
  liquidity_engine: "market_scanner", indicator_engine: "market_scanner", volume_intelligence: "market_scanner", quantitative_engine: "market_scanner" };

export default function AiPage() {
  const me = useMe();
  const live = useLive(["agent_activity"]);
  const { data: st } = useApi<any>("/api/agent/status", 10000, live.tick);
  const { data: dec } = useApi<any[]>("/api/agent/decisions?limit=30", 0, live.tick);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [disc, setDisc] = useState<any>(null);
  const R = 150, C = 190;
  const mods = st?.modules || {};
  const statusOf = (k: string) => (mods[MIRROR[k] || k]?.status as string) || "IDLE";
  return (
    <div>
      <PageTitle sub="Module states reflect real recorded activity; idle modules have not run.">AI Intelligence</PageTitle>
      <div className="grid gap-3 xl:grid-cols-[1fr_360px]">
        <div className="space-y-3">
          <Panel title="AI brain">
            <div className="flex justify-center">
              <svg viewBox="0 0 380 380" className="w-full max-w-[520px]" role="img" aria-label="AI module network">
                {NODES.map(([k], i) => {
                  const a = (i / NODES.length) * Math.PI * 2 - Math.PI / 2;
                  const x = C + R * Math.cos(a), y = C + R * Math.sin(a);
                  const col = COLORS[STATUS_TONE[statusOf(k)] || "dim"];
                  const active = !["IDLE", "waiting"].includes(statusOf(k));
                  return <motion.line key={k} x1={C} y1={C} x2={x} y2={y} stroke={col} strokeWidth={active ? 1.5 : 0.6}
                    strokeDasharray="4 4" animate={active ? { strokeDashoffset: [0, -16] } : {}} transition={{ repeat: Infinity, duration: 1, ease: "linear" }} />;
                })}
                <circle cx={C} cy={C} r={44} fill="rgba(180,92,255,.08)" stroke="#b45cff" />
                <motion.circle cx={C} cy={C} r={30} fill="rgba(57,255,136,.12)" stroke="#39ff88" style={{ transformOrigin: `${C}px ${C}px` }}
                  animate={{ scale: [0.94, 1.06, 0.94] }} transition={{ repeat: Infinity, duration: 2.4 }} />
                <text x={C} y={C - 3} textAnchor="middle" fill="#d7e3ea" fontSize="8" fontFamily="Orbitron">CLAUDE</text>
                <text x={C} y={C + 11} textAnchor="middle" fill={COLORS[STATUS_TONE[statusOf("claude_agent")] || "dim"]} fontSize="8">{statusOf("claude_agent")}</text>
                {NODES.map(([k, label], i) => {
                  const a = (i / NODES.length) * Math.PI * 2 - Math.PI / 2;
                  const x = C + R * Math.cos(a), y = C + R * Math.sin(a);
                  const col = COLORS[STATUS_TONE[statusOf(k)] || "dim"];
                  return (
                    <g key={k}>
                      <circle cx={x} cy={y} r={20} fill="#0a0d10" stroke={col} />
                      <text x={x} y={y + 32} textAnchor="middle" fill="#d7e3ea" fontSize="8.5">{label}</text>
                      <text x={x} y={y + 3} textAnchor="middle" fill={col} fontSize="7">{statusOf(k).toUpperCase().slice(0, 9)}</text>
                    </g>
                  );
                })}
              </svg>
            </div>
            <div className="mt-2 grid gap-1 text-[11px] sm:grid-cols-2">
              {[...NODES, ["claude_agent", "Claude Agent"] as [string, string]].map(([k, l]) => (
                <div key={k} className="flex items-center justify-between rounded border border-line px-2 py-1">
                  <span>{l}</span><span className="truncate pl-2 text-dim">{(mods[MIRROR[k] || k]?.message || "no activity").slice(0, 60)}</span>
                  <Badge tone={STATUS_TONE[statusOf(k)] || "dim"}>{statusOf(k)}</Badge></div>
              ))}
            </div>
          </Panel>
          <Panel title="Claude decisions" right={can(me, "strategy.manage") &&
            <Btn tone="violet" busy={busy} disabled={st && !st.claude_configured}
              onClick={async () => { setBusy(true); setErr(null); try { setDisc(await post("/api/agent/discover")); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } }}>
              <Sparkles size={12} />Performance review & discovery</Btn>}>
            <ErrorLine error={err} />
            {disc && (
              <div className="mb-3 space-y-2 rounded border border-violet/40 p-2 text-[11px]">
                <div className="text-violet">Findings</div><ul className="list-disc pl-4">{disc.performance_findings.map((f: string, i: number) => <li key={i}>{f}</li>)}</ul>
                <div className="text-violet">Disable candidates: <span className="text-text">{disc.disable_candidates.join(", ") || "none"}</span></div>
                <div className="text-violet">Experiments queued as RESEARCH (isolated from production): {disc.experiments.map((e: any) => e.title).join("; ")}</div>
              </div>
            )}
            <Table rows={dec} empty="No Claude reviews recorded"
              cols={[{ k: "at", h: "Time", r: (r) => fmt.time(r.at) }, { k: "symbol", h: "Symbol" },
                { k: "decision", h: "Decision", r: (r) => <Badge tone={r.decision === "BUY" ? "neon" : r.decision === "SELL" ? "bear" : "warn"}>{r.decision}</Badge> },
                { k: "obs", h: "Observations", sort: false, r: (r) => <span className="block max-w-[420px] truncate">{(r.payload?.observations || []).join(" · ")}</span> },
                { k: "conf", h: "Conflicts", sort: false, r: (r) => <span className="block max-w-[260px] truncate text-warn">{(r.payload?.conflicts || []).join(" · ") || "—"}</span> }]} />
          </Panel>
        </div>
        <AiPanel />
      </div>
      {!st && <Empty>Loading…</Empty>}
    </div>
  );
}
