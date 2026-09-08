# P1 — Structured Output Agent

**Failure mode this project exists to survive:** the model returns something you
cannot parse, or can parse but must not trust.

Status: **working end-to-end.** 24 tests, all offline.

## Setup

```bash
# from the repo root — creates the `lyzer` conda env and installs the shared core
make install

# or standalone
pip install -e ../core -e '..[dev]'
```

No API key needed: `LLM_PROVIDER=mock` (the default) replays recorded responses.

## Run

```bash
python -m p01_structured_output.cli extract --file sample_ticket.txt
python -m p01_structured_output.cli extract "checkout is down, 12k users hit, want a refund"
python -m p01_structured_output.cli tools            # tool input validation
python -m p01_structured_output.cli tools --broken   # tool output contract violation
python -m p01_structured_output.cli failures         # persisted validation-failure log

python ../scripts/smoke.py p01                       # end-to-end check
python -m pytest tests -q
```

Against a real model: set `ANTHROPIC_API_KEY` and `LLM_PROVIDER=` (empty).

## What it does

Extracts `TicketTriage` from free-text support tickets — enums, a bounded float,
a non-negative integer, a nested list of action items, an optional refund field,
and one business rule (`summary` must state the problem, not narrate the ticket)
that no JSON schema can express.

## The four things that make it production rather than demo

**1. Repair turns, not blind retries.** A bare retry re-rolls the same dice. On
a validation failure the agent appends the *specific* error and asks for a
correction:

```
That response failed validation against TicketTriage:
customer_sentiment: Input should be less than or equal to 1
Return corrected JSON only. Fix the named fields; change nothing else.
```

Tests assert the repair prompt names the field that broke — for every constraint
in the schema, including the business rule.

**2. Escalation instead of identical attempts.** After a failure the agent can
move up the model ladder (Haiku 4.5 → Sonnet 5 → Opus 5) rather than spending
three attempts on a model that has already shown it can't do the task.

**3. Failures are persisted with the offending output.** A validation failure you
can't reproduce from the log isn't actionable, and "what's our failure rate this
week" can't be answered from stderr. `cli failures` and `GET /api/p01/failures`
read the same store; P11 charts it.

**4. Exhaustion is an explicit typed failure.** When the repair loop runs out,
the result is `ok=False, value=None` — never a half-populated object, because a
half-populated object looks like an answer.

## Tool contracts: two different bugs wearing the same costume

`ValidatedTool` validates **both** sides of a tool call, because the two failures
need opposite handling:

| | What happened | Handling |
|---|---|---|
| **Bad input** | The model emitted arguments that don't fit the schema | Recoverable. Returned as an error `tool_result`; the model fixes it next turn. |
| **Bad output** | *Your function* returned something violating its own declared contract | Not the model's fault and not the model's to fix. Logged loudly, raised as `ToolContractError`, never shown to the model. |

Passing a contract violation into the conversation teaches the model to work
around your bug — and the symptom then surfaces three layers downstream as an
inexplicable answer. `cli tools --broken` demonstrates the caught case.

Tools are also declared with `strict: true`, so the API enforces argument schemas
server-side. The Pydantic check stays as a second line: strict mode can't express
cross-field rules, and does nothing in mock mode or behind a proxy.

## API

Mounted at `/api/p01` by `server/main.py`:

- `POST /extract` — `{text, max_attempts, escalate}` → the triage object plus
  `attempts`, `repaired`, `failures`, `cost_usd`
- `GET /failures` — aggregate stats + the recent failure log
- `POST /tools/demo?broken=true` — both validation paths side by side

## Where the code lives

| File | |
|---|---|
| `schemas.py` | `TicketTriage` and the tool input/output contracts |
| `agent.py` | `StructuredAgent` — escalation, failure persistence, explicit failure |
| `tools.py` | `ValidatedTool` — the two-sided contract |
| `smoke.py` | the end-to-end check behind the root README's triage table |

The repair loop itself is `agentcore.llm.parse` in the shared core, because ten
other projects need it. This project is the productionised surface around it —
see the root README for why the core is shared.
