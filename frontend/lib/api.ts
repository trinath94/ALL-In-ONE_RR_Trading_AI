"use client";
// Thin API client: same-origin requests (Nginx routes /api), session cookie is httpOnly,
// CSRF token is read from its readable cookie and echoed in X-CSRF-Token for writes.

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

function csrf(): string {
  const m = typeof document !== "undefined" ? document.cookie.match(/(?:^|; )rr_csrf=([^;]+)/) : null;
  return m ? decodeURIComponent(m[1]) : "";
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = opts.method || "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrf();
  const res = await fetch(path, { method, headers, credentials: "same-origin", cache: "no-store",
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  if (res.status === 401 && typeof window !== "undefined" && !path.startsWith("/api/auth/login")) {
    window.location.href = "/login";
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const d = data?.detail;
    throw new ApiError(res.status, typeof d === "string" ? d : JSON.stringify(d ?? res.statusText));
  }
  return data as T;
}

export const post = <T = any>(p: string, body?: unknown) => api<T>(p, { method: "POST", body: body ?? {} });
export const put = <T = any>(p: string, body: unknown) => api<T>(p, { method: "PUT", body });
export const patch = <T = any>(p: string, body: unknown) => api<T>(p, { method: "PATCH", body });

export function rid(): string {
  return (crypto as any).randomUUID ? crypto.randomUUID().replace(/-/g, "") : String(Date.now()) + Math.random().toString(16).slice(2);
}

export const fmt = {
  num: (v: any, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })),
  px: (v: any) => (v === null || v === undefined ? "—" : Number(v) >= 100 ? Number(v).toFixed(2) : Number(v).toFixed(5)),
  pct: (v: any, d = 2) => (v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(d)}%`),
  pctRaw: (v: any, d = 2) => (v === null || v === undefined ? "—" : `${Number(v).toFixed(d)}%`),
  time: (v: any) => (v ? new Date(v).toLocaleString() : "—"),
  dur: (s: number) => { const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return h ? `${h}h ${m}m` : `${m}m`; },
};
