"""The arithmetic that decides whether a cascade is worth running.

The trap in cheap-first routing: when the cheap model fails and you escalate,
**you pay for both calls.** A cascade is not automatically cheaper — it is
cheaper only when the cheap model succeeds often enough.

    cascade cost  = C_cheap + (1 − p) × C_strong
    always-strong = C_strong

    cascade wins  ⟺  C_cheap + (1 − p) × C_strong  <  C_strong
                  ⟺  C_cheap  <  p × C_strong
                  ⟺  p  >  C_cheap / C_strong

So the break-even success rate is just the price ratio. Haiku 4.5 against
Opus 5 is $1/$5 in and $5/$25 out — a ratio of 0.2, so **Haiku must handle at
least 20% of traffic unaided or the cascade costs more than always using
Opus.** Sonnet 5 against Opus 5 is 0.4, so Sonnet must clear 40%.

That number is the whole business case, and it is checkable rather than
asserted. `measured_savings()` reports what actually happened; if the observed
escalation rate is above break-even, the honest recommendation is to stop
routing and pin the strong model.

Two caveats this module makes explicit rather than hiding:

- **The classifier is not free, and it hurts most when you can least afford
  it.** An LLM call that decides which model to use is added to *every* request.
  Measured at the shapes in this repo, a small Haiku classifier call ($0.00075)
  is only 4-5% of the per-request saving while the cheap tier handles 80-90% of
  traffic — affordable. But the saving shrinks as the escalation rate rises
  while the classifier cost stays fixed, so at a 25% success rate (just above
  the 20% break-even) the same classifier eats **60%** of what is left. The
  overhead bites hardest exactly when routing is already marginal, which is
  when you are least able to absorb it. `classifier_verdict()` computes this;
  heuristics are free and add no latency, which is why they run first.
- **A retry is not free either.** Cost per *completed task* is the metric, not
  cost per request. A cheaper request that needs two attempts is not cheaper.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentcore import Usage, spec


@dataclass(frozen=True)
class BreakEven:
    cheap_model: str
    strong_model: str
    #: Minimum share of tasks the cheap model must handle unaided.
    required_success_rate: float
    cheap_cost_per_task: float
    strong_cost_per_task: float

    def verdict(self, observed_success_rate: float) -> str:
        if observed_success_rate > self.required_success_rate:
            margin = observed_success_rate - self.required_success_rate
            return (
                f"cascade pays: {observed_success_rate:.0%} handled by "
                f"{self.cheap_model} against a {self.required_success_rate:.0%} "
                f"break-even ({margin:.0%} margin)"
            )
        return (
            f"cascade does NOT pay: {observed_success_rate:.0%} handled by "
            f"{self.cheap_model} is below the {self.required_success_rate:.0%} "
            f"break-even — pin {self.strong_model} and stop paying twice"
        )


def break_even(
    cheap_model: str,
    strong_model: str,
    input_tokens: int = 2_000,
    output_tokens: int = 600,
) -> BreakEven:
    """Success rate the cheap model must clear for the cascade to be worth it.

    Takes a task shape, though on the *current* Anthropic ladder it does not
    change the answer: every model prices output at 5x its input, so the
    Haiku:Opus ratio is 0.2 on both and the break-even is 0.2 whether the
    workload is retrieval-heavy or generation-heavy. That is a convenient
    property — the business case does not move when the traffic mix does — and
    it is a property of today's price list, not a law. The shape stays a
    parameter so the number stays correct if a provider changes the multiple.
    """
    cheap = Usage(cheap_model, input_tokens=input_tokens, output_tokens=output_tokens).cost_usd
    strong = Usage(strong_model, input_tokens=input_tokens, output_tokens=output_tokens).cost_usd
    return BreakEven(
        cheap_model=cheap_model,
        strong_model=strong_model,
        required_success_rate=round(cheap / strong, 4) if strong else 1.0,
        cheap_cost_per_task=round(cheap, 6),
        strong_cost_per_task=round(strong, 6),
    )


def classifier_verdict(
    cheap_model: str,
    strong_model: str,
    success_rate: float,
    classifier_input_tokens: int = 500,
    classifier_output_tokens: int = 50,
) -> dict:
    """Is a per-request LLM classifier affordable at this success rate?

    Returns what the classifier costs, what the routing saves, and the share
    the classifier consumes. The share is the number that matters: it rises
    sharply as the cascade approaches break-even.
    """
    ceiling = classifier_overhead_break_even(cheap_model, strong_model, success_rate)
    cost = Usage(
        cheap_model,
        input_tokens=classifier_input_tokens,
        output_tokens=classifier_output_tokens,
    ).cost_usd
    share = (cost / ceiling) if ceiling > 0 else float("inf")
    return {
        "classifier_cost_usd": round(cost, 6),
        "saving_per_request_usd": ceiling,
        "share_of_saving_consumed": round(share, 4),
        "affordable": share < 0.25,
        "note": (
            "affordable at this success rate"
            if share < 0.25
            else "the classifier consumes most of the saving — use heuristics"
        ),
    }


def classifier_overhead_break_even(
    cheap_model: str,
    strong_model: str,
    success_rate: float,
    input_tokens: int = 2_000,
    output_tokens: int = 600,
) -> float:
    """Most a per-request classifier may cost before it eats the savings.

    Returns dollars per request. If an LLM classifier costs more than this,
    heuristics are not a shortcut — they are the only version that saves money.
    """
    be = break_even(cheap_model, strong_model, input_tokens, output_tokens)
    saving = be.strong_cost_per_task - (
        be.cheap_cost_per_task + (1 - success_rate) * be.strong_cost_per_task
    )
    return round(max(0.0, saving), 6)


def projected_cost(
    cheap_model: str,
    strong_model: str,
    success_rate: float,
    tasks: int = 1_000,
    input_tokens: int = 2_000,
    output_tokens: int = 600,
) -> dict:
    """What a cascade would cost against always using the strong model."""
    be = break_even(cheap_model, strong_model, input_tokens, output_tokens)
    cascade = tasks * (
        be.cheap_cost_per_task + (1 - success_rate) * be.strong_cost_per_task
    )
    baseline = tasks * be.strong_cost_per_task
    return {
        "tasks": tasks,
        "success_rate": success_rate,
        "cascade_usd": round(cascade, 4),
        "baseline_usd": round(baseline, 4),
        "saved_usd": round(baseline - cascade, 4),
        "saved_pct": round(100 * (baseline - cascade) / baseline, 2) if baseline else 0.0,
        "break_even_success_rate": be.required_success_rate,
        "worth_it": success_rate > be.required_success_rate,
    }


def measured_savings(usages: list[Usage], baseline_model: str) -> dict:
    """What routing actually cost, against the same work on one model.

    This is the number that goes in front of a customer. `projected_cost` is a
    model; this is evidence, and the two disagreeing is worth knowing about.
    """
    if not usages:
        return {"calls": 0, "actual_usd": 0.0, "baseline_usd": 0.0, "saved_pct": 0.0}

    actual = sum(u.cost_usd for u in usages)
    baseline = sum(
        Usage(
            baseline_model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_tokens,
            cache_write_tokens=u.cache_write_tokens,
        ).cost_usd
        for u in usages
    )
    by_model: dict[str, int] = {}
    for u in usages:
        by_model[u.model] = by_model.get(u.model, 0) + 1

    return {
        "calls": len(usages),
        "actual_usd": round(actual, 6),
        "baseline_usd": round(baseline, 6),
        "baseline_model": baseline_model,
        "saved_usd": round(baseline - actual, 6),
        "saved_pct": round(100 * (baseline - actual) / baseline, 2) if baseline else 0.0,
        "calls_by_model": by_model,
    }


def price_table() -> list[dict]:
    """The registry's prices, and each model's break-even against Opus 5."""
    from agentcore import LADDER

    rows = []
    for model in LADDER:
        s = spec(model)
        be = break_even(model, "claude-opus-5")
        rows.append({
            "model": model,
            "tier": s.tier,
            "input_per_mtok": s.input_per_mtok,
            "output_per_mtok": s.output_per_mtok,
            "cost_per_task": be.cheap_cost_per_task,
            "break_even_vs_opus": be.required_success_rate,
        })
    return rows
