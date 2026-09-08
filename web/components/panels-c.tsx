"use client";

import { useCallback, useEffect, useState } from "react";
import { get, post } from "@/lib/api";
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

/* ===================== P08 — event automation ===================== */

interface EventRow {
  id: string;
  type: string;
  status: string;
  attempts: number;
  max_attempts: number;
  last_error: string | null;
  result: string | null;
}

interface DeadLetter {
  id: string;
  type: string;
  attempts: number;
  last_error: string;
}

export function P08() {
  const [log, setLog] = useState<string[]>([]);
  const [events, setEvents] = useState<{ events: EventRow[]; stats: Record<string, unknown> } | null>(null);
  const [dead, setDead] = useState<{ dead_letters: DeadLetter[] } | null>(null);
  const [ledger, setLedger] = useState<Record<string, number>>({});
  const [lastDuplicate, setLastDuplicate] = useState<boolean | null>(null);
  const { error, running, run } = useRun<unknown>();

  const refresh = useCallback(async () => {
    try {
      setEvents(await get("/api/p08/events?limit=30"));
      setDead(await get("/api/p08/dead-letters"));
    } catch {
      /* surfaced by the action that triggered it */
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const deliver = async (times: number) => {
    const lines: string[] = [];
    let duplicate: boolean | null = null;
    await run(async () => {
      for (let i = 0; i < times; i++) {
        const res = await post<{ event_id: string; duplicate: boolean }>(
          "/api/p08/webhook",
          { type: "payment.succeeded", data: { account: "ACC-1001", amount: 4820 } },
          { "Idempotency-Key": "pay_88" },
        );
        duplicate = res.duplicate;
        lines.push(
          `delivery ${i + 1}: ${res.duplicate ? "duplicate suppressed" : "accepted"} → ${res.event_id}`,
        );
      }
      return null;
    });
    setLastDuplicate(duplicate);
    setLog((prev) => [...prev, ...lines]);
    await refresh();
  };

  const sendOther = async (type: string, data: Record<string, unknown>, key: string) => {
    await run(() => post("/api/p08/webhook", { type, data }, { "Idempotency-Key": key }));
    setLog((prev) => [...prev, `sent ${type}`]);
    await refresh();
  };

  const drain = async () => {
    const res = await post<{ worker: Record<string, number>; ledger: Record<string, number> }>(
      "/api/p08/drain",
    );
    setLedger(res.ledger);
    setLog((prev) => [...prev, `worker: ${JSON.stringify(res.worker)}`]);
    await refresh();
  };

  const replay = async (id: string) => {
    await post(`/api/p08/dead-letters/${id}/replay`);
    setLog((prev) => [...prev, `replayed ${id} under a new key`]);
    await refresh();
  };

  return (
    <>
      <Section title="A flaky sender">
        <div className="controls">
          <button className="btn btn-primary" onClick={() => deliver(3)} disabled={running}>
            Deliver the same payment 3×
          </button>
          <button
            className="btn"
            onClick={() => sendOther("ticket.created", { missing: "subject" }, "tkt-1")}
            disabled={running}
          >
            Send a malformed ticket
          </button>
          <button
            className="btn"
            onClick={() => sendOther("nobody.handles.this", {}, "unr-1")}
            disabled={running}
          >
            Send an unroutable event
          </button>
          <button className="btn btn-primary" onClick={drain} disabled={running}>
            Run the worker
          </button>
        </div>
        <p className="hint prose">
          Every real webhook source delivers at least once, so sometimes twice. The unique
          index on the idempotency key makes a duplicate impossible at the database level.
        </p>
      </Section>

      <RunStatus running={running} error={error} />

      {lastDuplicate !== null ? (
        <Verdict
          label={lastDuplicate ? "Duplicate suppressed" : "Accepted"}
          tone={lastDuplicate ? "held" : "ok"}
          note={
            lastDuplicate
              ? "the redelivery returned the original event — 200, not an error, so the sender stops retrying"
              : "new work queued"
          }
        />
      ) : null}

      {log.length ? (
        <Section title="Delivery log" right={<button className="btn" onClick={() => setLog([])}>Clear</button>}>
          <pre className="mono-block">{log.join("\n")}</pre>
        </Section>
      ) : null}

      {Object.keys(ledger).length ? (
        <Section title="Ledger">
          <div className="stat-row">
            {Object.entries(ledger).map(([account, amount]) => (
              <Stat key={account} label={account} value={`$${amount.toLocaleString()}`} tone="ok" />
            ))}
          </div>
          <p className="hint prose">Delivered three times. Credited once.</p>
        </Section>
      ) : null}

      <Section title="Queue" tight>
        {events?.events.length ? (
          <table>
            <thead>
              <tr>
                <th>type</th>
                <th>status</th>
                <th className="num">attempts</th>
                <th>result / error</th>
              </tr>
            </thead>
            <tbody>
              {events.events.map((e) => (
                <tr key={e.id}>
                  <td className="strong">{e.type}</td>
                  <td>
                    <Tag
                      tone={
                        e.status === "done" ? "ok" : e.status === "dead" ? "fail" : e.status === "failed" ? "warn" : undefined
                      }
                    >
                      {e.status}
                    </Tag>
                  </td>
                  <td className="num">
                    {e.attempts}/{e.max_attempts}
                  </td>
                  <td style={{ fontSize: 11.5 }}>{e.result ?? e.last_error ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>The queue is empty. Deliver an event to fill it.</Empty>
        )}
      </Section>

      {dead?.dead_letters.length ? (
        <Section title={`Dead letters (${dead.dead_letters.length})`}>
          {dead.dead_letters.map((dl) => (
            <div key={dl.id} style={{ marginBottom: 10 }}>
              <div className="strong" style={{ color: "var(--text)" }}>
                {dl.type} <span style={{ color: "var(--muted)" }}>· {dl.attempts} attempt(s)</span>
              </div>
              <div style={{ color: "var(--fail)", fontSize: 11.5 }}>{dl.last_error}</div>
              <button className="btn" style={{ marginTop: 6 }} onClick={() => replay(dl.id)}>
                Replay
              </button>
            </div>
          ))}
          <p className="hint prose">
            Replay uses a <em>new</em> idempotency key. Under the original it would hit the
            unique index and silently do nothing.
          </p>
        </Section>
      ) : null}
    </>
  );
}

/* ===================== P09 — debate ===================== */

interface DebateResult {
  verdict: string;
  recommendation: string;
  winner: string | null;
  agreement: number;
  tally: Record<string, number>;
  votes: Record<string, string>;
  notes: string[];
  proposals: Record<string, { recommendation: string; counterargument: string; confidence: number }>;
  flaws: { proposal_key: string; issue: string; fatal: boolean }[];
  confidence: number;
  cost_usd: number;
}

const P09_VERDICTS: Record<string, { label: string; tone: Tone; note: string }> = {
  consensus: { label: "Consensus", tone: "ok", note: "a clear winner the critic did not block" },
  majority: { label: "Majority", tone: "warn", note: "a winner, but a real minority existed" },
  contested: { label: "Contested", tone: "held", note: "tied — no winner taken, because breaking a tie by ordering invents a decision" },
  blocked: { label: "Blocked", tone: "fail", note: "the vote winner has a disqualifying flaw" },
  failed: { label: "Failed", tone: "fail", note: "too few proposals or no valid votes" },
};

export function P09() {
  const [question, setQuestion] = useState(
    "Should we move our document ingestion pipeline from nightly batch to streaming?",
  );
  const { data, error, running, run } = useRun<DebateResult>();

  const debate = () => run(() => post("/api/p09/debate", { question }));
  const verdict = data ? P09_VERDICTS[data.verdict] : null;
  const maxVotes = data ? Math.max(1, ...Object.values(data.tally)) : 1;

  return (
    <>
      <Section title="Question">
        <div className="field-row">
          <input className="input" style={{ flex: 1 }} value={question} onChange={(e) => setQuestion(e.target.value)} />
          <button className="btn btn-primary" onClick={debate} disabled={running}>
            Hold a debate
          </button>
        </div>
        <p className="hint prose">
          Four advisors with genuinely different objectives — delivery, risk, cost, the end
          user — answer without seeing each other&rsquo;s work. Anchoring is what turns four
          opinions into one opinion at four times the price.
        </p>
      </Section>

      <RunStatus running={running} error={error} />
      {verdict ? <Verdict {...verdict} /> : null}

      {data ? (
        <>
          <Section title="Vote" tight>
            <table>
              <thead>
                <tr>
                  <th>proposal</th>
                  <th className="num">votes</th>
                  <th style={{ width: "40%" }} />
                  <th>voters</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.tally)
                  .sort((a, b) => b[1] - a[1])
                  .map(([key, count]) => (
                    <tr key={key}>
                      <td className="strong">
                        {key} {key === data.winner ? <Tag tone="ok">winner</Tag> : null}
                      </td>
                      <td className="num">{count}</td>
                      <td>
                        <Bar value={count} max={maxVotes} tone={key === data.winner ? "ok" : undefined} />
                      </td>
                      <td style={{ fontSize: 11.5 }}>
                        {Object.entries(data.votes)
                          .filter(([, v]) => v === key)
                          .map(([voter]) => voter)
                          .join(", ") || "—"}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </Section>

          <div className="stat-row" style={{ marginBottom: 16 }}>
            <Stat label="agreement" value={pct(data.agreement)} />
            <Stat label="confidence" value={data.confidence.toFixed(2)} />
            <Stat label="cost" value={usd(data.cost_usd)} />
          </div>

          <Section title="Recommendation">
            <p className="mono-block">{data.recommendation}</p>
          </Section>

          <Section title="Proposals">
            {Object.entries(data.proposals).map(([key, p]) => (
              <div key={key} style={{ marginBottom: 14 }}>
                <div style={{ color: "var(--info)" }}>
                  {key} <span style={{ color: "var(--muted)" }}>· self-confidence {p.confidence.toFixed(2)}</span>
                </div>
                <div style={{ color: "var(--text)" }}>{p.recommendation}</div>
                <div className="hint">against itself: {p.counterargument}</div>
              </div>
            ))}
          </Section>

          {data.flaws.length ? (
            <Section title="Critic">
              {data.flaws.map((f, i) => (
                <div key={i}>
                  <Tag tone={f.fatal ? "fail" : "warn"}>{f.fatal ? "fatal" : "flaw"}</Tag>{" "}
                  <span style={{ color: "var(--muted)" }}>[{f.proposal_key}]</span> {f.issue}
                </div>
              ))}
            </Section>
          ) : null}

          {data.notes.length ? (
            <Section title="Notes">
              {data.notes.map((n, i) => (
                <div key={i} className="hint">
                  · {n}
                </div>
              ))}
            </Section>
          ) : null}
        </>
      ) : null}
    </>
  );
}

/* ===================== P10 — self-reflective ===================== */

interface Attempt {
  iteration: number;
  score: number;
  per_dimension: Record<string, number>;
  critique: string;
  fixes: string[];
  cost_usd: number;
}

interface ReflectResult {
  attempts: Attempt[];
  best: Attempt | null;
  best_text: string;
  trajectory: number[];
  regressed: boolean;
  stop: string;
  improvement: number;
  cost_usd: number;
  cost_per_point: number | null;
}

export function P10() {
  const [maxIter, setMaxIter] = useState(3);
  const { data, error, running, run } = useRun<ReflectResult>();

  const reflect = () => run(() => post("/api/p10/reflect", { max_iterations: maxIter }));

  const verdict = !data
    ? null
    : data.regressed
      ? {
          label: "Kept the best",
          tone: "held" as Tone,
          note: "a later rewrite scored worse — the best attempt was returned, not the last",
        }
      : data.stop === "target_reached"
        ? { label: "Target reached", tone: "ok" as Tone, note: `improved ${data.improvement.toFixed(2)} points` }
        : data.stop === "no_improvement"
          ? { label: "Plateaued", tone: "warn" as Tone, note: "the rewrite stopped helping, so the loop stopped" }
          : { label: data.stop, tone: "warn" as Tone, note: "" };

  const dims = data?.attempts[0] ? Object.keys(data.attempts[0].per_dimension) : [];

  return (
    <>
      <Section title="Draft a reply to an angry customer">
        <div className="controls">
          <button className="btn btn-primary" onClick={reflect} disabled={running}>
            Generate, judge, rewrite
          </button>
          <span className="field-label">max iterations</span>
          <select className="select" value={maxIter} onChange={(e) => setMaxIter(Number(e.target.value))}>
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>
        <p className="hint prose">
          Regeneration often makes output worse — the model over-corrects the flaw it was
          shown and breaks something that was already fine. Every attempt is scored and the
          highest scorer is returned, so an extra iteration can never leave you worse off.
        </p>
      </Section>

      <RunStatus running={running} error={error} />
      {verdict ? <Verdict {...verdict} /> : null}

      {data?.attempts.length ? (
        <>
          <Section title="Score trajectory" tight>
            <table>
              <thead>
                <tr>
                  <th className="num">iter</th>
                  <th className="num">score</th>
                  <th style={{ width: "28%" }} />
                  {dims.map((d) => (
                    <th key={d} className="num">
                      {d.slice(0, 6)}
                    </th>
                  ))}
                  <th>critique</th>
                </tr>
              </thead>
              <tbody>
                {data.attempts.map((a) => (
                  <tr key={a.iteration}>
                    <td className="num strong">
                      {a.iteration}
                      {a.iteration === data.best?.iteration ? " ★" : ""}
                    </td>
                    <td className="num">{a.score.toFixed(2)}</td>
                    <td>
                      <Bar
                        value={a.score}
                        max={5}
                        tone={a.iteration === data.best?.iteration ? "ok" : undefined}
                      />
                    </td>
                    {dims.map((d) => (
                      <td key={d} className="num">
                        {a.per_dimension[d]}
                      </td>
                    ))}
                    <td style={{ fontSize: 11.5 }}>{a.critique}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>

          <div className="stat-row" style={{ marginBottom: 16 }}>
            <Stat label="first → best" value={`${data.trajectory[0].toFixed(2)} → ${Math.max(...data.trajectory).toFixed(2)}`} />
            <Stat label="improvement" value={`+${data.improvement.toFixed(2)}`} tone="ok" />
            <Stat label="cost" value={usd(data.cost_usd)} />
            <Stat label="per point" value={data.cost_per_point ? usd(data.cost_per_point) : "—"} />
          </div>

          <Section title="Returned draft">
            <p className="mono-block">{data.best_text}</p>
          </Section>
        </>
      ) : null}
    </>
  );
}
