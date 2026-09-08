"""End-to-end smoke for P2.

Checks the three behaviours that distinguish grounded RAG from a demo: it
answers what the corpus covers, it *refuses* what the corpus does not, and it
catches a citation that does not support its claim.
"""

from __future__ import annotations

import json

from agentcore import mock

from .agent import Action, RagAgent, index_corpus
from .grounding import Claim, verify


def smoke() -> dict:
    results: dict[str, object] = {}
    results["chunks_indexed"] = index_corpus(force=True)

    # 1. A question the corpus answers, with citations that check out.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        json.dumps(
            {
                "answerable": True,
                "self_confidence": 0.9,
                "claims": [
                    {
                        "text": "Customers on monthly plans may request a full refund "
                                "within 14 days of a charge.",
                        "citation_ids": ["refunds#0"],
                    }
                ],
            }
        )
    )
    answered = RagAgent().ask("How long do customers have to request a refund?")
    assert answered.action is Action.answer, f"expected answer, got {answered.action}"
    assert answered.grounding.fully_grounded
    results["answer_confidence"] = round(answered.confidence.score, 3)

    # 2. The graded behaviour: the corpus does not cover it, so refuse rather
    #    than produce a fluent guess.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(json.dumps({"answerable": False, "self_confidence": 0.05,
                                    "claims": []}))
    refused = RagAgent(allow_search_fallback=False).ask(
        "What does the Enterprise plan cost per seat per year?"
    )
    assert refused.action is Action.refuse, f"expected refusal, got {refused.action}"
    assert not refused.answered
    results["refused_uncovered"] = True

    # 3. A confidently wrong citation — right topic, wrong number.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(
        json.dumps(
            {
                "answerable": True,
                "self_confidence": 0.95,
                "claims": [
                    {
                        "text": "Customers may request a full refund within 60 days "
                                "of a charge.",
                        "citation_ids": ["refunds#0"],
                    }
                ],
            }
        )
    )
    wrong = RagAgent(allow_search_fallback=False).ask("What is the refund window?")
    assert wrong.action is not Action.answer, "an unsupported figure must not be served"
    results["caught_wrong_figure"] = True

    # 4. A fabricated source id is disqualifying, not a near miss.
    grounding = verify(
        [Claim(text="Refunds take 90 days.", citation_ids=["totally-made-up#7"])],
        {"refunds#0": "Customers may request a full refund within 14 days."},
    )
    assert grounding.fabricated_citations == ["totally-made-up#7"]
    results["caught_fabricated_citation"] = True

    # 5. Search fallback fires for an uncovered question when enabled.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(json.dumps({"answerable": False, "self_confidence": 0.1,
                                    "claims": []}))
    fallback = RagAgent(allow_search_fallback=True).ask(
        "Do you sign a HIPAA business associate agreement?"
    )
    assert fallback.action is Action.fallback_search
    assert fallback.search_results
    results["search_fallback"] = True

    mock.PROVIDER.reset()
    return results
