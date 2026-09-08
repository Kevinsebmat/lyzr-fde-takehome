# P10 — Self-Reflective Agent with Auto-Eval

**The failure this survives:** a reflection loop that confidently ships a worse
draft than the one it started with.

Status: working end to end. 24 tests, all offline.

## Setup & run

```bash
make install    # from the repo root
python -m p10_self_reflective.cli run
python -m p10_self_reflective.cli run --max-iterations 5 --target 4.5
python -m p10_self_reflective.cli rubric
python -m p10_self_reflective.cli metrics
python ../scripts/smoke.py p10
python -m pytest tests -q
```

The task is drafting a reply to an angry enterprise customer after a two-hour
outage, using only the supplied case notes.

## Two things that make this more than a loop with an LLM in it

### Keep the best, not the last

Regeneration frequently makes output worse. The model over-corrects the flaw it
was shown and breaks something that was already fine, and a loop that returns its
final iteration ships that regression.

This one scores every attempt and returns the highest scorer, so an extra
iteration can never leave you worse off than stopping early would have. That
property has its own test. Regressions get reported rather than hidden, since
they're the evidence that keep-the-best is doing real work.

Two rules stop rewrites wandering: fix only what was named, and change nothing
else. Without them the model restructures wholesale and breaks the dimensions
that already scored well.

### The metric is the deliverable

"It reflects and improves" is a claim. A logged trajectory is evidence:

```
trajectory      [2.55, 3.80, 4.60]
improvement     +2.05
regressed       False
cost/point      $0.0042
stop            target_reached
```

`cli metrics` aggregates across runs: mean improvement, how many runs improved,
how many regressed, mean iterations, total spend. That's what decides whether
reflection is worth switching on for a given workload. If mean improvement sits
near zero, the honest recommendation is to switch the loop off and put the budget
into a better first-draft prompt instead.

## Four ways to stop

| | |
|---|---|
| `TARGET_REACHED` | weighted score ≥ 4.2, stops immediately with no gratuitous rewrite |
| `NO_IMPROVEMENT` | the last rewrite gained under 0.15, which is judge noise rather than progress |
| `MAX_ITERATIONS` | the hard ceiling |
| `BUDGET_EXHAUSTED` | spend cap hit mid-run |

A malformed judgement mid-run keeps the earlier attempts rather than losing the
whole run.

## The rubric is the engineering

"Rate this 1-5" gives you a number that drifts between calls and can't be
compared across runs. Every level here has a concrete anchor:

```
actionability — Says what happens next (weight 0.20)
    1 = No next step, or an unowned one ("someone will look into it").
    3 = A next step with no owner or no date.
    5 = A named owner and a specific date or timeframe for each next step.
```

The judge has to quote the text it's scoring and give one concrete change per
dimension. A critique that can't point at actual text is usually the judge
inventing a flaw, and it's useless as a rewrite instruction either way, so the
schema enforces both.

## The honest limitation

A model judging output from its own family inflates the scores. Anchored levels,
mandatory evidence quotes and a separate model tier for judging narrow that bias.
They don't remove it.

The real fix is a human-labelled calibration set: score 50 drafts by hand, check
how well the judge correlates with the humans, and adjust the anchors where it
disagrees. That's scoped work in an engagement, not something a demo can assert.
Treat these absolute scores as a relative signal between iterations of the same
task, which is all the loop actually needs, rather than an objective quality
measure.

## Where the code lives

| File | |
|---|---|
| `rubric.py` | dimensions, anchors, the judge schema and prompt |
| `agent.py` | the loop, keep-the-best, and the metrics |
