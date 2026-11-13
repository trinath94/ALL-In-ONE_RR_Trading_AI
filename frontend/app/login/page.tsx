"use client";
import { motion } from "framer-motion";
import { Bot, Lock } from "lucide-react";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { api } from "@/lib/api";
import { Btn, ErrorLine, inputCls } from "@/components/ui";

export default function Login() {
  const router = useRouter();
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setErr(null);
    try { await api("/api/auth/login", { method: "POST", body: { username: u, password: p } }); router.replace("/"); }
    catch (x: any) { setErr(x.status === 423 ? "Account temporarily locked after repeated failures" : x.status === 429 ? "Too many attempts — wait a minute" : "Invalid credentials"); }
    finally { setBusy(false); }
  }
  return (
    <div className="grid-bg flex min-h-screen items-center justify-center p-4">
      <motion.form onSubmit={submit} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
        className="glass w-full max-w-sm space-y-4 rounded-lg p-6 shadow-cyan">
        <div className="text-center">
          <Bot className="mx-auto text-neon" size={32} />
          <div className="mt-2 font-display text-sm font-bold tracking-[.3em]">RR TRADING ENGINE</div>
          <div className="font-display text-[9px] tracking-[.4em] text-cyan">AI COMMAND CENTER</div>
        </div>
        <label className="block text-[10px] uppercase tracking-widest text-dim">Username
          <input autoComplete="username" value={u} onChange={(e) => setU(e.target.value)} className={inputCls + " mt-1"} required /></label>
        <label className="block text-[10px] uppercase tracking-widest text-dim">Password
          <input type="password" autoComplete="current-password" value={p} onChange={(e) => setP(e.target.value)} className={inputCls + " mt-1"} required /></label>
        <ErrorLine error={err} />
        <Btn type="submit" tone="neon" busy={busy}><Lock size={12} />Authenticate</Btn>
        <p className="text-[10px] text-dim">Authorised personnel only. All actions are audited.</p>
      </motion.form>
    </div>
  );
}
