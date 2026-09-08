"""P7 tests.

The economics tests are the important ones. Routing that "feels cheaper" is
worthless; the claim has to survive arithmetic and a measured baseline.
"""

from __future__ import annotations

import pytest
from agentcore import Usage, mock, store
from p07_cost_router.economics import (
    break_even,
    classifier_verdict,
    measured_savings,
    price_table,
    projected_cost,
)
from p07_cost_router.router import (
    Complexity,
    CostAwareRouter,
    analytics,
    classify,
    reset_history,
)
from p07_cost_router.smoke import HARD, SIMPLE, answer


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("AGENTCORE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AGENTCORE_TRACE_FILE", str(tmp_path / "t.jsonl"))
    mock.PROVIDER.reset()
    yield
    mock.PROVIDER.reset()
    store.reset()


# ---------- the economics ----------


def test_break_even_is_the_price_ratio():
    """Haiku is 1/5 the price of Opus, so it must handle 20% of traffic
    unaided or the cascade costs more than always using Opus."""
    assert break_even("claude-haiku-4-5", "claude-opus-5").required_success_rate == 0.2
    assert break_even("claude-sonnet-5", "claude-opus-5").required_success_rate == 0.4


def test_break_even_is_shape_independent_on_the_current_ladder():
    """Every model on this ladder prices output at 5x its input, so the ratio
    is the same for input and output and the break-even does not move with the
    traffic mix. Convenient — the business case survives a change in workload —
    but a property of today's price list, not a law."""
    retrieval_heavy = break_even("claude-haiku-4-5", "claude-opus-5",
                                 input_tokens=20_000, output_tokens=200)
    generation_heavy = break_even("claude-haiku-4-5", "claude-opus-5",
                                  input_tokens=200, output_tokens=4_000)
    assert retrieval_heavy.required_success_rate == 0.2
    assert generation_heavy.required_success_rate == 0.2
    # The absolute per-task costs do differ, which is what the budget cares about.
    assert retrieval_heavy.strong_cost_per_task != generation_heavy.strong_cost_per_task


def test_a_cascade_below_break_even_costs_more():
    """The trap: when the cheap model fails you pay for both calls."""
    bad = projected_cost("claude-haiku-4-5", "claude-opus-5", success_rate=0.1)
    assert bad["worth_it"] is False
    assert bad["saved_usd"] < 0


def test_a_cascade_above_break_even_saves():
    good = projected_cost("claude-haiku-4-5", "claude-opus-5", success_rate=0.85)
    assert good["worth_it"] is True
    assert good["saved_pct"] > 40


def test_break_even_verdict_names_the_recommendation():
    be = break_even("claude-haiku-4-5", "claude-opus-5")
    assert "does NOT pay" in be.verdict(0.05)
    assert "pin claude-opus-5" in be.verdict(0.05)
    assert "cascade pays" in be.verdict(0.9)


def test_classifier_overhead_bites_hardest_when_routing_is_marginal():
    """The share stays fixed while the saving shrinks."""
    healthy = classifier_verdict("claude-haiku-4-5", "claude-opus-5", 0.8)
    marginal = classifier_verdict("claude-haiku-4-5", "claude-opus-5", 0.25)
    assert healthy["affordable"] and not marginal["affordable"]
    assert healthy["share_of_saving_consumed"] < 0.1
    assert marginal["share_of_saving_consumed"] > 0.5


def test_measured_savings_prices_the_same_tokens_on_one_model():
    usages = [Usage("claude-haiku-4-5", input_tokens=2000, output_tokens=600)] * 10
    result = measured_savings(usages, "claude-opus-5")
    assert result["saved_pct"] == pytest.approx(80.0, abs=0.1)
    assert result["calls_by_model"] == {"claude-haiku-4-5": 10}


def test_measured_savings_on_no_traffic_is_zero_not_a_crash():
    assert measured_savings([], "claude-opus-5")["calls"] == 0


def test_price_table_covers_the_whole_ladder():
    rows = price_table()
    assert [r["model"] for r in rows] == [
        "claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5",
    ]
    assert rows[-1]["break_even_vs_opus"] == 1.0


# ---------- classification ----------


@pytest.mark.parametrize(
    "task, expected",
    [
        ("Extract the invoice number from: INV-8842", Complexity.simple),
        ("Classify this ticket as billing or technical.", Complexity.simple),
        ("List the three action items in this note.", Complexity.simple),
        (HARD, Complexity.hard),
        (
            "Compare the trade-offs and recommend which architecture to fund, "
            "and analyse why the alternative is defensible.",
            Complexity.hard,
        ),
        ("Summarise this customer call in three bullets.", Complexity.simple),
    ],
)
def test_heuristic_classification(task, expected):
    assert classify(task).complexity is expected


def test_multiple_analysis_cues_outweigh_one():
    """One 'why' is weak evidence; five analysis verbs is a different request."""
    weak = classify("Why is the invoice wrong?")
    strong = classify(
        "Compare the options, analyse the trade-offs, evaluate the risk and "
        "recommend what to do."
    )
    assert weak.complexity is not Complexity.hard
    assert strong.complexity is Complexity.hard


def test_classification_costs_nothing():
    classify(HARD)
    assert mock.PROVIDER.calls == [], "the default classifier must not call a model"


def test_long_input_raises_complexity():
    assert classify("word " * 300).complexity is not Complexity.simple


def test_classification_explains_itself():
    assert classify(HARD).reasons
    assert classify(SIMPLE).reasons


# ---------- routing ----------


def test_a_simple_task_stays_on_the_cheap_model():
    mock.PROVIDER.queue(answer(confidence=0.95))
    result = CostAwareRouter().route(SIMPLE)
    assert result.final_model == "claude-haiku-4-5"
    assert result.early_exit and not result.escalated


def test_a_hard_task_starts_on_the_strong_model():
    """Starting cheap on something obviously hard just pays twice."""
    mock.PROVIDER.queue(answer(confidence=0.95))
    result = CostAwareRouter().route(HARD)
    assert result.final_model == "claude-opus-5"
    assert not result.escalated


def test_low_confidence_escalates_with_a_stated_reason():
    mock.PROVIDER.queue(answer(confidence=0.2), answer(confidence=0.95))
    result = CostAwareRouter().route(SIMPLE)
    assert result.escalated
    assert "below the" in result.attempts[0].escalated_because


def test_the_model_asking_for_help_escalates():
    """The cheapest escalation signal there is: it costs nothing to ask for."""
    mock.PROVIDER.queue(
        answer(confidence=0.99, needs_stronger=True), answer(confidence=0.95)
    )
    result = CostAwareRouter().route(SIMPLE)
    assert result.escalated
    assert "exceeded it" in result.attempts[0].escalated_because


def test_high_confidence_does_not_escalate():
    mock.PROVIDER.queue(answer(confidence=0.99))
    assert not CostAwareRouter().route(SIMPLE).escalated


def test_escalation_stops_at_the_top_of_the_ladder():
    mock.PROVIDER.queue(*[answer(confidence=0.1) for _ in range(6)])
    result = CostAwareRouter().route(SIMPLE)
    assert result.final_model == "claude-opus-5"
    assert len(result.attempts) <= 3


def test_schema_failure_escalates_rather_than_giving_up():
    mock.PROVIDER.queue(*["not json"] * 3, answer(confidence=0.95))
    result = CostAwareRouter().route(SIMPLE)
    assert result.attempts[0].escalated_because == "output failed schema validation"
    assert result.final_model == "claude-sonnet-5"


def test_escalation_path_climbs_one_tier_at_a_time():
    mock.PROVIDER.queue(answer(confidence=0.1), answer(confidence=0.1),
                        answer(confidence=0.99))
    result = CostAwareRouter().route(SIMPLE)
    assert [a.model for a in result.attempts] == [
        "claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5",
    ]


# ---------- the budget ----------


def test_the_budget_stops_the_call_before_it_is_made():
    """A budget checked after the fact is a report, not a control."""
    mock.PROVIDER.queue(answer(confidence=0.95))
    result = CostAwareRouter(task_budget_usd=0.000_001).route(HARD)
    assert result.budget_exceeded
    assert mock.PROVIDER.calls == [], "the expensive call must never have happened"


def test_the_budget_caps_a_long_cascade():
    mock.PROVIDER.queue(*[answer(confidence=0.1) for _ in range(10)])
    result = CostAwareRouter(task_budget_usd=0.004).route(SIMPLE)
    assert result.cost_usd <= 0.004


def test_a_budget_exhausted_task_still_answers():
    mock.PROVIDER.queue(answer(confidence=0.95))
    result = CostAwareRouter(task_budget_usd=0.000_001).route(HARD)
    assert result.answer


# ---------- analytics ----------


def test_analytics_measures_the_saving_against_a_baseline():
    reset_history()
    for _ in range(9):
        mock.PROVIDER.queue(answer(confidence=0.95))
        CostAwareRouter().route(SIMPLE)
    mock.PROVIDER.queue(answer(confidence=0.1), answer(confidence=0.95))
    CostAwareRouter().route(SIMPLE)

    stats = analytics()
    assert stats["decisions"] == 10
    assert stats["cheap_success_rate"] == 0.9
    assert stats["saved_pct"] > 0
    assert stats["routing_pays"] is True


def test_analytics_says_so_when_routing_does_not_pay():
    """Below break-even the honest recommendation is to stop routing."""
    reset_history()
    for _ in range(10):
        mock.PROVIDER.queue(answer(confidence=0.1), answer(confidence=0.1),
                            answer(confidence=0.95))
        CostAwareRouter().route(SIMPLE)

    stats = analytics()
    assert stats["cheap_success_rate"] == 0.0
    assert stats["routing_pays"] is False
    assert "does NOT pay" in stats["verdict"]


def test_analytics_reports_cost_per_decision():
    reset_history()
    mock.PROVIDER.queue(answer(confidence=0.95))
    CostAwareRouter().route(SIMPLE)
    assert analytics()["cost_per_decision_usd"] > 0


def test_analytics_is_empty_before_any_traffic():
    reset_history()
    assert analytics() == {"decisions": 0}
