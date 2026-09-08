# P1 — Structured Output Agent

**The failure this survives:** the model returns something you can't parse, or
can parse but shouldn't trust.

Status: working end to end. 24 tests, all offline.

## Setup

```bash
# from the repo root. Creates the `lyzer` conda env and installs the shared core.
make install

# or standalone
pip install -e ../core -e '..[dev]'
```

You don't need an API key. `LLM_PROVIDER=mock` is the default and replays
recorded responses.

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

To run against a real model, set `ANTHROPIC_API_KEY` and leave `LLM_PROVIDER`
empty.

## What it does

Pulls a `TicketTriage` object out of free-text support tickets. Enums, a bounded
float, a non-negative integer, a nested list of action items, an optional refund
field, and one business rule that no JSON schema can express: `summary` has to
state the problem instead of narrating the ticket.

## Four things that make it production rather than demo

**Repair turns instead of blind retries.** A bare retry just re-rolls the same
dice. When validation fails, the agent appends the specific error and asks for a
correction:

```
That response failed validation against TicketTriage:
customer_sentiment: Input should be less than or equal to 1
Return corrected JSON only. Fix the named fields; change nothing else.
```

There's a test for every constraint in the schema, including the business rule,
asserting the repair prompt actually names the field that broke.

After a failure the agent can also escalate, moving up the ladder from Haiku 4.5
to Sonnet 5 to Opus 5 rather than burning three attempts on a model that's
already shown it can't do the task.

Failures get persisted along with the output that caused them. A validation
failure you can't reproduce from the log isn't actionable, and you can't answer
"what's our failure rate this week" from stderr. `cli failures` and `GET
/api/p01/failures` read the same store, and P11 charts it.

Running out of attempts is an explicit typed failure. You get `ok=False,
value=None` and never a half-populated object, because a half-populated object
looks like an answer.

## Tool contracts: two different bugs wearing the same costume

`ValidatedTool` checks both sides of a tool call, because the two failures need
opposite handling.

| | What happened | Handling |
|---|---|---|
| **Bad input** | The model emitted arguments that don't fit the schema | Recoverable. Goes back as an error `tool_result` and the model fixes it next turn. |
| **Bad output** | Your own function returned something violating its declared contract | Not the model's fault and not its to fix. Logged loudly, raised as `ToolContractError`, never shown to the model. |

If you pass a contract violation into the conversation, you teach the model to
work around your bug, and the symptom surfaces three layers downstream as an
answer nobody can explain. `cli tools --broken` shows the caught case.

Tools are declared with `strict: true` so the API enforces argument schemas
server-side. I kept the Pydantic check as a second line anyway, since strict mode
can't express cross-field rules and does nothing in mock mode or behind a proxy.

## API

Mounted at `/api/p01` by `server/main.py`:

- `POST /extract` takes `{text, max_attempts, escalate}` and returns the triage
  object plus `attempts`, `repaired`, `failures` and `cost_usd`
- `GET /failures` returns aggregate stats and the recent failure log
- `POST /tools/demo?broken=true` shows both validation paths side by side

## Where the code lives

| File | |
|---|---|
| `schemas.py` | `TicketTriage` and the tool input/output contracts |
| `agent.py` | `StructuredAgent`: escalation, failure persistence, explicit failure |
| `tools.py` | `ValidatedTool` and the two-sided contract |
| `smoke.py` | the end-to-end check behind the root README's table |

The repair loop itself lives in `agentcore.llm.parse` in the shared core, since
ten other projects need it. This project is the productionised surface around it.
The root README explains why the core is shared.
