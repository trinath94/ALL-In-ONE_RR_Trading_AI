"use client";
import clsx from "clsx";
import { motion } from "framer-motion";
import { AlertTriangle, Loader2, X } from "lucide-react";
import { ReactNode, useState } from "react";

export function Panel({ title, icon, right, children, className, accent = "cyan" }:
  { title?: ReactNode; icon?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; accent?: "cyan" | "neon" | "bear" | "violet" }) {
  const ring = { cyan: "hover:shadow-cyan", neon: "hover:shadow-neon", bear: "shadow-bear", violet: "hover:shadow-cyan" }[accent];
  return (
    <section className={clsx("glass relative rounded-lg transition-shadow", ring, className)}>
      {title && (
        <header className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
          <h2 className="flex items-center gap-2 font-display text-[11px] uppercase tracking-[.18em] text-cyan">{icon}{title}</h2>
          {right}
        </header>
      )}
      <div className="p-3">{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "up" | "down" | "warn" | "plain" }) {
  const c = tone === "up" ? "text-neon" : tone === "down" ? "text-bear" : tone === "warn" ? "text-warn" : "text-text";
  return (
    <motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25 }}
      className="glass rounded-lg px-3 py-2">
      <div className="text-[10px] uppercase tracking-widest text-dim">{label}</div>
      <div className={clsx("tabular mt-1 text-lg font-semibold", c)}>{value}</div>
      {sub && <div className="text-[11px] text-dim">{sub}</div>}
    </motion.div>
  );
}

export const toneOf = (v: number | null | undefined) => (v === null || v === undefined ? "plain" : v > 0 ? "up" : v < 0 ? "down" : "plain");

export function Badge({ children, tone = "dim" }: { children: ReactNode; tone?: "neon" | "bear" | "cyan" | "warn" | "violet" | "dim" }) {
  const m = { neon: "border-neon/40 text-neon bg-neon/5", bear: "border-bear/40 text-bear bg-bear/5", cyan: "border-cyan/40 text-cyan bg-cyan/5",
    warn: "border-warn/40 text-warn bg-warn/5", violet: "border-violet/40 text-violet bg-violet/5", dim: "border-line text-dim" }[tone];
  return <span className={clsx("inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] uppercase tracking-wider", m)}>{children}</span>;
}

export const dirTone = (d?: string | null) => (d === "BUY" ? "neon" : d === "SELL" ? "bear" : d === "WAIT" ? "warn" : "dim") as any;
export const gradeTone = (g?: string | null) => (g === "A+" ? "neon" : g === "A" ? "cyan" : g === "B" ? "warn" : "dim") as any;

export function Btn({ children, onClick, tone = "cyan", disabled, busy, small, type = "button" }:
  { children: ReactNode; onClick?: () => void; tone?: "cyan" | "neon" | "bear" | "warn" | "dim" | "violet"; disabled?: boolean; busy?: boolean; small?: boolean; type?: "button" | "submit" }) {
  const m = { cyan: "border-cyan/50 text-cyan hover:bg-cyan/10", neon: "border-neon/50 text-neon hover:bg-neon/10",
    bear: "border-bear/60 text-bear hover:bg-bear/10", warn: "border-warn/50 text-warn hover:bg-warn/10", dim: "border-line text-dim hover:text-text",
    violet: "border-violet/50 text-violet hover:bg-violet/10" }[tone];
  return (
    <button type={type} onClick={onClick} disabled={disabled || busy}
      className={clsx("inline-flex items-center justify-center gap-1.5 rounded border uppercase tracking-wider transition disabled:cursor-not-allowed disabled:opacity-40",
        small ? "px-2 py-1 text-[10px]" : "px-3 py-1.5 text-[11px]", m)}>
      {busy && <Loader2 size={12} className="animate-spin" />}{children}
    </button>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-6 text-center text-[12px] text-dim">{children}</div>;
}

export function ErrorLine({ error }: { error?: string | null }) {
  if (!error) return null;
  return <div className="flex items-center gap-2 rounded border border-bear/40 bg-bear/5 px-2 py-1 text-[11px] text-bear"><AlertTriangle size={12} />{error}</div>;
}

export function Table({ cols, rows, empty = "No data", onRow, rowKey }:
  { cols: { k: string; h: ReactNode; r?: (row: any) => ReactNode; cls?: string; sort?: boolean }[]; rows: any[] | null | undefined; empty?: string; onRow?: (r: any) => void; rowKey?: (r: any, i: number) => string | number }) {
  const [sort, setSort] = useState<{ k: string; asc: boolean } | null>(null);
  const data = [...(rows || [])];
  if (sort) data.sort((a, b) => { const x = a[sort.k], y = b[sort.k]; const c = x === y ? 0 : x === null || x === undefined ? 1 : y === null || y === undefined ? -1 : x > y ? 1 : -1; return sort.asc ? c : -c; });
  if (!rows?.length) return <Empty>{empty}</Empty>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[12px]">
        <thead>
          <tr className="border-b border-line text-[10px] uppercase tracking-wider text-dim">
            {cols.map((c) => (
              <th key={c.k} className={clsx("whitespace-nowrap px-2 py-1.5 font-normal", c.sort !== false && "cursor-pointer hover:text-cyan", c.cls)}
                onClick={() => c.sort !== false && setSort((s) => ({ k: c.k, asc: s?.k === c.k ? !s.asc : false }))}>
                {c.h}{sort?.k === c.k ? (sort.asc ? " ▲" : " ▼") : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((r, i) => (
            <tr key={rowKey ? rowKey(r, i) : i} onClick={() => onRow?.(r)}
              className={clsx("tabular border-b border-line/50 hover:bg-cyan/[.04]", onRow && "cursor-pointer")}>
              {cols.map((c) => <td key={c.k} className={clsx("whitespace-nowrap px-2 py-1.5", c.cls)}>{c.r ? c.r(r) : String(r[c.k] ?? "—")}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Confirmation dialog for dangerous actions; optionally requires typing an exact phrase. */
export function Confirm({ open, title, body, phrase, tone = "bear", onCancel, onConfirm, busy }:
  { open: boolean; title: string; body: ReactNode; phrase?: string; tone?: "bear" | "warn" | "neon"; onCancel: () => void; onConfirm: (typed: string) => void; busy?: boolean }) {
  const [typed, setTyped] = useState("");
  if (!open) return null;
  const ok = !phrase || typed === phrase;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" role="dialog" aria-modal>
      <motion.div initial={{ scale: 0.96, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} className="glass w-full max-w-md rounded-lg shadow-bear">
        <div className="flex items-center justify-between border-b border-line px-4 py-2">
          <h3 className="font-display text-xs uppercase tracking-widest text-bear">{title}</h3>
          <button onClick={onCancel} aria-label="close" className="text-dim hover:text-text"><X size={14} /></button>
        </div>
        <div className="space-y-3 p-4 text-[12px]">
          <div>{body}</div>
          {phrase && (
            <label className="block text-dim">Type <span className="text-text">{phrase}</span> to confirm
              <input autoFocus value={typed} onChange={(e) => setTyped(e.target.value)}
                className="mt-1 w-full rounded border border-line bg-void px-2 py-1.5 text-text outline-none focus:border-bear" />
            </label>
          )}
          <div className="flex justify-end gap-2">
            <Btn tone="dim" onClick={() => { setTyped(""); onCancel(); }}>Cancel</Btn>
            <Btn tone={tone} disabled={!ok} busy={busy} onClick={() => { onConfirm(typed); setTyped(""); }}>Confirm</Btn>
          </div>
        </div>
      </motion.div>
    </div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="block text-[10px] uppercase tracking-wider text-dim">{label}<div className="mt-1 normal-case tracking-normal">{children}</div></label>;
}

export const inputCls = "w-full rounded border border-line bg-void px-2 py-1.5 text-[12px] text-text outline-none focus:border-cyan";
