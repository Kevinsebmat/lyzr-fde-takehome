# P11 — Production Agent with Observability

**Failure mode this project exists to survive:** an agent misbehaving in a way
that every conventional dashboard reports as healthy.

Status: **working end-to-end.** 35 tests, all offline.

## Setup & run

This project reads what the other ten emit, so it needs traces before it has
anything to show. `make smoke` produces them.

```bash
make install    # from the repo root — creates the `lyzer` conda env
make smoke      # generates ~800 spans across eight projects

python -m p11_observability.cli dashboard
python -m p11_observability.cli alerts
python -m p11_observability.cli errors
python -m p11_observability.cli runs
python -m p11_observability.cli show <run-id>
python -m p11_observability.cli canary --demo
python ../scripts/smoke.py p11
python -m pytest tests -q
```

## It reads real traces, not its own

Every LLM call, tool call and agent step in **P1–P10** goes through
`agentcore.tracing`, so everything below is computed from that actual traffic —
not a toy trace this project generated to have something to draw.

```
798 spans · 104 runs · 8 projects
$0.4661 spent · 38,400 in / 18,960 out
error rate 2.76% · 2 degraded

                    by project
project                 runs  spans     cost    $/run  errors
p09-debate                12    238  $0.1285  $0.0107    0.8%
p03-react-planner         10    148  $0.1260  $0.0126    1.4%
p07-cost-router           28    102  $0.0218  $0.0008    2.0%
```

## Percentiles, not means

A mean of 1020ms and a p95 of 5000ms describe very different systems, and the
p95 is the one the customer experiences. Every latency figure is p50/p95/p99.

Percentiles are **nearest-rank, not interpolated**: an interpolated p99 invents
a number no request actually took, and it's more useful to be able to open the
real slow request.

## Errors grouped by shape, not counted

"47 errors" is not actionable. "41 of them are the same `ValidationError` on
`TicketTriage.affected_users`" is a ticket someone picks up this afternoon.

Messages are collapsed to their shape — ids, numbers and quoted values replaced
— so errors differing only in an order id are one problem rather than forty.

## Alerts on failures agents actually have

Generic infrastructure monitoring misses all of these. CPU looks fine while an
agent loops; the p99 looks fine while a schema regression sends every third
request through three repair attempts.

| rule | catches |
|---|---|
| `LOOP` | a tool called repeatedly **with identical arguments** in one run |
| `COST_SPIKE` | a run far above its project's own median |
| `ERROR_RATE` | with the dominant error shape attached |
| `SCHEMA_DECAY` | repair attempts climbing — **zero errors, 2–3× the cost** |
| `LATENCY` | p95 above threshold, reported against the mean it hides behind |
| `SILENT_DEGRADE` | rising refusals and partial answers — successes everywhere else |

Every alert carries the run id, the count and an example. An alert that says
"error rate high" makes the on-call engineer start from scratch; one that names
the run and the shape starts them halfway through.

**Two rules that stop it crying wolf:**

- **A small sample doesn't page anyone.** Two failures out of three is not a 67%
  error rate worth waking someone for.
- **Repetition alone is not a loop.** The first version of the loop rule counted
  every tool *and LLM* span, and flagged every multi-step agent — a ReAct
  planner legitimately calls `think` once per iteration, and with no argument to
  distinguish them those calls collapse into one signature. It now requires the
  *inputs* to repeat too. Running it against the real traces is what exposed
  that; three P3 runs were being reported as looping when they were working
  correctly.

Thresholds are explicit constants rather than learned baselines. A learned
baseline quietly normalises a regression that's been running a week — precisely
when you most want to be told.

## Canary and rollback

```
a good release
  after 5 observations: only 5 canary observations, need 20 — this is unknown, not healthy
  after 25 observations: promoted v2 on 25 observations
a bad release
  rolled back after 20 canary requests
  error rate 25.0% exceeds the allowed 2.0% (baseline 0.0%)
  traffic now 0%, routing to baseline
```

**A canary that hasn't seen enough traffic is not passing — it's unknown.**
Promoting on three good requests is how a 30%-failure release reaches
production, and "we don't know yet" looks identical to "it's fine" on a
dashboard that only tracks failures. `min_samples` gates promotion and the state
says `pending`, not `healthy`.

**Rollback is automatic and immediate.** Guard rails are re-checked on *every*
observation, not on a timer — a bad release should be measured in requests, not
minutes. A canary you have to watch is a canary nobody watches at 2am.

**Guard rails are relative to the baseline's measured behaviour.** A 4% error
rate is fine against a 4% baseline and an emergency against 0.1%.

Routing is stable per request id, so a retry lands in the same arm rather than
smearing the comparison. Canary state is persisted, so it survives a restart.

## Trace viewer

`cli runs` finds the slowest and costliest runs; `cli show <run-id>` prints that
run as a tree with per-span latency and cost, so a slow run can be read rather
than guessed at.

## LangSmith / Arize

Not wired in. The trace schema (`run_id`, `parent_span_id`, model, tokens, cost,
status) is the shape those exporters want, so the adapter is small — but a
key-gated integration that can't run in this repo's offline demo would be a
claim, not a feature. Called out in the root README's triage rather than left
implied.

## Where the code lives

| File | |
|---|---|
| `analysis.py` | percentiles, per-project/model rollups, error shapes, trace trees |
| `alerts.py` | the six agent-shaped rules and their thresholds |
| `canary.py` | guard rails, automatic rollback, evidence-gated promotion |
