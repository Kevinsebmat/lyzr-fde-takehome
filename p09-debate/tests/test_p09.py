"""P9 tests.

The theme is honesty about agreement. A debate that always produces a
confident answer has not aggregated anything — it has laundered one opinion
through four API calls.
"""

from __future__ import annotations

import pytest
from agentcore import mock, store
from p09_debate.debate import PERSONAS, DebateSystem, Verdict
from p09_debate.smoke import KEYS, QUESTION, critique, proposal, vote


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("AGENTCORE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AGENTCORE_TRACE_FILE", str(tmp_path / "t.jsonl"))
    mock.PROVIDER.reset()
    yield
    mock.PROVIDER.reset()
    store.reset()


def run(votes, flaws=(), note=""):
    """Four proposals, a critique, the given votes, then a synthesis."""
    mock.PROVIDER.queue(
        *[proposal(f"Recommendation from {k}.") for k in KEYS],
        critique(*flaws, note=note),
        *[vote(v) for v in votes],
        "Final recommendation text.",
    )
    return DebateSystem().run(QUESTION)


# ---------- independence ----------


def test_proposers_never_see_each_others_work():
    """Anchoring destroys the independence that makes four agents worth paying
    for — several correlated opinions are one opinion at four times the price."""
    run(["pragmatist"] * 4)
    proposer_prompts = [c["messages"][-1]["content"] for c in mock.PROVIDER.calls[:4]]
    assert all("Recommendation from" not in p for p in proposer_prompts)


def test_each_proposer_gets_a_distinct_perspective():
    """Same prompt four times samples one distribution; different priorities
    produce arguments that can actually conflict."""
    run(["pragmatist"] * 4)
    systems = [c["system"] for c in mock.PROVIDER.calls[:4]]
    assert len(set(systems)) == 4
    for persona in PERSONAS:
        assert any(persona.brief in s for s in systems)


def test_voters_do_see_the_proposals_and_the_critique():
    run(["pragmatist"] * 4, flaws=[("risk", "too slow", False)])
    vote_prompt = mock.PROVIDER.calls[5]["messages"][-1]["content"]
    assert "PROPOSALS" in vote_prompt
    assert "too slow" in vote_prompt


# ---------- verdicts ----------


def test_unanimity_is_consensus():
    result = run(["pragmatist"] * 4)
    assert result.verdict is Verdict.consensus
    assert result.agreement == 1.0
    assert result.decided


def test_a_split_vote_is_a_bare_majority():
    result = run(["pragmatist", "pragmatist", "risk", "economist"])
    assert result.verdict is Verdict.majority
    assert result.winner == "pragmatist"
    assert result.agreement == 0.5


def test_a_tie_is_contested_not_broken_by_ordering():
    """Breaking a tie arbitrarily manufactures a decision nobody made."""
    result = run(["pragmatist", "pragmatist", "risk", "risk"])
    assert result.verdict is Verdict.contested
    assert result.winner is None
    assert not result.decided


def test_confidence_falls_as_agreement_falls():
    unanimous = run(["pragmatist"] * 4)
    mock.PROVIDER.reset()
    split = run(["pragmatist", "pragmatist", "risk", "economist"])
    assert split.confidence < unanimous.confidence


def test_confidence_is_discounted_by_flaws_in_the_winner():
    clean = run(["pragmatist"] * 4)
    mock.PROVIDER.reset()
    flawed = run(["pragmatist"] * 4,
                 flaws=[("pragmatist", "needs a new service", False)])
    assert flawed.confidence < clean.confidence


# ---------- the critic ----------


def test_a_fatal_flaw_in_the_winner_blocks_the_decision():
    result = run(["pragmatist"] * 4,
                 flaws=[("pragmatist", "drops events under backpressure", True)])
    assert result.verdict is Verdict.blocked
    assert result.winner is None


def test_blocking_does_not_promote_an_unvoted_runner_up():
    """Nobody voted for it. Promoting it invents a mandate."""
    result = run(["pragmatist"] * 4,
                 flaws=[("pragmatist", "unsafe", True)])
    assert result.winner is None
    assert "disqualifying flaw" in result.recommendation


def test_a_fatal_flaw_in_a_loser_does_not_block():
    result = run(["pragmatist"] * 4, flaws=[("risk", "unsafe", True)])
    assert result.verdict is Verdict.consensus


def test_a_critic_that_fails_everything_is_treated_as_uninformative():
    """Marking everything fatal blocks the decision without informing it."""
    result = run(
        ["pragmatist"] * 4,
        flaws=[(k, "flawed", True) for k in KEYS],
    )
    assert result.verdict is not Verdict.blocked
    assert any("uninformative" in n for n in result.notes)


def test_debate_continues_when_the_critic_fails():
    mock.PROVIDER.queue(
        *[proposal(f"From {k}.") for k in KEYS],
        *["not json"] * 3,
        *[vote("pragmatist") for _ in KEYS],
        "Final text.",
    )
    result = DebateSystem().run(QUESTION)
    assert result.decided
    assert any("critic failed" in n for n in result.notes)


# ---------- vote integrity ----------


def test_a_vote_for_a_nonexistent_proposal_is_discarded():
    """Guessing what they meant invents a result."""
    result = run(["hallucinated", "risk", "risk", "risk"])
    assert result.winner == "risk"
    assert sum(result.tally.values()) == 3
    assert any("not a proposal" in n for n in result.notes)


def test_no_valid_votes_fails_rather_than_picking_one():
    result = run(["nope"] * 4)
    assert result.verdict is Verdict.failed
    assert result.winner is None


def test_tally_covers_every_proposal_including_unvoted_ones():
    result = run(["pragmatist"] * 4)
    assert set(result.tally) == set(KEYS)
    assert result.tally["risk"] == 0


# ---------- degradation ----------


def test_one_failed_proposer_shrinks_the_debate_not_ends_it():
    mock.PROVIDER.queue(
        proposal("From pragmatist."), *["not json"] * 3,
        proposal("From economist."), proposal("From user_advocate."),
        critique(),
        vote("pragmatist"), vote("pragmatist"), vote("pragmatist"),
        "Final.",
    )
    result = DebateSystem().run(QUESTION)
    assert len(result.proposals) == 3
    assert result.decided


def test_fewer_than_two_proposals_is_not_a_debate():
    mock.PROVIDER.queue(
        proposal("Only one."), *["not json"] * 9,
    )
    result = DebateSystem().run(QUESTION)
    assert result.verdict is Verdict.failed
    assert "Too few" in result.recommendation


def test_synthesis_failure_falls_back_to_the_winning_proposal():
    from agentcore import LLM, Budget

    llm = LLM(project="p09", budget=Budget(limit_usd=10.0))
    mock.PROVIDER.queue(
        *[proposal(f"Recommendation from {k}.") for k in KEYS],
        critique(),
        *[vote("pragmatist") for _ in KEYS],
    )
    system = DebateSystem(llm=llm)

    original = llm.complete

    def failing(*args, **kwargs):
        raise RuntimeError("synthesis unavailable")

    llm.complete = failing
    result = system.run(QUESTION)
    llm.complete = original

    assert result.winner == "pragmatist"
    assert "Recommendation from pragmatist" in result.recommendation


# ---------- cost shape ----------


def test_proposers_use_the_cheaper_tier():
    """Four proposals on the top tier is where a debate's cost runs away."""
    run(["pragmatist"] * 4)
    proposer_models = {c["model"] for c in mock.PROVIDER.calls[:4]}
    synthesis_model = mock.PROVIDER.calls[-1]["model"]
    assert proposer_models == {"claude-sonnet-5"}
    assert synthesis_model == "claude-opus-5"
