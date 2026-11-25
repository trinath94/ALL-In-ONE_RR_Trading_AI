"use client";
import { useEffect } from "react";
import { post } from "@/lib/api";

/** Error boundary for every console page: the failure is reported as an incident and the rest of the console keeps
 *  working. No secrets are sent: only the page, the message and the stack. */
export default function ConsoleError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    post("/api/client-errors", { message: String(error?.message || error).slice(0, 1000), page: window.location.pathname,
      stack: String(error?.stack || "").slice(0, 4000), digest: error?.digest || "" }).catch(() => undefined);
  }, [error]);
  return (
    <div className="m-4 rounded border border-bear/60 bg-bear/5 p-3 text-[12px]">
      <div className="font-bold text-bear">This page hit an unexpected error.</div>
      <div className="mt-1 text-dim">It was reported as an incident (System Monitor → Incidents). Trading and the other pages are not affected.</div>
      <div className="mt-1 break-words font-mono text-[10px] text-dim">{String(error?.message || error)}</div>
      <button type="button" className="mt-2 rounded border border-cyan/50 px-2 py-1 text-cyan" onClick={() => reset()}>Try again</button>
    </div>
  );
}
