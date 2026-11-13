"use client";
import { useMemo, useState } from "react";
import { PageTitle } from "@/components/Shell";
import { Badge, dirTone, ErrorLine, gradeTone, inputCls, Panel, Table } from "@/components/ui";
import { fmt } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";

const TFS = ["", "M1", "M5", "M15", "M30", "H1", "H4", "D1"];

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
