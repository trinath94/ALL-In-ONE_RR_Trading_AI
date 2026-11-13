"use client";
import clsx from "clsx";
import {
  Activity, Bell, BookOpen, Bot, BrainCircuit, CandlestickChart, Cpu, FlaskConical, Gauge, History, LayoutDashboard,
  LogOut, Menu, Newspaper, Sparkles, Target, PieChart, Radar, Settings, ShieldAlert, Sliders, Terminal, Users, X, Zap,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, ReactNode, useContext, useEffect, useState } from "react";
import { api, post } from "@/lib/api";
import { useApi, useLive } from "@/lib/hooks";

export const NAV = [
  { href: "/", label: "Command Center", icon: LayoutDashboard },
  { href: "/ai", label: "AI Intelligence", icon: BrainCircuit },
  { href: "/scanner", label: "Market Scanner", icon: Radar },
  { href: "/signals", label: "Live Signals", icon: Zap },
  { href: "/trades", label: "Active Trades", icon: CandlestickChart },
  { href: "/history", label: "Trade History", icon: History },
  { href: "/strategy-intel", label: "Strategy Intelligence", icon: Gauge },
  { href: "/library", label: "Strategy Library", icon: BookOpen },
  { href: "/backtesting", label: "Backtesting Lab", icon: FlaskConical },
  { href: "/optimisation", label: "Strategy Optimisation", icon: Sliders },
  { href: "/risk", label: "Risk Management", icon: ShieldAlert },
  { href: "/portfolio", label: "Portfolio Analytics", icon: PieChart },
  { href: "/economic", label: "Economic Intelligence", icon: Newspaper },
  { href: "/terminal", label: "MT5 Terminal", icon: Terminal },
  { href: "/system", label: "System Monitoring", icon: Cpu },
  { href: "/notifications", label: "Notifications", icon: Bell },
  { href: "/users", label: "User Management", icon: Users },
  { href: "/settings", label: "Settings", icon: Settings },
  { href: "/learning", label: "AI Learning & Evolution", icon: Sparkles },
  { href: "/predictive", label: "Predictive Intelligence", icon: Target },
];

export type Me = { id: number; username: string; role: string; permissions: string[] };
const MeCtx = createContext<Me | null>(null);
export const useMe = () => useContext(MeCtx);
export const can = (me: Me | null, p: string) => !!me?.permissions.includes(p);

function Dot({ ok, warn }: { ok: boolean | null; warn?: boolean }) {
  return <span className={clsx("inline-block h-2 w-2 rounded-full", ok === null ? "bg-dim" : warn ? "bg-warn" : ok ? "bg-neon pulse-dot" : "bg-bear")} />;
}

function Clock() {
  const [t, setT] = useState<Date | null>(null);
  useEffect(() => { setT(new Date()); const i = setInterval(() => setT(new Date()), 1000); return () => clearInterval(i); }, []);
  return <span className="tabular hidden text-dim md:inline">{t ? t.toISOString().slice(11, 19) + " UTC" : ""}</span>;
}

export default function Shell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const path = usePathname();
  const [me, setMe] = useState<Me | null>(null);
  const [open, setOpen] = useState(false);
  const live = useLive(["control", "notification", "system_event"]);
  const { data: state } = useApi<any>(me ? "/api/control/state" : null, 15000, live.tick);
  const { data: sys } = useApi<any>(me ? "/api/system/status" : null, 20000);
  const { data: unread } = useApi<any[]>(me ? "/api/notifications?unread=true" : null, 30000, live.tick);

  useEffect(() => { api<Me>("/api/auth/me").then(setMe).catch(() => router.replace("/login")); }, [router]);
  useEffect(() => setOpen(false), [path]);
  if (!me) return <div className="grid-bg flex h-screen items-center justify-center font-display text-xs tracking-[.3em] text-cyan">AUTHENTICATING…</div>;

  const svc = sys?.services || {};
  const mt5 = svc.mt5_terminal?.status === "UP";
  const conn = svc.mt5_connector?.status === "UP";
  const claude = svc.claude_api?.status;
  const live_ = state?.mode === "LIVE";

  return (
    <MeCtx.Provider value={me}>
      <div className={clsx("grid-bg min-h-screen", live_ && "ring-2 ring-inset ring-bear/60")}>
        <header className="glass sticky top-0 z-40 flex h-12 items-center gap-3 border-x-0 border-t-0 px-3">
          <button className="lg:hidden" onClick={() => setOpen(!open)} aria-label="menu">{open ? <X size={18} /> : <Menu size={18} />}</button>
          <Link href="/" className="flex items-center gap-2">
            <Bot size={18} className="text-neon" />
            <div className="leading-none">
              <div className="font-display text-[12px] font-bold tracking-[.25em] text-text">RR TRADING ENGINE</div>
              <div className="font-display text-[8px] tracking-[.35em] text-cyan">AI COMMAND CENTER</div>
            </div>
          </Link>
          <div className="ml-auto flex items-center gap-3 text-[10px] uppercase tracking-wider">
            <span className="hidden items-center gap-1.5 sm:flex"><Dot ok={claude === "UP"} warn={claude === "DEGRADED" || claude === "NOT_CONFIGURED"} />
              AI {claude === "UP" ? "ONLINE" : claude === "NOT_CONFIGURED" ? "NOT CONFIGURED" : claude || "…"}</span>
            <span className="hidden items-center gap-1.5 md:flex"><Dot ok={sys ? conn : null} />Connector</span>
            <span className="hidden items-center gap-1.5 md:flex"><Dot ok={sys ? mt5 : null} />MT5 / Broker</span>
            <span className={clsx("rounded border px-2 py-0.5 font-bold", live_ ? "border-bear bg-bear/15 text-bear" : "border-cyan/50 text-cyan")}>
              {state?.mode || "…"}</span>
            <span className={clsx("rounded border px-2 py-0.5", state?.auto_trading ? "border-neon/60 text-neon" : "border-line text-dim")}>
              AUTO {state?.auto_trading ? "ON" : "OFF"}</span>
            {state?.kill_switch && <span className="rounded border border-bear bg-bear/20 px-2 py-0.5 text-bear">KILL SWITCH</span>}
            <Clock />
            <span className="hidden items-center gap-1 sm:flex" title={live.connected ? "Realtime stream connected" : "Realtime stream disconnected"}>
              <Activity size={12} className={live.connected ? "text-neon" : "text-bear"} /></span>
            <Link href="/notifications" className="relative" aria-label="notifications">
              <Bell size={15} />
              {!!unread?.length && <span className="absolute -right-1.5 -top-1.5 rounded-full bg-bear px-1 text-[9px] text-white">{unread.length}</span>}
            </Link>
            <span className="hidden text-dim sm:inline">{me.username} · {me.role}</span>
            <button aria-label="logout" onClick={async () => { await post("/api/auth/logout"); router.replace("/login"); }}><LogOut size={14} /></button>
          </div>
        </header>
        <div className="flex">
          <nav className={clsx("glass fixed inset-y-12 left-0 z-30 w-56 overflow-y-auto border-y-0 border-l-0 py-2 transition-transform lg:sticky lg:top-12 lg:h-[calc(100vh-3rem)] lg:translate-x-0",
            open ? "translate-x-0" : "-translate-x-full")}>
            {NAV.map(({ href, label, icon: I }) => {
              const active = href === "/" ? path === "/" : path.startsWith(href);
              return (
                <Link key={href} href={href}
                  className={clsx("flex items-center gap-2.5 border-l-2 px-3 py-2 text-[11px] uppercase tracking-wider transition",
                    active ? "border-neon bg-neon/[.06] text-neon" : "border-transparent text-dim hover:bg-cyan/[.04] hover:text-text")}>
                  <I size={14} />{label}
                </Link>
              );
            })}
            <div className="mt-4 px-3 text-[9px] leading-relaxed text-dim">Trading involves substantial risk. Signals are not guarantees.</div>
          </nav>
          <main className="min-w-0 flex-1 p-3 md:p-4">{children}</main>
        </div>
      </div>
    </MeCtx.Provider>
  );
}

export function PageTitle({ children, sub, right }: { children: ReactNode; sub?: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
      <div>
        <h1 className="font-display text-sm font-bold uppercase tracking-[.25em] text-text">{children}</h1>
        {sub && <p className="mt-0.5 text-[11px] text-dim">{sub}</p>}
      </div>
      {right}
    </div>
  );
}
