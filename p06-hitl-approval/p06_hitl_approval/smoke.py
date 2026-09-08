"""End-to-end smoke for P6.

The load-bearing check is the durable pause: a request created by one agent
object is resumed by a completely different one, with the first thrown away.
That stands in for a process restart, which is what an in-memory `await` does
not survive.
"""

from __future__ import annotations

import json

from agentcore import mock

from . import approvals
from .agent import ApprovalAgent, Outcome
from .approvals import Status


def proposal(action, arguments, confidence=0.95, needs_human=False, rationale="because") -> str:
    return json.dumps(
        {
            "action": action,
            "arguments": arguments,
            "rationale": rationale,
            "confidence": confidence,
            "needs_human": needs_human,
        }
    )


def smoke() -> dict:
    results: dict[str, object] = {}
    mock.PROVIDER.reset()

    # 1. Small, confident, in policy — acts without asking.
    mock.PROVIDER.queue(proposal("issue_refund", {"order_id": "ORD-1", "amount": 120.0}))
    auto = ApprovalAgent().handle("Refund $120 for a duplicate charge on ORD-1.")
    assert auto.outcome is Outcome.executed, auto.outcome
    results["auto_executed"] = True

    # 2. Over the limit — escalates regardless of the model's confidence.
    mock.PROVIDER.queue(
        proposal("issue_refund", {"order_id": "ORD-2", "amount": 48200.0}, confidence=0.99)
    )
    escalated = ApprovalAgent().handle("Refund $48,200 to Wexler Industries for the outage.")
    assert escalated.outcome is Outcome.awaiting_approval, escalated.outcome
    assert escalated.request.risk.value == "high"
    results["escalated_high_value"] = True

    request_id = escalated.request.id

    # 3. The approver sees the arguments, the rationale, and why it escalated.
    rendered = escalated.request.rendered
    assert "48200" in rendered and "ESCALATED BECAUSE" in rendered
    assert "ORIGINAL REQUEST" in rendered
    results["approver_sees_context"] = True

    # 4. THE point: a different agent object resumes it. The one that created
    #    the request is gone, as it would be after a deploy.
    del escalated
    approvals.decide(request_id, approved=True, actor="finance-lead",
                     note="matches the SLA credit calculation")
    resumed = ApprovalAgent().resume(request_id)
    assert resumed.outcome is Outcome.executed, resumed.outcome
    results["resumed_in_a_new_process"] = True

    # 5. Replaying the resume must not act twice.
    again = ApprovalAgent().resume(request_id)
    assert again.outcome is Outcome.executed
    assert approvals.get(request_id).status is Status.executed
    executions = [e for e in approvals.audit_trail(request_id) if e["event"] == "executed"]
    assert len(executions) == 1, "a replayed resume must be idempotent"
    results["resume_is_idempotent"] = True

    # 6. A rejection stops the action.
    mock.PROVIDER.queue(proposal("delete_account", {"account_id": "ACC-9"}))
    risky = ApprovalAgent().handle("Delete account ACC-9 as requested.")
    assert risky.outcome is Outcome.awaiting_approval
    approvals.decide(risky.request.id, approved=False, actor="security",
                     note="no written confirmation from the account owner")
    stopped = ApprovalAgent().resume(risky.request.id)
    assert stopped.outcome is Outcome.rejected
    assert "security" in stopped.result
    results["rejection_stops_the_action"] = True

    # 7. The audit trail can reconstruct the whole decision.
    trail = approvals.audit_trail(request_id)
    events = [e["event"] for e in trail]
    assert events == ["requested", "approved", "executed"], events
    assert any(e["actor"] == "finance-lead" for e in trail)
    results["audit_complete"] = True

    # 8. A retried run must not queue the same action twice.
    mock.PROVIDER.reset()
    for _ in range(2):
        mock.PROVIDER.queue(
            proposal("issue_refund", {"order_id": "ORD-7", "amount": 9000.0})
        )
        ApprovalAgent().handle("Refund $9,000 on ORD-7.")
    duplicates = [r for r in approvals.pending() if r.arguments.get("order_id") == "ORD-7"]
    assert len(duplicates) == 1, "an approver seeing the same request twice trusts the queue less"
    results["deduplicated"] = True

    mock.PROVIDER.reset()
    return results
