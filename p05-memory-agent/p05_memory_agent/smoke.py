"""End-to-end smoke for P5.

The two checks that matter: memory survives a session boundary, and a corrected
fact stops being retrievable. A store that hands back a stale fact with
confidence is worse than one with no memory at all.
"""

from __future__ import annotations

import json

from agentcore import mock

from .agent import MemoryAgent
from .memory import MemoryStore

USER = "wexler-ops"


def extraction(*facts) -> str:
    return json.dumps({"facts": list(facts)})


def fact(kind, text, supersedes=None) -> dict:
    return {"kind": kind, "text": text, "supersedes": supersedes}


def smoke() -> dict:
    results: dict[str, object] = {}
    mock.PROVIDER.reset()

    # --- session one -------------------------------------------------------
    agent = MemoryAgent(USER)
    mock.PROVIDER.queue(
        "Good to meet you.",
        extraction(
            fact("identity", "Works at Wexler Industries, an enterprise account."),
            fact("constraint", "All data must stay in the EU region."),
        ),
    )
    first = agent.chat("I'm at Wexler Industries. All our data has to stay in the EU.")
    assert len(first.learned) == 2, first.learned
    results["learned_first_turn"] = len(first.learned)

    mock.PROVIDER.queue("Noted.", extraction())
    nothing = agent.chat("Thanks, that's helpful!")
    assert nothing.learned == [], "pleasantries are not memories"
    results["ignored_pleasantry"] = True

    # --- session boundary --------------------------------------------------
    agent.end_session()
    assert MemoryStore(USER).buffer() == [], "the transcript should be gone"
    assert MemoryStore(USER).all_facts(), "the facts should not be"

    # --- session two: a brand new agent object -----------------------------
    reborn = MemoryAgent(USER)
    mock.PROVIDER.reset()
    mock.PROVIDER.queue("Yes — EU region, as before.", extraction())
    recalled = reborn.chat("Remind me where our data is hosted?")
    assert recalled.recalled, "cross-session recall failed — this is the whole project"
    assert any("EU" in s.fact.text for s in recalled.recalled)
    results["recalled_across_sessions"] = len(recalled.recalled)

    # --- the graded case: a fact changes -----------------------------------
    memory = MemoryStore(USER)
    eu_fact = next(f for f in memory.all_facts() if "EU" in f.text)
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        "Understood — updated to US.",
        extraction(fact("constraint", "All data must stay in the US region.",
                        supersedes=eu_fact.id)),
    )
    updated = reborn.chat("We've migrated. Everything is in the US region now.")
    assert updated.superseded == [eu_fact.id], updated.superseded

    after = MemoryStore(USER).recall("where is our data hosted")
    texts = [s.fact.text for s in after]
    assert any("US" in t for t in texts), "the new fact must be retrievable"
    assert not any("EU region" in t for t in texts), \
        "the superseded fact must never reach a prompt again"
    results["superseded_correctly"] = True

    # The old fact is still readable for audit — just not retrievable.
    audited = MemoryStore(USER).all_facts(include_superseded=True)
    assert any(not f.active for f in audited)
    results["audit_trail_intact"] = True

    # --- one user's memory must not leak into another's --------------------
    other = MemoryStore("unrelated-user")
    assert other.recall("where is our data hosted") == []
    results["no_cross_user_leak"] = True

    # --- compression -------------------------------------------------------
    chatty = MemoryAgent("chatty-user")
    chatty.memory.buffer_token_budget = 40
    mock.PROVIDER.reset()
    for i in range(4):
        # Exactly the two calls every turn makes. The compression call fires
        # only on some turns, so it is left to the mock's synthesized reply
        # rather than queued — queueing it unconditionally would leave an
        # unconsumed response to be misread as the next turn's answer.
        mock.PROVIDER.queue(f"reply {i} " + "padding text " * 20, extraction())
        chatty.chat(f"message {i} " + "padding text " * 20)
    assert chatty.memory.summary(), "buffer should have been compressed"
    assert chatty.memory.buffer_tokens() <= 400
    results["compressed"] = True

    mock.PROVIDER.reset()
    return results
