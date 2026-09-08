# P11 — Production Agent with Observability

**The failure this survives:** an agent misbehaving in a way every conventional
dashboard reports as healthy.

Status: working end to end. 35 tests, all offline.

## Setup & run

This project reads what the other ten emit, so it needs traces before it has
anything to show. `make smoke` produces them.

```bash
make install    # from the repo root, creates the `lyzer` conda env
make smoke      # generates ~400 spans across eight projects

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

Every LLM call, tool call and agent step in P1 through P10 goes through
`agentcore.tracing`, so everything below is computed from that actual traffic
rather than a toy trace this project generated to have something to draw.

```
397 spans · 52 runs · 8 projects
$0.2319 spent · 19,080 in / 9,460 out
error rate 2.77% · 1 degraded

               project  runs  spans     cost    $/run  errors
            p09-debate     6    119  $0.0643  $0.0107    0.8%
     p03-react-planner     5     74  $0.0630  $0.0126    1.4%
      p05-memory-agent     8     47  $0.0358  $0.0045    0.0%
   p10-self-reflective     4     31  $0.0265  $0.0066    0.0%
```

That's one `make smoke` run. Run it again and the counts roughly double, since
traces append.

## Percentiles, not means

A mean of 1020ms and a p95 of 5000ms describe very different systems, and the p95
is the one your customer experiences. Every latency figure here is p50, p95 and
p99.

Percentiles are nearest-rank rather than interpolated. An interpolated p99
invents a number no request actually took, and it's more useful to be able to go
and open the real slow request.

## Errors grouped by shape, not counted

"47 errors" isn't actionable. "41 of them are the same `ValidationError` on
`TicketTriage.affected_users`" is a ticket someone picks up this afternoon.

Messages get collapsed to their shape, with ids, numbers and quoted values
replaced, so errors differing only in an order id count as one problem rather
than forty.

## Alerts on the failures agents actually have

Generic infrastructure monitoring misses all of these. CPU looks fine while an
agent loops, and the p99 looks fine while a schema regression sends every third
request through three repair attempts.

| rule | catches |
|---|---|
| `LOOP` | a tool called repeatedly with identical arguments in one run |
| `COST_SPIKE` | a run far above its project's own median |
| `ERROR_RATE` | with the dominant error shape attached |
| `SCHEMA_DECAY` | repair attempts climbing: zero errors, 2–3× the cost |
| `LATENCY` | p95 above threshold, reported against the mean it hides behind |
| `SILENT_DEGRADE` | rising refusals and partial answers, which count as successes everywhere else |

Every alert carries the run id, the count and an example. An alert that just says
"error rate high" makes the on-call engineer start from scratch, while one that
names the run and the shape starts them halfway through.

Two rules stop it crying wolf. A small sample doesn't page anyone, because two
failures out of three isn't a 67% error rate worth waking someone for. And
repetition alone isn't a loop: my first version of that rule counted every tool
*and LLM* span, which flagged every multi-step agent, since a ReAct planner
legitimately calls `think` once per iteration and with no argument to distinguish
them those calls collapse into one signature. It now requires the inputs to
repeat too. Running it against the real traces is what exposed that, with three
P3 runs reported as looping while they were working correctly.

Thresholds are explicit constants rather than learned baselines. A learned
baseline quietly normalises a regression that's been running a week, which is
exactly when you most want to hear about it.

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

A canary that hasn't seen enough traffic isn't passing, it's unknown. Promoting
on three good requests is how a release with a 30% failure rate reaches
production, and "we don't know yet" looks identical to "it's fine" on a dashboard
that only tracks failures. `min_samples` gates promotion and the state stays
`pending` rather than `healthy`.

Rollback is automatic and immediate. Guard rails get re-checked on every
observation rather than on a timer, because a bad release should be measured in
requests rather than minutes. A canary you have to watch is a canary nobody
watches at 2am.

Guard rails are relative to the baseline's measured behaviour. A 4% error rate is
fine against a 4% baseline and an emergency against 0.1%.

Routing is stable per request id, so a retry lands in the same arm instead of
smearing the comparison. Canary state is persisted and survives a restart.

## Trace viewer

`cli runs` finds the slowest and costliest runs. `cli show <run-id>` prints one
as a tree with per-span latency and cost, so you can read a slow run rather than
guess at it.

## LangSmith and Arize

Not wired in. The trace schema carries `run_id`, `parent_span_id`, model, tokens,
cost and status, which is the shape those exporters want, so the adapter would be
small. I left it out because a key-gated integration that can't run in this
repo's offline demo would be a claim rather than a feature. It's named in the
root README's triage instead of left implied.

## Where the code lives

| File | |
|---|---|
| `analysis.py` | percentiles, per-project and per-model rollups, error shapes, trace trees |
| `alerts.py` | the six agent-shaped rules and their thresholds |
| `canary.py` | guard rails, automatic rollback, evidence-gated promotion |
