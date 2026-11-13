"use client";
import { ColorType, createChart, CrosshairMode, IChartApi, LineStyle, SeriesMarker, Time, UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { ErrorLine } from "./ui";

export const TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"];
const LAYERS = ["EMA", "VWAP", "Structure", "FVG", "OB", "Liquidity", "Fib", "RSI", "MACD"] as const;
type Layer = (typeof LAYERS)[number];

export type Levels = { entry?: number | null; stop_loss?: number | null; tp1?: number | null; tp2?: number | null; tp3?: number | null; direction?: string | null };

export default function TradingChart({ symbol, timeframe, onTimeframe, levels, height = 440 }:
  { symbol: string; timeframe: string; onTimeframe?: (tf: string) => void; levels?: Levels; height?: number }) {
  const box = useRef<HTMLDivElement>(null);
  const sub = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const [layers, setLayers] = useState<Set<Layer>>(new Set(["EMA", "Structure", "FVG", "OB", "Liquidity"]));
  const [err, setErr] = useState<string | null>(null);
  const [info, setInfo] = useState<string>("");

  useEffect(() => {
    if (!box.current || !symbol) return;
    let disposed = false;
    const c = createChart(box.current, {
      height, layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#6b7f8a", fontFamily: "JetBrains Mono" },
      grid: { vertLines: { color: "rgba(26,35,41,.6)" }, horzLines: { color: "rgba(26,35,41,.6)" } },
      crosshair: { mode: CrosshairMode.Normal }, rightPriceScale: { borderColor: "#1a2329" },
      timeScale: { borderColor: "#1a2329", timeVisible: true }, autoSize: true,
    });
    chart.current = c;
    const candles = c.addCandlestickSeries({ upColor: "#39ff88", downColor: "#ff3d5a", borderVisible: false, wickUpColor: "#39ff88", wickDownColor: "#ff3d5a" });
    const vol = c.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol" });
    c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    let sc: IChartApi | null = null;
    (async () => {
      try {
        const [bars, ov] = await Promise.all([
          api<any[]>(`/api/market/${encodeURIComponent(symbol)}/rates?timeframe=${timeframe}&count=500`),
          api<any>(`/api/market/${encodeURIComponent(symbol)}/overlays?timeframe=${timeframe}`).catch(() => ({})),
        ]);
        if (disposed) return;
        setErr(null);
        candles.setData(bars.map((b) => ({ time: b.time as UTCTimestamp, open: b.open, high: b.high, low: b.low, close: b.close })));
        vol.setData(bars.map((b) => ({ time: b.time as UTCTimestamp, value: b.volume, color: b.close >= b.open ? "rgba(57,255,136,.25)" : "rgba(255,61,90,.25)" })));
        setInfo(bars.length ? `${bars.length} bars · volume: ${bars[0].volume_type === "real" ? "exchange" : "broker tick"}` : "no data");
        const ind = ov.indicators || {};
        const line = (data: any[], color: string, width = 1, style = LineStyle.Solid) => {
          const s = c.addLineSeries({ color, lineWidth: width as any, lineStyle: style, priceLineVisible: false, lastValueVisible: false });
          s.setData((data || []).map((p: any) => ({ time: p.time as UTCTimestamp, value: p.value })));
          return s;
        };
        if (layers.has("EMA")) { line(ind.ema20, "#22e4ff"); line(ind.ema50, "#b45cff"); line(ind.ema200, "#ffb020", 2); }
        if (layers.has("VWAP")) line(ind.vwap, "#d7e3ea", 1, LineStyle.Dashed);
        const zone = (top: number, bottom: number, color: string, title: string) => {
          candles.createPriceLine({ price: top, color, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title });
          candles.createPriceLine({ price: bottom, color, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });
        };
        if (layers.has("FVG")) (ov.fvgs || []).slice(-6).forEach((g: any) => zone(g.top, g.bottom, g.direction === "BUY" ? "rgba(57,255,136,.55)" : "rgba(255,61,90,.55)", "FVG"));
        if (layers.has("OB")) (ov.order_blocks || []).slice(-4).forEach((z: any) => zone(z.top, z.bottom, z.direction === "BUY" ? "#2f7bff" : "#b45cff", "OB"));
        if (layers.has("Liquidity")) (ov.liquidity || []).forEach((l: any) => candles.createPriceLine({ price: l.price, color: "#ffb020", lineWidth: 1, lineStyle: LineStyle.LargeDashed, axisLabelVisible: false, title: l.kind }));
        if (layers.has("Fib") && ov.fibonacci) Object.entries(ov.fibonacci).forEach(([k, v]) => candles.createPriceLine({ price: v as number, color: "rgba(34,228,255,.4)", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: `fib ${k}` }));
        const markers: SeriesMarker<Time>[] = [];
        if (layers.has("Structure")) (ov.events || []).slice(-10).forEach((e: any) => markers.push({ time: e.time, position: e.direction === "BUY" ? "belowBar" : "aboveBar",
          color: e.kind === "CHOCH" ? "#ffb020" : e.direction === "BUY" ? "#39ff88" : "#ff3d5a", shape: e.direction === "BUY" ? "arrowUp" : "arrowDown", text: e.kind }));
        markers.sort((a, b) => (a.time as number) - (b.time as number));
        candles.setMarkers(markers);
        if (levels?.entry) {
          const L = (p: number | null | undefined, color: string, title: string) => p && candles.createPriceLine({ price: p, color, lineWidth: 2, lineStyle: LineStyle.Solid, axisLabelVisible: true, title });
          L(levels.entry, "#22e4ff", "ENTRY"); L(levels.stop_loss, "#ff3d5a", "SL"); L(levels.tp1, "#39ff88", "TP1"); L(levels.tp2, "#39ff88", "TP2"); L(levels.tp3, "#39ff88", "TP3");
        }
        c.timeScale().fitContent();
        if (sub.current && (layers.has("RSI") || layers.has("MACD"))) {
          sc = createChart(sub.current, { height: 120, autoSize: true, layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#6b7f8a" },
            grid: { vertLines: { visible: false }, horzLines: { color: "rgba(26,35,41,.6)" } }, timeScale: { visible: false } });
          if (layers.has("RSI")) { const r = sc.addLineSeries({ color: "#b45cff", lineWidth: 1 }); r.setData((ind.rsi || []).map((p: any) => ({ time: p.time, value: p.value })));
            r.createPriceLine({ price: 70, color: "#ff3d5a", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });
            r.createPriceLine({ price: 30, color: "#39ff88", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" }); }
          if (layers.has("MACD")) { const h = sc.addHistogramSeries({ priceScaleId: "macd" }); h.setData((ind.macd_hist || []).map((p: any) => ({ time: p.time, value: p.value, color: p.value >= 0 ? "#39ff88" : "#ff3d5a" }))); }
          c.timeScale().subscribeVisibleLogicalRangeChange((r) => r && sc?.timeScale().setVisibleLogicalRange(r));
        }
      } catch (e: any) { if (!disposed) setErr(e.message); }
    })();
    return () => { disposed = true; c.remove(); sc?.remove(); chart.current = null; };
  }, [symbol, timeframe, layers, height, levels?.entry, levels?.stop_loss, levels?.tp1, levels?.tp2, levels?.tp3]);

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-1">
        {TIMEFRAMES.map((tf) => (
          <button key={tf} onClick={() => onTimeframe?.(tf)}
            className={"rounded px-2 py-0.5 text-[10px] " + (tf === timeframe ? "bg-cyan/15 text-cyan" : "text-dim hover:text-text")}>{tf}</button>
        ))}
        <span className="mx-2 h-4 w-px bg-line" />
        {LAYERS.map((l) => (
          <button key={l} onClick={() => setLayers((s) => { const n = new Set(s); n.has(l) ? n.delete(l) : n.add(l); return n; })}
            className={"rounded px-2 py-0.5 text-[10px] " + (layers.has(l) ? "bg-violet/15 text-violet" : "text-dim hover:text-text")}>{l}</button>
        ))}
        <span className="ml-auto text-[10px] text-dim">{info}</span>
      </div>
      <ErrorLine error={err && `Chart data unavailable: ${err}`} />
      <div ref={box} style={{ height }} />
      {(layers.has("RSI") || layers.has("MACD")) && <div ref={sub} className="mt-1 border-t border-line" style={{ height: 120 }} />}
    </div>
  );
}
