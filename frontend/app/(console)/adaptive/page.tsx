"use client";
import { useState } from "react";
import { can, PageTitle, useMe } from "@/components/Shell";
import { Badge, Btn, ErrorLine, Panel, Stat, Table } from "@/components/ui";
import { post } from "@/lib/api";
import { useApi } from "@/lib/hooks";

const VERDICT: Record<string, any> = { POSITIVE_EDGE: "neon", NEGATIVE_EDGE: "bear", INCONCLUSIVE: "warn", INSUFFICIENT_DATA: "dim",
  NO_DATA: "dim", INDEPENDENT_VALUE: "neon", NO_ADDED_VALUE: "bear", STABLE: "neon", FRAGILE: "warn", OVERFIT: "bear",
  PROFILE_AVOIDED_LOSSES: "neon", PROFILE_SKIPPED_WINNERS: "bear", BACKTEST_ONLY: "violet" };
const STATUS: Record<string, any> = { PRODUCTION: "neon", VALIDATED: "cyan", APPROVED: "cyan", REJECTED: "bear", RETIRED: "dim", TRAINED: "dim" };
const num = (v: any, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
const label = (v: string) => v.replace(/_/g, " ").toLowerCase();
const V = ({ v }: { v?: string }) => (v ? <Badge tone={VERDICT[v] || "dim"}>{label(v)}</Badge> : <>—</>);
const ci = (c: any[] | undefined, d = 3) => (c && c[0] != null ? `${num(c[0], d)} … ${num(c[1], d)}` : "—");

/** Part 12: which strategies work where, which have deteriorated, which are redundant or conflict, and the adaptive
 *  weight profiles. Evidence is one account mode's realised trades; backtest results are shown separately. The
 *  original engine weights stay in use unless an administrator promotes a profile that beat them on unseen trades. */
export default function Adaptive() {
  const me = useMe();
  const manage = can(me, "strategy.manage"), admin = can(me, "trade.live");
  const [mode, setMode] = useState("DEMO"); const [dim, setDim] = useState("regime"); const [market, setMarket] = useState("");
  const { data: ov, error, reload } = useApi<any>(`/api/intelligence/adaptive/overview?mode=${mode}`, 60000);
  const { data: slices } = useApi<any>(`/api/intelligence/adaptive/slices?mode=${mode}`, 120000);
  const { data: red } = useApi<any>("/api/intelligence/adaptive/redundancy", 120000);
  const { data: params } = useApi<any[]>("/api/intelligence/adaptive/parameters", 120000);
  const { data: regimes } = useApi<any[]>("/api/intelligence/regimes", 120000);
  const [sym, tf] = (market || (regimes?.[0] ? `${regimes[0].symbol}|${regimes[0].timeframe}` : "")).split("|");
  const { data: outlook } = useApi<any>(sym ? `/api/intelligence/adaptive/regimes?symbol=${encodeURIComponent(sym)}&timeframe=${tf}&mode=${mode}` : null, 120000);
  const [err, setErr] = useState<string | null>(null); const [busy, setBusy] = useState("");

  async function act(key: string, fn: () => Promise<any>) {
    setErr(null); setBusy(key);
    try { await fn(); reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(""); }
  }
  const transition = (id: number, to: string) => act(`${id}${to}`, () => post(`/api/learning/models/${id}/transition`, { to, notes: "Adaptive Intelligence page" }));
  const ev = ov?.evidence;
  const sliceRows = slices ? Object.entries(slices[dim] || {}).map(([k, v]: any) => ({ key: k, ...v })) : [];
  const co = red?.co_firing;
  return (
    <div>
      <PageTitle sub="Built only on independently validated evidence: realised trades of one account mode (never mixed), with backtest out-of-sample results shown separately. Profiles run in shadow first; only an administrator can apply one. AI output is never an input here."
        right={<div className="flex gap-1">{["DEMO", "LIVE"].map((m) => <Btn key={m} small tone={mode === m ? (m === "LIVE" ? "bear" : "neon") : "dim"} onClick={() => setMode(m)}>{m}</Btn>)}</div>}>
        Adaptive Intelligence · {mode}</PageTitle>
      <ErrorLine error={error || err} />
      {ov && <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        <Stat label="Weighting in use" value={ov.weighting.startsWith("original") ? "Original" : "Adaptive"} sub={ov.weighting} tone={ov.weighting.startsWith("original") ? "plain" : "up"} />
        <Stat label="Shadow profile" value={ov.shadow || "none"} sub="recorded beside each decision, not used" />
        <Stat label="Evidence" value={`${ev.trades} trades`} sub={`${mode} realised · ${ev.min_per_slice} per slice, ${ev.min_for_profile} for a profile`} />
        <Stat label="Ranked strategies" value={(ov.ranking || []).filter((r: any) => r.rank).length} sub={`of ${(ov.ranking || []).length} with any evidence`} />
      </div>}

      <Panel title="Adaptive weight profiles" right={manage && <Btn small busy={busy === "fit"} onClick={() => act("fit", () => post("/api/intelligence/adaptive/fit", { mode }))}>Propose from {mode} trades</Btn>}>
        <Table rows={ov?.profiles} rowKey={(p) => p.id} empty="No profile proposed yet. The learner proposes one weekly; it needs 150 realised trades with an attributed strategy."
          cols={[{ k: "version", h: "Ver" }, { k: "status", h: "Status", r: (p) => <Badge tone={STATUS[p.status]}>{p.status}</Badge> },
            { k: "role", h: "Role" }, { k: "dataset", h: "Evidence", sort: false, r: (p) => `${p.dataset?.trades ?? "—"} ${p.dataset?.mode ?? ""}` },
            { k: "hold", h: "Held-out improvement (R/trade)", sort: false, r: (p) => p.metrics?.holdout ? <span>{num(p.metrics.holdout.improvement_r, 3)} <span className="text-dim">CI {ci(p.metrics.holdout.improvement_ci95)}</span></span> : "—" },
            { k: "wf", h: "Walk-forward", sort: false, r: (p) => p.metrics?.walk_forward ? <Badge tone={p.metrics.walk_forward.passed ? "neon" : "bear"}>{p.metrics.walk_forward.scored} folds · {num(p.metrics.walk_forward.mean_improvement_r, 3)}</Badge> : "—" },
            { k: "signature", h: "Signature", r: (p) => <Badge tone={p.signature === "valid" || p.signature === "not_enforced" ? "neon" : "bear"}>{p.signature}</Badge> },
            { k: "shadow", h: "Shadow vs original", sort: false, r: (p) => p.shadow_evaluation ? <span>{p.shadow_evaluation.decisions} decisions · agree {p.shadow_evaluation.agreement == null ? "—" : `${num(p.shadow_evaluation.agreement * 100, 0)}%`} <V v={p.shadow_evaluation.verdict} /></span> : "—" },
            { k: "act", h: "", sort: false, r: (p) => <div className="flex gap-1">
              {manage && p.status === "VALIDATED" && <Btn small busy={busy === `${p.id}APPROVED`} disabled={!["valid", "not_enforced"].includes(p.signature)} onClick={() => transition(p.id, "APPROVED")}>Approve</Btn>}
              {admin && p.status === "APPROVED" && <Btn small tone="neon" busy={busy === `${p.id}PRODUCTION`} onClick={() => transition(p.id, "PRODUCTION")}>Apply</Btn>}
              {admin && p.status === "PRODUCTION" && <Btn small tone="warn" busy={busy === `${p.id}RETIRED`} onClick={() => transition(p.id, "RETIRED")}>Back to original</Btn>}
            </div> }]} />
        <div className="mt-2 text-[10px] text-dim">A profile scales one strategy's setup strength by 0.5–1.5 in a regime where it has at least 30 trades; everything else keeps the original weights. It is validated only if weighting trades this way raised mean R on unseen later trades (95% interval above zero) and across walk-forward folds. Calibrated confidence is never changed by a profile. Applying needs administrator permission.</div>
        {(ov?.profiles || []).filter((p: any) => p.status !== "REJECTED").slice(0, 1).map((p: any) => (p.metrics?.largest_increases?.length || p.metrics?.largest_decreases?.length) ? <div key={p.id} className="mt-2 grid gap-2 text-[11px] md:grid-cols-2">
          <div><div className="text-[10px] uppercase text-dim">v{p.version} raises</div>{p.metrics.largest_increases.map((x: any) => <div key={x.strategy + x.regime}>{x.strategy} · {x.regime === "*" ? "all regimes" : x.regime} <b className="text-neon">×{x.multiplier}</b></div>)}</div>
          <div><div className="text-[10px] uppercase text-dim">v{p.version} lowers</div>{p.metrics.largest_decreases.map((x: any) => <div key={x.strategy + x.regime}>{x.strategy} · {x.regime === "*" ? "all regimes" : x.regime} <b className="text-bear">×{x.multiplier}</b></div>)}</div>
        </div> : null)}
      </Panel>

      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <Panel title="Strategy ranking (lower bound of realised expectancy)">
          <Table rows={ov?.ranking} rowKey={(r) => r.strategy} empty={`No ${mode} trades attributed to a strategy yet.`}
            cols={[{ k: "rank", h: "#", r: (r) => r.rank ?? "—" }, { k: "strategy", h: "Strategy" },
              { k: "n", h: "Trades", sort: false, r: (r) => r.realised.sample_size }, { k: "v", h: "Verdict", sort: false, r: (r) => <V v={r.realised.verdict} /> },
              { k: "ar", h: "Avg R", sort: false, r: (r) => num(r.realised.average_r, 3) },
              { k: "rank_key", h: "Expectancy CI", r: (r) => ci(r.realised.expectancy_ci95, 2) },
              { k: "bt", h: "Backtest OOS / WF (separate)", sort: false, r: (r) => Object.keys(r.backtest || {}).length ? Object.entries(r.backtest).map(([s, b]: any) => `${s}: ${b.trades} tr, PF ${num(b.profit_factor)}`).join(" · ") : "—" }]} />
        </Panel>
        <Panel title="Which strategies work where" right={<select className="rounded border border-line bg-void px-2 py-1 text-[11px]" value={dim} onChange={(e) => setDim(e.target.value)}>{["regime", "symbol", "timeframe", "session"].map((d) => <option key={d}>{d}</option>)}</select>}>
          <Table rows={sliceRows} rowKey={(r) => r.key} empty={`No ${mode} trades carry a ${dim}.`}
            cols={[{ k: "key", h: dim }, { k: "best", h: "Significantly positive (best first)", sort: false, r: (r) => r.best.length ? r.best.map((b: any) => `${b.strategy} (${num(b.average_r, 2)}R, n=${b.trades})`).join(", ") : <span className="text-dim">none proven</span> },
              { k: "negative", h: "Negative", sort: false, r: (r) => r.negative.length ? <span className="text-bear">{r.negative.join(", ")}</span> : "—" },
              { k: "evaluated", h: "Evaluated" }, { k: "insufficient", h: "< 30 trades" }]} />
        </Panel>
        <Panel title="Deteriorated strategies">
          <Table rows={ov?.deterioration} rowKey={(d, i) => `${d.strategy}${i}`} empty="None: no strategy's last 30 trades are significantly worse than before, and no performance drift was recorded in 30 days."
            cols={[{ k: "strategy", h: "Strategy" }, { k: "source", h: "Source", r: (d) => label(d.source) },
              { k: "detail", h: "Evidence", sort: false, r: (d) => d.source === "realised" ? `${num(d.prior_average_r, 2)}R → ${num(d.recent_average_r, 2)}R (change CI ${ci(d.change_ci95, 2)})` : `${d.metric} ${num(d.value)} vs ${num(d.threshold)} (${d.severity})` }]} />
        </Panel>
        <Panel title="Combinations with independent predictive value">
          <Table rows={ov?.combinations} rowKey={(c) => c.pair.join("+")} empty="No pair of strategies has 30 shared trades yet."
            cols={[{ k: "pair", h: "Pair", r: (c) => c.pair.join(" + ") }, { k: "together", h: "Together" },
              { k: "together_average_r", h: "Avg R together", r: (c) => num(c.together_average_r, 3) }, { k: "single_average_r", h: "Avg R one alone", r: (c) => num(c.single_average_r, 3) },
              { k: "inc", h: "Increment CI", sort: false, r: (c) => ci(c.increment_ci95) }, { k: "verdict", h: "Verdict", r: (c) => <V v={c.verdict} /> }]} />
        </Panel>
        <Panel title={`Redundant and conflicting strategies (scanner, ${co?.days ?? 30} days)`}>
          <div className="mb-1 text-[11px] text-dim">{co ? `${co.analyses} analyses, ${co.strategies_fired} strategies fired. ${red?.fingerprints?.static?.length ?? 0} pairs with near-identical code (fingerprints).` : "…"}</div>
          <Table rows={[...(co?.redundant || []), ...(co?.conflicting || [])]} rowKey={(p) => `${p.a}${p.b}`} empty="No pair fired together 20 times."
            cols={[{ k: "kind", h: "Kind", r: (p) => <Badge tone={p.kind === "redundant" ? "warn" : "bear"}>{p.kind}</Badge> }, { k: "a", h: "A" }, { k: "b", h: "B" },
              { k: "together", h: "Together" }, { k: "jaccard", h: "Overlap", r: (p) => num(p.jaccard) }, { k: "same_direction", h: "Same dir" }, { k: "opposite_direction", h: "Opposite" }]} />
          <div className="mt-1 text-[10px] text-dim">Report only: no strategy is merged, disabled or removed.</div>
        </Panel>
        <Panel title="Regime probability outlook" right={<select className="rounded border border-line bg-void px-2 py-1 text-[11px]" value={market} onChange={(e) => setMarket(e.target.value)}>{(regimes || []).map((r: any) => <option key={`${r.symbol}|${r.timeframe}`} value={`${r.symbol}|${r.timeframe}`}>{r.symbol} {r.timeframe}</option>)}</select>}>
          {!outlook || !outlook.available ? <div className="text-[12px] text-dim">{outlook?.note || "No regime history yet: the scanner records it for every scanned market."}</div> : <div className="text-[11px]">
            <div className="mb-1">Now <b>{outlook.current}</b> (confidence {num(outlook.confidence)}, stability {num(outlook.stability)}) over {outlook.bars} bars{outlook.transition_warning ? <span className="text-warn"> · {outlook.transition_warning}</span> : null}</div>
            <div className="mb-1 flex flex-wrap gap-2">{Object.entries(outlook.probabilities).map(([k, v]: any) => <span key={k}>{k} <b>{num(v * 100, 0)}%</b> <span className="text-dim">stays {outlook.persistence[k] == null ? "—" : `${num(outlook.persistence[k] * 100, 0)}%`}</span></span>)}</div>
            <Table rows={outlook.favoured} rowKey={(f) => f.strategy} empty={`No strategy has 30 ${mode} trades in these regimes.`}
              cols={[{ k: "strategy", h: "Strategy" }, { k: "expected_r_given_regimes", h: "Expected R given regime probabilities", r: (f) => num(f.expected_r_given_regimes, 3) },
                { k: "coverage", h: "Coverage", r: (f) => `${num(f.coverage * 100, 0)}%` }]} />
          </div>}
        </Panel>
        <Panel title="Parameter stability (optimiser runs)">
          <Table rows={params} rowKey={(p) => p.job_id} empty="No completed optimisation runs. Run one in the Optimisation lab."
            cols={[{ k: "strategy", h: "Strategy" }, { k: "market", h: "Market", sort: false, r: (p) => `${p.symbol ?? "—"} ${p.timeframe ?? ""}` },
              { k: "best_params", h: "Best parameters", sort: false, r: (p) => JSON.stringify(p.best_params) },
              { k: "pf", h: "PF in / out of sample", sort: false, r: (p) => `${num(p.in_sample_pf)} / ${num(p.out_of_sample_pf)}` },
              { k: "nb", h: "Neighbours good OOS", sort: false, r: (p) => `${p.neighbours_profitable_oos} / ${p.neighbours}` }, { k: "verdict", h: "Verdict", r: (p) => <V v={p.verdict} /> }]} />
        </Panel>
        <Panel title="Failure intelligence (losing trades)">
          <div className="mb-1 text-[11px] text-dim">{ov?.failures?.losses_analysed ?? 0} losing {mode} trades analysed by the failure analysis job.</div>
          <Table rows={ov?.failures?.strategies} rowKey={(s) => s.strategy} empty="No analysed losses yet."
            cols={[{ k: "strategy", h: "Strategy" }, { k: "losses", h: "Losses" },
              { k: "top_causes", h: "Most frequent causes", sort: false, r: (s) => s.top_causes.map((c: any) => `${label(c.cause)} ${num(c.share * 100, 0)}%`).join(" · ") || "—" }]} />
        </Panel>
      </div>
    </div>
  );
}
