"""End-to-end smoke for P1, called by `scripts/smoke.py`.

Exercises the failure mode this project is graded on, not just the happy path:
a malformed response must be repaired, and a tool that breaks its own contract
must be caught rather than passed through.
"""

from __future__ import annotations

import json

from agentcore import mock

from .agent import StructuredAgent
from .schemas import TicketTriage
from .tools import ToolContractError, build_account_tool, build_broken_tool

TICKET = """Subject: URGENT - checkout completely down

Our entire checkout has been failing since 09:00 UTC. Every customer hitting
the payment step gets a 500. We're an enterprise account (ACC-1001), roughly
12,000 users affected. This is costing us real money every minute. We want a
credit for the downtime — at least $5,000. Please escalate immediately."""

_VALID = {
    "severity": "critical",
    "category": "outage",
    "summary": "Checkout returns HTTP 500 at the payment step for all users since 09:00 UTC.",
    "customer_sentiment": -0.8,
    "affected_users": 12000,
    "action_items": [
        {
            "description": "Roll back the payment service to the last good deploy",
            "owner_team": "payments",
            "due_within_hours": 1,
        }
    ],
    "requires_human_review": True,
    "refund_amount_usd": 5000.0,
}


def smoke() -> dict:
    results: dict[str, object] = {}

    # 1. Happy path.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(json.dumps(_VALID))
    clean = StructuredAgent().extract(TICKET, TicketTriage)
    assert clean.ok, clean.error
    assert clean.value.severity.value == "critical"
    assert clean.attempts == 1
    results["clean_attempts"] = clean.attempts

    # 2. The graded failure mode: unusable output, then a repair turn.
    mock.PROVIDER.reset()
    bad = dict(_VALID, customer_sentiment=-45.0, action_items=[])  # two rule violations
    mock.PROVIDER.queue(json.dumps(bad), json.dumps(_VALID))
    repaired = StructuredAgent().extract(TICKET, TicketTriage)
    assert repaired.ok, repaired.error
    assert repaired.repaired, "expected the repair loop to have run"
    results["repaired_attempts"] = repaired.attempts

    # 3. Exhaustion is an explicit failure, never a half-filled object.
    mock.PROVIDER.reset()
    mock.PROVIDER.queue(*["definitely not json"] * 3)
    gave_up = StructuredAgent(max_attempts=3).extract(TICKET, TicketTriage)
    assert not gave_up.ok
    assert gave_up.value is None, "a failed extraction must not return a partial object"
    results["gave_up_cleanly"] = True

    # 4. Tool input validation — recoverable, handed back to the model.
    tool = build_account_tool()
    assert tool.invoke({"account_id": "ACC-1001"}).ok
    bad_args = tool.invoke({"account_id": "not-an-account"})
    assert not bad_args.ok and bad_args.model_recoverable
    results["tool_input_rejected"] = True

    # 5. Tool output validation — our bug, raised rather than passed through.
    try:
        build_broken_tool().invoke({"account_id": "ACC-1001"})
        raise AssertionError("a contract violation must not pass silently")
    except ToolContractError:
        results["tool_contract_enforced"] = True

    mock.PROVIDER.reset()
    return results
