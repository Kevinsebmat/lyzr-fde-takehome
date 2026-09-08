"""End-to-end smoke for P7.

Checks the routing works, the budget is a control rather than a report, and —
the part that matters most — that the savings claim is measured against a
baseline instead of asserted.
"""

from __future__ import annotations

import json

from agentcore import mock

from .economics import break_even, classifier_verdict, projected_cost
from .router import Complexity, CostAwareRouter, analytics, classify, reset_history


def answer(text="the answer", confidence=0.9, needs_stronger=False) -> str:
    return json.dumps(
        {"answer": text, "confidence": confidence, "needs_stronger_model": needs_stronger}
    )


SIMPLE = "Extract the invoice number and total from this line: INV-8842, $1,204.00"
HARD = (
    "Compare running our ingestion as nightly batch against streaming. Analyse "
    "the trade-offs for cost, operational risk and time to detect a bad record, "
    "and recommend which we should fund next quarter. Why would the other "
    "option be defensible?"
)


def smoke() -> dict:
    results: dict[str, object] = {}

    # 1. The break-even is the price ratio, and it is checkable.
    be = break_even("claude-haiku-4-5", "claude-opus-5")
    assert 0.19 < be.required_success_rate < 0.21, be.required_success_rate
    results["haiku_break_even"] = be.required_success_rate

    sonnet_be = break_even("claude-sonnet-5", "claude-opus-5")
    assert 0.39 < sonnet_be.required_success_rate < 0.41
    results["sonnet_break_even"] = sonnet_be.required_success_rate

    # 2. Classification is free and routes the obvious cases correctly.
    assert classify(SIMPLE).complexity is Complexity.simple, classify(SIMPLE)
    assert classify(HARD).complexity is Complexity.hard, classify(HARD)
    results["classification"] = "heuristic, no LLM call"

    # 3. A simple task exits early on the cheap model.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(answer("INV-8842, $1,204.00", confidence=0.95))
    cheap = CostAwareRouter().route(SIMPLE)
    assert cheap.final_model == "claude-haiku-4-5", cheap.final_model
    assert cheap.early_exit and not cheap.escalated
    results["simple_stayed_cheap"] = True

    # 4. Low confidence escalates — with a stated reason, not a reflex.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        answer("not sure", confidence=0.3),
        answer("still unsure", confidence=0.4),
        answer("a confident answer", confidence=0.95),
    )
    escalated = CostAwareRouter().route(SIMPLE)
    assert escalated.escalated
    assert escalated.final_model == "claude-opus-5"
    assert escalated.attempts[0].escalated_because
    results["escalation_path"] = [a.model for a in escalated.attempts]

    # 5. The model saying so is the cheapest escalation signal there is.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        answer("beyond me", confidence=0.99, needs_stronger=True),
        answer("handled", confidence=0.95),
    )
    self_reported = CostAwareRouter().route(SIMPLE)
    assert self_reported.escalated
    assert "exceeded it" in self_reported.attempts[0].escalated_because
    results["self_reported_escalation"] = True

    # 6. The budget stops the cascade before the call, not after.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(*[answer("no", confidence=0.1) for _ in range(5)])
    broke = CostAwareRouter(task_budget_usd=0.000_01).route(HARD)
    assert broke.budget_exceeded
    assert broke.cost_usd <= 0.000_01 or broke.attempts == []
    results["budget_is_a_control"] = True

    # 7. The savings claim is measured against a baseline, not asserted.
    #    History is cleared first: a rate computed over the earlier checks'
    #    decisions is not the rate being asserted.
    mock.PROVIDER.reset()
    reset_history()
    for _ in range(8):
        mock.PROVIDER.queue(answer("done", confidence=0.95))
        CostAwareRouter().route(SIMPLE)
    for _ in range(2):
        mock.PROVIDER.queue(answer("hmm", confidence=0.2), answer("done", confidence=0.95))
        CostAwareRouter().route(SIMPLE)

    stats = analytics()
    assert stats["decisions"] == 10
    assert stats["saved_pct"] > 0, stats
    assert stats["cheap_success_rate"] == 0.8
    assert stats["routing_pays"] is True, stats["verdict"]
    results["measured_saving_pct"] = stats["saved_pct"]
    results["cheap_success_rate"] = stats["cheap_success_rate"]

    # 8. And it says so honestly when routing would NOT pay.
    bad = projected_cost("claude-haiku-4-5", "claude-opus-5", success_rate=0.1)
    assert bad["worth_it"] is False
    assert bad["saved_usd"] < 0, "escalating 90% of the time costs more, not less"
    results["reports_when_routing_loses"] = True

    # 9. Classifier overhead bites hardest exactly when routing is marginal.
    healthy = classifier_verdict("claude-haiku-4-5", "claude-opus-5", success_rate=0.8)
    marginal = classifier_verdict("claude-haiku-4-5", "claude-opus-5", success_rate=0.25)
    assert healthy["affordable"], healthy
    assert not marginal["affordable"], marginal
    assert marginal["share_of_saving_consumed"] > 4 * healthy["share_of_saving_consumed"]
    results["classifier_share_at_80pct"] = healthy["share_of_saving_consumed"]
    results["classifier_share_at_25pct"] = marginal["share_of_saving_consumed"]

    mock.PROVIDER.reset()
    return results
