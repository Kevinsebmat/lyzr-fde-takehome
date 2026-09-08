#!/usr/bin/env python3
"""Write a cassette by hand for a known request.

Recording from a live model (`AGENTCORE_RECORD=1`) is the normal path. This
exists for the demo inputs that ship in the repo, so a fresh clone with no key
shows a real answer rather than schema-shaped placeholder text.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agentcore import mock  # noqa: E402
from agentcore.llm import _schema_system_prompt  # noqa: E402

import conftest  # noqa: E402,F401


def write(model: str, system: str | None, text: str, schema, payload: dict) -> Path:
    """Key must be computed exactly as `LLM._call` would compute it."""
    full_system = _schema_system_prompt(system, schema) if schema else system
    messages = [{"role": "user", "content": text}]
    key = mock.request_key(model, full_system, messages, schema)
    body = json.dumps(payload, indent=2)
    mock.PROVIDER.record(
        key,
        {
            "text": body,
            "input_tokens": 900 + len(text) // 4,
            "output_tokens": len(body) // 4,
            "stop_reason": "end_turn",
        },
    )
    return mock.fixtures_dir() / f"{key}.json"


if __name__ == "__main__":
    from p01_structured_output.agent import SYSTEM
    from p01_structured_output.schemas import TicketTriage

    ticket = (ROOT / "p01-structured-output" / "sample_ticket.txt").read_text()
    path = write(
        "claude-opus-5",
        SYSTEM,
        ticket,
        TicketTriage,
        {
            "severity": "critical",
            "category": "outage",
            "summary": "Checkout returns HTTP 500 at the payment step for every "
                       "customer since 09:00 UTC.",
            "customer_sentiment": -0.85,
            "affected_users": 12000,
            "action_items": [
                {
                    "description": "Roll back the payment service to the last known "
                                   "good deploy",
                    "owner_team": "payments",
                    "due_within_hours": 1,
                },
                {
                    "description": "Confirm the $5,000 downtime credit with the "
                                   "enterprise account manager",
                    "owner_team": "billing",
                    "due_within_hours": 24,
                },
            ],
            "requires_human_review": True,
            "refund_amount_usd": 5000.0,
        },
    )
    print(f"wrote {path}")
