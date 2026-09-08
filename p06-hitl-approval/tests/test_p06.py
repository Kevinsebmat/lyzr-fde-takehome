"""P6 tests.

The durability tests matter most. An in-memory approval gate passes every
functional test and then loses every pending decision on the next deploy.
"""

from __future__ import annotations

import time

import pytest
from agentcore import mock, store
from p06_hitl_approval import approvals
from p06_hitl_approval.agent import (
    AUTO_APPROVE_LIMIT,
    ApprovalAgent,
    Outcome,
    Proposal,
    apply_policy,
)
from p06_hitl_approval.approvals import Risk, Status
from p06_hitl_approval.smoke import proposal


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("AGENTCORE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AGENTCORE_TRACE_FILE", str(tmp_path / "t.jsonl"))
    mock.PROVIDER.reset()
    yield
    mock.PROVIDER.reset()
    store.reset()


def make_proposal(**kwargs) -> Proposal:
    base = {
        "action": "issue_refund",
        "arguments": {"amount": 100.0},
        "rationale": "duplicate charge",
        "confidence": 0.95,
        "needs_human": False,
    }
    return Proposal(**{**base, **kwargs})


def drop_connections():
    """Simulate a process restart: forget every cached connection."""
    conns = getattr(store._LOCAL, "conns", {})
    for conn in list(conns.values()):
        conn.close()
    conns.clear()


# ---------- policy: the rules the model cannot argue with ----------


def test_small_confident_request_needs_no_human():
    assert not apply_policy(make_proposal(arguments={"amount": 100.0})).escalate


def test_amount_over_the_limit_escalates():
    verdict = apply_policy(make_proposal(arguments={"amount": AUTO_APPROVE_LIMIT + 1}))
    assert verdict.escalate and verdict.risk is Risk.medium


def test_large_amount_is_high_risk():
    verdict = apply_policy(make_proposal(arguments={"amount": 48_200.0}))
    assert verdict.risk is Risk.high


def test_irreversible_action_always_escalates():
    verdict = apply_policy(make_proposal(action="delete_account", arguments={}))
    assert verdict.escalate and verdict.risk is Risk.high


def test_cancelled_order_escalates():
    verdict = apply_policy(
        make_proposal(arguments={"amount": 10.0, "order_status": "cancelled"})
    )
    assert verdict.escalate and verdict.risk is Risk.high


def test_unparseable_amount_escalates_rather_than_being_ignored():
    verdict = apply_policy(make_proposal(arguments={"amount": "lots"}))
    assert verdict.escalate, "an amount we cannot read is a reason to ask, not to proceed"


def test_high_model_confidence_cannot_override_policy():
    """A policy the model can talk itself out of is a suggestion, not a policy."""
    mock.PROVIDER.queue(
        proposal("issue_refund", {"amount": 48_200.0}, confidence=1.0, needs_human=False)
    )
    result = ApprovalAgent().handle("Refund $48,200.")
    assert result.outcome is Outcome.awaiting_approval


def test_low_confidence_escalates_even_within_policy():
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 50.0}, confidence=0.4))
    result = ApprovalAgent().handle("Refund something, maybe $50?")
    assert result.outcome is Outcome.awaiting_approval
    assert any("confidence" in r for r in result.escalation_reasons)


def test_agent_can_ask_for_review_itself():
    mock.PROVIDER.queue(
        proposal("close_ticket", {}, confidence=0.99, needs_human=True)
    )
    result = ApprovalAgent().handle("Close the ticket.")
    assert result.outcome is Outcome.awaiting_approval
    assert any("asked for a human" in r for r in result.escalation_reasons)


# ---------- the durable pause ----------


def test_pending_request_survives_a_process_restart():
    """The whole project. An in-memory await loses this."""
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 48_200.0}))
    request_id = ApprovalAgent().handle("Big refund").request.id

    drop_connections()

    recovered = approvals.get(request_id)
    assert recovered is not None
    assert recovered.status is Status.pending


def test_a_different_agent_object_can_resume():
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 48_200.0}))
    request_id = ApprovalAgent().handle("Big refund").request.id

    approvals.decide(request_id, approved=True, actor="finance-lead")
    drop_connections()

    result = ApprovalAgent().resume(request_id)
    assert result.outcome is Outcome.executed


def test_resume_is_idempotent():
    """A replayed resume must not issue the refund twice."""
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 48_200.0}))
    request_id = ApprovalAgent().handle("Big refund").request.id
    approvals.decide(request_id, approved=True, actor="lead")

    ApprovalAgent().resume(request_id)
    ApprovalAgent().resume(request_id)
    ApprovalAgent().resume(request_id)

    executed = [e for e in approvals.audit_trail(request_id) if e["event"] == "executed"]
    assert len(executed) == 1


def test_resuming_a_pending_request_does_not_act():
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 48_200.0}))
    request_id = ApprovalAgent().handle("Big refund").request.id
    result = ApprovalAgent().resume(request_id)
    assert result.outcome is Outcome.awaiting_approval


def test_rejection_stops_the_action():
    mock.PROVIDER.queue(proposal("delete_account", {"account_id": "ACC-9"}))
    request_id = ApprovalAgent().handle("Delete ACC-9").request.id
    approvals.decide(request_id, approved=False, actor="security", note="no confirmation")
    result = ApprovalAgent().resume(request_id)
    assert result.outcome is Outcome.rejected
    assert "no confirmation" in result.result


def test_resuming_an_unknown_request_raises():
    with pytest.raises(KeyError):
        ApprovalAgent().resume("req_nope")


# ---------- approver authority ----------


def test_approver_supplied_context_overrides_the_agents_arguments():
    """They are the authority the pause existed to consult."""
    executed = {}
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 48_200.0, "order_id": "ORD-2"}))
    agent = ApprovalAgent(
        executor=lambda action, args: executed.update(args) or f"did {action}"
    )
    request_id = agent.handle("Big refund").request.id
    approvals.decide(
        request_id, approved=True, actor="finance-lead",
        supplied_context={"amount": 9640.0, "approval_ref": "FIN-2211"},
    )
    agent.resume(request_id)
    assert executed["amount"] == 9640.0, "the human's correction must win"
    assert executed["approval_ref"] == "FIN-2211"
    assert executed["order_id"] == "ORD-2", "unchanged arguments are preserved"


def test_rendered_request_is_frozen_at_creation():
    """A later template change must not rewrite what was consented to."""
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 48_200.0}))
    request = ApprovalAgent().handle("Refund the outage credit").request
    stored = approvals.get(request.id)
    assert stored.rendered == request.rendered
    assert "ESCALATED BECAUSE" in stored.rendered
    assert "ORIGINAL REQUEST" in stored.rendered


def test_approver_sees_arguments_and_rationale_not_just_an_action_name():
    """Deciding from a bare action name is rubber-stamping."""
    mock.PROVIDER.queue(
        proposal("issue_refund", {"amount": 48_200.0}, rationale="SLA credit for the outage")
    )
    rendered = ApprovalAgent().handle("Refund").request.rendered
    assert "48200" in rendered
    assert "SLA credit for the outage" in rendered
    assert "CONFIDENCE" in rendered


# ---------- decision integrity ----------


def test_a_settled_request_cannot_be_re_decided():
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 48_200.0}))
    request_id = ApprovalAgent().handle("Big refund").request.id

    approvals.decide(request_id, approved=False, actor="security")
    approvals.decide(request_id, approved=True, actor="someone-else")

    assert approvals.get(request_id).status is Status.rejected
    assert approvals.get(request_id).decided_by == "security"


def test_an_overturn_attempt_is_audited():
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 48_200.0}))
    request_id = ApprovalAgent().handle("Big refund").request.id
    approvals.decide(request_id, approved=False, actor="security")
    approvals.decide(request_id, approved=True, actor="someone-else")

    events = [e["event"] for e in approvals.audit_trail(request_id)]
    assert "decision_rejected" in events, "a quiet overturn attempt must leave a trace"


def test_executing_an_unapproved_request_raises():
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 48_200.0}))
    request_id = ApprovalAgent().handle("Big refund").request.id
    with pytest.raises(ValueError, match="not approved"):
        approvals.mark_executed(request_id, "sneaky")


def test_duplicate_requests_are_deduplicated():
    for _ in range(3):
        mock.PROVIDER.queue(proposal("issue_refund", {"order_id": "ORD-7", "amount": 9000.0}))
        ApprovalAgent().handle("Refund ORD-7")
    assert len(approvals.pending()) == 1


def test_different_arguments_are_not_deduplicated():
    for amount in (9000.0, 9500.0):
        mock.PROVIDER.queue(proposal("issue_refund", {"order_id": "ORD-7", "amount": amount}))
        ApprovalAgent().handle("Refund ORD-7")
    assert len(approvals.pending()) == 2


# ---------- expiry ----------


def test_a_stale_request_expires():
    """Approving a two-week-old refund against today's account state is not
    the decision the approver thinks they are making."""
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 48_200.0}))
    request_id = ApprovalAgent(ttl_seconds=0).handle("Big refund").request.id
    time.sleep(0.01)
    assert approvals.get(request_id).status is Status.expired


def test_an_expired_request_cannot_be_approved():
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 48_200.0}))
    request_id = ApprovalAgent(ttl_seconds=0).handle("Big refund").request.id
    time.sleep(0.01)
    approvals.decide(request_id, approved=True, actor="lead")
    assert approvals.get(request_id).status is Status.expired


# ---------- audit ----------


def test_the_trail_reconstructs_the_full_lifecycle():
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 48_200.0}))
    request_id = ApprovalAgent().handle("Big refund").request.id
    approvals.decide(request_id, approved=True, actor="finance-lead", note="checked")
    ApprovalAgent().resume(request_id)

    trail = approvals.audit_trail(request_id)
    assert [e["event"] for e in trail] == ["requested", "approved", "executed"]
    approved = trail[1]
    assert approved["actor"] == "finance-lead"
    assert approved["detail"]["note"] == "checked"
    assert "seconds_pending" in approved["detail"]


def test_audit_entries_in_the_same_instant_do_not_collide():
    """Losing an entry to a key collision is a silent hole in the evidence."""
    for i in range(50):
        approvals.audit("req_x", f"event_{i}", "tester")
    assert len(approvals.audit_trail("req_x")) == 50


def test_stats_summarise_the_queue():
    mock.PROVIDER.queue(proposal("issue_refund", {"amount": 48_200.0}))
    request_id = ApprovalAgent().handle("Big refund").request.id
    approvals.decide(request_id, approved=True, actor="lead")
    s = approvals.stats()
    assert s["total"] == 1
    assert s["by_status"]["approved"] == 1
    assert s["mean_seconds_to_decide"] is not None


def test_malformed_proposal_fails_closed():
    """When the agent cannot form a well-defined action, it must not act."""
    mock.PROVIDER.queue(*["not json"] * 3)
    result = ApprovalAgent().handle("do something")
    assert result.outcome is Outcome.failed
    assert approvals.pending() == []
