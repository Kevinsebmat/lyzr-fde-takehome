"use client";

import { useCallback, useEffect, useState } from "react";
import { get, post } from "@/lib/api";
import {
  Bar,
  Empty,
  RunStatus,
  Section,
  Stat,
  Tag,
  Verdict,
  pct,
  usd,
  useRun,
  type Tone,
} from "./ui";

interface Latency {
  count: number;
  p50: number;
  p95: number;
  p99: number;
  max: number;
  mean: number;
}

interface Dashboard {
  overview: {
    spans: number;
    runs: number;
    llm_calls: number;
    tool_calls: number;
    projects: number;
    total_cost_usd: number;
    input_tokens: number;
    output_tokens: number;
    error_rate: number;
    degraded: number;
    run_latency_ms: Latency;
    llm_latency_ms: Latency;
  };
  by_project: {
    project: string;
    runs: number;
    spans: number;
    cost_usd: number;
    cost_per_run_usd: number;
    error_rate: number;
    p95_run_ms: number;
  }[];
  by_model: { model: string; calls: number; cost_usd: number; mean_cost_usd: number; output_tokens: number }[];
  error_shapes: { error_type: string; shape: string; count: number; projects: string[] }[];
  alerts: { rule: string; severity: string; title: string; detail: string; evidence: Record<string, unknown> }[];
  slowest_runs: { run_id: string; project: string; duration_ms: number; status: string }[];
  costliest_runs: { run_id: string; project: string; cost_usd: number }[];
}

interface TraceNode {
  name: string;
  kind: string;
  status: string;
  duration_ms: number;
  cost_usd: number | null;
  model: string | null;
  error_type: string | null;
  children: TraceNode[];
}

const SEVERITY_TONE: Record<string, Tone> = {
  critical: "fail",
  warning: "warn",
  info: "info",
};

function Node({ node, depth = 0 }: { node: TraceNode; depth?: number }) {
  const colour =
    node.status === "error" ? "var(--fail)" : node.status === "degraded" ? "var(--warn)" : "var(--text)";
  return (
    <div className={depth === 0 ? undefined : "trace-node"}>
      <div className="trace-row">
        <span className="trace-name" style={{ color: colour }}>
          {node.name}
        </span>
        <span className="trace-meta">
          {node.duration_ms.toFixed(1)}ms
          {node.cost_usd ? ` · ${usd(node.cost_usd)}` : ""}
          {node.model ? ` · ${node.model}` : ""}
          {node.error_type ? ` · ${node.error_type}` : ""}
        </span>
      </div>
      {node.children.map((c, i) => (
        <Node key={i} node={c} depth={depth + 1} />
      ))}
    </div>
  );
}

export function P11() {
  const [dash, setDash] = useState<Dashboard | null>(null);
  const [trace, setTrace] = useState<{ found: boolean; tree?: TraceNode; total_cost_usd?: number } | null>(null);
  const [canaryOut, setCanaryOut] = useState<Record<string, unknown> | null>(null);
  const { error, running, run } = useRun<unknown>();

  const load = useCallback(async () => {
    await run(async () => {
      setDash(await get<Dashboard>("/api/p11/dashboard"));
      return null;
    });
  }, [run]);

  useEffect(() => {
    void load();
  }, [load]);

  const openTrace = async (runId: string) => {
    setTrace(await get(`/api/p11/trace/${runId}`));
  };

  const runCanary = async () => {
    setCanaryOut(await post("/api/p11/canaries/demo"));
  };

  const o = dash?.overview;

  return (
    <>
      <Section
        title="Traces from the other ten projects"
        right={
          <button className="btn" onClick={load} disabled={running}>
            Reload
          </button>
        }
      >
        <p className="prose" style={{ margin: 0 }}>
          Every LLM call, tool call and step in P1–P10 goes through the shared tracer, so this
          is real traffic rather than a trace this project generated for itself. Run{" "}
          <code>make smoke</code> from the repo root to produce more.
        </p>
      </Section>

      <RunStatus running={running} error={error} />

      {o ? (
        <>
          <div className="stat-row" style={{ marginBottom: 18 }}>
            <Stat label="spans" value={o.spans.toLocaleString()} />
            <Stat label="runs" value={o.runs} />
            <Stat label="projects" value={o.projects} />
            <Stat label="spend" value={usd(o.total_cost_usd, 4)} />
            <Stat label="error rate" value={pct(o.error_rate, 2)} tone={o.error_rate > 0.05 ? "fail" : undefined} />
            <Stat label="degraded" value={o.degraded} />
          </div>

          <Section title="Latency — percentiles, because the mean hides the tail" tight>
            <table>
              <thead>
                <tr>
                  <th>scope</th>
                  <th className="num">count</th>
                  <th className="num">p50</th>
                  <th className="num">p95</th>
                  <th className="num">p99</th>
                  <th className="num">max</th>
                  <th className="num">mean</th>
                </tr>
              </thead>
              <tbody>
                {([["run", o.run_latency_ms], ["llm call", o.llm_latency_ms]] as const).map(
                  ([scope, l]) => (
                    <tr key={scope}>
                      <td className="strong">{scope}</td>
                      <td className="num">{l.count}</td>
                      <td className="num">{l.p50.toFixed(1)}</td>
                      <td className="num strong">{l.p95.toFixed(1)}</td>
                      <td className="num">{l.p99.toFixed(1)}</td>
                      <td className="num">{l.max.toFixed(1)}</td>
                      <td className="num" style={{ color: "var(--muted)" }}>
                        {l.mean.toFixed(1)}
                      </td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          </Section>

          {dash.alerts.length ? (
            <Section title={`Alerts (${dash.alerts.length})`}>
              {dash.alerts.map((a, i) => (
                <div key={i} style={{ marginBottom: 12 }}>
                  <Tag tone={SEVERITY_TONE[a.severity]}>{a.severity}</Tag>{" "}
                  <span className="strong" style={{ color: "var(--text)" }}>
                    {a.rule}
                  </span>{" "}
                  <span style={{ color: "var(--text-dim)" }}>{a.title}</span>
                  <div style={{ color: "var(--text-dim)", marginTop: 2 }}>{a.detail}</div>
                  <div className="hint">
                    {Object.entries(a.evidence).map(([k, v]) => (
                      <span key={k} style={{ marginRight: 14 }}>
                        {k}: {String(v)}
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </Section>
          ) : (
            <Verdict label="No alerts" tone="ok" note="every rule is quiet on the current traces" />
          )}

          <Section title="By project" tight>
            <table>
              <thead>
                <tr>
                  <th>project</th>
                  <th className="num">runs</th>
                  <th className="num">cost</th>
                  <th className="num">$/run</th>
                  <th className="num">errors</th>
                  <th className="num">p95 ms</th>
                </tr>
              </thead>
              <tbody>
                {dash.by_project.map((p) => (
                  <tr key={p.project}>
                    <td className="strong">{p.project}</td>
                    <td className="num">{p.runs}</td>
                    <td className="num">{usd(p.cost_usd, 4)}</td>
                    <td className="num">{usd(p.cost_per_run_usd, 4)}</td>
                    <td className="num" style={{ color: p.error_rate > 0.05 ? "var(--fail)" : undefined }}>
                      {pct(p.error_rate, 1)}
                    </td>
                    <td className="num">{p.p95_run_ms.toFixed(0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>

          <Section title="By model" tight>
            <table>
              <thead>
                <tr>
                  <th>model</th>
                  <th className="num">calls</th>
                  <th className="num">cost</th>
                  <th style={{ width: "30%" }} />
                  <th className="num">$/call</th>
                </tr>
              </thead>
              <tbody>
                {dash.by_model.map((m) => (
                  <tr key={m.model}>
                    <td className="strong">{m.model}</td>
                    <td className="num">{m.calls}</td>
                    <td className="num">{usd(m.cost_usd, 4)}</td>
                    <td>
                      <Bar value={m.cost_usd} max={Math.max(...dash.by_model.map((x) => x.cost_usd))} />
                    </td>
                    <td className="num">{usd(m.mean_cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>

          {dash.error_shapes.length ? (
            <Section title="Errors grouped by shape, not counted" tight>
              <table>
                <thead>
                  <tr>
                    <th className="num">count</th>
                    <th>type</th>
                    <th>shape</th>
                    <th>projects</th>
                  </tr>
                </thead>
                <tbody>
                  {dash.error_shapes.map((e, i) => (
                    <tr key={i}>
                      <td className="num strong">{e.count}</td>
                      <td>{e.error_type}</td>
                      <td style={{ fontSize: 11.5 }}>{e.shape}</td>
                      <td style={{ fontSize: 11.5 }}>{e.projects.join(", ") || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Section>
          ) : null}

          <Section title="Open a run">
            <div className="controls">
              {dash.costliest_runs.slice(0, 4).map((r) => (
                <button key={r.run_id} className="btn" onClick={() => openTrace(r.run_id)}>
                  {r.project ?? "—"} · {usd(r.cost_usd)}
                </button>
              ))}
            </div>
            {trace?.found && trace.tree ? (
              <div style={{ marginTop: 12 }}>
                <Node node={trace.tree} />
                <div className="hint">total cost {usd(trace.total_cost_usd ?? 0)}</div>
              </div>
            ) : trace ? (
              <Empty>That run is no longer in the trace file.</Empty>
            ) : null}
          </Section>

          <Section
            title="Canary and rollback"
            right={
              <button className="btn btn-primary" onClick={runCanary}>
                Run a good release and a bad one
              </button>
            }
          >
            {canaryOut ? (
              <>
                <Verdict
                  label="Rolled back"
                  tone="held"
                  note={String(canaryOut.rollback_reason ?? "")}
                />
                <dl className="kv">
                  <dt>after 5 observations</dt>
                  <dd style={{ color: "var(--warn)" }}>{String(canaryOut.early_promotion_refused)}</dd>
                  <dt>after 25 observations</dt>
                  <dd style={{ color: "var(--ok)" }}>{String(canaryOut.promoted)}</dd>
                  <dt>bad release rolled back after</dt>
                  <dd>{String(canaryOut.rolled_back_after_requests)} canary requests</dd>
                  <dt>now routing to</dt>
                  <dd>{String(canaryOut.routing_now)}</dd>
                </dl>
                <p className="hint prose">
                  An under-observed canary is <em>unknown</em>, not healthy — promoting on five
                  good requests is how a bad release reaches production.
                </p>
              </>
            ) : (
              <Empty>Run the demo to see promotion gated on evidence and rollback on breach.</Empty>
            )}
          </Section>
        </>
      ) : null}

      {!dash && !running && !error ? (
        <Empty>No traces yet. Run `make smoke` from the repo root.</Empty>
      ) : null}
    </>
  );
}
