"use client";
import { useState } from "react";
import { can, useMe } from "@/components/Shell";
import { Badge, Btn, inputCls, Panel, Table } from "@/components/ui";
import { api, put } from "@/lib/api";
import { useApi } from "@/lib/hooks";

/** Priority symbols: the owner's high-volatility list, scanned first and ranked first every day, with each symbol's
 *  latest spread against the Risk Engine's spread limit (priority never loosens a limit; the owner sets one per symbol). */
export function PrioritySymbols() {
  const me = useMe();
  const manage = can(me, "strategy.manage"), risk = can(me, "risk.manage");
  const { data, reload } = useApi<any>("/api/scanner/priority", 60000);
  const [text, setText] = useState<string | null>(null);
  const [msg, setMsg] = useState("");
  const [limits, setLimits] = useState<Record<string, string>>({});
  async function save(symbols: string[] | null) {
    setMsg("");
    try {
      const r: any = await put("/api/scanner/priority", { symbols });
      setMsg(`Saved. ${Object.keys(r.found).length} of ${r.list.length} names found at your broker` +
        (r.scan_enabled_now.length ? `; scanning switched on for ${r.scan_enabled_now.join(", ")}` : "") + ".");
      setText(null); reload();
    } catch (e: any) { setMsg(e.message); }
  }
  async function saveLimits() {
    setMsg("");
    try {
      const cfg: any = await api(`/api/risk/config/${data.mode}`);
      const own = { ...(cfg.symbol_max_spread_points || {}) };
      for (const [s, v] of Object.entries(limits)) { if (v === "") delete own[s]; else own[s] = Number(v); }
      await put(`/api/risk/config/${data.mode}`, { symbol_max_spread_points: own });
      setMsg(`Spread limits saved for ${data.mode}.`); setLimits({}); reload();
    } catch (e: any) { setMsg(e.message); }
  }
  if (!data) return <Panel title="Priority symbols"><div className="text-[12px] text-dim">Loading…</div></Panel>;
  const blocked = data.symbols.filter((r: any) => r.blocked_by_spread).length;
  return (
    <Panel title="Priority symbols (scanned and ranked first every day)">
      <div className="space-y-2 text-[12px]">
        <div className="text-dim">These symbols are scanned first in every cycle and their trade candidates are ranked first. Matched to your broker by name (XAUUSD finds XAUUSD.r or XAUUSDm). Priority never loosens a risk limit: every trade still goes through the Risk Engine.</div>
        {blocked > 0 && <div className="rounded border border-warn/50 p-2 text-warn">{blocked} priority symbol(s) have a spread above the Risk Engine's spread limit, so their trades are rejected. High-volatility symbols have wider spreads than forex: set a limit per symbol below if you accept that spread.</div>}
        {text === null ? <div className="flex flex-wrap items-center gap-1">
            {data.list.map((n: string) => <Badge key={n} tone={data.symbols.some((r: any) => r.name === n) ? "neon" : "dim"}>{n}</Badge>)}
            {manage && <Btn onClick={() => setText(data.list.join("\n"))}>Edit list</Btn>}
            {manage && !data.is_default && <Btn onClick={() => save(null)}>Reset to default</Btn>}
          </div>
          : <div className="space-y-1">
            <textarea className={`${inputCls} h-40 font-mono`} value={text} onChange={(e) => setText(e.target.value)} />
            <div className="flex gap-2"><Btn onClick={() => save(text.split(/[\s,]+/).filter(Boolean))}>Save list</Btn><Btn onClick={() => setText(null)}>Cancel</Btn></div>
          </div>}
        <Table rows={data.symbols} empty="None of these names were found at your broker yet (run symbol discovery on the Symbols page)." rowKey={(r: any) => r.symbol}
          cols={[{ k: "symbol", h: "Broker symbol" }, { k: "name", h: "Listed as" }, { k: "category", h: "Category" },
            { k: "scanning", h: "Scanning", r: (r: any) => r.excluded ? <Badge tone="dim">excluded</Badge> : !r.tradable ? <Badge tone="dim">not tradable</Badge> : r.scanning ? <Badge tone="neon">yes</Badge> : <Badge tone="warn">no</Badge> },
            { k: "spread_points", h: "Last spread (pt)", r: (r: any) => r.spread_points == null ? <span className="text-dim">not scanned yet</span> : <span className={r.blocked_by_spread ? "text-bear" : ""}>{r.spread_points}</span> },
            { k: "spread_limit", h: "Spread limit (pt)", sort: false, r: (r: any) => risk
              ? <input className={`${inputCls} w-24`} placeholder={String(r.spread_limit)} value={limits[r.symbol] ?? (r.own_limit ?? "")} onChange={(e) => setLimits({ ...limits, [r.symbol]: e.target.value })} />
              : r.spread_limit }]} />
        {risk && Object.keys(limits).length > 0 && <Btn onClick={saveLimits}>Save spread limits ({data.mode})</Btn>}
        {msg && <div className="text-[11px] text-dim">{msg}</div>}
      </div>
    </Panel>
  );
}
