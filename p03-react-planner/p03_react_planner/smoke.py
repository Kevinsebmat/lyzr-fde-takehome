"""End-to-end smoke for P3.

Every check here is a termination check. A ReAct agent that reaches the right
answer on a good day has proved nothing about the failure mode it is graded on.
"""

from __future__ import annotations

import json

from agentcore import mock

from .agent import Outcome, ReActAgent

TASK = "Does order ORD-4417 need manager approval for a refund?"


def step(thought, done=False, action=None, action_input=None, final_answer=None) -> str:
    return json.dumps(
        {
            "thought": thought,
            "done": done,
            "action": action,
            "action_input": action_input,
            "final_answer": final_answer,
        }
    )


def reflection(progressed=True, can_answer=False, critique="ok") -> str:
    return json.dumps(
        {"progressed": progressed, "critique": critique, "can_answer_now": can_answer}
    )


def smoke() -> dict:
    results: dict[str, object] = {}

    # 1. Solves a real multi-step task.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        step("Look up the order first.", action="lookup_order", action_input="ORD-4417"),
        reflection(True, critique="found the order total"),
        step("Now check the refund policy.", action="policy", action_input="refund"),
        reflection(True, can_answer=True, critique="policy retrieved"),
        step(
            "The order is $12,480, over the $1,000 threshold.",
            done=True,
            final_answer="Yes. ORD-4417 totals $12,480, and orders of $1,000 or "
                         "more require manager approval.",
        ),
    )
    solved = ReActAgent().run(TASK)
    assert solved.outcome is Outcome.solved, solved.outcome
    assert "manager approval" in solved.answer
    results["solved_in_steps"] = len(solved.steps)

    # 2. Loop detection: the same call, over and over.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        *[
            x
            for _ in range(10)
            for x in (
                step("Try the archive.", action="search_archive", action_input="ORD-4417"),
                reflection(False, critique="empty result again"),
            )
        ]
    )
    looped = ReActAgent(max_iterations=20, max_stalls=99).run(TASK)
    assert looped.outcome is Outcome.loop_detected, looped.outcome
    assert len(looped.steps) <= 4, "must catch the loop early, not at the ceiling"
    results["loop_caught_at_step"] = len(looped.steps)

    # 3. The hard ceiling holds even when every action is different.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        *[
            x
            for i in range(30)
            for x in (
                step(f"Try input {i}.", action="search_archive", action_input=f"query-{i}"),
                reflection(True, critique="different input, keep going"),
            )
        ]
    )
    capped = ReActAgent(max_iterations=4).run(TASK)
    assert capped.outcome is Outcome.max_iterations, capped.outcome
    assert len(capped.steps) == 4
    results["ceiling_held"] = True

    # 4. Self-critique stops a wandering agent before the ceiling.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        *[
            x
            for i in range(10)
            for x in (
                step(f"Another angle {i}.", action="search_archive", action_input=f"a{i}"),
                reflection(False, critique="still nothing useful"),
            )
        ]
    )
    stalled = ReActAgent(max_iterations=20, max_stalls=2).run(TASK)
    assert stalled.outcome is Outcome.no_progress, stalled.outcome
    results["stalled_at_step"] = len(stalled.steps)

    # 5. Graceful degradation: partial work is kept, not thrown away.
    assert looped.degraded or looped.answer, "an early exit must still answer"
    assert looped.answer, "terminating with nothing discards the work already done"
    results["degraded_gracefully"] = True

    # 6. A failing tool is an observation, not a crash.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        step("Try the CRM.", action="legacy_crm", action_input="ORD-4417"),
        reflection(False, critique="the CRM is down"),
        step("Use the order lookup instead.", action="lookup_order",
             action_input="ORD-4417"),
        reflection(True, can_answer=True, critique="recovered"),
        step("Answering now.", done=True, final_answer="ORD-4417 totals $12,480."),
    )
    recovered = ReActAgent().run(TASK)
    assert recovered.outcome is Outcome.solved
    assert any("ERROR" in (s.observation or "") for s in recovered.steps)
    results["recovered_from_tool_failure"] = True

    mock.PROVIDER.reset()
    return results
