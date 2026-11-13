"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

/** Poll an endpoint; refetches when `refreshOn` (websocket event counter) changes. */
export function useApi<T = any>(path: string | null, intervalMs = 0, refreshOn?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    if (!path) return;
    try { setData(await api<T>(path)); setError(null); }
    catch (e: any) { setError(e.message); }
    finally { setLoading(false); }
  }, [path]);
  useEffect(() => { load(); }, [load, refreshOn]);
  useEffect(() => {
    if (!intervalMs) return;
    const t = setInterval(load, intervalMs);
    return () => clearInterval(t);
  }, [load, intervalMs]);
  return { data, error, loading, reload: load };
}

export type LiveEvent = { kind: string; ts: string; data: any };

/** Single shared WebSocket to /api/ws with automatic reconnect. */
const listeners = new Set<(e: LiveEvent) => void>();
let socket: WebSocket | null = null;
let connected = false;
const statusListeners = new Set<(c: boolean) => void>();

function connect() {
  if (typeof window === "undefined" || socket) return;
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${window.location.host}/api/ws`);
  socket.onopen = () => { connected = true; statusListeners.forEach((l) => l(true)); };
  socket.onmessage = (m) => { try { const e = JSON.parse(m.data); listeners.forEach((l) => l(e)); } catch { /* ignore */ } };
  socket.onclose = () => {
    connected = false; statusListeners.forEach((l) => l(false)); socket = null;
    setTimeout(connect, 3000);
  };
}

export function useLive(kinds?: string[]) {
  const [last, setLast] = useState<LiveEvent | null>(null);
  const [tick, setTick] = useState(0);
  const [isConnected, setConnected] = useState(connected);
  const kindsRef = useRef(kinds);
  useEffect(() => {
    connect();
    const l = (e: LiveEvent) => {
      if (!kindsRef.current || kindsRef.current.includes(e.kind)) { setLast(e); setTick((t) => t + 1); }
    };
    listeners.add(l); statusListeners.add(setConnected);
    return () => { listeners.delete(l); statusListeners.delete(setConnected); };
  }, []);
  return { last, tick, connected: isConnected };
}
