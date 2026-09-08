"""P2 tests.

Almost all of these are about *not* answering. A RAG system that answers
correctly when the corpus is good is table stakes; what is being tested here is
the behaviour at the edges, because that is where hallucination lives.
"""

from __future__ import annotations

import json

import pytest
from agentcore import mock, store
from p02_rag_citations.agent import Action, RagAgent, index_corpus
from p02_rag_citations.corpus import ANSWERABLE, COLLECTION, DOCUMENTS, all_chunks, chunk_document
from p02_rag_citations.grounding import (
    Claim,
    Grounding,
    score_confidence,
    support_score,
    verify,
)
from p02_rag_citations.search import web_search

REFUND_TEXT = (
    "Refund and Credit Policy — Standard refund window\n\n"
    "Customers on monthly plans may request a full refund within 14 days of a "
    "charge. Requests after 14 days are declined automatically by billing."
)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("AGENTCORE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AGENTCORE_TRACE_FILE", str(tmp_path / "t.jsonl"))
    mock.PROVIDER.reset()
    yield
    mock.PROVIDER.reset()
    store.reset()


def draft(claims, answerable=True, confidence=0.9) -> str:
    return json.dumps(
        {"answerable": answerable, "self_confidence": confidence, "claims": claims}
    )


# ---------- chunking ----------


def test_chunks_keep_sections_whole():
    """Fixed-width chunking splits a rule mid-sentence and then cites half of
    it — the failure that makes citations untrustworthy."""
    chunks = chunk_document(DOCUMENTS[0])
    assert len(chunks) >= 4
    assert all(c.text.count("\n\n") >= 1 for c in chunks)
    window = next(c for c in chunks if "Standard refund window" in c.text)
    assert "14 days" in window.text
    assert "declined automatically" in window.text, "the rule must not be split"


def test_every_chunk_carries_its_heading_and_source():
    for c in all_chunks():
        assert c.title and c.heading
        assert "#" in c.source, "a citation needs a deep link, not just a filename"


def test_indexing_is_idempotent():
    first = index_corpus(force=True)
    assert index_corpus() == first
    assert store.count(COLLECTION) == first


# ---------- support scoring ----------


def test_matching_claim_scores_high():
    score, reason = support_score(
        "Monthly plans may request a full refund within 14 days of a charge.", REFUND_TEXT
    )
    assert score >= 0.5 and reason == "supported"


def test_wrong_figure_is_caught_even_when_the_prose_matches():
    """The highest-value check: right topic, wrong number."""
    score, reason = support_score(
        "Monthly plans may request a full refund within 60 days of a charge.", REFUND_TEXT
    )
    assert score <= 0.25
    assert "60" in reason


def test_number_formatting_does_not_cause_false_alarms():
    evidence = "Refunds above $10,000 require written approval from the VP of Finance."
    score, _ = support_score("Refunds above $10,000 require VP of Finance approval.", evidence)
    assert score >= 0.5


def test_unrelated_claim_scores_low():
    score, reason = support_score("Audit logs are retained for 400 days.", REFUND_TEXT)
    assert score < 0.5
    assert reason != "supported"


def test_paraphrase_of_the_source_still_verifies():
    """Over-refusing is a failure too: a system that rejects correct answers
    gets switched off as fast as one that hallucinates."""
    score, _ = support_score("Refunds are available within 14 days.", REFUND_TEXT)
    assert score >= 0.5


# ---------- citation verification ----------


def test_verified_claim_is_marked_grounded():
    g = verify([Claim("Refunds are available within 14 days.", ["refunds#0"])],
               {"refunds#0": REFUND_TEXT})
    assert g.fully_grounded
    assert g.grounded_fraction == 1.0


def test_uncited_claim_is_unsupported():
    g = verify([Claim("Refunds are available within 14 days.", [])], {"refunds#0": REFUND_TEXT})
    assert not g.fully_grounded
    assert g.unsupported[0].reason == "no citation"


def test_fabricated_source_is_never_a_near_miss():
    g = verify([Claim("Refunds take 14 days.", ["invented#9"])], {"refunds#0": REFUND_TEXT})
    assert g.fabricated_citations == ["invented#9"]
    assert not g.fully_grounded


def test_claim_citing_a_real_but_irrelevant_chunk_fails():
    g = verify(
        [Claim("SCIM sync runs every four hours.", ["refunds#0"])],
        {"refunds#0": REFUND_TEXT},
    )
    assert g.unsupported, "a citation that does not support the claim must not pass"


def test_partial_grounding_is_reported_as_a_fraction():
    g = verify(
        [
            Claim("Monthly plans may request a full refund within 14 days.", ["refunds#0"]),
            Claim("Audit logs are kept for 400 days.", ["refunds#0"]),
        ],
        {"refunds#0": REFUND_TEXT},
    )
    assert g.grounded_fraction == 0.5


# ---------- confidence ----------


def test_fabricated_citation_caps_confidence():
    g = Grounding(fabricated_citations=["nope#1"], grounded_fraction=1.0)
    c = score_confidence([0.9], g, self_reported=1.0)
    assert c.score <= 0.2, "a fabricated source is disqualifying whatever else scored"
    assert any("fabricated" in r for r in c.reasons)


def test_confidence_is_low_when_nothing_was_retrieved():
    c = score_confidence([], Grounding(), self_reported=0.9)
    assert c.score < 0.3
    assert "nothing retrieved from the corpus" in c.reasons


def test_model_self_confidence_cannot_carry_a_bad_answer():
    """The model's opinion of its own work is the weakest signal, by design."""
    c = score_confidence([0.2], Grounding(grounded_fraction=0.0), self_reported=1.0)
    assert c.score < 0.45


# ---------- end to end ----------


def test_answers_a_covered_question():
    mock.PROVIDER.queue(
        draft([{"text": "Customers on monthly plans may request a full refund within "
                        "14 days of a charge.", "citation_ids": ["refunds#0"]}])
    )
    result = RagAgent().ask(ANSWERABLE[0])
    assert result.action is Action.answer
    assert result.answered
    assert "14 days" in result.answer


def test_refuses_when_the_model_says_the_corpus_does_not_cover_it():
    mock.PROVIDER.queue(draft([], answerable=False, confidence=0.05))
    result = RagAgent(allow_search_fallback=False).ask("What does Enterprise cost per seat?")
    assert result.action is Action.refuse
    assert not result.answered


def test_refuses_a_confidently_wrong_figure():
    """The model is sure. The evidence disagrees. The evidence wins."""
    mock.PROVIDER.queue(
        draft(
            [{"text": "Customers may request a full refund within 60 days of a charge.",
              "citation_ids": ["refunds#0"]}],
            confidence=0.99,
        )
    )
    result = RagAgent(allow_search_fallback=False).ask("What is the refund window?")
    assert result.action is not Action.answer


def test_refusal_explains_itself():
    mock.PROVIDER.queue(draft([], answerable=False, confidence=0.0))
    result = RagAgent(allow_search_fallback=False).ask("What is the CEO's phone number?")
    assert result.action is Action.refuse
    assert result.answer, "a refusal with no reason is not actionable for the user"


def test_unsupported_claims_are_dropped_not_hedged():
    """Keeping an unsupported claim behind a hedge still ships it as fact."""
    mock.PROVIDER.queue(
        draft(
            [
                {"text": "Customers on monthly plans may request a full refund within "
                         "14 days of a charge.", "citation_ids": ["refunds#0"]},
                {"text": "Refunds are always processed within one hour.",
                 "citation_ids": ["refunds#0"]},
            ],
            confidence=0.7,
        )
    )
    result = RagAgent(allow_search_fallback=False).ask(ANSWERABLE[0])
    if result.action is Action.answer_caveated:
        assert "one hour" not in result.answer, "the unsupported claim must be removed"
        assert "could not be verified" in result.answer


def test_no_llm_call_when_retrieval_finds_nothing():
    """Calling the model with no evidence produces a fluent answer from its own
    parameters — the exact failure this project prevents."""
    mock.PROVIDER.reset()
    agent = RagAgent(allow_search_fallback=False, retrieval_floor=0.99)
    result = agent.ask("something entirely unrelated to the corpus")
    assert result.action is Action.refuse
    assert mock.PROVIDER.calls == [], "must short-circuit before spending a token"


def test_citations_record_which_were_actually_used():
    mock.PROVIDER.queue(
        draft([{"text": "Customers on monthly plans may request a full refund within "
                        "14 days of a charge.", "citation_ids": ["refunds#0"]}])
    )
    result = RagAgent().ask(ANSWERABLE[0])
    used = [c for c in result.citations if c["used"]]
    assert used and used[0]["id"] == "refunds#0"
    assert all("source" in c for c in result.citations)


def test_malformed_generation_refuses_rather_than_crashing():
    mock.PROVIDER.queue(*["not json at all"] * 3)
    result = RagAgent(allow_search_fallback=False).ask(ANSWERABLE[0])
    assert result.action is Action.refuse


# ---------- search fallback ----------


def test_fallback_fires_for_an_uncovered_question():
    mock.PROVIDER.queue(draft([], answerable=False, confidence=0.1))
    result = RagAgent(allow_search_fallback=True).ask(
        "Do you sign a HIPAA business associate agreement?"
    )
    assert result.action is Action.fallback_search
    assert result.search_results


def test_fallback_output_is_labelled_as_unverified():
    mock.PROVIDER.queue(draft([], answerable=False, confidence=0.1))
    result = RagAgent(allow_search_fallback=True).ask("HIPAA business associate agreement?")
    assert "not verified" in result.answer.lower()


def test_fallback_with_no_results_refuses():
    """An empty fallback must degrade to refusal, not to a softer answer."""
    mock.PROVIDER.queue(draft([], answerable=False, confidence=0.1))
    result = RagAgent(allow_search_fallback=True).ask(
        "What is the CEO's direct phone number?"
    )
    assert result.action is Action.refuse


def test_search_returns_nothing_when_unconfigured(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert web_search("anything") == []
