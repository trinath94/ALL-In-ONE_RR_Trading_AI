"use client";
import { useMemo, useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, inputCls, Panel, Table } from "@/components/ui";
import { api, fmt, patch } from "@/lib/api";
import { useApi } from "@/lib/hooks";

export default function Library() {
  const me = useMe();
  const { data, error, reload } = useApi<any[]>("/api/strategies");
  const { data: exps, reload: reloadExp } = useApi<any[]>("/api/experiments");
  const [q, setQ] = useState(""); const [cat, setCat] = useState("");
  const [sel, setSel] = useState<any>(null);
  const [params, setParams] = useState("");
  const [versions, setVersions] = useState<any[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [mode, setMode] = useState("DEMO");
  const manage = can(me, "strategy.manage");
  const cats = useMemo(() => Array.from(new Set((data || []).map((s) => s.category))), [data]);
  const rows = (data || []).filter((s) => (!cat || s.category === cat) && (!q || (s.name + s.code).toLowerCase().includes(q.toLowerCase())));
  async function choose(s: any) { setSel(s); setParams(JSON.stringify(s.params, null, 2)); setErr(null); setVersions(await api(`/api/strategies/${s.code}/versions`)); }
  async function save(body: any) {
    setErr(null);
    try { await patch(`/api/strategies/${sel.code}`, { ...body, mode }); await reload(); } catch (e: any) { setErr(e.message); }
  }
  async function stage(id: number, st: string) {
    try { await api(`/api/experiments/${id}/stage`, { method: "POST", body: { stage: st, evidence: {} } }); reloadExp(); } catch (e: any) { setErr(e.message); }
  }
  const STAGES = ["RESEARCH", "IMPLEMENTATION", "UNIT_TESTING", "BACKTESTING", "OUT_OF_SAMPLE", "WALK_FORWARD", "PAPER_TRADING", "ADMIN_APPROVAL", "PRODUCTION"];
  return (
    <div>
      <PageTitle sub="Production strategies are code-reviewed modules with version history. Claude proposals live separately as experiments.">Strategy Library</PageTitle>
      <ErrorLine error={error || err} />
      <div className="grid gap-3 xl:grid-cols-[1fr_420px]">
        <Panel right={<div className="flex gap-2"><input className={inputCls + " w-40"} placeholder="search" value={q} onChange={(e) => setQ(e.target.value)} />
          <select className={inputCls + " w-36"} value={cat} onChange={(e) => setCat(e.target.value)}><option value="">all</option>{cats.map((c) => <option key={c}>{c}</option>)}</select></div>}>
          <Table rows={rows} onRow={choose} rowKey={(r) => r.code}
            cols={[{ k: "name", h: "Strategy" }, { k: "category", h: "Category" }, { k: "component", h: "Score component" },
              { k: "enabled", h: "State", r: (s) => <Badge tone={s.enabled ? "neon" : "dim"}>{s.enabled ? "enabled" : "disabled"}</Badge> },
              { k: "availability", h: "Data", r: (s) => s.availability === "available" ? <Badge tone="cyan">available</Badge> : <Badge tone="warn">{s.availability.replace("unavailable: requires ", "needs ")}</Badge> },
              { k: "risky", h: "Risk", r: (s) => s.risky ? <Badge tone="bear">risky</Badge> : "" }, { k: "version", h: "Ver" }]} />
        </Panel>
        {sel && (
          <Panel title={sel.name}>
            <p className="text-[12px]">{sel.description}</p>
            <div className="mt-2 grid grid-cols-2 gap-1 text-[11px]">
              <div className="text-dim">Code</div><div>{sel.code}</div>
              <div className="text-dim">Timeframes</div><div>{sel.timeframes.join(", ")}</div>
              <div className="text-dim">Suited regimes</div><div>{sel.regimes.join(", ") || "all"}</div>
              <div className="text-dim">Requires</div><div>{sel.requires.join(", ") || "OHLC (+ tick volume)"}</div>
              <div className="text-dim">Code hash</div><div>{sel.code_hash}</div>
            </div>
            {manage && <>
              <div className="mt-3 flex items-center gap-2"><select className={inputCls + " w-28"} value={mode} onChange={(e) => setMode(e.target.value)}><option>DEMO</option><option>LIVE</option></select>
                <Btn tone="neon" small onClick={() => save({ enabled: true })}>Enable</Btn><Btn tone="warn" small onClick={() => save({ enabled: false })}>Disable</Btn></div>
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
