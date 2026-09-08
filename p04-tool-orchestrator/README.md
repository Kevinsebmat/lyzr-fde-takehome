# P4 — Multi-Tool Orchestrator Agent

**Failure modes this project exists to survive:** an agent calling a tool it
isn't allowed to, one slow tool sinking a whole batch of results, and two
sources disagreeing with no rule for which wins.

Status: **working end-to-end.** 32 tests, all offline.

## Setup & run

```bash
make install    # from the repo root
python -m p04_tool_orchestrator.cli tools --caller analyst
python -m p04_tool_orchestrator.cli gather ACC-1001 --caller analyst
python -m p04_tool_orchestrator.cli gather ACC-1001 --caller readonly   # denials
python -m p04_tool_orchestrator.cli route billing balance --caller analyst
python ../scripts/smoke.py p04 && python -m pytest tests -q
```

## Permission is enforced at invocation, not by filtering the menu

Hiding a tool from the list shown to the model is **not access control.** The
model can name a tool it was never shown, prompt injection can tell it to, and a
retried plan from a different scope can reference one.

The list is a *hint*; the check inside `invoke()` is the control. There's a test
that confirms a tool absent from the analyst's menu still raises
`PermissionDenied` when called directly — that's the call a filtered list would
have silently allowed.

Denials are **recorded, not silently dropped**. An over-broad plan that gets
quietly filtered looks identical to a correct one, and you never find out.

Scopes support `billing:read`, `billing:*` and `*`.

## Capabilities, not names, route the work

Asking for "the tool called `get_invoice_v2`" couples the planner to today's
names. Asking for "something that can read a billing balance" survives the tool
being renamed, replaced, or gaining a second implementation — which is the point
of a dynamic registry. There's a test that swaps `billing_service` for
`billing_service_v2` and the route still resolves.

Ties break on `authority`, then latency.

## The timeout bug worth knowing about

A timeout in Python **cannot cancel a blocking call.** `asyncio.wait_for` around
`asyncio.to_thread` returns on schedule, but the worker thread runs the call to
completion regardless. And because `to_thread` uses the loop's *default*
executor, which `asyncio.run` joins on the way out, a 200ms timeout on a
10-second tool produces a batch that returns in 200ms and then **blocks for
another 9.8 seconds at shutdown.**

That was measured here, not theorised — the first version of this batch took
10,015ms with a 200ms timeout set. The fixes:

- a **private executor that is never joined**, so the timeout actually bounds
  wall-clock time (205ms now, tested);
- a **bounded pool**, so abandoned threads can't starve the rest of the process;
- an **`abandoned` counter**, because a rising count is the signal a downstream
  dependency is sick, and it's invisible if you only watch timeouts.

The real fix is tools that are async or cooperatively cancellable. Until they
are, this bounds the damage and makes it measurable. The error text says
`worker abandoned, not cancelled` rather than pretending otherwise.

## Failure isolation

Every failure mode — raised, timed out, denied, unknown tool — becomes a
recorded `ToolResult`, never an exception that kills the batch. The
orchestrator's job is to come back with as much as it could get plus an honest
account of the rest. Concurrency is capped so a wide plan can't exhaust
downstream connection pools.

## Conflict resolution is deterministic, not delegated

When the billing service says $48,200 and the cache says $12,000, asking the
model to pick makes the answer depend on prompt phrasing and vary between runs.
The registry declares an authority ordering — system of record beats cache beats
third party — and the code applies it. Same inputs, same answer, whatever order
the results arrive in.

**Equal authority disagreeing is escalated, not resolved.** Two sources of the
same rank giving different numbers is a data integrity problem; quietly picking
one hides it behind a confident answer. The merged field is set to `None` and
`needs_escalation` is raised.

Agreement across tools is not a conflict, however many tools report it.

## Where the code lives

| File | |
|---|---|
| `registry.py` | tools, capabilities, scopes, the enforcement point |
| `orchestrator.py` | parallel batches, timeouts, the merge and conflict rules |
| `tools.py` | the demo toolset, including two that disagree on purpose |
