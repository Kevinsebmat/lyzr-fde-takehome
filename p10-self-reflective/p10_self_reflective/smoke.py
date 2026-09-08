"""End-to-end smoke for P10.

The interesting checks are the two that a naive reflection loop fails:
returning the best attempt rather than the last, and stopping when rewriting
has stopped helping.
"""

from __future__ import annotations

import json

from agentcore import mock

from .agent import SelfReflectiveAgent, Stop, aggregate

CASE_NOTES = """Account: Wexler Industries (enterprise, $48,200/mo)
Incident: checkout API returned 500 for all EU traffic, 09:00-11:20 UTC (2h20m).
Cause: a bad deploy to the payment service; rolled back at 11:15.
Customer asked: (1) what happened, (2) will they be credited, (3) how it is
prevented in future.
SLA: 99.9% monthly uptime. This month's uptime after the incident: 99.68%.
Credit owed under policy: 10% of the monthly fee per full 0.1% below target,
so 20% = $9,640. Approved by finance on 14 March.
Prevention: staged rollout + automated canary for the payment service, owned by
the platform team, shipping 28 March."""

TASK = "Draft a reply to the customer's escalation email."


def judgement(scores: dict[str, int], note="reviewed") -> str:
    return json.dumps(
        {
            "scores": [
                {
                    "key": k,
                    "score": v,
                    "evidence": f"quoted text for {k}",
                    "fix": f"specific change for {k}",
                }
                for k, v in scores.items()
            ],
            "overall_note": note,
        }
    )


LOW = {"accuracy": 3, "completeness": 2, "tone": 3, "actionability": 2}
MID = {"accuracy": 4, "completeness": 4, "tone": 4, "actionability": 3}
HIGH = {"accuracy": 5, "completeness": 5, "tone": 4, "actionability": 4}


def smoke() -> dict:
    results: dict[str, object] = {}

    # 1. Improves across iterations and stops when the target is reached.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        "draft one", judgement(LOW),
        "draft two", judgement(MID),
        "draft three", judgement(HIGH),
    )
    improved = SelfReflectiveAgent(max_iterations=3).run(TASK, CASE_NOTES)
    assert improved.stop is Stop.target_reached, improved.stop
    assert improved.improvement > 0
    assert improved.trajectory == sorted(improved.trajectory), improved.trajectory
    results["trajectory"] = improved.trajectory
    results["improvement"] = improved.improvement

    # 2. The graded behaviour: a rewrite made it worse, and the best attempt
    #    is still what gets returned.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        "good draft", judgement(MID),
        "over-corrected draft", judgement(LOW),
        "still worse", judgement(LOW),
    )
    regressed = SelfReflectiveAgent(max_iterations=3, target_score=4.9).run(TASK, CASE_NOTES)
    assert regressed.best is not None
    assert regressed.best.score == max(regressed.trajectory)
    assert regressed.best.text == "good draft", "must return the best, not the last"
    results["kept_best_on_regression"] = True

    # 3. Stops rewriting once it has stopped helping.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        "draft", judgement(MID),
        "barely different", judgement(MID),
        "still the same", judgement(MID),
    )
    plateaued = SelfReflectiveAgent(max_iterations=5, target_score=4.9).run(TASK, CASE_NOTES)
    assert plateaued.stop is Stop.no_improvement, plateaued.stop
    assert len(plateaued.attempts) == 2, "should not burn all five iterations"
    results["stopped_at_plateau"] = len(plateaued.attempts)

    # 4. The rewrite prompt carries the specific fixes, not just "do better".
    mock.PROVIDER.reset()
    mock.PROVIDER.queue("first", judgement(LOW), "second", judgement(HIGH))
    SelfReflectiveAgent(max_iterations=2).run(TASK, CASE_NOTES)
    rewrite_prompt = mock.PROVIDER.calls[2]["messages"][-1]["content"]
    assert "REQUIRED CHANGES" in rewrite_prompt
    assert "specific change for" in rewrite_prompt
    results["critique_is_specific"] = True

    # 5. The trajectory is persisted — improvement is a claim until it's logged.
    agg = aggregate()
    assert agg["runs"] >= 4
    results["runs_logged"] = agg["runs"]

    mock.PROVIDER.reset()
    return results
