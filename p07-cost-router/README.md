# P7 — Cost-Aware Agent Router

**Failure mode this project exists to survive:** a cascade that everyone
believes is saving money and is actually costing more.

Status: **working end-to-end.** 34 tests, all offline.

## Setup & run

```bash
make install    # from the repo root
python -m p07_cost_router.cli prices        # ← the arithmetic
python -m p07_cost_router.cli model --tasks 50000
python -m p07_cost_router.cli route "Extract the invoice number from: INV-8842"
python -m p07_cost_router.cli route "Compare batch vs streaming and recommend which to fund. Why?"
python -m p07_cost_router.cli stats
python ../scripts/smoke.py p07 && python -m pytest tests -q
```

## The trap: when the cheap model fails, you pay for both calls

A cascade is **not** automatically cheaper.

```
cascade cost  = C_cheap + (1 − p) × C_strong
always-strong = C_strong

cascade wins  ⟺  C_cheap < p × C_strong
              ⟺  p > C_cheap / C_strong
```

**The break-even success rate is just the price ratio.** Haiku 4.5 is $1/$5 per
Mtok against Opus 5's $5/$25 — a ratio of 0.2. So **Haiku must handle at least
20% of traffic unaided, or the cascade costs more than always using Opus.**
Sonnet 5 must clear 40%.

That number is the whole business case, and it's checkable rather than asserted.
`cli stats` reports the *observed* success rate against it, and says plainly when
routing isn't paying:

```
cascade does NOT pay: 12% handled by claude-haiku-4-5 is below the 20%
break-even — pin claude-opus-5 and stop paying twice
```

### What this says about the brief's "40–60% savings"

At 50,000 tasks/month, Haiku↔Opus:

| cheap handles | cascade | always-Opus | saved |
|---|---|---|---|
| 10% | $1,375 | $1,250 | **−$125 (−10%)** |
| 20% | $1,250 | $1,250 | $0 — break-even |
| 50% | $875 | $1,250 | $375 (30%) |
| 70% | $625 | $1,250 | $625 (50%) |
| 85% | $437 | $1,250 | $812 (65%) |

So the 40–60% figure is **achievable but conditional**: it needs the cheap tier
handling roughly 60–85% of traffic unaided. That is a claim about the customer's
task mix, not about the router, and it is the first thing to measure in an
engagement rather than promise in a pitch.

A useful property of the current price list: every model prices output at 5× its
input, so the ratio is identical for input and output and **the break-even
doesn't move with the traffic mix.** That's convenient — the business case
survives a change in workload — but it's a property of today's prices, not a
law, so the shape stays a parameter.

## Why the classifier is heuristic

An LLM call that decides which model to use is added to **every** request,
including the ones the cheap model would have handled unaided. Measured here:

| cheap model handles | saving/request | a small Haiku classifier eats |
|---|---|---|
| 90% | $0.01750 | 4% |
| 80% | $0.01500 | 5% |
| 50% | $0.00750 | 10% |
| 25% | $0.00125 | **60%** |

The classifier's cost is fixed while the saving shrinks — so **the overhead
bites hardest exactly when routing is already marginal**, which is when you can
least absorb it. Heuristics cost nothing and add no latency, so they run first;
`--llm-classifier` exists as an opt-in for genuinely ambiguous inputs.

The heuristic scores length, structure, and *distinct* analysis cues — one "why"
is weak evidence, but "compare … analyse … trade-offs … recommend … why" is a
different kind of request. Ties break toward cheap: guessing cheap costs one
extra call, guessing expensive costs the entire saving on that request.

## Escalation is a decision, not a reflex

Three concrete triggers, each recorded with its reason:

- confidence below the floor
- `needs_stronger_model` — the cheapest escalation signal there is, since asking
  for it costs nothing extra
- output that failed schema validation

Obviously-hard tasks start at the top. Starting cheap on something you can see
is hard just pays twice.

## The budget is a control, not a report

Enforced *before* each call. A test confirms that a task exceeding its budget
makes **zero** model calls — not one call that gets reported as over budget.

## What `stats` reports

Cost per decision, escalation rate, early-exit rate, actual vs. baseline spend
on the same tokens, and the routing-pays verdict. `measured_savings` is
evidence; `projected_cost` is a model, and the two disagreeing is itself worth
knowing about.

## Where the code lives

| File | |
|---|---|
| `economics.py` | break-even, projections, classifier overhead, measured savings |
| `router.py` | heuristic classification, the cascade, budget enforcement, analytics |

Prices come from `core/agentcore/models.py`, verified against the current
Anthropic price list rather than recalled.
