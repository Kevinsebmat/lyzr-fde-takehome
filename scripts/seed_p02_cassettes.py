#!/usr/bin/env python3
"""Seed P2's demo cassettes.

Without these, a fresh clone with no API key can only demonstrate refusal —
the mock provider's schema stub returns `answerable: false` — which makes the
demo look like the system refuses everything.

Each cassette is keyed on the request the agent will actually build, via
`build_prompt`, so the retrieval step still runs for real and only the model's
reply is replayed.

Run after changing the corpus, the system prompt, or the chunker:
    python scripts/seed_p02_cassettes.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import conftest  # noqa: E402,F401

os.environ.setdefault("LLM_PROVIDER", "mock")

from agentcore import mock, store  # noqa: E402
from agentcore.llm import _schema_system_prompt  # noqa: E402
from p02_rag_citations.agent import (  # noqa: E402
    SYSTEM,
    DraftAnswer,
    build_prompt,
    index_corpus,
)
from p02_rag_citations.corpus import COLLECTION  # noqa: E402

MODEL = "claude-opus-5"

#: question -> the grounded answer a good model gives from this corpus.
#: Every claim here is checked against the real chunks below before writing,
#: so a corpus edit that invalidates one fails loudly instead of shipping a
#: cassette whose citations no longer verify.
ANSWERS: dict[str, dict] = {
    "How long do customers have to request a refund on a monthly plan?": {
        "answerable": True,
        "self_confidence": 0.95,
        "claims": [
            {
                "text": "Customers on monthly plans may request a full refund within "
                        "14 days of a charge.",
                "citation_ids": ["refunds#0"],
            },
            {
                "text": "Requests made after 14 days are declined automatically by billing.",
                "citation_ids": ["refunds#0"],
            },
        ],
    },
    "What uptime do we commit to and which plans does it cover?": {
        "answerable": True,
        "self_confidence": 0.92,
        "claims": [
            {
                "text": "Northwind Cloud commits to 99.9% monthly uptime for the API "
                        "and dashboard on Business and Enterprise plans.",
                "citation_ids": ["sla#0"],
            },
            {
                "text": "Starter plans carry no uptime commitment.",
                "citation_ids": ["sla#0"],
            },
        ],
    },
    "How long are audit logs kept?": {
        "answerable": True,
        "self_confidence": 0.94,
        "claims": [
            {
                "text": "Audit logs are retained for 400 days.",
                "citation_ids": ["retention#0"],
            },
            {
                "text": "Application logs are retained for 30 days.",
                "citation_ids": ["retention#0"],
            },
        ],
    },
    "Which SSO protocols work on the Business plan?": {
        "answerable": True,
        "self_confidence": 0.9,
        "claims": [
            {
                "text": "SAML 2.0 and OIDC are supported on Business and Enterprise plans.",
                "citation_ids": ["sso#0"],
            },
            {
                "text": "SCIM 2.0 directory sync is Enterprise only.",
                "citation_ids": ["sso#0"],
            },
        ],
    },
    "What happens when a customer exceeds their rate limit?": {
        "answerable": True,
        "self_confidence": 0.88,
        "claims": [
            {
                "text": "A token bucket allows short bursts up to twice the sustained "
                        "limit for a maximum of ten seconds.",
                "citation_ids": ["ratelimits#1"],
            },
            {
                "text": "After that, requests receive HTTP 429 with a Retry-After header.",
                "citation_ids": ["ratelimits#1"],
            },
        ],
    },
    # A question the corpus genuinely cannot answer. Cassetted too, so the
    # demo's refusal is a recorded model decision rather than a stub artefact.
    "What does the Enterprise plan cost per seat per year?": {
        "answerable": False,
        "self_confidence": 0.02,
        "claims": [],
    },
    "Can Northwind Cloud be deployed on-premise in our own datacentre?": {
        "answerable": False,
        "self_confidence": 0.03,
        "claims": [],
    },
    "Do you sign a HIPAA business associate agreement?": {
        "answerable": False,
        "self_confidence": 0.02,
        "claims": [],
    },
    "What is the CEO's direct phone number?": {
        "answerable": False,
        "self_confidence": 0.0,
        "claims": [],
    },
}


def main() -> int:
    index_corpus(force=True)
    embedder = store.Embedder()
    full_system = _schema_system_prompt(SYSTEM, DraftAnswer)
    written = 0
    problems: list[str] = []

    for question, payload in ANSWERS.items():
        hits = store.search(COLLECTION, question, embedder, k=4)
        retrieved_ids = {h.id for h in hits}

        # Guard: a cassette citing a chunk that retrieval no longer returns
        # would be replayed as a fabricated citation and refuse. Catch it here.
        for claim in payload["claims"]:
            for cid in claim["citation_ids"]:
                if cid not in retrieved_ids:
                    problems.append(
                        f"{question[:50]!r}: cites {cid}, but retrieval returned "
                        f"{sorted(retrieved_ids)}"
                    )

        DraftAnswer.model_validate(payload)  # fail now, not at replay time

        body = json.dumps(payload, indent=2)
        prompt = build_prompt(question, hits)
        key = mock.request_key(MODEL, full_system, [{"role": "user", "content": prompt}],
                               DraftAnswer)
        mock.PROVIDER.record(
            key,
            {
                "text": body,
                "input_tokens": len(prompt) // 4,
                "output_tokens": len(body) // 4,
                "stop_reason": "end_turn",
            },
        )
        written += 1
        print(f"  {key}  {question[:60]}")

    if problems:
        print("\nPROBLEMS — the corpus and these cassettes have drifted apart:")
        for p in problems:
            print(f"  {p}")
        return 1

    print(f"\nwrote {written} cassettes to {mock.fixtures_dir()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
