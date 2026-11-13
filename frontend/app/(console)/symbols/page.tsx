"use client";
import { useMemo, useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, inputCls, Panel, Stat, Table } from "@/components/ui";
import { fmt, patch, post } from "@/lib/api";
import { useApi } from "@/lib/hooks";

/** Broker symbol catalogue: everything the connected broker offers, and which symbols the scanner may analyse. */
export default function Symbols() {
  const me = useMe();
  const manage = can(me, "strategy.manage");
  const admin = can(me, "system.manage");
  const { data, error, reload } = useApi<any[]>("/api/symbols", 30000);
  const [q, setQ] = useState(""); const [cat, setCat] = useState(""); const [view, setView] = useState("all");
  const [msg, setMsg] = useState<string | null>(null); const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const cats = useMemo(() => Array.from(new Set((data || []).map((s) => s.category))).sort(), [data]);
  const rows = (data || []).filter((s) => (!q || (s.symbol + " " + s.description).toLowerCase().includes(q.toLowerCase()))
    && (!cat || s.category === cat)
    && (view === "all" || (view === "scanning" && s.scan_enabled) || (view === "eligible" && s.eligible)
      || (view === "excluded" && s.excluded) || (view === "missing" && s.missing_since) || (view === "untradable" && !s.tradable)));
  const n = (f: (s: any) => boolean) => (data || []).filter(f).length;

  async function run(fn: () => Promise<any>, ok: (r: any) => string) {
    setBusy(true); setErr(null); setMsg(null);
    try { const r = await fn(); setMsg(ok(r)); await reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  const skipped = (r: any) => Object.entries(r.skipped || {}).map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`).join(", ");
  const scope = (enable: boolean, body: any) => run(() => post("/api/symbols/scan-scope", { enable, ...body }),
    (r) => `${r.changed} symbol(s) ${enable ? "enabled" : "disabled"} for scanning${skipped(r) ? ` (skipped: ${skipped(r)})` : ""}.`);

  return (
    <div>
      <PageTitle sub={<>Discovered from the connected broker. New symbols are <b>not scanned</b> until you enable them. Trading
        sessions are not provided by the MT5 terminal API and are not guessed.</>}
        right={admin && <Btn busy={busy} onClick={() => run(() => post("/api/symbols/discover"),
          (r) => `Catalogue refreshed: ${r.discovered} symbols (${r.new ?? 0} new, ${r.missing ?? 0} no longer offered).`)}>Discover now</Btn>}>
        Broker Symbols
      </PageTitle>
      <ErrorLine error={error || err} />
      {msg && <div className="mb-2 rounded border border-neon/40 bg-neon/5 px-2 py-1 text-[11px] text-neon">{msg}</div>}
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
        <Stat label="Discovered" value={n(() => true)} />
        <Stat label="Scanning" value={n((s) => s.scan_enabled)} tone="up" />
        <Stat label="Eligible" value={n((s) => s.eligible)} sub="fully tradable, offered, not excluded" />
        <Stat label="Excluded" value={n((s) => s.excluded)} tone="warn" />
        <Stat label="No longer offered" value={n((s) => !!s.missing_since)} tone="down" />
      </div>
      {manage && (
        <Panel title="Scan scope">
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            <Btn small tone="neon" busy={busy} onClick={() => scope(true, { scope: "all_eligible" })}>Scan all eligible</Btn>
            <Btn small tone="dim" busy={busy} onClick={() => scope(false, { scope: "all" })}>Stop scanning all</Btn>
            {cat && <>
              <Btn small tone="neon" busy={busy} onClick={() => scope(true, { scope: "categories", categories: [cat] })}>Scan class “{cat}”</Btn>
              <Btn small tone="dim" busy={busy} onClick={() => scope(false, { scope: "categories", categories: [cat] })}>Stop class “{cat}”</Btn>
            </>}
            <Btn small tone="cyan" busy={busy} disabled={!rows.length} onClick={() => scope(true, { scope: "symbols", symbol_ids: rows.map((s) => s.id) })}>Scan filtered ({rows.length})</Btn>
            <span className="text-dim">Excluded, no-longer-offered and not fully tradable symbols are never enabled.</span>
            <Btn small tone="cyan" busy={busy} disabled={!n((s) => !s.peers.length && s.suggested_peers.length > 0)}
              onClick={() => run(() => post("/api/symbols/peers/apply-suggested"), (r) => `Suggested peers set on ${r.changed.length} symbol(s).`)}>
              Apply suggested peers ({n((s) => !s.peers.length && s.suggested_peers.length > 0)})</Btn>
          </div>
        </Panel>
      )}
      <Panel title={`Catalogue (${rows.length} of ${(data || []).length})`} className="mt-3">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <div className="w-44"><input className={inputCls} placeholder="search" value={q} onChange={(e) => setQ(e.target.value)} /></div>
          <div className="w-40"><select className={inputCls} value={cat} onChange={(e) => setCat(e.target.value)}><option value="">all classes</option>{cats.map((c) => <option key={c}>{c}</option>)}</select></div>
          <div className="w-40"><select className={inputCls} value={view} onChange={(e) => setView(e.target.value)}>
            {["all", "scanning", "eligible", "excluded", "missing", "untradable"].map((v) => <option key={v}>{v}</option>)}</select></div>
        </div>
        <Table rows={rows} rowKey={(r) => r.id} empty="No symbols yet: they are discovered automatically once the MT5 connector is linked and the account passes its startup checks."
          cols={[
            { k: "symbol", h: "Symbol", r: (s) => <span title={s.description}>{s.symbol}</span> },
            { k: "category", h: "Class" },
            { k: "trade_mode", h: "Trading", r: (s) => <Badge tone={s.trade_mode === "FULL" ? "neon" : "warn"}>{s.trade_mode}</Badge> },
            { k: "spread_points", h: "Spread", r: (s) => s.spread_points == null ? "—" : `${s.spread_points}${s.spread_floating ? " (float)" : ""}` },
            { k: "volume_min", h: "Lots min/step/max", sort: false, r: (s) => `${s.volume_min} / ${s.volume_step} / ${s.volume_max ?? "—"}` },
            { k: "tick_value", h: "Tick size / value", sort: false, r: (s) => `${s.tick_size} / ${s.tick_value}` },
            { k: "contract_size", h: "Contract" },
            { k: "priority", h: "Priority", r: (s) => manage
              ? <input type="number" min={0} max={1000} defaultValue={s.priority} className={inputCls + " w-20"}
                  onBlur={(e) => { const v = Number(e.target.value); if (v !== s.priority) run(() => patch(`/api/symbols/${s.id}`, { priority: v }), () => `${s.symbol}: priority ${v}.`); }} />
              : s.priority },
            { k: "scan_enabled", h: "Scan", r: (s) => manage && !s.excluded
              ? <Btn small tone={s.scan_enabled ? "neon" : "dim"} disabled={!s.eligible && !s.scan_enabled}
                  onClick={() => run(() => patch(`/api/symbols/${s.id}`, { scan_enabled: !s.scan_enabled }), () => `${s.symbol}: scanning ${s.scan_enabled ? "off" : "on"}.`)}>
                  {s.scan_enabled ? "on" : "off"}</Btn>
              : <Badge tone={s.scan_enabled ? "neon" : "dim"}>{s.scan_enabled ? "on" : "off"}</Badge> },
            { k: "peers", h: "Peers", sort: false, r: (s) => manage
              ? <div className="w-44"><input className={inputCls} defaultValue={s.peers.join(", ")}
                  placeholder={s.suggested_peers.length ? `suggested: ${s.suggested_peers.join(", ")}` : "none"}
                  onBlur={(e) => { const v = e.target.value.split(",").map((x) => x.trim()).filter(Boolean);
                    if (v.join(",") !== s.peers.join(",")) run(() => patch(`/api/symbols/${s.id}`, { peers: v }), () => `${s.symbol}: peers ${v.join(", ") || "cleared"}.`); }} /></div>
              : (s.peers.join(", ") || <span className="text-dim">{s.suggested_peers.length ? `suggested: ${s.suggested_peers.join(", ")}` : "—"}</span>) },
            { k: "excluded", h: "Exclude", r: (s) => manage
              ? <Btn small tone={s.excluded ? "warn" : "dim"} onClick={() => run(() => patch(`/api/symbols/${s.id}`, { excluded: !s.excluded }), () => `${s.symbol}: ${s.excluded ? "no longer excluded" : "excluded (scanning off)"}.`)}>
                  {s.excluded ? "excluded" : "exclude"}</Btn>
              : (s.excluded ? <Badge tone="warn">excluded</Badge> : "—") },
            { k: "last_seen_at", h: "Status", r: (s) => s.missing_since
              ? <Badge tone="bear">not offered since {fmt.time(s.missing_since)}</Badge>
              : <span className="text-dim">seen {fmt.time(s.last_seen_at)}</span> },
          ]} />
      </Panel>
    </div>
  );
}
