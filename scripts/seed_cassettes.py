#!/usr/bin/env python3
"""Seed the demo cassettes for every project.

Recording from a live model (`AGENTCORE_RECORD=1` with a key) is the normal
path. This exists for the demo inputs that ship in the repo, so a fresh clone
with no API key shows a real answer rather than schema-shaped placeholder text.

Each cassette is keyed on the request the agent will actually build, so the
retrieval and policy steps still run for real — only the model's reply is
replayed. Where a cassette depends on retrieval, the seeder checks that its
citations still match what retrieval returns and fails loudly if a corpus edit
has left them stale.

    python scripts/seed_cassettes.py            # all
    python scripts/seed_cassettes.py p02 p06    # a subset
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
os.environ.setdefault("AGENTCORE_DB", str(ROOT / ".smoke" / "seed.db"))

from agentcore import mock, store  # noqa: E402
from agentcore.llm import _schema_system_prompt  # noqa: E402

MODEL = "claude-opus-5"
_problems: list[str] = []


def record(model: str, system: str | None, prompt: str, schema, payload) -> str:
    """Write one cassette, keyed exactly as `LLM._call` will key the request."""
    full_system = _schema_system_prompt(system, schema) if schema else system
    if schema is not None:
        schema.model_validate(payload)  # fail now, not at replay time
    body = json.dumps(payload, indent=2) if not isinstance(payload, str) else payload
    key = mock.request_key(model, full_system, [{"role": "user", "content": prompt}], schema)
    mock.PROVIDER.record(
        key,
        {
            "text": body,
            "input_tokens": max(50, len(prompt) // 4),
            "output_tokens": max(20, len(body) // 4),
            "stop_reason": "end_turn",
        },
    )
    return key


# ---------- p01 ----------


def seed_p01() -> int:
    from p01_structured_output.agent import SYSTEM
    from p01_structured_output.schemas import TicketTriage

    ticket = (ROOT / "p01-structured-output" / "sample_ticket.txt").read_text()
    record(
        MODEL, SYSTEM, ticket, TicketTriage,
        {
            "severity": "critical",
            "category": "outage",
            "summary": "Checkout returns HTTP 500 at the payment step for every "
                       "customer since 09:00 UTC.",
            "customer_sentiment": -0.85,
            "affected_users": 12000,
            "action_items": [
                {"description": "Roll back the payment service to the last known "
                                "good deploy",
                 "owner_team": "payments", "due_within_hours": 1},
                {"description": "Confirm the $5,000 downtime credit with the "
                                "enterprise account manager",
                 "owner_team": "billing", "due_within_hours": 24},
            ],
            "requires_human_review": True,
            "refund_amount_usd": 5000.0,
        },
    )
    return 1


# ---------- p02 ----------

P02_ANSWERS: dict[str, dict] = {
    "How long do customers have to request a refund on a monthly plan?": {
        "answerable": True, "self_confidence": 0.95,
        "claims": [
            {"text": "Customers on monthly plans may request a full refund within "
                     "14 days of a charge.", "citation_ids": ["refunds#0"]},
            {"text": "Requests made after 14 days are declined automatically by billing.",
             "citation_ids": ["refunds#0"]},
        ],
    },
    "What uptime do we commit to and which plans does it cover?": {
        "answerable": True, "self_confidence": 0.92,
        "claims": [
            {"text": "Northwind Cloud commits to 99.9% monthly uptime for the API and "
                     "dashboard on Business and Enterprise plans.",
             "citation_ids": ["sla#0"]},
            {"text": "Starter plans carry no uptime commitment.", "citation_ids": ["sla#0"]},
        ],
    },
    "How long are audit logs kept?": {
        "answerable": True, "self_confidence": 0.94,
        "claims": [
            {"text": "Audit logs are retained for 400 days.", "citation_ids": ["retention#0"]},
            {"text": "Application logs are retained for 30 days.",
             "citation_ids": ["retention#0"]},
        ],
    },
    "Which SSO protocols work on the Business plan?": {
        "answerable": True, "self_confidence": 0.9,
        "claims": [
            {"text": "SAML 2.0 and OIDC are supported on Business and Enterprise plans.",
             "citation_ids": ["sso#0"]},
            {"text": "SCIM 2.0 directory sync is Enterprise only.", "citation_ids": ["sso#0"]},
        ],
    },
    "What happens when a customer exceeds their rate limit?": {
        "answerable": True, "self_confidence": 0.88,
        "claims": [
            {"text": "A token bucket allows short bursts up to twice the sustained limit "
                     "for a maximum of ten seconds.", "citation_ids": ["ratelimits#1"]},
            {"text": "After that, requests receive HTTP 429 with a Retry-After header.",
             "citation_ids": ["ratelimits#1"]},
        ],
    },
    # Deliberately uncovered. Cassetted so the demo's refusal is a recorded
    # model decision rather than an artefact of the mock's default stub.
    "What does the Enterprise plan cost per seat per year?":
        {"answerable": False, "self_confidence": 0.02, "claims": []},
    "Can Northwind Cloud be deployed on-premise in our own datacentre?":
        {"answerable": False, "self_confidence": 0.03, "claims": []},
    "Do you sign a HIPAA business associate agreement?":
        {"answerable": False, "self_confidence": 0.02, "claims": []},
    "What is the CEO's direct phone number?":
        {"answerable": False, "self_confidence": 0.0, "claims": []},
}


def seed_p02() -> int:
    from p02_rag_citations.agent import SYSTEM, DraftAnswer, build_prompt, index_corpus
    from p02_rag_citations.corpus import COLLECTION

    index_corpus(force=True)
    embedder = store.Embedder()

    for question, payload in P02_ANSWERS.items():
        hits = store.search(COLLECTION, question, embedder, k=4)
        retrieved = {h.id for h in hits}
        for claim in payload["claims"]:
            for cid in claim["citation_ids"]:
                if cid not in retrieved:
                    # Replayed, this would look like a fabricated citation and
                    # the agent would refuse. Better to fail here.
                    _problems.append(
                        f"p02 {question[:45]!r} cites {cid}; retrieval returned "
                        f"{sorted(retrieved)}"
                    )
        record(MODEL, SYSTEM, build_prompt(question, hits), DraftAnswer, payload)
    return len(P02_ANSWERS)


# ---------- p06 ----------

P06_CASES: dict[str, dict] = {
    "Refund $48,200 to Wexler Industries for the two-hour outage on ORD-2.": {
        "action": "issue_refund",
        "arguments": {"order_id": "ORD-2", "amount": 48200.0, "account": "Wexler Industries"},
        "rationale": "SLA credit for a 2h20m outage; 99.68% uptime against a 99.9% "
                     "commitment gives a 20% credit.",
        "confidence": 0.88,
        "needs_human": True,
    },
    "Refund $120 for a duplicate charge on ORD-1.": {
        "action": "issue_refund",
        "arguments": {"order_id": "ORD-1", "amount": 120.0},
        "rationale": "Clear duplicate charge, well inside the auto-approval limit.",
        "confidence": 0.96,
        "needs_human": False,
    },
    "Delete account ACC-9 as the customer requested over chat.": {
        "action": "delete_account",
        "arguments": {"account_id": "ACC-9"},
        "rationale": "Customer asked for deletion, but only verbally over chat.",
        "confidence": 0.55,
        "needs_human": True,
    },
}


def seed_p06() -> int:
    from p06_hitl_approval.agent import SYSTEM, Proposal

    for case, payload in P06_CASES.items():
        record(MODEL, SYSTEM, case, Proposal, payload)
    return len(P06_CASES)


SEEDERS = {"p01": seed_p01, "p02": seed_p02, "p06": seed_p06}


def main() -> int:
    wanted = sys.argv[1:] or list(SEEDERS)
    total = 0
    for slug in wanted:
        seeder = SEEDERS.get(slug)
        if seeder is None:
            print(f"  {slug}: no cassettes needed")
            continue
        n = seeder()
        total += n
        print(f"  {slug}: {n} cassettes")

    if _problems:
        print("\nPROBLEMS — cassettes and code have drifted apart:")
        for p in _problems:
            print(f"  {p}")
        return 1

    print(f"\nwrote {total} cassettes to {mock.fixtures_dir()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
