"use client";

import { useState } from "react";
import { post } from "@/lib/api";
import {
  Bar,
  Empty,
  KeyValues,
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

/* ===================== P01 — structured output ===================== */

const CLEAN_TICKET = `Subject: URGENT - checkout completely down

Our entire checkout has been failing since 09:00 UTC. Every customer hitting the
payment step gets a 500 error. We are an enterprise account (ACC-1001) and this
is affecting roughly 12,000 users. This is costing us real money every minute.

We want a credit for the downtime - at least $5,000. Please escalate immediately.`;

interface ExtractResult {
  ok: boolean;
  value: Record<string, unknown> | null;
  attempts: number;
  repaired: boolean;
  failures: string[];
  model: string;
  cost_usd: number;
  error: string | null;
}

export function P01() {
  const [text, setText] = useState(CLEAN_TICKET);
  const [attempts, setAttempts] = useState(3);
  const { data, error, running, run } = useRun<ExtractResult>();

  const extract = () => run(() => post("/api/p01/extract", { text, max_attempts: attempts }));

  const verdict = !data
    ? null
    : !data.ok
      ? { label: "Gave up cleanly", tone: "fail" as Tone, note: data.error ?? "" }
      : data.repaired
        ? {
            label: "Repaired",
            tone: "warn" as Tone,
            note: `${data.failures.length} validation failure(s) fed back, corrected on attempt ${data.attempts}`,
          }
        : { label: "Valid first time", tone: "ok" as Tone, note: "no repair turn needed" };

  const value = data?.value as
    | {
        severity: string;
        category: string;
        summary: string;
        customer_sentiment: number;
        affected_users: number;
        requires_human_review: boolean;
        refund_amount_usd: number | null;
        action_items: { description: string; owner_team: string; due_within_hours: number }[];
      }
    | null
    | undefined;

  return (
    <>
      <Section title="Support ticket">
        <textarea
          className="textarea"
          value={text}
          onChange={(e) => setText(e.target.value)}
          spellCheck={false}
          aria-label="Ticket text"
        />
        <div className="controls" style={{ marginTop: 10 }}>
          <button className="btn btn-primary" onClick={extract} disabled={running}>
            Extract triage
          </button>
          <span className="field-label">max attempts</span>
          <select
            className="select"
            value={attempts}
            onChange={(e) => setAttempts(Number(e.target.value))}
          >
            {[1, 2, 3, 4].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>
        <p className="hint prose">
          Break the ticket — delete the user count, or ask for a nonsense refund — and the
          repair loop feeds the validation error back rather than retrying blind.
        </p>
      </Section>

      <RunStatus running={running} error={error} />

      {verdict ? <Verdict {...verdict} /> : null}

      {data?.failures.length ? (
        <Section title="Validation failures fed back">
          {data.failures.map((f, i) => (
            <pre key={i} className="mono-block" style={{ marginBottom: 8 }}>
              {f}
            </pre>
          ))}
        </Section>
      ) : null}

      {value ? (
        <Section title="Extracted">
          <KeyValues
            rows={[
              ["severity", <Tag key="s" tone={value.severity === "critical" ? "fail" : "warn"}>{value.severity}</Tag>],
              ["category", value.category],
              ["summary", value.summary],
              ["sentiment", value.customer_sentiment.toFixed(2)],
              ["affected users", value.affected_users.toLocaleString()],
              ["human review", value.requires_human_review ? "yes" : "no"],
              ["refund", value.refund_amount_usd ? `$${value.refund_amount_usd.toLocaleString()}` : "—"],
            ]}
          />
          <table style={{ marginTop: 14 }}>
            <thead>
              <tr>
                <th>action item</th>
                <th>owner</th>
                <th className="num">due (h)</th>
              </tr>
            </thead>
            <tbody>
              {value.action_items.map((a, i) => (
                <tr key={i}>
                  <td className="strong">{a.description}</td>
                  <td>{a.owner_team}</td>
                  <td className="num">{a.due_within_hours}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      ) : null}

      {data ? (
        <div className="stat-row">
          <Stat label="attempts" value={data.attempts} />
          <Stat label="model" value={data.model} />
          <Stat label="cost" value={usd(data.cost_usd)} />
        </div>
      ) : null}
    </>
  );
}

/* ===================== P02 — RAG with citation grounding ===================== */

interface Claim {
  text: string;
  citation_ids: string[];
  verified: boolean;
  support_score: number;
  reason: string;
}

interface AskResult {
  action: string;
  answered: boolean;
  answer: string;
  claims: Claim[];
  citations: { id: string; title: string; heading: string; source: string; score: number; used: boolean }[];
  confidence: { score: number; retrieval: number; grounding: number; self_reported: number; reasons: string[] } | null;
  retrieved: number;
  cost_usd: number;
}

const P02_VERDICTS: Record<string, { label: string; tone: Tone; note: string }> = {
  answer: { label: "Answered", tone: "ok", note: "every claim verified against the text it cites" },
  answer_caveated: {
    label: "Answered with caveat",
    tone: "warn",
    note: "unverified claims were removed, not softened",
  },
  fallback_search: {
    label: "Fell back to search",
    tone: "info",
    note: "outside the knowledge base, and labelled as unverified",
  },
  refuse: { label: "Refused", tone: "held", note: "no trustworthy answer available — and it says why" },
};

export function P02() {
  const [question, setQuestion] = useState("How long are audit logs kept?");
  const [search, setSearch] = useState(false);
  const { data, error, running, run } = useRun<AskResult>();

  const ask = (q?: string) => {
    const asked = q ?? question;
    if (q) setQuestion(q);
    return run(() =>
      post("/api/p02/ask", { question: asked, allow_search_fallback: search }),
    );
  };

  const verdict = data ? P02_VERDICTS[data.action] : null;

  return (
    <>
      <Section title="Ask the knowledge base">
        <div className="field-row">
          <input
            className="input"
            style={{ flex: 1 }}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && ask()}
            aria-label="Question"
          />
          <button className="btn btn-primary" onClick={() => ask()} disabled={running}>
            Ask
          </button>
        </div>
        <div className="controls">
          <label className="field-label" style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input type="checkbox" checked={search} onChange={(e) => setSearch(e.target.checked)} />
            allow web-search fallback
          </label>
        </div>

        <p className="hint prose" style={{ marginTop: 12 }}>
          The corpus covers refunds, SLA, retention, SSO and rate limits — and deliberately
          says nothing about HIPAA, pricing or on-premise deployment. The second row is the
          interesting one.
        </p>
        <div className="controls" style={{ marginTop: 8 }}>
          {["How long do customers have to request a refund on a monthly plan?", "Which SSO protocols work on the Business plan?"].map(
            (q) => (
              <button key={q} className="btn" onClick={() => ask(q)} disabled={running}>
                {q.length > 44 ? `${q.slice(0, 44)}…` : q}
              </button>
            ),
          )}
        </div>
        <div className="controls" style={{ marginTop: 6 }}>
          {["Do you sign a HIPAA business associate agreement?", "What does the Enterprise plan cost per seat per year?"].map(
            (q) => (
              <button key={q} className="btn" onClick={() => ask(q)} disabled={running}>
                {q.length > 44 ? `${q.slice(0, 44)}…` : q}
              </button>
            ),
          )}
        </div>
      </Section>

      <RunStatus running={running} error={error} />

      {verdict ? <Verdict {...verdict} /> : null}

      {data ? (
        <Section title="Answer">
          <p className="mono-block">{data.answer}</p>
        </Section>
      ) : null}

      {data?.confidence ? (
        <Section title="Confidence">
          <div className="stat-row" style={{ marginBottom: 14 }}>
            <Stat
              label="score"
              value={data.confidence.score.toFixed(2)}
              tone={data.confidence.score >= 0.62 ? "ok" : "fail"}
            />
            <Stat label="retrieval" value={data.confidence.retrieval.toFixed(2)} />
            <Stat label="grounding" value={data.confidence.grounding.toFixed(2)} />
            <Stat label="model self-report" value={data.confidence.self_reported.toFixed(2)} />
          </div>
          {data.confidence.reasons.map((r, i) => (
            <div key={i} className="hint">
              · {r}
            </div>
          ))}
          <p className="hint prose" style={{ marginTop: 10 }}>
            Grounding carries the most weight (0.50) because it is the only signal computed
            from the evidence rather than asserted. The model&rsquo;s own confidence carries
            the least (0.20).
          </p>
        </Section>
      ) : null}

      {data?.claims.length ? (
        <Section title="Claim verification" tight>
          <table>
            <thead>
              <tr>
                <th style={{ width: 24 }} />
                <th>claim</th>
                <th>cites</th>
                <th className="num">support</th>
              </tr>
            </thead>
            <tbody>
              {data.claims.map((c, i) => (
                <tr key={i}>
                  <td>
                    <span className="claim-mark" data-ok={c.verified}>
                      {c.verified ? "✓" : "✗"}
                    </span>
                  </td>
                  <td className="strong">
                    {c.text}
                    {!c.verified ? (
                      <div style={{ color: "var(--fail)", fontSize: 11.5, marginTop: 3 }}>
                        {c.reason}
                      </div>
                    ) : null}
                  </td>
                  <td>{c.citation_ids.join(", ") || "—"}</td>
                  <td className="num">{c.support_score.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      ) : null}

      {data?.citations.length ? (
        <Section title={`Retrieved (${data.retrieved})`} tight>
          <table>
            <thead>
              <tr>
                <th>chunk</th>
                <th>source</th>
                <th className="num">score</th>
                <th>used</th>
              </tr>
            </thead>
            <tbody>
              {data.citations.map((c) => (
                <tr key={c.id}>
                  <td className="strong">
                    {c.title} — {c.heading}
                  </td>
                  <td style={{ fontSize: 11 }}>{c.source}</td>
                  <td className="num">{c.score.toFixed(3)}</td>
                  <td>{c.used ? <Tag tone="ok">cited</Tag> : <Tag>unused</Tag>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      ) : null}
    </>
  );
}

/* ===================== P03 — ReAct planner ===================== */

interface Step {
  iteration: number;
  thought: string;
  action: string | null;
  action_input: string | null;
  observation: string | null;
  critique: string | null;
  progressed: boolean;
}

interface ReActResult {
  outcome: string;
  answer: string;
  reason: string;
  solved: boolean;
  degraded: boolean;
  steps: Step[];
  iterations: number;
  tool_calls: number;
  cost_usd: number;
}

const P03_VERDICTS: Record<string, { label: string; tone: Tone; note: string }> = {
  solved: { label: "Solved", tone: "ok", note: "reached an answer and stopped" },
  loop_detected: { label: "Loop detected", tone: "held", note: "same action, same input, three times" },
  max_iterations: { label: "Hit the ceiling", tone: "warn", note: "the backstop, not the plan" },
  no_progress: { label: "No progress", tone: "held", note: "self-critique stopped it wandering" },
  budget_exhausted: { label: "Budget exhausted", tone: "fail", note: "spend ceiling reached mid-run" },
  failed: { label: "Planner failed", tone: "fail", note: "unusable planner output" },
};

export function P03() {
  const [task, setTask] = useState("Does order ORD-4417 need manager approval for a refund?");
  const [maxIter, setMaxIter] = useState(8);
  const [reflect, setReflect] = useState(true);
  const { data, error, running, run } = useRun<ReActResult>();

  const go = () =>
    run(() => post("/api/p03/run", { task, max_iterations: maxIter, reflect }));

  const verdict = data ? P03_VERDICTS[data.outcome] : null;

  return (
    <>
      <Section title="Task">
        <div className="field-row">
          <input
            className="input"
            style={{ flex: 1 }}
            value={task}
            onChange={(e) => setTask(e.target.value)}
            aria-label="Task"
          />
        </div>
        <div className="controls">
          <button className="btn btn-primary" onClick={go} disabled={running}>
            Run the loop
          </button>
          <span className="field-label">max iterations</span>
          <select className="select" value={maxIter} onChange={(e) => setMaxIter(Number(e.target.value))}>
            {[1, 2, 3, 5, 8, 12].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
          <label className="field-label" style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input type="checkbox" checked={reflect} onChange={(e) => setReflect(e.target.checked)} />
            self-critique
          </label>
        </div>
        <p className="hint prose">
          Four stopping conditions run together. Drop max iterations to 1, or turn off
          self-critique, and a different one takes over — every exit still answers.
        </p>
      </Section>

      <RunStatus running={running} error={error} />

      {verdict ? <Verdict {...verdict} note={data?.reason || verdict.note} /> : null}

      {data ? (
        <Section title="Answer">
          <p className="mono-block">{data.answer}</p>
        </Section>
      ) : null}

      {data?.steps.length ? (
        <Section title="Steps">
          {data.steps.map((s) => (
            <div key={s.iteration} style={{ marginBottom: 14 }}>
              <div style={{ color: "var(--muted)", fontSize: 11, letterSpacing: "0.1em" }}>
                STEP {s.iteration}
              </div>
              <div style={{ color: "var(--text-dim)" }}>{s.thought}</div>
              {s.action ? (
                <>
                  <div style={{ color: "var(--info)", marginTop: 3 }}>
                    → {s.action}({s.action_input})
                  </div>
                  <div
                    style={{
                      color: s.observation?.startsWith("ERROR") ? "var(--fail)" : "var(--text)",
                      marginTop: 2,
                    }}
                  >
                    ← {s.observation}
                  </div>
                </>
              ) : null}
              {s.critique ? (
                <div style={{ marginTop: 3 }}>
                  <Tag tone={s.progressed ? "ok" : "warn"}>
                    {s.progressed ? "progress" : "no progress"}
                  </Tag>{" "}
                  <span style={{ color: "var(--muted)" }}>{s.critique}</span>
                </div>
              ) : null}
            </div>
          ))}
        </Section>
      ) : null}

      {data ? (
        <div className="stat-row">
          <Stat label="iterations" value={data.iterations} />
          <Stat label="tool calls" value={data.tool_calls} />
          <Stat label="cost" value={usd(data.cost_usd)} />
        </div>
      ) : null}
    </>
  );
}

/* ===================== P04 — tool orchestrator ===================== */

interface GatherResult {
  results: { tool: string; ok: boolean; value: unknown; error: string | null; duration_ms: number }[];
  conflicts: {
    field: string;
    values: Record<string, string>;
    winner: string | null;
    resolution: string;
    escalate: boolean;
  }[];
  merged: Record<string, unknown>;
  denied: string[];
  abandoned_workers: number;
  called: number;
  ok: number;
  failed: number;
  duration_ms: number;
}

export function P04() {
  const [caller, setCaller] = useState("analyst");
  const { data, error, running, run } = useRun<GatherResult>();

  const gather = () => run(() => post("/api/p04/gather", { account: "ACC-1001", caller }));

  const verdict = !data
    ? null
    : data.conflicts.some((c) => c.escalate)
      ? { label: "Escalated", tone: "held" as Tone, note: "sources of equal authority disagree — no winner taken" }
      : data.conflicts.length
        ? {
            label: "Conflict resolved",
            tone: "warn" as Tone,
            note: `${data.conflicts.length} disagreement(s) settled by declared authority`,
          }
        : { label: "No conflicts", tone: "ok" as Tone, note: "every source agreed" };

  return (
    <>
      <Section title="Fan out across every tool">
        <div className="controls">
          <span className="field-label">caller</span>
          <select className="select" value={caller} onChange={(e) => setCaller(e.target.value)}>
            <option value="readonly">readonly — crm:read</option>
            <option value="analyst">analyst — billing/crm/analytics:read</option>
            <option value="admin">admin — *</option>
          </select>
          <button className="btn btn-primary" onClick={gather} disabled={running}>
            Gather
          </button>
        </div>
        <p className="hint prose">
          Switch to <strong>readonly</strong>: the denied calls are recorded, not silently
          filtered. Hiding a tool from the menu is not access control — the check happens at
          invocation.
        </p>
      </Section>

      <RunStatus running={running} error={error} />

      {verdict ? <Verdict {...verdict} /> : null}

      {data ? (
        <>
          <Section title="Tool results" tight>
            <table>
              <thead>
                <tr>
                  <th>tool</th>
                  <th>outcome</th>
                  <th className="num">ms</th>
                  <th>value / error</th>
                </tr>
              </thead>
              <tbody>
                {data.results.map((r) => (
                  <tr key={r.tool}>
                    <td className="strong">{r.tool}</td>
                    <td>{r.ok ? <Tag tone="ok">ok</Tag> : <Tag tone="fail">failed</Tag>}</td>
                    <td className="num">{r.duration_ms.toFixed(0)}</td>
                    <td style={{ fontSize: 11.5 }}>
                      {r.ok ? JSON.stringify(r.value) : r.error}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>

          {data.conflicts.length ? (
            <Section title="Conflicts">
              {data.conflicts.map((c) => (
                <div key={c.field} style={{ marginBottom: 12 }}>
                  <div className="strong" style={{ color: "var(--text)" }}>
                    {c.field}
                  </div>
                  <table style={{ marginTop: 4 }}>
                    <tbody>
                      {Object.entries(c.values).map(([tool, v]) => (
                        <tr key={tool}>
                          <td style={{ width: 200 }}>{tool}</td>
                          <td className={tool === c.winner ? "strong" : ""}>
                            {v} {tool === c.winner ? <Tag tone="ok">taken</Tag> : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="hint">{c.resolution}</div>
                </div>
              ))}
            </Section>
          ) : null}

          <Section title="Merged">
            <pre className="mono-block">{JSON.stringify(data.merged, null, 2)}</pre>
          </Section>

          <div className="stat-row">
            <Stat label="called" value={data.called} />
            <Stat label="ok" value={data.ok} tone="ok" />
            <Stat label="failed" value={data.failed} tone={data.failed ? "fail" : undefined} />
            <Stat label="denied" value={data.denied.length} />
            <Stat label="wall clock" value={`${data.duration_ms.toFixed(0)}ms`} />
            <Stat
              label="abandoned workers"
              value={data.abandoned_workers}
              tone={data.abandoned_workers ? "warn" : undefined}
            />
          </div>
          {data.abandoned_workers ? (
            <p className="hint prose">
              A timeout bounds how long we wait; it cannot cancel a blocking call. Those
              threads are still running, which is why they are counted rather than ignored.
            </p>
          ) : null}
        </>
      ) : null}

      {!data && !running && !error ? <Empty>Run a gather to see the fan-out.</Empty> : null}
    </>
  );
}
