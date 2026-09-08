# P3 — ReAct Planning Agent

**Failure mode this project exists to survive:** the agent that never stops.

Status: **working end-to-end.** 20 tests, all offline.

## Setup & run

```bash
make install    # from the repo root
python -m p03_react_planner.cli run "Does order ORD-4417 need manager approval for a refund?"
python -m p03_react_planner.cli run "..." --max-iterations 3 --no-reflect
python -m p03_react_planner.cli tools
python ../scripts/smoke.py p03
python -m pytest tests -q
```

## One termination condition is not enough

That is the whole lesson of this project. Each of these leaves a hole the
others cover:

| Condition | Catches | Misses |
|---|---|---|
| `MAX_ITERATIONS` | everything, eventually | lets the agent burn its whole budget repeating one dead call first |
| `LOOP_DETECTED` | the same action + input repeated | an agent wandering through slightly *different* useless actions |
| `NO_PROGRESS` | wandering, via self-critique | a critique step that is itself wrong |
| `BUDGET_EXHAUSTED` | spend, regardless of shape | nothing — but it's the most expensive way to find out |

So all four run together, and any one ends the run. The iteration cap is the
backstop, not the plan.

Loop detection normalises case and whitespace — otherwise `ORD-4417`,
`ord-4417 ` and `Ord-4417` read as three different actions and the loop escapes.
`max_repeats=2` allows one legitimate retry; three is a pattern.

## Every exit still answers

An early exit calls `_salvage()`, which asks for the best answer the
observations already gathered will support, and labels it:

```
Order ORD-4417 totals $12,480 and is shipped. I could not retrieve the refund
policy, so I cannot confirm the approval threshold.

[Partial: repeated `search_archive` with the same input 3 times.]
```

Throwing away six tool calls' worth of work because the seventh didn't happen
is a worse outcome than a partial answer that says it is partial. `run()` never
raises on termination — tested against every stopping condition.

## Self-critique

After each observation a second, cheaper call judges one thing: did that step
move the task forward? An error, an empty result, or a repeat is not progress.
Two consecutive unproductive steps ends the run.

The critique is a safety net, so a failure *in* it doesn't abort a working run —
if it errors or fails validation the loop continues on the hard caps. A safety
net that can take down the thing it protects is not a safety net.

## Tools that misbehave on purpose

`search_archive` always returns nothing. `legacy_crm` always raises. An agent
that only ever sees successful tool calls never has to prove it can terminate,
so the registry ships both.

Tool failures become **observations**, not exceptions — the agent reads
`ERROR: ConnectionError: upstream service unavailable`, reflects, and tries
something else. An unknown tool name returns the list of real ones, because a
hallucinated name is recoverable if you tell the agent what actually exists.

`calculator` uses `eval`, so it enforces a character allowlist first. Passing
model-generated text to `eval` is a remote-code-execution hole; the allowlist is
the control that closes it, and there is a test that tries to break it.

## A note on frameworks

The plan for this repo said LangGraph for P3. It's hand-rolled instead. The
graded property here is *precise, testable termination*, and owning the loop
makes every stopping condition an explicit branch with a test against it, rather
than a recursion limit inherited from a framework. LangGraph would be the right
call for a graph with genuine branching and human checkpoints — closer to P6's
shape than this one's.

## Where the code lives

| File | |
|---|---|
| `agent.py` | the loop and its four exits |
| `tools.py` | the registry, including the deliberately unreliable tools |
