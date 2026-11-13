"use client";
import clsx from "clsx";
import { useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { DriftPanel, ModelsPanel, ResearchLab, ReliabilityChart } from "@/components/Intel";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, dirTone, Empty, ErrorLine, Panel, Stat, Table } from "@/components/ui";
import { fmt, post } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const TABS = ["Regime", "Consensus", "Probability", "Portfolio Risk", "Failure", "Execution", "Governance", "Research Lab"] as const;
const tip = { background: "#0a0d10", border: "1px solid #1a2329", fontSize: 11 };
const regTone = (r: string) => (r?.includes("BULL") ? "neon" : r?.includes("BEAR") ? "bear" : r === "UNCERTAIN" || r === "NEWS_DRIVEN" ? "warn" : "cyan") as any;

function RegimeTab() {
  const { data } = useApi<any[]>("/api/intelligence/regimes", 30000);
  const [sel, setSel] = useState<any>(null);
  const probs = sel ? Object.entries(sel.probabilities).map(([k, v]: any) => ({ k, v })).sort((a, b) => b.v - a.v) : [];
  return (
    <div className="grid gap-3 xl:grid-cols-[1fr_420px]">
      <Panel title="Current regime by symbol"><Table rows={data} onRow={setSel} empty="No regime history yet (populated by the scanner)"
        cols={[{ k: "symbol", h: "Symbol" }, { k: "timeframe", h: "TF" }, { k: "regime", h: "Regime", r: (r) => <Badge tone={regTone(r.regime)}>{r.regime}</Badge> },
          { k: "confidence", h: "Conf", r: (r) => fmt.pct(r.confidence, 0) }, { k: "stability", h: "Stability", r: (r) => fmt.pct(r.stability, 0) },
          { k: "hmm", h: "HMM vol state", sort: false, r: (r) => r.hmm?.state_probs ? Object.entries(r.hmm.state_probs).map(([k, v]: any) => `${k} ${Math.round(v * 100)}%`).join(" · ") : "—" },
          { k: "transition_warning", h: "Transition", r: (r) => r.transition_warning ? <span className="text-warn">{r.transition_warning}</span> : <span className="text-dim">stable</span> },
          { k: "bar_time", h: "Bar", r: (r) => fmt.time(r.bar_time) }]} /></Panel>
      <Panel title={sel ? `${sel.symbol} ${sel.timeframe} probabilities` : "Regime probabilities"}>
        {sel ? <>
          <div className="h-64"><ResponsiveContainer><BarChart data={probs} layout="vertical" margin={{ left: 40 }}><CartesianGrid stroke="#1a2329" />
            <XAxis type="number" domain={[0, 1]} stroke="#6b7f8a" fontSize={10} /><YAxis type="category" dataKey="k" stroke="#6b7f8a" fontSize={9} width={110} />
            <Tooltip contentStyle={tip} /><Bar dataKey="v" fill="#b45cff" /></BarChart></ResponsiveContainer></div>
          <div className="text-[11px] text-dim">Suitable strategies: {sel.suitable.join(", ") || "—"} ({sel.unsuitable_count} unsuitable) · model {sel.model_version}</div>
        </> : <Empty>Select a symbol</Empty>}
      </Panel>
    </div>
  );
}

function ConsensusTab() {
  const { data } = useApi<any[]>("/api/intelligence/consensus?limit=60", 30000);
  const { data: an } = useApi<any>("/api/intelligence/consensus/analytics", 120000);
  const [sel, setSel] = useState<any>(null);
  const vc = an?.vote_correlation;
  return (
    <div className="space-y-3">
      <div className="grid gap-3 xl:grid-cols-[1fr_460px]">
        <Panel title="Specialist consensus decisions"><Table rows={data} onRow={setSel} empty="No consensus decisions yet"
          cols={[{ k: "at", h: "Time", r: (c) => fmt.time(c.at) }, { k: "symbol", h: "Symbol" }, { k: "timeframe", h: "TF" },
            { k: "decision", h: "Consensus", r: (c) => <Badge tone={dirTone(c.decision === "NO_TRADE" ? null : c.decision)}>{c.decision}</Badge> },
            { k: "agreement", h: "Agreement", r: (c) => fmt.pct(c.agreement, 0) }, { k: "weights_version", h: "Weights" },
            { k: "conflicts", h: "Conflicts", sort: false, r: (c) => <span className="block max-w-[300px] truncate text-warn">{c.conflicts.join("; ") || "—"}</span> }]} /></Panel>
        <Panel title={sel ? `${sel.symbol} ${sel.timeframe} specialists` : "Specialists"}>
          {sel ? <div className="space-y-1 text-[11px]">
            {sel.specialists.map((s: any) => <div key={s.specialist} className="flex items-start gap-2 rounded border border-line p-1.5">
              <span className="w-32 shrink-0 text-cyan">{s.specialist}</span>
              <Badge tone={s.decision === "BUY" ? "neon" : s.decision === "SELL" ? "bear" : s.decision === "PASS" ? "cyan" : s.decision === "ABSTAIN" ? "dim" : "warn"}>{s.decision}</Badge>
              <span className="tabular">{s.confidence ? fmt.pct(s.confidence, 0) : ""}</span>
              <span className="text-dim">{[...(s.evidence || []).slice(0, 2), s.data_quality !== "ok" ? s.data_quality : ""].filter(Boolean).join(" · ")}</span></div>)}
            <div className="text-dim">{sel.explanation}</div>
          </div> : <Empty>Select a decision</Empty>}
        </Panel>
      </div>
      <Panel title="Specialist independence & track record">
        {vc?.available ? <div className="text-[11px]">Effective independent specialists: <span className="text-neon">{vc.effective_independent_specialists}</span> of {vc.specialists.length} (n={vc.rows})</div>
          : <div className="text-[11px] text-dim">{vc?.note || "…"} — correlated specialists are grouped into evidence families until weights are validated.</div>}
        <Table rows={Object.entries(an?.specialist_performance || {}).map(([k, v]: any) => ({ k, ...v }))}
          cols={[{ k: "k", h: "Specialist" }, { k: "aligned_trades", h: "Aligned trades" }, { k: "win_rate_when_aligned", h: "Win when aligned", r: (r) => fmt.pct(r.win_rate_when_aligned, 1) },
            { k: "win_rate_when_silent", h: "Win when silent", r: (r) => fmt.pct(r.win_rate_when_silent, 1) }]} />
      </Panel>
    </div>
  );
}

function ProbabilityTab() {
  const { data } = useApi<any>("/api/intelligence/probability", 60000);
  const m = data?.production || data?.latest_validated;
  return (
    <div className="space-y-3">
      <div className={clsx("rounded border p-2 text-[12px]", data?.production ? "border-neon/40 text-neon" : "border-warn/40 text-warn")}>{data?.status}</div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-6">
        <Stat label="Model" value={m ? `v${m.version}` : "—"} sub={m?.algorithm} /><Stat label="Status" value={m?.status ?? "—"} />
        <Stat label="Test samples" value={m?.metrics?.test?.n ?? "—"} /><Stat label="Brier skill" value={fmt.num(m?.metrics?.test?.brier_skill_score, 4)} />
        <Stat label="ECE" value={fmt.num(m?.calibration?.test_reliability?.ece, 3)} /><Stat label="AUC" value={fmt.num(m?.metrics?.test?.auc, 3)} />
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Reliability (final test set)"><ReliabilityChart bins={m?.calibration?.test_reliability?.bins} /></Panel>
        <Panel title="Calibration by segment">
          {m?.calibration?.segments ? Object.entries(m.calibration.segments).map(([seg, rows]: any) => (
            <div key={seg} className="mb-2"><div className="text-[10px] uppercase text-dim">{seg}</div>
              <Table rows={Object.entries(rows).map(([k, v]: any) => ({ k, ...v }))} cols={[{ k: "k", h: seg }, { k: "count", h: "n" },
                { k: "brier", h: "Brier", r: (r) => r.brier ?? r.note }, { k: "ece", h: "ECE" }, { k: "base_rate", h: "Base rate" }]} /></div>))
            : <Empty>No model</Empty>}
        </Panel>
      </div>
      <Panel title="Recent predictions (raw score · calibrated · uncertainty)"><Table rows={data?.recent_predictions} empty="No predictions recorded"
        cols={[{ k: "at", h: "Time", r: (p) => fmt.time(p.at) }, { k: "symbol", h: "Symbol" }, { k: "timeframe", h: "TF" }, { k: "raw", h: "Raw", r: (p) => fmt.num(p.raw, 3) },
          { k: "probability", h: "P(TP first)", r: (p) => fmt.pct(p.probability, 1) }, { k: "uncertainty", h: "± 95%", r: (p) => fmt.num(p.uncertainty, 3) },
          { k: "shadow", h: "Mode", r: (p) => <Badge tone={p.shadow ? "warn" : "neon"}>{p.shadow ? "shadow" : "production"}</Badge> }, { k: "model_version", h: "Ver" }]} /></Panel>
    </div>
  );
}

function PortfolioTab() {
  const { data } = useApi<any>("/api/intelligence/portfolio", 30000);
  const m = data?.metrics || {};
  const hm = data?.heatmap;
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-6">
        <Stat label="Open risk" value={fmt.num(m.total_open_risk)} sub={fmt.pctRaw(m.total_open_risk_pct, 2)} />
        <Stat label="Positions" value={m.open_positions ?? "—"} /><Stat label="Independent bets" value={m.effective_independent_bets ?? "—"} />
        <Stat label="Margin used" value={fmt.pctRaw(m.margin_utilisation_pct)} />
        <Stat label="Long risk" value={fmt.num(m.risk_by_direction?.BUY)} tone="up" /><Stat label="Short risk" value={fmt.num(m.risk_by_direction?.SELL)} tone="down" />
      </div>
      {data?.recommendations?.map((r: string) => <div key={r}><Badge tone="warn">{r}</Badge></div>)}
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Risk by symbol / strategy / currency">
          {["risk_by_symbol", "risk_by_strategy", "net_risk_by_currency"].map((k) => <div key={k} className="mb-2 text-[11px]"><span className="text-dim">{k.replace(/_/g, " ")}: </span>
            {Object.entries(m[k] || {}).map(([a, b]: any) => `${a} ${fmt.num(b)}`).join(" · ") || "—"}</div>)}
          <div className="text-[10px] text-dim">Portfolio intelligence can only recommend or let the Risk Engine reject — it never raises limits.</div>
        </Panel>
        <Panel title="Rolling correlation heatmap">
          {hm?.symbols?.length ? <div className="overflow-x-auto"><table className="text-[9px]"><thead><tr><th />{hm.symbols.map((s: string) => <th key={s} className="px-0.5 text-dim [writing-mode:vertical-rl]">{s}</th>)}</tr></thead>
            <tbody>{hm.matrix.map((row: (number | null)[], i: number) => <tr key={i}><td className="pr-1 text-dim">{hm.symbols[i]}</td>
              {row.map((v, j) => <td key={j} className="h-5 w-5 text-center" style={{ background: v === null ? "transparent" : v > 0 ? `rgba(255,61,90,${Math.abs(v) * 0.7})` : `rgba(57,255,136,${Math.abs(v) * 0.7})` }}>{v === null ? "" : v.toFixed(1)}</td>)}</tr>)}</tbody></table></div>
            : <Empty>{hm?.note || "No correlation data"}</Empty>}
        </Panel>
      </div>
    </div>
  );
}

function FailureTab() {
  const [mode, setMode] = useState("DEMO");
  const { data } = useApi<any>(`/api/learning/failures?mode=${mode}`, 60000);
  const lp = Object.entries(data?.losing_patterns || {}).map(([k, v]: any) => ({ k, v }));
  return (
    <div className="space-y-3">
      <div className="flex gap-1">{["DEMO", "LIVE"].map((m) => <Btn key={m} small tone={m === mode ? "cyan" : "dim"} onClick={() => setMode(m)}>{m}</Btn>)}</div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Losing patterns">{lp.length ? <div className="h-56"><ResponsiveContainer><BarChart data={lp} layout="vertical" margin={{ left: 60 }}>
          <CartesianGrid stroke="#1a2329" /><XAxis type="number" stroke="#6b7f8a" fontSize={10} /><YAxis type="category" dataKey="k" stroke="#6b7f8a" fontSize={9} width={140} />
          <Tooltip contentStyle={tip} /><Bar dataKey="v" fill="#ff3d5a" /></BarChart></ResponsiveContainer></div> : <Empty>No analysed losses yet</Empty>}</Panel>
        <Panel title="Repeated errors by strategy / winning evidence">
          {Object.entries(data?.repeated_errors_by_strategy || {}).map(([s, c]: any) => <div key={s} className="text-[11px]"><span className="text-cyan">{s}</span>: {Object.entries(c).map(([a, b]: any) => `${a} ×${b}`).join(", ")}</div>)}
          <div className="mt-2 text-[11px] text-neon">Wins: {Object.entries(data?.winning_patterns || {}).map(([a, b]: any) => `${a} ×${b}`).join(", ") || "—"}</div>
        </Panel>
      </div>
      <Panel title="Missed opportunities (hypothetical — excluded from performance)">
        <div className="mb-2 text-[11px] text-dim">{data?.missed_opportunities?.note} · {JSON.stringify(data?.missed_opportunities?.counts || {})}</div>
        <Table rows={data?.missed_opportunities?.recent} empty="None evaluated" cols={[{ k: "signal_id", h: "Signal" }, { k: "outcome", h: "Hypothetical" },
          { k: "r", h: "R" }, { k: "reason", h: "Why not executed", r: (m) => <span className="block max-w-[520px] truncate">{m.reason}</span> }]} />
      </Panel>
    </div>
  );
}

function ExecutionTab() {
  const { data } = useApi<any>("/api/intelligence/execution", 60000);
  const { data: adj } = useApi<any[]>("/api/intelligence/adjustments", 60000);
  const o = data?.overall || {};
  const by = (k: string) => Object.entries(data?.[k] || {}).map(([key, v]: any) => ({ key, ...v }));
  const cols = [{ k: "key", h: "Group" }, { k: "orders", h: "Orders" }, { k: "fill_rate", h: "Fill", r: (r: any) => fmt.pct(r.fill_rate, 1) },
    { k: "rejections", h: "Rejected" }, { k: "requotes", h: "Requotes" }, { k: "slippage_points_mean", h: "Slip avg" }, { k: "latency_ms_p95", h: "Lat p95" }];
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-7">
        <Stat label="Orders" value={o.orders ?? "—"} /><Stat label="Fill rate" value={fmt.pct(o.fill_rate, 1)} /><Stat label="Rejections" value={o.rejections ?? "—"} />
        <Stat label="Requotes" value={o.requotes ?? "—"} /><Stat label="Partial fills" value={o.partial_fills ?? "—"} />
        <Stat label="Slippage p95" value={o.slippage_points_p95 ?? "—"} sub="points" /><Stat label="Latency p95" value={o.latency_ms_p95 ?? "—"} sub="ms" />
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="By symbol"><Table rows={by("by_symbol")} cols={cols} /></Panel>
        <Panel title="By session / order type"><Table rows={[...by("by_session"), ...by("by_order_type")]} cols={cols} /></Panel>
      </div>
      <Panel title="Position adjustments (every change logged with previous value)"><Table rows={adj} empty="No adjustments"
        cols={[{ k: "at", h: "Time", r: (a) => fmt.time(a.at) }, { k: "ticket", h: "Ticket" }, { k: "field", h: "Field" }, { k: "previous", h: "Previous" }, { k: "new", h: "New" },
          { k: "accepted", h: "Result", r: (a) => <Badge tone={a.accepted ? "neon" : "bear"}>{a.accepted ? "applied" : "refused"}</Badge> }, { k: "reason", h: "Reason" }]} /></Panel>
    </div>
  );
}

function Anomalies() {
  const me = useMe();
  const { data, reload } = useApi<any>("/api/intelligence/anomalies?active=true", 20000);
  const [err, setErr] = useState<string | null>(null);
  if (!data?.events?.length) return null;
  return (
    <Panel title="Active anomalies" accent="bear" className="mb-3"><ErrorLine error={err} />
      <Table rows={data.events} cols={[{ k: "symbol", h: "Symbol" }, { k: "kind", h: "Kind" }, { k: "severity", h: "Sev", r: (a) => <Badge tone={a.severity === "critical" ? "bear" : "warn"}>{a.severity}</Badge> },
        { k: "explanation", h: "Explanation" }, { k: "blocks_entries", h: "Entries", r: (a) => a.blocks_entries ? <Badge tone="bear">blocked</Badge> : "allowed" },
        { k: "detected_at", h: "Since", r: (a) => fmt.time(a.detected_at) },
        { k: "x", h: "", sort: false, r: (a) => can(me, "trade.control") && <Btn small onClick={async () => { const note = window.prompt("Recovery check note (what was verified?)"); if (!note) return;
          try { await post(`/api/intelligence/anomalies/${a.id}/resolve`, { note }); reload(); } catch (e: any) { setErr(e.message); } }}>Resolve</Btn> }]} />
      <div className="mt-1 text-[10px] text-dim">Unavailable data (never fabricated): {data.unavailable_data.map((u: any) => u.data).join(", ")}</div>
    </Panel>
  );
}

export default function Predictive() {
  const [tab, setTab] = useState<(typeof TABS)[number]>("Regime");
  return (
    <div>
      <PageTitle sub="Advisory intelligence. Nothing on this page can place, size or approve an order — the Risk Engine keeps final authority.">Institutional Predictive Intelligence Center</PageTitle>
      <Anomalies />
      <div className="mb-3 flex flex-wrap gap-1">{TABS.map((t) => <button key={t} onClick={() => setTab(t)}
        className={clsx("rounded border px-3 py-1 text-[11px] uppercase tracking-wider", t === tab ? "border-cyan/60 bg-cyan/10 text-cyan" : "border-line text-dim hover:text-text")}>{t}</button>)}</div>
      {tab === "Regime" && <RegimeTab />}
      {tab === "Consensus" && <ConsensusTab />}
      {tab === "Probability" && <ProbabilityTab />}
      {tab === "Portfolio Risk" && <PortfolioTab />}
      {tab === "Failure" && <FailureTab />}
      {tab === "Execution" && <ExecutionTab />}
      {tab === "Governance" && <div className="space-y-3"><ModelsPanel /><DriftPanel /></div>}
      {tab === "Research Lab" && <ResearchLab />}
    </div>
  );
}
