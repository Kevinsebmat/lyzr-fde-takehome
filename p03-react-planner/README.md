# P3 — ReAct Planning Agent

**The failure this survives:** the agent that never stops.

Status: working end to end. 20 tests, all offline.

## Setup & run

```bash
make install    # from the repo root
python -m p03_react_planner.cli run "Does order ORD-4417 need manager approval for a refund?"
python -m p03_react_planner.cli run "..." --max-iterations 3 --no-reflect
python -m p03_react_planner.cli tools
python ../scripts/smoke.py p03
python -m pytest tests -q
```

## One termination condition isn't enough

That's the whole lesson of this project. Each of these leaves a hole the others
have to cover:

| Condition | Catches | Misses |
|---|---|---|
| `MAX_ITERATIONS` | everything, eventually | lets the agent burn its whole budget repeating one dead call first |
| `LOOP_DETECTED` | the same action and input repeated | an agent wandering through slightly *different* useless actions |
| `NO_PROGRESS` | wandering, via self-critique | a critique step that's wrong itself |
| `BUDGET_EXHAUSTED` | spend, whatever shape the failure takes | nothing, but it's the most expensive way to find out |

So all four run together and any one of them ends the run. The iteration cap is
the backstop, not the plan.

Loop detection normalises case and whitespace. Without that, `ORD-4417`,
`ord-4417 ` and `Ord-4417` read as three different actions and the loop escapes.
`max_repeats=2` allows one legitimate retry, since three is a pattern.

## Every exit still answers

An early exit calls `_salvage()`, which asks for the best answer the observations
already gathered will support, then labels it:

```
Order ORD-4417 totals $12,480 and is shipped. I could not retrieve the refund
policy, so I cannot confirm the approval threshold.

[Partial: repeated `search_archive` with the same input 3 times.]
```

Throwing away six tool calls' worth of work because the seventh didn't happen is
worse than a partial answer that admits it's partial. `run()` never raises on
termination, and that's tested against every stopping condition.

## Self-critique

After each observation, a second and cheaper call judges one thing: did that step
move the task forward? An error, an empty result or a repeat doesn't count. Two
unproductive steps in a row ends the run.

The critique is a safety net, so a failure inside it doesn't abort a working run.
If it errors or fails validation, the loop carries on under the hard caps. A
safety net that can take down the thing it protects isn't one.

## Tools that misbehave on purpose

`search_archive` always returns nothing and `legacy_crm` always raises. An agent
that only ever sees successful tool calls never has to prove it can terminate, so
the registry ships both.

Tool failures come back as observations rather than exceptions. The agent reads
`ERROR: ConnectionError: upstream service unavailable`, reflects and tries
something else. An unknown tool name returns the list of real ones, since a
hallucinated name is recoverable if you tell the agent what actually exists.

`calculator` uses `eval`, so it enforces a character allowlist first. Passing
model-generated text to `eval` is a remote-code-execution hole, and the allowlist
is the control that closes it. There's a test that tries to break out of it.

## A note on frameworks

My plan for this repo said LangGraph for P3. I hand-rolled it instead. What
matters here is precise, testable termination, and owning the loop makes every
stopping condition an explicit branch with a test against it rather than a
recursion limit inherited from a framework. LangGraph would be the right call for
a graph with real branching and human checkpoints, which is closer to P6's shape
than this one's.

## Where the code lives

| File | |
|---|---|
| `agent.py` | the loop and its four exits |
| `tools.py` | the registry, including the deliberately unreliable tools |
