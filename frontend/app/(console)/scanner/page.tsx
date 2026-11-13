"use client";
import { useEffect, useMemo, useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, dirTone, ErrorLine, gradeTone, inputCls, Panel, Stat, Table } from "@/components/ui";
import { fmt, put } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";

const TFS = ["", "M1", "M5", "M15", "M30", "H1", "H4", "D1"];

/** Scheduler state (Phase 8): what the scanner is set to do and what its last cycles actually did. */
function ScannerStatus() {
  const me = useMe();
  const manage = can(me, "strategy.manage");
  const { data: st, error, reload } = useApi<any>("/api/scanner/status", 15000);
  const [sel, setSel] = useState<string[]>([]);
  const [msg, setMsg] = useState<string | null>(null); const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (st) setSel(st.settings.timeframes); }, [st?.settings.timeframes.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!st) return <ErrorLine error={error} />;
  const c = st.last_cycle;
  const cfg = st.settings;
  const dirty = sel.join(",") !== cfg.timeframes.join(",");
  async function save() {
    setErr(null); setMsg(null);
    try { const r = await put("/api/scanner/settings", { timeframes: sel }); setMsg(`Scanning ${r.timeframes.join(", ")} from the next cycle.`); reload(); }
    catch (e: any) { setErr(e.message); }
  }
  const appl = Object.fromEntries(st.applicability.map((a: any) => [a.timeframe, a]));
  return (
    <div className="mb-3 grid gap-3 xl:grid-cols-[1fr_1fr]">
      <Panel title="Scanner schedule">
        <ErrorLine error={err} />
        {msg && <div className="mb-2 text-[11px] text-neon">{msg}</div>}
        <div className="text-[10px] uppercase text-dim">Timeframes ({cfg.source === "console" ? "set here" : "installation default"})</div>
        <div className="mt-1 grid grid-cols-7 gap-1 text-center text-[11px]">
          {cfg.selectable_timeframes.map((t: string) => {
            const on = sel.includes(t); const a = appl[t];
            return (
              <button key={t} disabled={!manage} onClick={() => setSel(on ? sel.filter((x) => x !== t) : cfg.selectable_timeframes.filter((x: string) => x === t || sel.includes(x)))}
                className={"rounded border px-1 py-1 " + (on ? "border-cyan text-cyan" : "border-line text-dim")}>
                <div>{t}</div><div className="text-[10px]">{a.applicable} strategies</div></button>);
          })}
        </div>
        {manage && <div className="mt-2 flex items-center gap-2"><Btn small tone="neon" disabled={!dirty || !sel.length} onClick={save}>Save timeframes</Btn>
          {dirty && <span className="text-[10px] text-warn">not saved</span>}</div>}
        <div className="mt-2 text-[10px] text-dim">
          {cfg.enforce_timeframes ? "Each strategy runs only on the timeframes it declares; the count is how many of the enabled strategies apply." : "Timeframe enforcement is off: every enabled strategy runs on every timeframe."}{" "}
          {cfg.once_per_bar ? "Each symbol and timeframe is analysed once per newly closed bar." : "Every symbol is re-analysed every cycle."}{" "}
          Cycle every {cfg.interval_seconds}s, budget {cfg.cycle_budget_seconds}s, job timeout {cfg.job_timeout_seconds}s, {cfg.max_parallel} in parallel. W1 is not offered: no original strategy declares it.
        </div>
      </Panel>
      <Panel title={c ? `Last cycle · ${fmt.time(c.started_at)}` : "Last cycle"}>
        {!c ? <div className="text-[11px] text-dim">No scan cycle has run yet. The analysis service scans once the MT5 connector is connected.</div> : <>
          <div className="grid grid-cols-3 gap-2 md:grid-cols-6">
            <Stat label="Analysed" value={c.analysed} tone="up" />
            <Stat label="Not due" value={c.not_due} sub="bar already analysed" />
            <Stat label="Deferred" value={c.deferred} sub="next cycle" tone={c.deferred ? "warn" : "plain"} />
            <Stat label="Timed out" value={c.timed_out} tone={c.timed_out ? "down" : "plain"} />
            <Stat label="Errors" value={c.errors} tone={c.errors ? "down" : "plain"} />
            <Stat label="Duration" value={`${c.duration_s}s`} sub={`of ${c.budget_s}s budget`} />
          </div>
          <div className="mt-2 text-[11px] text-dim">{c.symbols} symbols × {c.timeframes.join(", ")} = {c.jobs_total} jobs, {c.due} due
            {c.insufficient_data ? `, ${c.insufficient_data} with too few bars` : ""}{c.in_flight_elsewhere ? `, ${c.in_flight_elsewhere} still running from an earlier cycle` : ""}{c.cancelled ? `, ${c.cancelled} cancelled (stopping)` : ""}.</div>
          {c.error_samples.map((e: string) => <div key={e} className="text-[11px] text-bear">{e}</div>)}
          <div className="mt-2 text-[10px] uppercase text-dim">Recent cycles</div>
          <div className="flex flex-wrap gap-1">{st.history.slice(0, 12).map((h: any, i: number) => (
            <span key={i} title={`${fmt.time(h.started_at)}: ${h.analysed} analysed, ${h.deferred} deferred, ${h.errors} errors`}
              className={"rounded border px-1 text-[10px] " + (h.errors || h.timed_out ? "border-bear text-bear" : h.deferred ? "border-warn text-warn" : "border-line text-dim")}>{h.analysed}</span>))}</div>
        </>}
      </Panel>
    </div>
  );
}

export default function Scanner() {
  const live = useLive(["signal"]);
  const { data, error } = useApi<any[]>("/api/scanner", 20000, live.tick);
  const [q, setQ] = useState(""); const [cat, setCat] = useState(""); const [tf, setTf] = useState("");
  const [strat, setStrat] = useState(""); const [regime, setRegime] = useState(""); const [onlySig, setOnlySig] = useState(false);
  const cats = useMemo(() => Array.from(new Set((data || []).map((r) => r.category))).sort(), [data]);
  const regimes = useMemo(() => Array.from(new Set((data || []).map((r) => r.regime))).sort(), [data]);
  const rows = (data || []).filter((r) => (!q || r.symbol.toLowerCase().includes(q.toLowerCase())) && (!cat || r.category === cat) && (!tf || r.timeframe === tf)
    && (!regime || r.regime === regime) && (!strat || r.strategies.some((s: string) => s.includes(strat))) && (!onlySig || r.decision === "BUY" || r.decision === "SELL"));
  const best = rows.find((r) => r.grade === "A+" || r.grade === "A");
  return (
    <div>
      <PageTitle sub={`${data?.length ?? 0} symbol/timeframe analyses from the latest scan cycles`}>Market Scanner</PageTitle>
      <ErrorLine error={error} />
      <ScannerStatus />
      <Panel>
        <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-6">
          <input placeholder="search symbol" value={q} onChange={(e) => setQ(e.target.value)} className={inputCls} />
          <select value={cat} onChange={(e) => setCat(e.target.value)} className={inputCls}><option value="">all categories</option>{cats.map((c) => <option key={c}>{c}</option>)}</select>
          <select value={tf} onChange={(e) => setTf(e.target.value)} className={inputCls}>{TFS.map((t) => <option key={t} value={t}>{t || "all timeframes"}</option>)}</select>
          <input placeholder="strategy filter" value={strat} onChange={(e) => setStrat(e.target.value)} className={inputCls} />
          <select value={regime} onChange={(e) => setRegime(e.target.value)} className={inputCls}><option value="">all regimes</option>{regimes.map((c) => <option key={c}>{c}</option>)}</select>
          <label className="flex items-center gap-2 text-[11px] text-dim"><input type="checkbox" checked={onlySig} onChange={(e) => setOnlySig(e.target.checked)} />BUY/SELL only</label>
        </div>
        <Table rows={rows} empty="No scan results yet — the worker populates this after its first cycle." rowKey={(r) => r.symbol + r.timeframe}
          cols={[
            { k: "symbol", h: "Symbol", r: (r) => <span className={best && r === best ? "text-neon" : ""}>{r.symbol}</span> },
            { k: "timeframe", h: "TF" }, { k: "category", h: "Class" },
            { k: "htf_bias", h: "Trend", r: (r) => r.htf_bias > 0 ? <span className="text-neon">▲ bull</span> : r.htf_bias < 0 ? <span className="text-bear">▼ bear</span> : <span className="text-dim">—</span> },
            { k: "decision", h: "Signal", r: (r) => <Badge tone={dirTone(r.decision)}>{r.decision || "—"}</Badge> },
            { k: "strategies", h: "Strategy", sort: false, r: (r) => <span className="block max-w-[200px] truncate text-dim">{r.strategies.join(", ") || "—"}</span> },
            { k: "score", h: "AI score", r: (r) => <span>{r.score ?? "—"} <Badge tone={gradeTone(r.grade)}>{r.grade}</Badge></span> },
            { k: "entry", h: "Entry", r: (r) => fmt.px(r.entry) }, { k: "stop_loss", h: "SL", r: (r) => fmt.px(r.stop_loss) },
            { k: "take_profit", h: "TP", r: (r) => fmt.px(r.take_profit) }, { k: "risk_reward", h: "R:R" },
            { k: "regime", h: "Regime" }, { k: "news", h: "News", r: (r) => <Badge tone={r.news === "CLEAR" ? "neon" : r.news === "RESTRICTED" ? "bear" : "warn"}>{r.news}</Badge> },
            { k: "analysed_at", h: "Status", r: (r) => <span className="text-dim">{fmt.time(r.analysed_at)}</span> },
          ]} />
      </Panel>
    </div>
  );
}
