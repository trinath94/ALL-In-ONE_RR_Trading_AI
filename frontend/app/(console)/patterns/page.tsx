"use client";
import { useState } from "react";
import { PageTitle } from "@/components/Shell";
import { Badge, ErrorLine, Panel, Stat, Table } from "@/components/ui";
import { api, fmt } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const STATE: Record<string, any> = { FORMING: "cyan", COMPLETED: "violet", CONFIRMED: "warn", TARGET_REACHED: "neon",
  INVALIDATED: "bear", EXPIRED: "dim" };
const pct = (v: number | null) => (v == null ? "—" : `${(v * 100).toFixed(0)}%`);
const px = (v: any) => (typeof v === "number" ? v.toPrecision(6) : "—");

/** Part 28: the pattern catalogue (derived from the original strategies), detections from the new harmonic and
 *  Elliott detectors with their lifecycle, the strategies that support or contradict them, and outcome statistics
 *  measured on price bars. Advisory: detections never change a trading decision. */
export default function Patterns() {
  const [state, setState] = useState("");
  const [liveOnly, setLiveOnly] = useState(false);
  const q = `limit=200${state ? `&state=${state}` : ""}${liveOnly ? "&live_only=true" : ""}`;
  const { data, error } = useApi<any[]>(`/api/patterns/detections?${q}`, 20000);
  const { data: stats } = useApi<any>(`/api/patterns/stats?live_only=${liveOnly}`, 60000);
  const { data: cat } = useApi<any[]>("/api/patterns/catalogue", 0);
  const [sel, setSel] = useState<any>(null);
  async function open(d: any) { setSel({ loading: true }); setSel(await api(`/api/patterns/detections/${d.id}`)); }
  const fams = (cat || []).filter((c) => c.is_pattern).reduce((m: Record<string, number>, c) => ({ ...m, [c.family]: (m[c.family] || 0) + 1 }), {});
  const open_ = (data || []).filter((d) => ["FORMING", "COMPLETED", "CONFIRMED"].includes(d.state)).length;
  return (
    <div>
      <PageTitle sub="Harmonic (Gartley, Bat, Butterfly, Crab, AB=CD) and conservative Elliott impulse detectors run beside the 215 original strategies, which already cover classical, candlestick, SMC, ICT, Wyckoff, VSA and gap patterns. Detections are advisory: they are recorded, related to the strategies and measured, but never change a decision.">
        Pattern Intelligence</PageTitle>
      <ErrorLine error={error} />
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        <Stat label="Catalogue" value={(cat || []).filter((c) => c.is_pattern).length} sub={`${Object.keys(fams).length} families`} />
        <Stat label="Open formations" value={open_} sub="in the list below" />
        <Stat label="Measured outcomes" value={(stats?.rows || []).reduce((n: number, r: any) => n + r.measured, 0)} sub={`last ${stats?.days ?? 90} days`} />
        <Stat label="Groups with enough data" value={(stats?.rows || []).filter((r: any) => r.sufficient).length} sub={`≥ ${stats?.min_sample ?? 30} decided outcomes`} />
      </div>
      <div className="grid gap-3 xl:grid-cols-[1fr_480px]">
        <Panel title="Detections">
          <div className="mb-2 flex flex-wrap items-center gap-1">
            {["", ...Object.keys(STATE)].map((s) => (
              <button key={s || "all"} onClick={() => setState(s)}
                className={`rounded border px-2 py-0.5 text-[10px] ${state === s ? "border-cyan text-cyan" : "border-line text-dim"}`}>
                {s ? s.replaceAll("_", " ").toLowerCase() : "all"}</button>))}
            <label className="ml-auto flex items-center gap-1 text-[10px] text-dim">
              <input type="checkbox" checked={liveOnly} onChange={(e) => setLiveOnly(e.target.checked)} /> found live only (not reconstructed from history)</label>
          </div>
          <Table rows={data} onRow={open} rowKey={(d) => d.id}
            empty="No detections yet. They are recorded when the analysis service analyses a symbol whose bars form one of the patterns."
            cols={[{ k: "known_at", h: "Known at", r: (d) => fmt.time(d.known_at) },
              { k: "symbol", h: "Symbol" }, { k: "timeframe", h: "TF" },
              { k: "pattern", h: "Pattern", r: (d) => <span>{d.pattern.replace("_", " ")}{!d.found_live && <span className="text-dim"> (history)</span>}</span> },
              { k: "direction", h: "Dir" },
              { k: "state", h: "State", r: (d) => <Badge tone={STATE[d.state]}>{d.state.replaceAll("_", " ")}</Badge> },
              { k: "quality", h: "Fit", r: (d) => pct(d.quality) },
              { k: "inv", h: "Invalidation", sort: false, r: (d) => px(d.levels?.invalidation) },
              { k: "tgt", h: "Target", sort: false, r: (d) => px(d.levels?.targets?.[0]) },
              { k: "mfe", h: "MFE / MAE (R)", sort: false, r: (d) => d.measure?.r ? `${d.measure.mfe_r} / ${d.measure.mae_r}` : "—" },
              { k: "rel", h: "Strategies", sort: false, r: (d) => d.relations?.supporting ? <span className="text-[10px]"><span className="text-neon">+{d.relations.supporting.length}</span> / <span className="text-bear">−{d.relations.contradicting.length}</span></span> : "—" }]} />
        </Panel>
        {sel && (
          <Panel title={sel.loading ? "Loading…" : `${sel.pattern} · ${sel.symbol} ${sel.timeframe} · ${sel.direction}`}>
            {!sel.loading && <>
              <div className="mb-2 text-[11px]"><Badge tone={STATE[sel.state]}>{sel.state}</Badge> <span className="text-dim">{sel.rules}</span></div>
              <div className="text-[10px] uppercase text-dim">Points</div>
              <div className="mb-2 text-[11px]">{sel.points.map((p: any) => `${p.label} ${px(p.price)} (${fmt.time(p.time)})`).join(" · ")}</div>
              <div className="text-[10px] uppercase text-dim">Levels</div>
              <div className="mb-2 text-[11px]">invalidation {px(sel.levels.invalidation)} · targets {(sel.levels.targets || []).map(px).join(", ") || "—"}{sel.levels.prz ? ` · completion zone ${px(sel.levels.prz[0])}–${px(sel.levels.prz[1])}` : ""}{sel.levels.confirmation ? ` · confirmation ${px(sel.levels.confirmation)}` : ""}</div>
              <div className="text-[10px] uppercase text-dim">Lifecycle</div>
              {sel.history.map((h: any, i: number) => <div key={i} className="text-[11px]">{fmt.time(h.time)} · <b>{h.state}</b> <span className="text-dim">{h.reason}</span></div>)}
              <div className="mt-2 text-[10px] uppercase text-dim">Measurement (price bars, not a trade)</div>
              <div className="text-[11px]">{sel.measure?.r ? `entry ${px(sel.measure.entry)} · R ${px(sel.measure.r)} · MFE ${sel.measure.mfe_r} R · MAE ${sel.measure.mae_r} R · ${sel.measure.bars} bars · ${sel.measure.outcome ?? "open"}` : sel.measure?.completed === false ? "did not complete" : "not measured yet"}</div>
              <div className="mt-2 text-[10px] uppercase text-dim">Strategies on the same bar</div>
              <div className="text-[11px]"><span className="text-neon">supporting:</span> {sel.relations?.supporting?.join(", ") || "none"}<br /><span className="text-bear">contradicting:</span> {sel.relations?.contradicting?.join(", ") || "none"}</div>
              <details className="mt-2 text-[10px] text-dim"><summary>Ratios, rules and context</summary><pre className="whitespace-pre-wrap">{JSON.stringify({ ratios: sel.ratios, context: sel.context }, null, 2)}</pre></details>
            </>}
          </Panel>)}
      </div>
      <Panel title="Outcomes by pattern, timeframe and regime (measured on price bars)" className="mt-3">
        <Table rows={stats?.rows} rowKey={(r) => `${r.pattern}|${r.timeframe}|${r.regime}`}
          empty="No closed formations yet."
          cols={[{ k: "pattern", h: "Pattern" }, { k: "timeframe", h: "TF" }, { k: "regime", h: "Regime" },
            { k: "measured", h: "Measured" }, { k: "target_reached", h: "Target" }, { k: "invalidated", h: "Invalidated" },
            { k: "false_breakouts", h: "False breakouts" }, { k: "expired", h: "Expired" }, { k: "did_not_complete", h: "Did not complete" },
            { k: "success_rate", h: "Success", r: (r) => <span>{pct(r.success_rate)}{!r.sufficient && <span className="text-warn"> (insufficient)</span>}</span> },
            { k: "avg_mfe_r", h: "Avg MFE / MAE (R)", sort: false, r: (r) => r.avg_mfe_r == null ? "—" : `${r.avg_mfe_r} / ${r.avg_mae_r}` }]} />
      </Panel>
      <Panel title="Catalogue" className="mt-3">
        <div className="text-[11px] text-dim">{Object.entries(fams).map(([f, n]) => `${f.replaceAll("_", " ")} ${n}`).join(" · ")}</div>
      </Panel>
    </div>
  );
}
