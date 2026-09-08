# P7 — Cost-Aware Agent Router

**The failure this survives:** a cascade everyone believes is saving money that's
actually costing more.

Status: working end to end. 34 tests, all offline.

## Setup & run

```bash
make install    # from the repo root
python -m p07_cost_router.cli prices        # the arithmetic
python -m p07_cost_router.cli model --tasks 50000
python -m p07_cost_router.cli route "Extract the invoice number from: INV-8842"
python -m p07_cost_router.cli route "Compare batch vs streaming and recommend which to fund. Why?"
python -m p07_cost_router.cli stats
python ../scripts/smoke.py p07 && python -m pytest tests -q
```

## The trap: when the cheap model fails, you pay for both calls

A cascade isn't automatically cheaper.

```
cascade cost  = C_cheap + (1 − p) × C_strong
always-strong = C_strong

cascade wins  ⟺  C_cheap < p × C_strong
              ⟺  p > C_cheap / C_strong
```

The break-even success rate is just the price ratio. Haiku 4.5 costs $1/$5 per
Mtok against Opus 5's $5/$25, a ratio of 0.2. So Haiku has to handle at least 20%
of traffic unaided or the cascade costs more than always using Opus. Sonnet 5
needs to clear 40%.

That number is the whole business case, and you can check it rather than assert
it. `cli stats` reports the observed success rate against the break-even and says
plainly when routing isn't paying:

```
cascade does NOT pay: 12% handled by claude-haiku-4-5 is below the 20%
break-even — pin claude-opus-5 and stop paying twice
```

### What this says about the brief's "40–60% savings"

At 50,000 tasks a month, Haiku against Opus:

| cheap handles | cascade | always-Opus | saved |
|---|---|---|---|
| 10% | $1,375 | $1,250 | **−$125 (−10%)** |
| 20% | $1,250 | $1,250 | $0, break-even |
| 50% | $875 | $1,250 | $375 (30%) |
| 70% | $625 | $1,250 | $625 (50%) |
| 85% | $437 | $1,250 | $812 (65%) |

So 40–60% is achievable but conditional. It needs the cheap tier handling roughly
60–85% of traffic unaided, which is a claim about the customer's task mix rather
than about the router. That's the first thing to measure in an engagement, not
something to promise in a pitch.

One convenient property of the current price list: every model prices output at
5× its input, so the ratio is identical for input and output and the break-even
doesn't move with the traffic mix. The business case survives a change in
workload. That's a property of today's prices though, not a law, so the task
shape stays a parameter.

## Why the classifier is heuristic

An LLM call that decides which model to use gets added to every request,
including the ones the cheap model would have handled unaided. Measured here:

| cheap model handles | saving/request | a small Haiku classifier eats |
|---|---|---|
| 90% | $0.01750 | 4% |
| 80% | $0.01500 | 5% |
| 50% | $0.00750 | 10% |
| 25% | $0.00125 | **60%** |

The classifier's cost stays fixed while the saving shrinks, so the overhead bites
hardest exactly when routing is already marginal, which is when you can least
absorb it. Heuristics cost nothing and add no latency, so they run first.
`--llm-classifier` is there as an opt-in for genuinely ambiguous inputs.

The heuristic scores length, structure and distinct analysis cues. One "why" is
weak evidence, but "compare … analyse … trade-offs … recommend … why" is a
different kind of request. Ties break toward cheap, since guessing cheap costs
one extra call while guessing expensive costs the whole saving on that request.

## Escalation is a decision, not a reflex

Three concrete triggers, each recorded with its reason: confidence below the
floor, `needs_stronger_model` (the cheapest escalation signal there is, since
asking for it costs nothing extra), and output that failed schema validation.

Obviously hard tasks start at the top. Starting cheap on something you can see is
hard just pays twice.

## The budget is a control, not a report

It's enforced before each call. A test confirms that a task exceeding its budget
makes zero model calls, rather than one call that gets reported as over budget
afterwards.

## What `stats` reports

Cost per decision, escalation rate, early-exit rate, actual against baseline
spend on the same tokens, and the routing-pays verdict. `measured_savings` is
evidence and `projected_cost` is a model, so the two disagreeing is itself worth
knowing about.

## Where the code lives

| File | |
|---|---|
| `economics.py` | break-even, projections, classifier overhead, measured savings |
| `router.py` | heuristic classification, the cascade, budget enforcement, analytics |

Prices come from `core/agentcore/models.py`. I verified them against the current
Anthropic price list rather than recalling them, since this project's whole
argument rests on those numbers.
