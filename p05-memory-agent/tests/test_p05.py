"""P5 tests.

Concentrated on the two ways memory goes wrong in production: it forgets
across sessions, or it remembers something that is no longer true.
"""

from __future__ import annotations

import time

import pytest
from agentcore import mock, store
from p05_memory_agent.agent import MemoryAgent
from p05_memory_agent.memory import FactKind, MemoryStore
from p05_memory_agent.smoke import extraction, fact

USER = "u1"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("AGENTCORE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AGENTCORE_TRACE_FILE", str(tmp_path / "t.jsonl"))
    mock.PROVIDER.reset()
    yield
    mock.PROVIDER.reset()
    store.reset()


# ---------- store ----------


def test_facts_survive_a_new_store_object():
    """The minimum bar for 'cross-session'."""
    MemoryStore(USER).remember(FactKind.identity, "Works at Wexler Industries.")
    assert MemoryStore(USER).all_facts()


def test_clearing_the_buffer_keeps_the_facts():
    m = MemoryStore(USER)
    m.remember(FactKind.constraint, "Data must stay in the EU region.")
    m.append("user", "hello")
    m.clear_buffer()
    assert m.buffer() == []
    assert m.all_facts(), "ending a session must not erase what was learned"


def test_recall_finds_a_relevant_fact():
    m = MemoryStore(USER)
    m.remember(FactKind.constraint, "All data must stay in the EU region.")
    m.remember(FactKind.preference, "Prefers meetings on Tuesday mornings.")
    hits = m.recall("where is our data hosted")
    assert hits and "EU" in hits[0].fact.text


def test_recall_on_an_empty_store_is_empty():
    assert MemoryStore("nobody").recall("anything") == []


def test_recall_never_crosses_users():
    """A memory leak between users is a data breach, not a bug."""
    MemoryStore("alice").remember(FactKind.identity, "Alice works at Acme Corporation.")
    assert MemoryStore("bob").recall("where does alice work") == []


# ---------- supersession: the graded behaviour ----------


def test_superseded_fact_is_not_retrievable():
    m = MemoryStore(USER)
    old = m.remember(FactKind.constraint, "All data must stay in the EU region.")
    m.remember(FactKind.constraint, "All data must stay in the US region.", supersedes=old.id)

    texts = [s.fact.text for s in m.recall("where must our data stay")]
    assert any("US" in t for t in texts)
    assert not any("EU" in t for t in texts), \
        "a confidently stale fact is worse than no memory at all"


def test_superseded_fact_is_still_readable_for_audit():
    """'What did we believe on 3 March, and why' is a real incident question."""
    m = MemoryStore(USER)
    old = m.remember(FactKind.constraint, "Data in the EU region.")
    new = m.remember(FactKind.constraint, "Data in the US region.", supersedes=old.id)

    every = m.all_facts(include_superseded=True)
    stale = next(f for f in every if f.id == old.id)
    assert not stale.active
    assert stale.superseded_by == new.id


def test_active_facts_exclude_superseded():
    m = MemoryStore(USER)
    old = m.remember(FactKind.preference, "Prefers email.")
    m.remember(FactKind.preference, "Prefers Slack.", supersedes=old.id)
    assert len(m.all_facts()) == 1
    assert len(m.all_facts(include_superseded=True)) == 2


def test_superseding_an_unknown_id_still_stores_the_fact():
    """Dropping a real update because an id was wrong is the worse failure."""
    m = MemoryStore(USER)
    m.remember(FactKind.preference, "Prefers Slack.", supersedes="does-not-exist")
    assert len(m.all_facts()) == 1


def test_forget_is_a_hard_delete_unlike_supersede():
    """A correction leaves a trail; a right-to-erasure request must not."""
    m = MemoryStore(USER)
    f = m.remember(FactKind.identity, "Personal detail.")
    assert m.forget(f.id) is True
    assert m.all_facts(include_superseded=True) == []
    assert m.forget("nonexistent") is False


# ---------- relevance ----------


def test_recency_breaks_ties_between_equally_relevant_facts():
    m = MemoryStore(USER)
    old = m.remember(FactKind.preference, "Prefers Tuesday morning meetings.")
    row = store.kv_get(f"p05_facts::{USER}", old.id)
    row["created_at"] = time.time() - 400 * 86400  # over a year old
    store.kv_set(f"p05_facts::{USER}", old.id, row)
    recent = m.remember(FactKind.preference, "Prefers Tuesday afternoon meetings.")

    hits = m.recall("when do they prefer meetings")
    assert hits[0].fact.id == recent.id
    assert hits[0].recency > next(h for h in hits if h.fact.id == old.id).recency


def test_usage_is_tracked_so_dead_memory_is_visible():
    m = MemoryStore(USER)
    m.remember(FactKind.constraint, "All data must stay in the EU region.")
    assert m.stats()["never_used"] == 1
    m.recall("where is data hosted")
    assert m.stats()["never_used"] == 0


# ---------- compression ----------


def test_compression_keeps_recent_turns_verbatim():
    """Pronouns and follow-ups resolve against the last few turns."""
    m = MemoryStore(USER, buffer_token_budget=10)
    for i in range(10):
        m.append("user", f"message number {i}")
    m.compress("Earlier: ten messages about numbers.", keep_last=4)
    kept = m.buffer()
    assert len(kept) == 4
    assert kept[-1].content == "message number 9"
    assert "Earlier:" in m.summary()


def test_compression_accumulates_summaries():
    m = MemoryStore(USER)
    m.append("user", "a")
    m.compress("first summary", keep_last=0)
    m.append("user", "b")
    m.compress("second summary", keep_last=0)
    assert "first summary" in m.summary() and "second summary" in m.summary()


def test_needs_compression_tracks_the_budget():
    m = MemoryStore(USER, buffer_token_budget=20)
    assert not m.needs_compression()
    m.append("user", "x" * 500)
    assert m.needs_compression()


# ---------- the agent ----------


def test_durable_facts_are_learned():
    mock.PROVIDER.queue(
        "Noted.",
        extraction(fact("constraint", "All data must stay in the EU region.")),
    )
    result = MemoryAgent(USER).chat("Our data has to stay in the EU.")
    assert len(result.learned) == 1
    assert result.learned[0].kind is FactKind.constraint


def test_pleasantries_are_not_learned():
    """Storing every turn produces a store where facts are outnumbered by
    chit-chat, and retrieval then returns chit-chat."""
    mock.PROVIDER.queue("You're welcome.", extraction())
    result = MemoryAgent(USER).chat("Thanks, that's really helpful!")
    assert result.learned == []


def test_recalled_facts_reach_the_answering_prompt():
    MemoryStore(USER).remember(FactKind.constraint, "All data must stay in the EU region.")
    mock.PROVIDER.queue("It's in the EU.", extraction())
    MemoryAgent(USER).chat("Where is our data hosted?")
    prompt = mock.PROVIDER.calls[0]["messages"][-1]["content"]
    assert "REMEMBERED ABOUT THIS USER" in prompt
    assert "EU region" in prompt


def test_extraction_prompt_shows_existing_facts_with_ids():
    """Without ids the model can only add, and contradictions accumulate."""
    m = MemoryStore(USER)
    existing = m.remember(FactKind.constraint, "Data in the EU region.")
    mock.PROVIDER.queue("ok", extraction())
    MemoryAgent(USER).chat("anything")
    extract_prompt = mock.PROVIDER.calls[1]["messages"][-1]["content"]
    assert existing.id in extract_prompt


def test_a_failed_extraction_does_not_fail_the_turn():
    """Memory is an enhancement; losing it costs one fact, failing the turn
    costs the conversation."""
    mock.PROVIDER.queue("Here's your answer.", *["not json"] * 3)
    result = MemoryAgent(USER).chat("tell me something")
    assert result.reply == "Here's your answer."
    assert result.learned == []


def test_agent_supersedes_through_the_full_turn():
    m = MemoryStore(USER)
    old = m.remember(FactKind.constraint, "All data must stay in the EU region.")
    mock.PROVIDER.queue(
        "Updated.",
        extraction(fact("constraint", "All data must stay in the US region.",
                        supersedes=old.id)),
    )
    result = MemoryAgent(USER).chat("We've moved everything to the US.")
    assert result.superseded == [old.id]
    assert not any("EU" in s.fact.text for s in MemoryStore(USER).recall("data region"))


def test_a_new_agent_object_recalls_the_previous_session():
    mock.PROVIDER.queue(
        "Noted.", extraction(fact("identity", "Works at Wexler Industries."))
    )
    first = MemoryAgent(USER)
    first.chat("I'm at Wexler Industries.")
    first.end_session()

    mock.PROVIDER.reset()
    mock.PROVIDER.queue("Wexler Industries.", extraction())
    second = MemoryAgent(USER).chat("Where do I work again?")
    assert second.recalled, "this is the entire point of the project"


def test_stats_report_the_shape_of_the_store():
    m = MemoryStore(USER)
    old = m.remember(FactKind.preference, "Prefers email.")
    m.remember(FactKind.preference, "Prefers Slack.", supersedes=old.id)
    s = m.stats()
    assert s["facts_active"] == 1 and s["facts_superseded"] == 1
