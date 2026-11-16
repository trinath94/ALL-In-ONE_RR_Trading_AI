"use client";
import { useEffect, useState } from "react";
import { FusionDetail } from "@/components/Fusion";
import { PageTitle } from "@/components/Shell";
import TradingChart from "@/components/TradingChart";
import { Badge, dirTone, ErrorLine, gradeTone, inputCls, Panel, Table } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi } from "@/lib/hooks";

/** Part 17 item 7: the latest full analysis of one market, exactly as the scanner recorded it: context (regime,
 *  higher-timeframe bias, sessions, news, spread), every strategy setup, the original engine's result and the fused
 *  decision with its factors. */
export default function MarketAnalysis() {
  const { data: rows } = useApi<any[]>("/api/scanner", 60000);
  const [key, setKey] = useState("");
  useEffect(() => { if (!key && rows?.length) setKey(`${rows[0].symbol}|${rows[0].timeframe}`); }, [rows, key]);
  const [symbol, tf] = key.split("|");
  const { data: a, error } = useApi<any>(symbol ? `/api/analysis/latest?symbol=${encodeURIComponent(symbol)}&timeframe=${tf}` : null, 60000);
  const [chartTf, setChartTf] = useState("H1");
  useEffect(() => { if (tf) setChartTf(tf); }, [tf]);
  const e = a?.engine;
  return (
    <div>
      <PageTitle sub="The latest recorded analysis of one market. Nothing here is recomputed or estimated: it is what the scanner analysed on its last closed bar."
        right={<select className={inputCls + " w-52"} value={key} onChange={(ev) => setKey(ev.target.value)}>
          {(rows || []).map((r) => <option key={`${r.symbol}|${r.timeframe}`} value={`${r.symbol}|${r.timeframe}`}>{r.symbol} · {r.timeframe}</option>)}</select>}>
        Market Analysis</PageTitle>
      <ErrorLine error={error} />
      {!rows?.length && <Panel><div className="text-[12px] text-dim">No market has been analysed yet. Enable symbols for scanning (Broker Symbol Discovery).</div></Panel>}
      {symbol && <Panel title={`${symbol} chart`}><TradingChart symbol={symbol} timeframe={chartTf} onTimeframe={setChartTf} height={360}
        levels={e && e.entry ? { entry: e.entry, stop_loss: e.stop_loss, tp1: e.tp1, tp2: e.tp2, tp3: e.tp3, direction: e.direction } : undefined} /></Panel>}
      {a?.available === false && <Panel><div className="text-[12px] text-dim">{a.note}</div></Panel>}
      {a?.available && <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <Panel title={`Context · bar ${fmt.time(a.bar_time)}`}>
          <div className="grid grid-cols-[140px_1fr] gap-1 text-[11px]">
            <div className="text-dim">Regime</div><div>{a.regime?.primary ?? "—"} {a.regime?.tags?.length ? <span className="text-dim">{a.regime.tags.join(", ")}</span> : null}</div>
            <div className="text-dim">Higher timeframe</div><div>{a.htf_bias > 0 ? <span className="text-neon">bullish</span> : a.htf_bias < 0 ? <span className="text-bear">bearish</span> : "neutral"}</div>
            <div className="text-dim">Sessions</div><div>{a.sessions?.join(", ") || "none open"}</div>
            <div className="text-dim">News</div><div><Badge tone={a.news === "CLEAR" ? "neon" : a.news === "RESTRICTED" ? "bear" : "warn"}>{a.news}</Badge></div>
            <div className="text-dim">Price / spread</div><div>{fmt.px(a.price)} · {a.spread_points ?? "—"} points · ATR {fmt.px(a.atr)}</div>
            <div className="text-dim">Data from broker</div><div>{a.data_caps?.join(", ") || "—"}</div>
            <div className="text-dim">Strategies</div><div>{a.evaluation ? Object.entries(a.evaluation).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(" · ") : "—"}</div>
          </div>
        </Panel>
        <Panel title="Original signal engine">
          {e && <div className="text-[11px]">
            <div className="mb-1 flex gap-2"><Badge tone={dirTone(e.decision)}>{e.decision}</Badge><Badge tone={gradeTone(e.grade)}>{e.grade}</Badge>
              <span>confluence {e.score}/100 (not a probability)</span>
              <span className="text-dim">calibrated: {e.calibrated_confidence == null ? `n/a (${e.confidence_sample} outcomes)` : fmt.pct(e.calibrated_confidence, 1)}</span></div>
            <p>{e.explanation}</p>
            {e.warnings?.length > 0 && <ul className="mt-1 list-disc pl-4 text-warn">{e.warnings.map((w: string, i: number) => <li key={i}>{w}</li>)}</ul>}
            <div className="mt-1 text-dim">Components: {Object.entries(e.components || {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"}</div>
          </div>}
        </Panel>
        <Panel title={`Strategy setups on the last closed bar (${a.setups.length})`}>
          <Table rows={a.setups} rowKey={(s, i) => `${s.strategy}${i}`} empty="No strategy produced a setup on this bar (NO TRADE is a valid outcome)."
            cols={[{ k: "strategy", h: "Strategy" }, { k: "direction", h: "Dir", r: (s) => <Badge tone={dirTone(s.direction)}>{s.direction}</Badge> },
              { k: "strength", h: "Strength", r: (s) => fmt.num(s.strength) }, { k: "component", h: "Component" },
              { k: "entry", h: "Entry", r: (s) => fmt.px(s.entry) }, { k: "stop_loss", h: "SL", r: (s) => fmt.px(s.stop_loss) },
              { k: "reasons", h: "Reasons", sort: false, cls: "whitespace-normal", r: (s) => <span className="text-dim">{(s.reasons || []).join("; ")}</span> }]} />
        </Panel>
        <Panel title="Signal fusion (all evidence)"><FusionDetail f={a.fusion} /></Panel>
      </div>}
    </div>
  );
}
