"""P10 tests.

The loop working is the easy half. These concentrate on the ways a reflection
loop quietly makes things worse: shipping a regression, spending iterations on
noise, and reporting improvement it did not achieve.
"""

from __future__ import annotations

import pytest
from agentcore import LLM, Budget, mock, store
from p10_self_reflective.agent import SelfReflectiveAgent, Stop, aggregate, history
from p10_self_reflective.rubric import DIMENSIONS, Judgement
from p10_self_reflective.smoke import CASE_NOTES, HIGH, LOW, MID, TASK, judgement


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("AGENTCORE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AGENTCORE_TRACE_FILE", str(tmp_path / "t.jsonl"))
    mock.PROVIDER.reset()
    yield
    mock.PROVIDER.reset()
    store.reset()


# ---------- scoring ----------


def test_weights_sum_to_one():
    assert sum(d.weight for d in DIMENSIONS) == pytest.approx(1.0)


def test_every_dimension_has_anchored_levels():
    """'Rate this 1-5' drifts between calls; anchors are what make a score
    comparable to last week's."""
    for d in DIMENSIONS:
        assert set(d.anchors) >= {1, 3, 5}
        assert all(len(text) > 20 for text in d.anchors.values())


def test_weighted_score_respects_weights():
    j = Judgement.model_validate_json(judgement({"accuracy": 5, "completeness": 1,
                                                 "tone": 1, "actionability": 1}))
    # accuracy carries 0.35, so a 5 there must outweigh three 1s.
    assert 2.0 < j.weighted() < 2.5


def test_unknown_dimension_is_ignored_not_counted():
    j = Judgement.model_validate_json(
        judgement({"accuracy": 5, "completeness": 5, "tone": 5, "actionability": 5,
                   "invented_dimension": 1})
    )
    assert j.weighted() == pytest.approx(5.0)


def test_weakest_dimensions_drive_the_critique():
    j = Judgement.model_validate_json(judgement(LOW))
    weakest = [s.key for s in j.weakest(2)]
    assert set(weakest) == {"completeness", "actionability"}


def test_judge_must_quote_the_text_it_criticises():
    """A critique that can't point at actual text is usually invented."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Judgement.model_validate(
            {"scores": [{"key": "tone", "score": 2, "evidence": "", "fix": "be warmer"}],
             "overall_note": "x"}
        )


# ---------- the loop ----------


def test_improves_and_stops_at_target():
    mock.PROVIDER.queue("d1", judgement(LOW), "d2", judgement(MID), "d3", judgement(HIGH))
    result = SelfReflectiveAgent(max_iterations=3).run(TASK, CASE_NOTES)
    assert result.stop is Stop.target_reached
    assert result.improvement > 0


def test_stops_immediately_when_the_first_draft_is_good_enough():
    mock.PROVIDER.queue("great draft", judgement(HIGH))
    result = SelfReflectiveAgent(max_iterations=5).run(TASK, CASE_NOTES)
    assert result.stop is Stop.target_reached
    assert len(result.attempts) == 1, "no reason to spend a rewrite"


def test_iteration_ceiling_holds():
    mock.PROVIDER.queue(*[x for i in range(10) for x in (f"d{i}", judgement(LOW))])
    result = SelfReflectiveAgent(max_iterations=3, target_score=4.9, min_gain=-99).run(
        TASK, CASE_NOTES
    )
    assert result.stop is Stop.max_iterations
    assert len(result.attempts) == 3


def test_plateau_stops_the_loop():
    """Chasing sub-threshold gains is chasing judge noise."""
    mock.PROVIDER.queue("d1", judgement(MID), "d2", judgement(MID), "d3", judgement(MID))
    result = SelfReflectiveAgent(max_iterations=5, target_score=4.9).run(TASK, CASE_NOTES)
    assert result.stop is Stop.no_improvement
    assert len(result.attempts) == 2


def test_budget_ceiling_stops_the_loop():
    llm = LLM(project="p10", budget=Budget(limit_usd=0.0001))
    mock.PROVIDER.queue(*[x for i in range(10) for x in (f"d{i}", judgement(LOW))])
    result = SelfReflectiveAgent(llm=llm, max_iterations=10).run(TASK, CASE_NOTES)
    assert result.stop is Stop.budget_exhausted


# ---------- the part naive loops get wrong ----------


def test_returns_the_best_attempt_not_the_last():
    """A rewrite that over-corrects is common. Shipping it is the bug."""
    mock.PROVIDER.queue(
        "good", judgement(MID), "worse", judgement(LOW), "worse still", judgement(LOW)
    )
    result = SelfReflectiveAgent(max_iterations=3, target_score=4.9, min_gain=-99).run(
        TASK, CASE_NOTES
    )
    assert result.best.text == "good"
    assert result.best.score == max(result.trajectory)


def test_regression_is_reported_not_hidden():
    mock.PROVIDER.queue(
        "good", judgement(HIGH), "worse", judgement(LOW), "worse", judgement(LOW)
    )
    result = SelfReflectiveAgent(max_iterations=3, target_score=4.99, min_gain=-99).run(
        TASK, CASE_NOTES
    )
    assert result.regressed
    assert result.summary()["regressed"] is True


def test_an_extra_iteration_can_never_leave_you_worse_off():
    mock.PROVIDER.queue("good", judgement(MID), "bad", judgement(LOW))
    two = SelfReflectiveAgent(max_iterations=2, target_score=4.9, min_gain=-99).run(
        TASK, CASE_NOTES
    )
    mock.PROVIDER.reset()
    mock.PROVIDER.queue("good", judgement(MID))
    one = SelfReflectiveAgent(max_iterations=1, target_score=4.9).run(TASK, CASE_NOTES)
    assert two.best.score >= one.best.score


def test_improvement_is_measured_from_the_first_attempt():
    mock.PROVIDER.queue("d1", judgement(LOW), "d2", judgement(HIGH))
    result = SelfReflectiveAgent(max_iterations=2).run(TASK, CASE_NOTES)
    assert result.improvement == pytest.approx(
        max(result.trajectory) - result.trajectory[0]
    )


def test_no_improvement_reports_no_cost_per_point():
    """Rather than dividing by zero and claiming infinite efficiency."""
    mock.PROVIDER.queue("d", judgement(MID), "d", judgement(MID))
    result = SelfReflectiveAgent(max_iterations=2, target_score=4.9).run(TASK, CASE_NOTES)
    assert result.cost_per_point is None


# ---------- regeneration constraints ----------


def test_rewrite_prompt_names_the_specific_fixes():
    mock.PROVIDER.queue("d1", judgement(LOW), "d2", judgement(HIGH))
    SelfReflectiveAgent(max_iterations=2).run(TASK, CASE_NOTES)
    prompt = mock.PROVIDER.calls[2]["messages"][-1]["content"]
    assert "REQUIRED CHANGES" in prompt
    assert "specific change for completeness" in prompt


def test_rewrite_prompt_forbids_changing_what_already_works():
    """Without this the model rewrites wholesale and breaks the dimensions
    that already scored well."""
    mock.PROVIDER.queue("d1", judgement(LOW), "d2", judgement(HIGH))
    SelfReflectiveAgent(max_iterations=2).run(TASK, CASE_NOTES)
    prompt = mock.PROVIDER.calls[2]["messages"][-1]["content"]
    assert "Keep everything the reviewer did not criticise" in prompt


def test_rewrite_sees_the_previous_draft_and_its_score():
    mock.PROVIDER.queue("the first draft text", judgement(LOW), "d2", judgement(HIGH))
    SelfReflectiveAgent(max_iterations=2).run(TASK, CASE_NOTES)
    prompt = mock.PROVIDER.calls[2]["messages"][-1]["content"]
    assert "the first draft text" in prompt


def test_judge_runs_on_a_separate_model_tier():
    mock.PROVIDER.queue("d1", judgement(HIGH))
    SelfReflectiveAgent(max_iterations=1, judge_model="claude-sonnet-5").run(
        TASK, CASE_NOTES
    )
    assert mock.PROVIDER.calls[0]["model"] != mock.PROVIDER.calls[1]["model"]


# ---------- metrics ----------


def test_every_run_is_logged():
    mock.PROVIDER.queue("d1", judgement(HIGH))
    SelfReflectiveAgent(max_iterations=1).run(TASK, CASE_NOTES)
    rows = history()
    assert rows and rows[0]["trajectory"]


def test_aggregate_answers_whether_reflection_pays():
    for scores in (HIGH, LOW):
        mock.PROVIDER.reset()
        mock.PROVIDER.queue("d1", judgement(scores), "d2", judgement(HIGH))
        SelfReflectiveAgent(max_iterations=2).run(TASK, CASE_NOTES)
    agg = aggregate()
    assert agg["runs"] == 2
    assert "mean_improvement" in agg and "runs_that_regressed" in agg


def test_aggregate_is_empty_before_any_runs():
    assert aggregate() == {"runs": 0}


def test_malformed_judge_output_keeps_earlier_work():
    mock.PROVIDER.queue("good draft", judgement(MID), "d2", *["not json"] * 3)
    result = SelfReflectiveAgent(max_iterations=3, target_score=4.9, min_gain=-99).run(
        TASK, CASE_NOTES
    )
    assert result.stop is Stop.failed
    assert result.best is not None, "a later failure must not lose a good earlier draft"
    assert result.best.text == "good draft"
