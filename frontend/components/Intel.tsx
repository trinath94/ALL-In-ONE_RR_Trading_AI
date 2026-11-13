"use client";
// Shared Part 25/26 panels (models, research lab, drift, jobs). All data comes from /api/learning & /api/intelligence.
import { useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis } from "recharts";
import { can, useMe } from "./Shell";
import { Badge, Btn, Confirm, Empty, ErrorLine, inputCls, Panel, Table } from "./ui";
import { api, fmt, patch, post } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const tip = { background: "#0a0d10", border: "1px solid #1a2329", fontSize: 11 };
export const STAGE_TONE: Record<string, any> = { DISCOVERED: "dim", DEVELOPING: "cyan", EXPERIMENTAL: "cyan", BACKTESTING: "violet",
  VALIDATION: "violet", DEMO_TESTING: "warn", APPROVAL_REQUIRED: "warn", APPROVED: "neon", PRODUCTION: "neon", REJECTED: "bear", DISABLED: "bear" };
export const MODEL_TONE: Record<string, any> = { TRAINED: "dim", VALIDATED: "cyan", APPROVED: "warn", PRODUCTION: "neon", RETIRED: "dim", REJECTED: "bear" };
const LIFECYCLE = ["DISCOVERED", "DEVELOPING", "EXPERIMENTAL", "BACKTESTING", "VALIDATION", "DEMO_TESTING", "APPROVAL_REQUIRED", "APPROVED", "PRODUCTION"];

export function Verdict({ v }: { v?: string }) {
  const tone = v === "POSITIVE_EDGE" ? "neon" : v === "NEGATIVE_EDGE" ? "bear" : v === "INCONCLUSIVE" ? "warn" : "dim";
  return <Badge tone={tone}>{(v || "NO_DATA").replace(/_/g, " ")}</Badge>;
}

export const ci = (a?: (number | null)[] | null, d = 2) => (a && a[0] !== null && a[0] !== undefined ? `[${fmt.num(a[0], d)}, ${fmt.num(a[1], d)}]` : "—");

export function ReliabilityChart({ bins }: { bins?: { mean_predicted: number; observed: number; count: number }[] }) {
  if (!bins?.length) return <Empty>No calibration data</Empty>;
  return (
    <div className="h-56"><ResponsiveContainer><ScatterChart margin={{ left: -10 }}>
      <CartesianGrid stroke="#1a2329" /><XAxis type="number" dataKey="mean_predicted" domain={[0, 1]} stroke="#6b7f8a" fontSize={10} name="predicted" />
      <YAxis type="number" dataKey="observed" domain={[0, 1]} stroke="#6b7f8a" fontSize={10} name="observed" /><ZAxis dataKey="count" range={[30, 300]} name="samples" />
      <Tooltip contentStyle={tip} /><Scatter data={[{ mean_predicted: 0, observed: 0, count: 1 }, { mean_predicted: 1, observed: 1, count: 1 }]} line={{ stroke: "#3a4a54", strokeDasharray: "4 4" }} fill="transparent" />
      <Scatter data={bins} fill="#22e4ff" /></ScatterChart></ResponsiveContainer></div>
  );
}

export function ModelsPanel() {
  const me = useMe();
  const { data, reload } = useApi<any[]>("/api/learning/models", 30000);
  const [sel, setSel] = useState<any>(null);
  const [dlg, setDlg] = useState<{ m: any; to: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  async function go(notes: string) {
    try { await post(`/api/learning/models/${dlg!.m.id}/transition`, { to: dlg!.to, notes }); setDlg(null); reload(); } catch (e: any) { setErr(e.message); setDlg(null); }
  }
  return (
    <Panel title="Model governance" right={<span className="text-[10px] text-dim">training never promotes; production needs administrator approval</span>}>
      <ErrorLine error={err} />
      <Table rows={data} onRow={setSel} empty="No models trained yet (learner job 'train_probability')"
        cols={[{ k: "model_key", h: "Model" }, { k: "version", h: "Ver" }, { k: "algorithm", h: "Algorithm" },
          { k: "status", h: "Status", r: (m) => <Badge tone={MODEL_TONE[m.status]}>{m.status}</Badge> },
          { k: "rows", h: "Dataset", r: (m) => m.dataset?.rows ? `${m.dataset.rows} (test ${m.dataset.test})` : "—" },
          { k: "bss", h: "Brier skill", r: (m) => fmt.num(m.metrics?.test?.brier_skill_score, 4) },
          { k: "auc", h: "AUC", r: (m) => fmt.num(m.metrics?.test?.auc, 3) },
          { k: "ece", h: "ECE", r: (m) => fmt.num(m.calibration?.test_reliability?.ece, 3) },
          { k: "trained_at", h: "Trained", r: (m) => fmt.time(m.trained_at) },
          { k: "act", h: "", sort: false, r: (m) => can(me, "strategy.manage") && <div className="flex gap-1" onClick={(e) => e.stopPropagation()}>
            {m.status === "VALIDATED" && <Btn small tone="warn" onClick={() => setDlg({ m, to: "APPROVED" })}>Approve</Btn>}
            {m.status === "APPROVED" && can(me, "trade.live") && <Btn small tone="neon" onClick={() => setDlg({ m, to: "PRODUCTION" })}>Promote</Btn>}
            {["VALIDATED", "APPROVED"].includes(m.status) && <Btn small tone="bear" onClick={() => setDlg({ m, to: "REJECTED" })}>Reject</Btn>}
            {m.status === "PRODUCTION" && m.rollback_to && can(me, "trade.live") &&
              <Btn small tone="dim" onClick={async () => { try { await post(`/api/learning/models/${m.model_key}/rollback`); reload(); } catch (e: any) { setErr(e.message); } }}>Rollback</Btn>}
          </div> }]} />
      {sel && (
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          <div className="text-[11px]">
            <div className="mb-1 text-cyan">{sel.model_key} v{sel.version} — {sel.notes}</div>
            <div className="text-dim">Candidates (validation log-loss):</div>
            {Object.entries(sel.metrics?.candidates || {}).map(([k, v]: any) => <div key={k}>{k}: {v.val_log_loss ?? v.error}</div>)}
            <div className="mt-1 text-dim">Test: n={sel.metrics?.test?.n}, Brier {sel.metrics?.test?.brier} vs base-rate {sel.metrics?.test?.brier_base_rate};
              Brier-gain CI low {sel.metrics?.test?.brier_gain_ci95_low}; AUC−0.5 CI low {sel.metrics?.test?.auc_minus_half_ci95_low}</div>
            <div className="mt-1 text-dim">Dataset: {JSON.stringify(sel.dataset)}</div>
          </div>
          <div><div className="text-[10px] uppercase text-dim">Reliability diagram (final test)</div><ReliabilityChart bins={sel.calibration?.test_reliability?.bins} /></div>
        </div>
      )}
      <Confirm open={!!dlg} title={`${dlg?.to} ${dlg?.m.model_key} v${dlg?.m.version}`} tone={dlg?.to === "PRODUCTION" ? "bear" : "warn"}
        phrase={dlg?.to === "PRODUCTION" ? "PROMOTE MODEL" : undefined}
        body={dlg?.to === "PRODUCTION" ? "This model will influence live decisions (still subject to the Risk Engine). The current production version is retired and kept as rollback target."
          : "The transition is recorded in the approval log."} onCancel={() => setDlg(null)} onConfirm={(t) => go(t || "")} />
    </Panel>
  );
}

export function ResearchLab() {
  const me = useMe();
  const { data, reload } = useApi<any[]>("/api/learning/experiments", 30000);
  const [sel, setSel] = useState<any>(null);
  const [src, setSrc] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const manage = can(me, "strategy.manage");
  async function open(e: any) { const d = await api(`/api/learning/experiments/${e.id}`); setSel(d); setSrc(d.source_code || ""); setErr(null); }
  async function act(fn: () => Promise<any>) { setBusy(true); setErr(null); try { await fn(); await reload(); if (sel) await open(sel); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } }
  const next = (st: string) => LIFECYCLE[LIFECYCLE.indexOf(st) + 1];
  const v = sel?.validation_summary;
  return (
    <div className="space-y-3">
      <Panel title="Strategy lifecycle" right={manage && <div className="flex gap-2">
        <Btn small busy={busy} onClick={() => act(() => post("/api/learning/experiments/library"))}>Seed reference library</Btn>
        <Btn small tone="violet" busy={busy} onClick={() => act(() => post("/api/learning/research"))}>Claude research</Btn></div>}>
        <ErrorLine error={err} />
        <div className="grid gap-2 overflow-x-auto md:grid-cols-5 xl:grid-cols-9">
          {LIFECYCLE.map((st) => (
            <div key={st} className="min-w-[140px] rounded border border-line p-2">
              <div className="mb-1 text-[9px] uppercase tracking-wider text-dim">{st.replace("_", " ")}</div>
              {(data || []).filter((e) => e.stage === st).map((e) => (
                <button key={e.id} onClick={() => open(e)} className="mb-1 block w-full truncate rounded border border-violet/30 px-1.5 py-1 text-left text-[10.5px] hover:bg-violet/10">
                  {e.code || e.title}<span className="ml-1 text-dim">v{e.version ?? 0}</span></button>))}
            </div>))}
        </div>
        <div className="mt-2 text-[10px] text-dim">Rejected/disabled: {(data || []).filter((e) => ["REJECTED", "DISABLED"].includes(e.stage)).map((e) => e.code || e.title).join(", ") || "none"}</div>
      </Panel>
      {sel && (
        <Panel title={`${sel.code || sel.title} · v${sel.version ?? 0}`} right={<Badge tone={STAGE_TONE[sel.stage]}>{sel.stage}</Badge>}>
          <p className="text-[12px]">{sel.hypothesis}</p>
          {sel.duplicate_of?.length > 0 && <div className="mt-1 text-[11px] text-warn">Closest existing: {sel.duplicate_of.map((d: any) => `${d.strategy} (${d.overlap})`).join(", ")}</div>}
          <div className="mt-2 flex flex-wrap gap-2">
            {manage && sel.source_code && ["DEVELOPING", "EXPERIMENTAL"].includes(sel.stage) && <Btn small busy={busy} onClick={() => act(() => post(`/api/learning/experiments/${sel.id}/unit`))}>Run sandbox unit tests</Btn>}
            {manage && sel.stage === "EXPERIMENTAL" && <Btn small busy={busy} onClick={() => act(() => post("/api/learning/jobs/experiments/run"))}>Queue validation (learner)</Btn>}
            {manage && sel.stage === "DISCOVERED" && <Btn small tone="violet" busy={busy} onClick={() => act(() => post(`/api/learning/experiments/${sel.id}/develop`))}>Claude: implement</Btn>}
            {manage && next(sel.stage) && <Btn small tone="warn" busy={busy} onClick={() => act(() => post(`/api/experiments/${sel.id}/stage`, { stage: next(sel.stage), evidence: {} }))}>Advance → {next(sel.stage)}</Btn>}
            {manage && sel.versions?.length > 0 && <Btn small tone="dim" busy={busy} onClick={() => act(() => post(`/api/learning/experiments/${sel.id}/rollback`))}>Rollback version</Btn>}
            {manage && !["REJECTED", "DISABLED"].includes(sel.stage) && <Btn small tone="bear" busy={busy} onClick={() => act(() => post(`/api/experiments/${sel.id}/stage`, { stage: "REJECTED", evidence: {} }))}>Reject</Btn>}
          </div>
          {sel.static_errors?.length > 0 && <div className="mt-2 text-[11px] text-bear">Static analysis: {sel.static_errors.join("; ")}</div>}
          {sel.unit && <div className="mt-2 text-[11px]">Unit tests: <Badge tone={sel.unit.passed ? "neon" : "bear"}>{sel.unit.passed ? "passed" : "failed"}</Badge> signals {JSON.stringify(sel.unit.signals)} {sel.unit.problems?.join("; ")}</div>}
          {v && (
            <div className="mt-3 grid gap-3 lg:grid-cols-2">
              <div className="text-[11px]">
                <div className="mb-1 text-cyan">Validation (identical costs & data; final test touched only if validation passes)</div>
                {Object.entries(v.checks || {}).map(([k, ok]: any) => <div key={k}><Badge tone={ok ? "neon" : "bear"}>{ok ? "pass" : "fail"}</Badge> {k.replace(/_/g, " ")}</div>)}
                <div className="mt-1 text-dim">Expectancy R lower bound (Bonferroni): {fmt.num(v.expectancy_r_lower_bound, 3)}</div>
                <Table rows={[{ s: "validation", ...v.validation }, { s: "final test", ...(v.final_test || {}) }, { s: `benchmark`, ...(v.benchmark || {}) }]}
                  cols={[{ k: "s", h: "Set" }, { k: "total_trades", h: "Trades" }, { k: "profit_factor", h: "PF" }, { k: "net_profit", h: "Net" },
                    { k: "max_drawdown_pct", h: "DD%" }, { k: "win_rate", h: "Win", r: (r) => fmt.pct(r.win_rate, 1) }]} />
              </div>
              <div className="text-[11px]">
                <div className="text-dim">Walk-forward folds</div>
                <Table rows={v.walk_forward} cols={[{ k: "fold", h: "#" }, { k: "trades", h: "Trades" }, { k: "net", h: "Net" }, { k: "pf", h: "PF" }]} />
                <div className="mt-1 text-dim">Cost sensitivity: {(v.cost || []).map((c: any) => `${c.spread_x}x→${fmt.num(c.net)}`).join(" · ")}</div>
                <div className="text-dim">Monte Carlo P(loss): {fmt.pct(v.monte_carlo?.probability_of_loss, 1)} · DD p95 {v.monte_carlo?.max_drawdown_pct?.[95]}%</div>
              </div>
            </div>
          )}
          {sel.forward && <div className="mt-2 text-[11px]">DEMO forward (shadow): {sel.forward.resolved} resolved, {sel.forward.wins} wins, ΣR {fmt.num(sel.forward.sum_r)} — {sel.forward.note}</div>}
          {manage && <div className="mt-3"><div className="text-[10px] uppercase text-dim">Source (statically analysed; executes only in the sandbox)</div>
            <textarea value={src} onChange={(e) => setSrc(e.target.value)} rows={12} className={inputCls + " font-mono text-[11px]"} />
            <Btn small busy={busy} onClick={() => act(() => post(`/api/learning/experiments/${sel.id}/source`, { source: src }))}>Save as new version</Btn></div>}
          <div className="mt-3 text-[10px] uppercase text-dim">History</div>
          {sel.history?.map((h: any, i: number) => <div key={i} className="text-[11px] text-dim">{fmt.time(h.at)} · {h.from} → {h.to} · {h.notes}</div>)}
        </Panel>
      )}
    </div>
  );
}

export function DriftPanel() {
  const { data } = useApi<any[]>("/api/learning/drift", 60000);
  return <Panel title="Drift monitoring"><Table rows={data} empty="No drift detected (or not enough recent predictions/trades)"
    cols={[{ k: "at", h: "Time", r: (d) => fmt.time(d.at) }, { k: "scope", h: "Scope" }, { k: "subject", h: "Subject" }, { k: "metric", h: "Metric" },
      { k: "value", h: "Value" }, { k: "threshold", h: "Threshold" }, { k: "severity", h: "Sev", r: (d) => <Badge tone={d.severity === "critical" ? "bear" : "warn"}>{d.severity}</Badge> }]} /></Panel>;
}

export function JobsPanel() {
  const me = useMe();
  const { data, reload } = useApi<any>("/api/learning/jobs", 15000);
  return (
    <Panel title="Learning jobs (learner process)">
      <div className="mb-2 flex flex-wrap gap-1">{data?.schedule?.map((j: any) => (
        <span key={j.name} className="rounded border border-line px-1.5 py-0.5 text-[10px]">{j.name} · every {Math.round(j.period_seconds / 60)}m
          {can(me, "strategy.manage") && <button className="ml-1 text-cyan" onClick={async () => { await post(`/api/learning/jobs/${j.name}/run`); reload(); }}>run</button>}</span>))}</div>
      <Table rows={data?.history} empty="No jobs have run yet — start the learner service"
        cols={[{ k: "started_at", h: "Started", r: (j) => fmt.time(j.started_at) }, { k: "job", h: "Job" },
          { k: "status", h: "Status", r: (j) => <Badge tone={j.status === "DONE" ? "neon" : j.status === "SKIPPED" ? "dim" : j.status === "RUNNING" ? "cyan" : "bear"}>{j.status}</Badge> },
          { k: "attempts", h: "Tries" }, { k: "result", h: "Result", sort: false, r: (j) => <span className="block max-w-[420px] truncate text-dim">{j.error || JSON.stringify(j.result)}</span> }]} />
    </Panel>
  );
}

export function RecommendationsPanel() {
  const me = useMe();
  const { data, reload } = useApi<any[]>("/api/learning/recommendations", 60000);
  return <Panel title="Evidence-based recommendations"><Table rows={data} empty="No recommendations yet"
    cols={[{ k: "at", h: "Time", r: (r) => fmt.time(r.at) }, { k: "source", h: "Source" }, { k: "subject", h: "Subject" },
      { k: "recommendation", h: "Recommendation", r: (r) => <span className="block max-w-[520px] whitespace-normal">{r.recommendation}</span> },
      { k: "status", h: "Status", r: (r) => <Badge tone={r.status === "OPEN" ? "warn" : "dim"}>{r.status}</Badge> },
      { k: "a", h: "", sort: false, r: (r) => can(me, "strategy.manage") && r.status === "OPEN" && <div className="flex gap-1">
        <Btn small onClick={async () => { await patch(`/api/learning/recommendations/${r.id}`, { status: "ACCEPTED" }); reload(); }}>Accept</Btn>
        <Btn small tone="dim" onClick={async () => { await patch(`/api/learning/recommendations/${r.id}`, { status: "DISMISSED" }); reload(); }}>Dismiss</Btn></div> }]} /></Panel>;
}

export function MonthlyLine({ monthly }: { monthly?: Record<string, number> }) {
  const rows = Object.entries(monthly || {}).map(([m, v]) => ({ m, v }));
  if (!rows.length) return <Empty>No monthly data</Empty>;
  return <div className="h-40"><ResponsiveContainer><LineChart data={rows}><CartesianGrid stroke="#1a2329" /><XAxis dataKey="m" stroke="#6b7f8a" fontSize={10} />
    <YAxis stroke="#6b7f8a" fontSize={10} /><Tooltip contentStyle={tip} /><Line dataKey="v" stroke="#39ff88" dot={false} /></LineChart></ResponsiveContainer></div>;
}
