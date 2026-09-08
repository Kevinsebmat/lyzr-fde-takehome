"""End-to-end smoke for P9.

The checks that matter are about honesty: a tie is reported as a tie, a
disqualified winner does not silently promote the runner-up, and proposers
never see each other's work.
"""

from __future__ import annotations

import json

from agentcore import mock

from .debate import DebateSystem, Verdict

QUESTION = (
    "Should we move our document ingestion pipeline from nightly batch to "
    "streaming?"
)


def proposal(rec, confidence=0.8) -> str:
    return json.dumps(
        {
            "recommendation": rec,
            "reasoning": "Detailed reasoning supporting this recommendation.",
            "strongest_counterargument": "The main argument against this position.",
            "confidence": confidence,
        }
    )


def critique(*flaws, note="") -> str:
    return json.dumps(
        {
            "flaws": [
                {"proposal_key": k, "issue": issue, "fatal": fatal}
                for k, issue, fatal in flaws
            ],
            "note": note,
        }
    )


def vote(choice, reason="best on the merits") -> str:
    return json.dumps({"choice": choice, "reason": reason})


KEYS = ("pragmatist", "risk", "economist", "user_advocate")


def smoke() -> dict:
    results: dict[str, object] = {}

    # 1. A clear consensus.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        *[proposal(f"Recommendation from {k}.") for k in KEYS],
        critique(("risk", "Underestimates the migration window.", False)),
        *[vote("pragmatist") for _ in KEYS],
        "Final recommendation: stage the migration over two quarters.",
    )
    consensus = DebateSystem().run(QUESTION)
    assert consensus.verdict is Verdict.consensus, consensus.verdict
    assert consensus.winner == "pragmatist"
    assert consensus.agreement == 1.0
    results["consensus_confidence"] = consensus.confidence

    # 2. Proposers must not see each other's work — anchoring destroys the
    #    independence that makes running four agents worth the money.
    proposer_prompts = [c["messages"][-1]["content"] for c in mock.PROVIDER.calls[:4]]
    assert all("Recommendation from" not in p for p in proposer_prompts), \
        "a proposal leaked into another proposer's prompt"
    results["proposers_independent"] = True

    # 3. A split vote is reported as contested, not resolved by tie-break.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        *[proposal(f"Recommendation from {k}.") for k in KEYS],
        critique(),
        vote("pragmatist"), vote("pragmatist"), vote("risk"), vote("risk"),
        "The team is split. This decision needs a human owner.",
    )
    tied = DebateSystem().run(QUESTION)
    assert tied.verdict is Verdict.contested, tied.verdict
    assert tied.winner is None, "breaking a tie by ordering manufactures a decision"
    results["tie_is_reported"] = True

    # 4. A fatal flaw in the winner blocks rather than promoting the runner-up.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        *[proposal(f"Recommendation from {k}.") for k in KEYS],
        critique(("pragmatist", "Drops events silently under backpressure.", True)),
        *[vote("pragmatist") for _ in KEYS],
    )
    blocked = DebateSystem().run(QUESTION)
    assert blocked.verdict is Verdict.blocked, blocked.verdict
    assert blocked.winner is None
    assert "disqualifying flaw" in blocked.recommendation
    results["fatal_flaw_blocks"] = True

    # 5. A bare majority reports lower confidence than unanimity.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        *[proposal(f"Recommendation from {k}.") for k in KEYS],
        critique(),
        vote("pragmatist"), vote("pragmatist"), vote("risk"), vote("economist"),
        "A narrow call.",
    )
    narrow = DebateSystem().run(QUESTION)
    assert narrow.verdict is Verdict.majority, narrow.verdict
    assert narrow.confidence < consensus.confidence, \
        "half the room disagreeing must not read as confidently as unanimity"
    results["majority_confidence"] = narrow.confidence

    # 6. A vote for a nonexistent proposal is discarded, not guessed at.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        *[proposal(f"Recommendation from {k}.") for k in KEYS],
        critique(),
        vote("hallucinated_option"), vote("risk"), vote("risk"), vote("risk"),
        "Clear enough.",
    )
    invalid = DebateSystem().run(QUESTION)
    assert invalid.winner == "risk"
    assert sum(invalid.tally.values()) == 3, "the invalid vote must not be counted"
    assert any("not a proposal" in n for n in invalid.notes)
    results["invalid_vote_discarded"] = True

    # 7. One proposer failing shrinks the debate rather than ending it.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        proposal("From pragmatist."), "not json", "not json", "not json",
        proposal("From economist."), proposal("From user_advocate."),
        critique(),
        vote("pragmatist"), vote("pragmatist"), vote("pragmatist"),
        "Proceeding with three advisors.",
    )
    degraded = DebateSystem().run(QUESTION)
    assert degraded.decided
    assert any("failed and was dropped" in n for n in degraded.notes)
    results["survived_proposer_failure"] = len(degraded.proposals)

    mock.PROVIDER.reset()
    return results
