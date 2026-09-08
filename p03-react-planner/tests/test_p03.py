"""P3 tests — almost entirely about stopping."""

from __future__ import annotations

import pytest
from agentcore import LLM, Budget, mock, store
from p03_react_planner.agent import Outcome, ReActAgent
from p03_react_planner.smoke import reflection, step
from p03_react_planner.tools import Registry, Tool, default_registry

TASK = "Does order ORD-4417 need manager approval for a refund?"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("AGENTCORE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AGENTCORE_TRACE_FILE", str(tmp_path / "t.jsonl"))
    mock.PROVIDER.reset()
    yield
    mock.PROVIDER.reset()
    store.reset()


def repeat(action, arg, n, progressed=False):
    """n identical steps, each followed by a reflection."""
    out = []
    for _ in range(n):
        out.append(step("trying", action=action, action_input=arg))
        out.append(reflection(progressed, critique="no change"))
    return out


# ---------- termination ----------


def test_identical_action_is_a_loop():
    mock.PROVIDER.queue(*repeat("search_archive", "x", 10))
    result = ReActAgent(max_iterations=20, max_stalls=99).run(TASK)
    assert result.outcome is Outcome.loop_detected


def test_a_single_retry_is_allowed():
    """max_repeats=2 permits one legitimate retry; three is a pattern."""
    mock.PROVIDER.queue(
        *repeat("search_archive", "x", 2, progressed=True),
        step("done", done=True, final_answer="finished"),
    )
    result = ReActAgent(max_iterations=10).run(TASK)
    assert result.outcome is Outcome.solved


def test_case_and_whitespace_do_not_defeat_loop_detection():
    mock.PROVIDER.queue(
        step("a", action="search_archive", action_input="ORD-4417"),
        reflection(True),
        step("b", action="search_archive", action_input="  ord-4417  "),
        reflection(True),
        step("c", action="search_archive", action_input="Ord-4417"),
        reflection(True),
        step("d", action="search_archive", action_input="ORD-4417 "),
        reflection(True),
    )
    result = ReActAgent(max_iterations=10, max_stalls=99).run(TASK)
    assert result.outcome is Outcome.loop_detected


def test_hard_ceiling_holds_when_every_action_differs():
    """Loop detection can't help here — this is what the cap is for."""
    mock.PROVIDER.queue(
        *[
            x
            for i in range(30)
            for x in (step(f"t{i}", action="search_archive", action_input=f"q{i}"),
                      reflection(True))
        ]
    )
    result = ReActAgent(max_iterations=3).run(TASK)
    assert result.outcome is Outcome.max_iterations
    assert len(result.steps) == 3


def test_self_critique_stops_a_wanderer_before_the_ceiling():
    mock.PROVIDER.queue(
        *[
            x
            for i in range(20)
            for x in (step(f"t{i}", action="search_archive", action_input=f"q{i}"),
                      reflection(False, critique="nothing useful"))
        ]
    )
    result = ReActAgent(max_iterations=20, max_stalls=2).run(TASK)
    assert result.outcome is Outcome.no_progress
    assert len(result.steps) < 20, "critique must stop it well before the ceiling"


def test_progress_resets_the_stall_counter():
    mock.PROVIDER.queue(
        step("a", action="search_archive", action_input="1"),
        reflection(False),
        step("b", action="lookup_order", action_input="ORD-4417"),
        reflection(True, critique="found it"),
        step("c", action="search_archive", action_input="2"),
        reflection(False),
        step("d", done=True, final_answer="done"),
    )
    result = ReActAgent(max_iterations=10, max_stalls=2).run(TASK)
    assert result.outcome is Outcome.solved


def test_budget_ceiling_terminates_the_run():
    llm = LLM(project="p03", budget=Budget(limit_usd=0.0001))
    mock.PROVIDER.queue(*repeat("search_archive", "x", 10, progressed=True))
    agent = ReActAgent(llm=llm, max_iterations=50)
    result = agent.run(TASK)
    assert result.outcome is Outcome.budget_exhausted


def test_no_action_and_not_done_counts_as_a_stall():
    """Nothing will change next turn, so it is not a step."""
    mock.PROVIDER.queue(*[step("thinking...", done=False) for _ in range(10)])
    result = ReActAgent(max_iterations=10, max_stalls=2).run(TASK)
    assert result.outcome is Outcome.no_progress


def test_malformed_planner_output_stops_rather_than_loops():
    mock.PROVIDER.queue(*["not json"] * 3)
    result = ReActAgent().run(TASK)
    assert result.outcome is Outcome.failed
    assert result.answer


# ---------- graceful degradation ----------


@pytest.mark.parametrize("outcome_setup", ["loop", "ceiling", "stall"])
def test_every_early_exit_still_answers(outcome_setup):
    """Discarding six tool calls because the seventh didn't happen is worse
    than a partial answer that says it is partial."""
    if outcome_setup == "loop":
        mock.PROVIDER.queue(*repeat("search_archive", "x", 10))
        agent = ReActAgent(max_iterations=20, max_stalls=99)
    elif outcome_setup == "ceiling":
        mock.PROVIDER.queue(
            *[x for i in range(20)
              for x in (step(f"t{i}", action="search_archive", action_input=f"q{i}"),
                        reflection(True))]
        )
        agent = ReActAgent(max_iterations=3)
    else:
        mock.PROVIDER.queue(
            *[x for i in range(20)
              for x in (step(f"t{i}", action="search_archive", action_input=f"q{i}"),
                        reflection(False))]
        )
        agent = ReActAgent(max_iterations=20, max_stalls=2)

    result = agent.run(TASK)
    assert not result.solved
    assert result.answer, "an early exit must never return nothing"
    assert result.reason, "and must say why it stopped"


def test_partial_answers_are_labelled_partial():
    mock.PROVIDER.queue(
        step("look up", action="lookup_order", action_input="ORD-4417"),
        reflection(True),
        *repeat("search_archive", "x", 10),
    )
    result = ReActAgent(max_iterations=20, max_stalls=99).run(TASK)
    assert not result.solved
    assert "[Partial:" in result.answer or "could not complete" in result.answer


def test_run_never_raises_on_termination():
    for kwargs in ({"max_iterations": 1}, {"max_stalls": 1}, {"max_repeats": 0}):
        mock.PROVIDER.reset()
        mock.PROVIDER.queue(*repeat("search_archive", "x", 20))
        ReActAgent(**kwargs).run(TASK)  # must not raise


# ---------- tools ----------


def test_tool_failure_becomes_an_observation():
    registry = default_registry()
    assert registry.get("legacy_crm")("anything").startswith("ERROR:")


def test_unknown_tool_names_the_real_ones():
    mock.PROVIDER.queue(
        step("guess", action="nonexistent_tool", action_input="x"),
        reflection(False, critique="no such tool"),
        step("done", done=True, final_answer="ok"),
    )
    result = ReActAgent().run(TASK)
    observation = result.steps[0].observation
    assert "no tool named" in observation
    assert "lookup_order" in observation, "tell the agent what does exist"


def test_calculator_rejects_code_injection():
    """eval on model-supplied text is an RCE hole; the allowlist closes it."""
    calc = default_registry().get("calculator")
    assert calc("2 + 2") == "4"
    assert "ERROR" in calc("__import__('os').system('echo pwned')")
    assert "ERROR" in calc("open('/etc/passwd').read()")


def test_agent_recovers_from_a_failing_tool():
    mock.PROVIDER.queue(
        step("crm", action="legacy_crm", action_input="ORD-4417"),
        reflection(False, critique="down"),
        step("fallback", action="lookup_order", action_input="ORD-4417"),
        reflection(True, can_answer=True),
        step("answer", done=True, final_answer="ORD-4417 totals $12,480."),
    )
    result = ReActAgent().run(TASK)
    assert result.outcome is Outcome.solved


def test_registry_describes_its_tools():
    r = Registry().add(Tool("t", "does a thing", lambda x: x))
    assert "t: does a thing" in r.describe()


# ---------- tracing ----------


def test_steps_are_recorded_for_replay():
    mock.PROVIDER.queue(
        step("look up", action="lookup_order", action_input="ORD-4417"),
        reflection(True, critique="got it"),
        step("done", done=True, final_answer="yes"),
    )
    result = ReActAgent().run(TASK)
    first = result.steps[0].as_dict()
    assert first["action"] == "lookup_order"
    assert first["observation"] and first["critique"]
