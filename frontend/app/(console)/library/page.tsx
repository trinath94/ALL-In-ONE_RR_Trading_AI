"use client";
import { useMemo, useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, inputCls, Panel, Stat, Table } from "@/components/ui";
import { api, fmt, patch } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const DATA: Record<string, { tone: any; short: string }> = {
  runnable: { tone: "cyan", short: "runnable" },
  per_symbol: { tone: "warn", short: "per symbol" },
  not_supplied: { tone: "warn", short: "data not supplied" },
  unsupported: { tone: "bear", short: "MT5 lacks data" },
};
const TFS = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"];
const SOURCES: Record<string, string> = {
  BACKTEST: "Backtest (in-sample)", OOS: "Out-of-sample", WALKFORWARD: "Walk-forward", PAPER: "Paper", LIVE: "Live",
};

/** The 215 production strategies with their real registry metadata, what the engine can run today, and evidence that
 *  is actually recorded. Nothing here is estimated. */
export default function Library() {
  const me = useMe();
  const { data, error, reload } = useApi<any[]>("/api/strategies", 60000);
  const { data: sum, reload: reloadSum } = useApi<any>("/api/strategies/summary", 60000);
  const { data: exps, reload: reloadExp } = useApi<any[]>("/api/experiments");
  const [q, setQ] = useState(""); const [cat, setCat] = useState(""); const [ds, setDs] = useState("");
  const [tf, setTf] = useState(""); const [view, setView] = useState("");
  const [sel, setSel] = useState<any>(null);
  const [params, setParams] = useState("");
  const [versions, setVersions] = useState<any[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [mode, setMode] = useState("DEMO");
  const manage = can(me, "strategy.manage");
  const cats = useMemo(() => Array.from(new Set((data || []).map((s) => s.category))), [data]);
  const rows = (data || []).filter((s) => (!cat || s.category === cat) && (!ds || s.data_status === ds)
    && (!tf || s.timeframes.includes(tf))
    && (!view || (view === "risky" && s.risky) || (view === "demo" && s.scanned.DEMO) || (view === "live" && s.scanned.LIVE)
      || (view === "signals" && s.activity.signals > 0) || (view === "errors" && s.activity.errors > 0)
      || (view === "changed" && s.baseline !== "unchanged"))
    && (!q || (s.name + " " + s.code).toLowerCase().includes(q.toLowerCase())));
  const cur = sel && (data || []).find((s) => s.code === sel.code) || sel;
  async function choose(s: any) { setSel(s); setParams(JSON.stringify(s.params, null, 2)); setErr(null); setVersions(await api(`/api/strategies/${s.code}/versions`)); }
  async function save(body: any) {
    setErr(null);
    try { await patch(`/api/strategies/${cur.code}`, { ...body, mode }); await reload(); await reloadSum(); } catch (e: any) { setErr(e.message); }
  }
  async function stage(id: number, st: string) {
    try { await api(`/api/experiments/${id}/stage`, { method: "POST", body: { stage: st, evidence: {} } }); reloadExp(); } catch (e: any) { setErr(e.message); }
  }
  const STAGES = ["RESEARCH", "IMPLEMENTATION", "UNIT_TESTING", "BACKTESTING", "OUT_OF_SAMPLE", "WALK_FORWARD", "PAPER_TRADING", "ADMIN_APPROVAL", "PRODUCTION"];
  const notRunnable = sum ? sum.data_status.filter((d: any) => d.status !== "runnable").reduce((a: number, d: any) => a + d.count, 0) : null;
  const canSignal = (m: string) => (data || []).filter((s) => s.scanned[m] && s.data_status === "runnable").length;
  const yes = (b: boolean) => b ? <Badge tone="neon">scanned</Badge> : <span className="text-dim">—</span>;
  return (
    <div>
      <PageTitle sub="The original production strategies, as registered in code. Data status, scan state, baseline integrity and activity are read from the engine; performance appears only where trades were actually recorded. AI proposals live separately as experiments.">Strategy Library</PageTitle>
      <ErrorLine error={error || err} />
      {sum && (
        <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-6">
          <Stat label="Strategies" value={`${sum.total} / ${sum.expected_total}`} sub="registered / baseline" tone={sum.total === sum.expected_total ? "up" : "down"} />
          <Stat label="Baseline integrity" value={sum.baseline ? `${sum.baseline.unchanged} / ${sum.baseline.baseline_count}` : "unknown"}
            sub={sum.baseline ? (sum.baseline.modified.length ? `${sum.baseline.modified.length} modified` : "identical to the frozen baseline") : "baseline inventory not shipped"}
            tone={sum.baseline && !sum.baseline.modified.length && !sum.baseline.not_in_baseline.length ? "up" : "warn"} />
          <Stat label="Runnable on MT5 data" value={sum.data_status.find((d: any) => d.status === "runnable").count} sub={`${notRunnable} cannot run today`} />
          <Stat label="Scanned in DEMO" value={sum.scanned.DEMO} sub={`enabled; ${canSignal("DEMO")} can produce signals`} />
          <Stat label="Scanned in LIVE" value={sum.scanned.LIVE} sub={`${canSignal("LIVE")} can produce signals; never risky`} />
          <Stat label={`Signals (${sum.activity.days} d)`} value={sum.activity.strategies_with_signals}
            sub={`strategies, from ${sum.activity.analyses} stored analyses`} tone={sum.activity.strategies_with_errors.length ? "warn" : "plain"} />
        </div>
      )}
      {sum && (
        <div className="mb-3 grid gap-3 xl:grid-cols-2">
          <Panel title="Data the engine can supply">
            {sum.data_status.map((d: any) => (
              <button key={d.status} onClick={() => setDs(ds === d.status ? "" : d.status)}
                className={"flex w-full items-center justify-between py-0.5 text-left text-[11px] " + (ds === d.status ? "text-cyan" : "")}>
                <span><Badge tone={DATA[d.status].tone}>{DATA[d.status].short}</Badge> {d.label}</span><b>{d.count}</b>
              </button>))}
            <div className="mt-1 text-[10px] text-dim">Click a row to filter. Risky (grid / martingale / DCA): {sum.risky.join(", ")}. Disabled by default and never scanned in LIVE.</div>
          </Panel>
          <Panel title="Declared timeframes">
            <div className="grid grid-cols-8 gap-1 text-center text-[11px]">
              {TFS.map((t) => (
                <button key={t} onClick={() => setTf(tf === t ? "" : t)} className={"rounded border px-1 py-1 " + (tf === t ? "border-cyan text-cyan" : "border-line")}>
                  <div className="text-dim">{t}</div><b>{sum.timeframes[t]}</b></button>))}
            </div>
            <div className="mt-2 text-[10px] text-warn">{sum.timeframes_note}</div>
          </Panel>
        </div>
      )}
      <div className="grid gap-3 xl:grid-cols-[1fr_440px]">
        <Panel title={`Strategies (${rows.length} of ${(data || []).length})`}>
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <div className="w-44"><input className={inputCls} placeholder="search" value={q} onChange={(e) => setQ(e.target.value)} /></div>
            <div className="w-40"><select className={inputCls} value={cat} onChange={(e) => setCat(e.target.value)}><option value="">all categories</option>{cats.map((c) => <option key={c}>{c}</option>)}</select></div>
            <div className="w-48"><select className={inputCls} value={view} onChange={(e) => setView(e.target.value)}>
              <option value="">all strategies</option><option value="demo">scanned in DEMO</option><option value="live">scanned in LIVE</option>
              <option value="risky">risky</option><option value="signals">signalled recently</option><option value="errors">errors recently</option>
              <option value="changed">differs from baseline</option></select></div>
            {ds && <Badge tone={DATA[ds].tone}>{DATA[ds].short}</Badge>}{tf && <Badge tone="cyan">{tf}</Badge>}
            {(cat || ds || tf || view || q) && <Btn small tone="dim" onClick={() => { setCat(""); setDs(""); setTf(""); setView(""); setQ(""); }}>Clear filters</Btn>}
          </div>
          <Table rows={rows} onRow={choose} rowKey={(r) => r.code}
            cols={[{ k: "name", h: "Strategy" }, { k: "category", h: "Category" },
              { k: "data_status", h: "Data", r: (s) => <Badge tone={DATA[s.data_status].tone}>{DATA[s.data_status].short}</Badge> },
              { k: "timeframes", h: "Timeframes", r: (s) => <span className="text-[10px]">{s.timeframes.join(" ")}</span> },
              { k: "demo", h: "DEMO", r: (s) => yes(s.scanned.DEMO) }, { k: "live", h: "LIVE", r: (s) => yes(s.scanned.LIVE) },
              { k: "risky", h: "Risk", r: (s) => s.risky ? <Badge tone="bear">risky</Badge> : "" },
              { k: "signals", h: "Signals 7d", r: (s) => s.activity.errors ? <span>{s.activity.signals} <Badge tone="bear">{s.activity.errors} err</Badge></span> : s.activity.signals },
              { k: "baseline", h: "Baseline", r: (s) => s.baseline === "unchanged" ? <span className="text-dim">unchanged</span> : <Badge tone="warn">{s.baseline}</Badge> }]} />
        </Panel>
        {cur && (
          <Panel title={cur.name}>
            <p className="text-[12px]">{cur.description}</p>
            <div className="mt-2 grid grid-cols-[110px_1fr] gap-1 text-[11px]">
              <div className="text-dim">Code</div><div>{cur.code} · v{cur.version}</div>
              <div className="text-dim">Category</div><div>{cur.category} · scores into {cur.component}</div>
              <div className="text-dim">Timeframes</div><div>{cur.timeframes.join(", ")} <span className="text-dim">(declared; not enforced by the scanner yet)</span></div>
              <div className="text-dim">Suited regimes</div><div>{cur.regimes.join(", ") || "all"}</div>
              <div className="text-dim">Minimum bars</div><div>{cur.min_bars}</div>
              <div className="text-dim">Data</div><div><Badge tone={DATA[cur.data_status].tone}>{DATA[cur.data_status].short}</Badge>
                {cur.data_notes.map((n: string) => <div key={n}>{n}</div>)}</div>
              <div className="text-dim">Risk</div><div>{cur.risky ? <span className="text-bear">Risky position sizing (grid / martingale / DCA). Disabled by default; never scanned in LIVE.</span> : "standard"}</div>
              <div className="text-dim">Scanned</div><div>DEMO {cur.scanned.DEMO ? "yes" : "no"} · LIVE {cur.scanned.LIVE ? "yes" : "no"} · stage {cur.stage}</div>
              <div className="text-dim">Code hash</div><div>{cur.code_hash} · <span className={cur.baseline === "unchanged" ? "text-neon" : "text-warn"}>{cur.baseline}</span></div>
              <div className="text-dim">Last 7 days</div><div>{cur.activity.signals} signal(s){cur.activity.last_signal_at ? `, last ${fmt.time(cur.activity.last_signal_at)}` : ""}{cur.activity.errors ? `, ${cur.activity.errors} error(s)` : ""}</div>
              <div className="text-dim">Open positions</div><div>{cur.active_positions}</div>
            </div>
            <div className="mt-3 text-[10px] uppercase text-dim">Recorded performance</div>
            {Object.keys(cur.performance_by_source).length === 0
              ? <div className="text-[11px] text-dim">No recorded trades. No statistics are shown until there are some.</div>
              : Object.entries(cur.performance_by_source).map(([src, p]: any) => (
                <div key={src} className="text-[11px]">{SOURCES[src] || src}: {p.trades} trades, {p.trades ? `${Math.round(100 * p.wins / p.trades)}% wins` : "—"}</div>))}
            {manage && <>
              <div className="mt-3 flex items-center gap-2"><div className="w-28"><select className={inputCls} value={mode} onChange={(e) => setMode(e.target.value)}><option>DEMO</option><option>LIVE</option></select></div>
                <Btn tone="neon" small disabled={mode === "LIVE" && cur.risky} onClick={() => save({ enabled: true })}>Enable</Btn><Btn tone="warn" small onClick={() => save({ enabled: false })}>Disable</Btn></div>
              {cur.data_status !== "runnable" && <div className="mt-1 text-[10px] text-warn">This strategy cannot produce signals until its data is supplied, even if enabled.</div>}
              <div className="mt-3 text-[10px] uppercase text-dim">Parameters (per mode)</div>
              <textarea value={params} onChange={(e) => setParams(e.target.value)} rows={8} className={inputCls + " font-mono"} />
              <Btn small onClick={() => { try { save({ params: JSON.parse(params) }); } catch { setErr("Parameters must be valid JSON"); } }}>Save parameters</Btn>
            </>}
            <div className="mt-3 text-[10px] uppercase text-dim">Version history</div>
            {versions.map((v, i) => <div key={i} className="text-[11px]">{v.version} · {v.code_hash} · {fmt.time(v.at)} · {v.by}</div>)}
          </Panel>
        )}
      </div>
      <Panel title="Experimental strategies (isolated from production)" className="mt-3">
        <Table rows={exps} empty="No experiments. Use AI Intelligence → Performance review & discovery."
          cols={[{ k: "title", h: "Title" }, { k: "hypothesis", h: "Hypothesis", r: (e) => <span className="block max-w-[420px] truncate">{e.hypothesis}</span> },
            { k: "stage", h: "Stage", r: (e) => <Badge tone={e.stage === "REJECTED" ? "bear" : "violet"}>{e.stage}</Badge> },
            { k: "act", h: "", sort: false, r: (e) => manage && e.stage !== "REJECTED" && e.stage !== "ADMIN_APPROVAL" && <div className="flex gap-1">
              <Btn small onClick={() => stage(e.id, STAGES[STAGES.indexOf(e.stage) + 1])}>Advance</Btn><Btn small tone="bear" onClick={() => stage(e.id, "REJECTED")}>Reject</Btn></div> }]} />
      </Panel>
    </div>
  );
}
