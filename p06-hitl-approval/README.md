# P6 — Human-in-the-Loop Approval Agent

**Failure mode this project exists to survive:** an agent that takes an
irreversible action nobody authorised — and the subtler one, an approval gate
that loses every pending decision on the next deploy.

Status: **working end-to-end.** 29 tests, all offline.

## Setup & run

The approve step is a **separate command in a separate process**. That's the
demonstration, not a convenience:

```bash
make install    # from the repo root

python -m p06_hitl_approval.cli handle "Refund \$48,200 to Wexler Industries for the two-hour outage on ORD-2."
# → AWAITING APPROVAL — high risk, request id req_xxxx

python -m p06_hitl_approval.cli approve req_xxxx --actor finance-lead \
    --note "SLA calc gives 9640, not 48200" --set-amount 9640
# → executed issue_refund with {'amount': 9640.0, ...}

python -m p06_hitl_approval.cli queue --all
python -m p06_hitl_approval.cli trail req_xxxx
python ../scripts/smoke.py p06 && python -m pytest tests -q
```

## The pause is a row, not an `await`

An agent that blocks a coroutine waiting for a human works perfectly in a demo
and loses every pending decision the first time the process restarts — and a
deploy during business hours is not an edge case.

Here the paused state is a **database row**. The agent stops; the row survives;
any process picks it up. `resume()` depends on nothing from the process that
created the request. There's a test that closes every database connection to
simulate a restart, and the flow above spans three separate OS processes.

Resume is **idempotent** — a replayed resume must not issue the refund twice.

## Two independent uncertainty signals

Either alone fails in a way the other catches:

**Deterministic policy** — rules in code: over $500, anything irreversible,
anything touching a cancelled order. These fire regardless of what the model
thinks. *A policy the model can talk itself out of is a suggestion, not a
policy*, and there's a test asserting that confidence `1.0` cannot override the
$10,000 threshold. Every regulated control belongs here.

**Model-assessed uncertainty** — its own confidence, and whether it wants a
second pair of eyes. This catches what the rules didn't anticipate: the novel
case, the malformed request, the one where the numbers don't add up.

Policy wins ties. An amount that *can't be parsed* escalates rather than being
treated as absent — an amount we can't read is a reason to ask, not to proceed.

## The approver's authority is real

They see the arguments, the agent's rationale, its confidence, the specific
reasons it escalated, and the original request. Deciding from a bare action name
is rubber-stamping, and an audit record of a rubber stamp documents a decision
nobody actually made.

The rendered text is **frozen at creation**, so a later template change can't
retroactively alter what was consented to.

`--set-amount` shows the deeper point: corrections supplied by the approver are
merged over the agent's arguments and **win**. They are the authority the pause
existed to consult, so the agent's number is a proposal, not a fact.

## Audit trail

Append-only. An approval record you can edit is not evidence, and "who approved
the $48,000 refund, when, and what were they shown" is exactly what a compliance
review asks.

- A settled request **cannot be re-decided** — the original stands, and the
  attempt is recorded as `decision_rejected`, so a quiet overturn leaves a trace.
- Entries are keyed by nanosecond + random suffix; losing one to a key collision
  would be a silent hole in the evidence, and there's a test for it.
- `executed` is a distinct recorded state. An approved-but-never-executed
  request is a silent failure, so it is never assumed.

## Two more production details

**Expiry.** Requests go stale after 24h. Approving a two-week-old refund against
today's account state is not the decision the approver thinks they're making.

**Deduplication.** A retried agent run must not queue the same $48,000 refund
twice — an approver seeing duplicates either approves both or trusts the queue
less, and both are bad. Requests are fingerprinted on action + arguments.

**Failing closed.** If the agent can't form a well-defined action, it queues
nothing and does nothing.

## Where the code lives

| File | |
|---|---|
| `approvals.py` | the durable queue, expiry, dedup, append-only audit |
| `agent.py` | policy rules, uncertainty detection, propose/resume |
