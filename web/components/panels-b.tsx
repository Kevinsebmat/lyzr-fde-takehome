"use client";

import { useCallback, useEffect, useState } from "react";
import { get, post } from "@/lib/api";
import {
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

/* ===================== P05 — memory ===================== */

interface Fact {
  id: string;
  kind: string;
  text: string;
  superseded_by: string | null;
  uses: number;
}

interface ChatResult {
  reply: string;
  recalled: { kind: string; text: string; similarity: number; recency: number; score: number }[];
  learned: { id: string; kind: string; text: string }[];
  superseded: string[];
  compressed: boolean;
  cost_usd: number;
}

export function P05() {
  const [user, setUser] = useState("demo-wexler");
  const [message, setMessage] = useState("I'm at Wexler Industries. All our data has to stay in the EU region.");
  const [facts, setFacts] = useState<{ facts: Fact[]; stats: Record<string, unknown> } | null>(null);
  const { data, error, running, run } = useRun<ChatResult>();

  const loadFacts = useCallback(async () => {
    try {
      setFacts(await get(`/api/p05/facts/${encodeURIComponent(user)}?include_superseded=true`));
    } catch {
      /* the chat call surfaces connection problems; don't double-report them */
    }
  }, [user]);

  useEffect(() => {
    void loadFacts();
  }, [loadFacts]);

  const send = async (text?: string) => {
    const msg = text ?? message;
    if (text) setMessage(text);
    await run(() => post("/api/p05/chat", { user_id: user, message: msg }));
    await loadFacts();
  };

  const endSession = async () => {
    await post(`/api/p05/end-session/${encodeURIComponent(user)}`);
    await loadFacts();
  };

  const verdict = !data
    ? null
    : data.superseded.length
      ? {
          label: "Fact superseded",
          tone: "held" as Tone,
          note: "the old fact stays for audit but will never reach a prompt again",
        }
      : data.learned.length
        ? { label: "Learned", tone: "ok" as Tone, note: `${data.learned.length} durable fact(s) stored` }
        : { label: "Nothing durable", tone: "info" as Tone, note: "most messages contain nothing worth remembering" };

  return (
    <>
      <Section title="Conversation">
        <div className="field-row">
          <span className="field-label">user</span>
          <input className="input" style={{ minWidth: 160 }} value={user} onChange={(e) => setUser(e.target.value)} />
          <button className="btn" onClick={endSession} disabled={running}>
            End session
          </button>
        </div>
        <div className="field-row">
          <input
            className="input"
            style={{ flex: 1 }}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
            aria-label="Message"
          />
          <button className="btn btn-primary" onClick={() => send()} disabled={running}>
            Send
          </button>
        </div>
        <div className="controls">
          {[
            "Thanks, that's helpful!",
            "Remind me where our data is hosted?",
            "We've migrated. Everything is in the US region now.",
          ].map((m) => (
            <button key={m} className="btn" onClick={() => send(m)} disabled={running}>
              {m.length > 40 ? `${m.slice(0, 40)}…` : m}
            </button>
          ))}
        </div>
        <p className="hint prose">
          Say the EU line, end the session, then ask where the data is hosted — the transcript
          is gone and the facts are not. Then say you have migrated to the US and watch the old
          fact stop being retrievable.
        </p>
      </Section>

      <RunStatus running={running} error={error} />
      {verdict ? <Verdict {...verdict} /> : null}

      {data ? (
        <Section title="Reply">
          <p className="mono-block">{data.reply}</p>
          {data.recalled.length ? (
            <table style={{ marginTop: 12 }}>
              <thead>
                <tr>
                  <th>recalled</th>
                  <th className="num">similarity</th>
                  <th className="num">recency</th>
                  <th className="num">score</th>
                </tr>
              </thead>
              <tbody>
                {data.recalled.map((r, i) => (
                  <tr key={i}>
                    <td className="strong">
                      <Tag>{r.kind}</Tag> {r.text}
                    </td>
                    <td className="num">{r.similarity.toFixed(2)}</td>
                    <td className="num">{r.recency.toFixed(2)}</td>
                    <td className="num">{r.score.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </Section>
      ) : null}

      <Section title="Long-term memory" tight>
        {facts?.facts.length ? (
          <table>
            <thead>
              <tr>
                <th>kind</th>
                <th>fact</th>
                <th className="num">uses</th>
                <th>state</th>
              </tr>
            </thead>
            <tbody>
              {facts.facts.map((f) => (
                <tr key={f.id}>
                  <td>{f.kind}</td>
                  <td className={f.superseded_by ? "" : "strong"}>{f.text}</td>
                  <td className="num">{f.uses}</td>
                  <td>
                    {f.superseded_by ? <Tag>superseded</Tag> : <Tag tone="ok">active</Tag>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>No facts stored for this user yet.</Empty>
        )}
      </Section>
    </>
  );
}

/* ===================== P06 — human in the loop ===================== */

interface ApprovalRequest {
  id: string;
  action: string;
  arguments: Record<string, unknown>;
  risk: string;
  status: string;
  rendered: string;
  decided_by: string | null;
  decision_note: string;
}

interface HandleResult {
  outcome: string;
  result: string;
  escalation_reasons: string[];
  request: ApprovalRequest | null;
}

export function P06() {
  const [caseText, setCaseText] = useState(
    "Refund $48,200 to Wexler Industries for the two-hour outage on ORD-2.",
  );
  const [amount, setAmount] = useState("9640");
  const [queue, setQueue] = useState<{ requests: ApprovalRequest[]; stats: Record<string, unknown> } | null>(null);
  const [trail, setTrail] = useState<{ trail: { event: string; actor: string; detail: Record<string, unknown> }[] } | null>(null);
  const { data, error, running, run } = useRun<HandleResult>();

  const refresh = useCallback(async () => {
    try {
      setQueue(await get("/api/p06/queue?all_statuses=true"));
      setTrail(await get("/api/p06/audit"));
    } catch {
      /* surfaced by the handle call */
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handle = async () => {
    await run(() => post("/api/p06/handle", { case: caseText }));
    await refresh();
  };

  const decide = async (id: string, approved: boolean) => {
    await post(`/api/p06/requests/${id}/decide`, {
      approved,
      actor: "finance-lead",
      note: approved ? "SLA calculation checked" : "no written confirmation",
      set_amount: approved && amount ? Number(amount) : null,
    });
    await refresh();
  };

  const verdict = !data
    ? null
    : data.outcome === "awaiting_approval"
      ? { label: "Paused for a human", tone: "held" as Tone, note: "the pause is a database row — it survives a restart" }
      : data.outcome === "executed"
        ? { label: "Executed", tone: "ok" as Tone, note: data.result }
        : { label: data.outcome, tone: "fail" as Tone, note: data.result };

  const pending = queue?.requests.filter((r) => r.status === "pending") ?? [];

  return (
    <>
      <Section title="Case">
        <div className="field-row">
          <input className="input" style={{ flex: 1 }} value={caseText} onChange={(e) => setCaseText(e.target.value)} />
          <button className="btn btn-primary" onClick={handle} disabled={running}>
            Handle
          </button>
        </div>
        <div className="controls">
          {[
            "Refund $120 for a duplicate charge on ORD-1.",
            "Delete account ACC-9 as the customer requested over chat.",
          ].map((c) => (
            <button key={c} className="btn" onClick={() => setCaseText(c)} disabled={running}>
              {c.length > 42 ? `${c.slice(0, 42)}…` : c}
            </button>
          ))}
        </div>
        <p className="hint prose">
          The $120 refund executes without asking. The other two escalate — one on the amount,
          one because deleting an account is irreversible. Policy fires regardless of how
          confident the agent is.
        </p>
      </Section>

      <RunStatus running={running} error={error} />
      {verdict ? <Verdict {...verdict} /> : null}

      {data?.escalation_reasons.length ? (
        <Section title="Escalated because">
          {data.escalation_reasons.map((r, i) => (
            <div key={i}>· {r}</div>
          ))}
        </Section>
      ) : null}

      {pending.length ? (
        <Section title={`Approval queue (${pending.length})`}>
          {pending.map((r) => (
            <div key={r.id} style={{ marginBottom: 16 }}>
              <pre className="mono-block">{r.rendered}</pre>
              <div className="controls" style={{ marginTop: 8 }}>
                <button className="btn btn-primary" onClick={() => decide(r.id, true)}>
                  Approve as finance-lead
                </button>
                <span className="field-label">correct the amount to</span>
                <input
                  className="input"
                  style={{ minWidth: 110 }}
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                />
                <button className="btn" onClick={() => decide(r.id, false)}>
                  Reject
                </button>
              </div>
              <p className="hint prose">
                The approver&rsquo;s correction is merged over the agent&rsquo;s arguments and
                wins. They are the authority the pause existed to consult.
              </p>
            </div>
          ))}
        </Section>
      ) : null}

      {queue?.requests.length ? (
        <Section title="All requests" tight>
          <table>
            <thead>
              <tr>
                <th>action</th>
                <th>risk</th>
                <th>status</th>
                <th>decided by</th>
              </tr>
            </thead>
            <tbody>
              {queue.requests.map((r) => (
                <tr key={r.id}>
                  <td className="strong">{r.action}</td>
                  <td>
                    <Tag tone={r.risk === "high" ? "fail" : r.risk === "medium" ? "warn" : "ok"}>
                      {r.risk}
                    </Tag>
                  </td>
                  <td>{r.status}</td>
                  <td>{r.decided_by ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      ) : null}

      {trail?.trail.length ? (
        <Section title="Audit trail (append only)" tight>
          <table>
            <thead>
              <tr>
                <th>event</th>
                <th>actor</th>
                <th>detail</th>
              </tr>
            </thead>
            <tbody>
              {trail.trail.slice(-12).map((e, i) => (
                <tr key={i}>
                  <td className="strong">{e.event}</td>
                  <td>{e.actor}</td>
                  <td style={{ fontSize: 11 }}>{JSON.stringify(e.detail).slice(0, 120)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      ) : null}
    </>
  );
}

/* ===================== P07 — cost router ===================== */

interface RouteResult {
  answer: string;
  classification: { complexity: string; reasons: string[]; method: string };
  attempts: { model: string; confidence: number; escalated_because: string | null; cost_usd: number }[];
  final_model: string;
  escalated: boolean;
  early_exit: boolean;
  budget_exceeded: boolean;
  cost_usd: number;
}

interface Economics {
  prices: { model: string; tier: string; input_per_mtok: number; output_per_mtok: number; cost_per_task: number; break_even_vs_opus: number }[];
  break_even: { required_success_rate: number; cheap_cost_per_task: number; strong_cost_per_task: number };
  projections: { success_rate: number; cascade_usd: number; baseline_usd: number; saved_usd: number; saved_pct: number; worth_it: boolean }[];
  classifier_overhead: { success_rate: number; classifier_cost_usd: number; saving_per_request_usd: number; share_of_saving_consumed: number; affordable: boolean }[];
  measured: Record<string, unknown>;
}

const P07_TASKS = [
  "Extract the invoice number from: INV-8842, $1,204.00",
  "Compare running our ingestion as nightly batch against streaming. Analyse the trade-offs for cost, operational risk and time to detect a bad record, and recommend which we should fund next quarter. Why would the other option be defensible?",
];

export function P07() {
  const [task, setTask] = useState(P07_TASKS[0]);
  const [econ, setEcon] = useState<Economics | null>(null);
  const { data, error, running, run } = useRun<RouteResult>();

  useEffect(() => {
    void (async () => {
      try {
        setEcon(await get("/api/p07/economics?tasks=50000"));
      } catch {
        /* the route call surfaces connection problems */
      }
    })();
  }, [data]);

  const route = (t?: string) => {
    const chosen = t ?? task;
    if (t) setTask(t);
    return run(() => post("/api/p07/route", { task: chosen }));
  };

  const verdict = !data
    ? null
    : data.budget_exceeded
      ? { label: "Budget stopped it", tone: "fail" as Tone, note: "checked before the call, not after" }
      : data.escalated
        ? {
            label: "Escalated",
            tone: "warn" as Tone,
            note: data.attempts[0]?.escalated_because ?? "",
          }
        : {
            label: "Early exit",
            tone: "ok" as Tone,
            note: `${data.final_model} was enough — no escalation, no second call`,
          };

  return (
    <>
      <Section title="Route a task">
        <textarea className="textarea" value={task} onChange={(e) => setTask(e.target.value)} />
        <div className="controls" style={{ marginTop: 8 }}>
          <button className="btn btn-primary" onClick={() => route()} disabled={running}>
            Route
          </button>
          <button className="btn" onClick={() => route(P07_TASKS[0])} disabled={running}>
            A simple task
          </button>
          <button className="btn" onClick={() => route(P07_TASKS[1])} disabled={running}>
            A hard one
          </button>
        </div>
      </Section>

      <RunStatus running={running} error={error} />
      {verdict ? <Verdict {...verdict} /> : null}

      {data ? (
        <>
          <Section title="Decision">
            <KeyValues
              rows={[
                ["complexity", <Tag key="c" tone={data.classification.complexity === "hard" ? "fail" : "ok"}>{data.classification.complexity}</Tag>],
                ["classified by", `${data.classification.method} — ${data.classification.reasons.join("; ")}`],
                ["final model", data.final_model],
                ["cost", usd(data.cost_usd)],
              ]}
            />
            <table style={{ marginTop: 12 }}>
              <thead>
                <tr>
                  <th>attempt</th>
                  <th className="num">confidence</th>
                  <th className="num">cost</th>
                  <th>outcome</th>
                </tr>
              </thead>
              <tbody>
                {data.attempts.map((a, i) => (
                  <tr key={i}>
                    <td className="strong">{a.model}</td>
                    <td className="num">{a.confidence.toFixed(2)}</td>
                    <td className="num">{usd(a.cost_usd)}</td>
                    <td>
                      {a.escalated_because ? (
                        <span style={{ color: "var(--warn)" }}>{a.escalated_because}</span>
                      ) : (
                        <Tag tone="ok">accepted</Tag>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
          <Section title="Answer">
            <p className="mono-block">{data.answer}</p>
          </Section>
        </>
      ) : null}

      {econ ? (
        <>
          <Section title="Does the cascade pay?">
            <p className="prose" style={{ marginTop: 0 }}>
              When the cheap model fails you pay for <em>both</em> calls, so a cascade only
              saves money above a break-even success rate — and that rate is just the price
              ratio. Haiku is a fifth of Opus, so it must handle{" "}
              <strong style={{ color: "var(--text)" }}>
                {pct(econ.break_even.required_success_rate)}
              </strong>{" "}
              of traffic unaided.
            </p>
            <table style={{ marginTop: 12 }}>
              <thead>
                <tr>
                  <th className="num">cheap handles</th>
                  <th className="num">cascade</th>
                  <th className="num">always-Opus</th>
                  <th className="num">saved</th>
                  <th>verdict</th>
                </tr>
              </thead>
              <tbody>
                {econ.projections.map((p) => (
                  <tr key={p.success_rate}>
                    <td className="num strong">{pct(p.success_rate)}</td>
                    <td className="num">${p.cascade_usd.toLocaleString()}</td>
                    <td className="num">${p.baseline_usd.toLocaleString()}</td>
                    <td className="num" style={{ color: p.worth_it ? "var(--ok)" : "var(--fail)" }}>
                      ${p.saved_usd.toLocaleString()} ({p.saved_pct.toFixed(0)}%)
                    </td>
                    <td>
                      {p.worth_it ? <Tag tone="ok">pays</Tag> : <Tag tone="fail">costs more</Tag>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="hint">50,000 tasks/month, Haiku 4.5 against Opus 5.</p>
          </Section>

          <Section title="Why the classifier is heuristic" tight>
            <table>
              <thead>
                <tr>
                  <th className="num">cheap handles</th>
                  <th className="num">saving / request</th>
                  <th className="num">classifier eats</th>
                  <th>verdict</th>
                </tr>
              </thead>
              <tbody>
                {econ.classifier_overhead.map((c) => (
                  <tr key={c.success_rate}>
                    <td className="num strong">{pct(c.success_rate)}</td>
                    <td className="num">{usd(c.saving_per_request_usd)}</td>
                    <td className="num" style={{ color: c.affordable ? "var(--text-dim)" : "var(--fail)" }}>
                      {pct(c.share_of_saving_consumed)}
                    </td>
                    <td>
                      {c.affordable ? <Tag>affordable</Tag> : <Tag tone="fail">eats the saving</Tag>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="section-body">
              <p className="prose" style={{ margin: 0 }}>
                The classifier&rsquo;s cost is fixed while the saving shrinks, so the overhead
                bites hardest exactly when routing is already marginal — which is when you can
                least absorb it.
              </p>
            </div>
          </Section>
        </>
      ) : null}
    </>
  );
}
