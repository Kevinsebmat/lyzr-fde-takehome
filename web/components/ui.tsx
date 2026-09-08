"use client";

import { useCallback, useState } from "react";
import { ApiError } from "@/lib/api";

export type Tone = "ok" | "warn" | "fail" | "info" | "held";

/* ---------- the signature element ----------
   Every result leads with the safety mechanism that engaged. A reader should
   be able to answer "what did the system do about it?" without reading a
   single number. */

export function Verdict({
  label,
  tone,
  note,
}: {
  label: string;
  tone: Tone;
  note?: string;
}) {
  return (
    <div className="verdict" data-tone={tone} role="status">
      <span className="verdict-label">{label}</span>
      {note ? <span className="verdict-note">{note}</span> : null}
    </div>
  );
}

export function Section({
  title,
  right,
  tight,
  children,
}: {
  title: string;
  right?: React.ReactNode;
  tight?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section className="section">
      <header className="section-head">
        <span>{title}</span>
        {right ? (
          <>
            <span style={{ flex: 1 }} />
            {right}
          </>
        ) : null}
      </header>
      <div className={tight ? "section-body tight" : "section-body"}>{children}</div>
    </section>
  );
}

export function Tag({ children, tone }: { children: React.ReactNode; tone?: Tone }) {
  return (
    <span className="tag" data-tone={tone}>
      {children}
    </span>
  );
}

export function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  tone?: Tone;
}) {
  return (
    <div>
      <span className="stat-label">{label}</span>
      <span className="stat-value" style={tone ? { color: `var(--${tone})` } : undefined}>
        {value}
      </span>
    </div>
  );
}

export function KeyValues({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <dl className="kv">
      {rows.map(([k, v]) => (
        <div key={k} style={{ display: "contents" }}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Bar({ value, max, tone }: { value: number; max: number; tone?: Tone }) {
  const pct = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  return (
    <div className="bar-track" aria-hidden="true">
      <div className="bar-fill" data-tone={tone} style={{ width: `${pct}%` }} />
    </div>
  );
}

export const usd = (n: number | null | undefined, places = 5) =>
  n === null || n === undefined ? "—" : `$${n.toFixed(places)}`;

export const pct = (n: number | null | undefined, places = 0) =>
  n === null || n === undefined ? "—" : `${(n * 100).toFixed(places)}%`;

/* ---------- run state ----------
   One hook so every panel handles pending, error and result the same way. An
   inconsistent loading state across eleven panels reads as eleven prototypes. */

export interface RunState<T> {
  data: T | null;
  error: string | null;
  running: boolean;
  run: (fn: () => Promise<T>) => Promise<void>;
  reset: () => void;
}

export function useRun<T>(): RunState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const run = useCallback(async (fn: () => Promise<T>) => {
    setRunning(true);
    setError(null);
    try {
      setData(await fn());
    } catch (e) {
      setError(
        e instanceof ApiError
          ? e.message
          : e instanceof Error
            ? e.message
            : "the request failed",
      );
      setData(null);
    } finally {
      setRunning(false);
    }
  }, []);

  const reset = useCallback(() => {
    setData(null);
    setError(null);
  }, []);

  return { data, error, running, run, reset };
}

export function RunStatus({ running, error }: { running: boolean; error: string | null }) {
  if (running) return <div className="running" aria-label="Running" />;
  if (error)
    return (
      <div className="error" role="alert">
        {error}
        <div className="hint">
          Is the API running? Start it with <code>make dev</code>, or{" "}
          <code>uvicorn server.main:app --port 8000</code>.
        </div>
      </div>
    );
  return null;
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>;
}
