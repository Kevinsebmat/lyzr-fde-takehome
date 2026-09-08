"""The approval queue and the audit trail.

The pause is **durable**, not an in-memory `await`. That distinction is the
whole project. An agent that blocks a coroutine waiting for a human works
perfectly in a demo and loses every pending decision the first time the process
restarts — and a deploy during business hours is not an edge case. So the
paused state is a row: the agent stops, the row survives, and any process can
pick it up.

The audit trail is append-only. An approval record you can edit is not
evidence, and "who approved the $48,000 refund, when, and what were they
shown" is precisely the question a compliance review asks. Every record stores
the *rendered request the approver actually saw*, so a later change to the
prompt template cannot rewrite history.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from agentcore import store

log = logging.getLogger("p06.approvals")

REQUESTS_NAMESPACE = "p06_requests"
AUDIT_NAMESPACE = "p06_audit"

#: A stale approval is a security problem: the world moves on while a request
#: sits in a queue, and approving a two-week-old refund against today's account
#: state is not the decision the approver thought they were making.
DEFAULT_TTL_SECONDS = 24 * 3600


class Status(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"
    executed = "executed"
    cancelled = "cancelled"


class Risk(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


@dataclass
class ApprovalRequest:
    id: str
    action: str
    arguments: dict
    reason: str
    risk: Risk
    #: Exactly what the approver was shown. Frozen at creation so a later
    #: template change cannot retroactively alter what was consented to.
    rendered: str
    status: Status = Status.pending
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    decided_at: float | None = None
    decided_by: str | None = None
    decision_note: str = ""
    #: Extra facts the approver supplied, merged into the resumed context.
    supplied_context: dict = field(default_factory=dict)
    executed_at: float | None = None
    result: str | None = None

    @property
    def expired(self) -> bool:
        return self.status is Status.pending and time.time() > self.expires_at

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["status"] = self.status.value
        d["risk"] = self.risk.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ApprovalRequest:
        d = dict(d)
        d["status"] = Status(d["status"])
        d["risk"] = Risk(d["risk"])
        return cls(**d)

    def fingerprint(self) -> str:
        """Identity of the action, so the same request is not queued twice."""
        payload = json.dumps({"action": self.action, "arguments": self.arguments},
                             sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------- audit ----------


def audit(request_id: str, event: str, actor: str, detail: dict | None = None) -> None:
    """Append one immutable audit entry.

    Keyed by nanosecond timestamp plus a random suffix so two events in the
    same nanosecond cannot overwrite each other — losing an audit entry to a
    key collision would be a silent hole in the evidence.
    """
    key = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"
    store.kv_set(
        AUDIT_NAMESPACE,
        key,
        {
            "request_id": request_id,
            "event": event,
            "actor": actor,
            "at": time.time(),
            "detail": detail or {},
        },
    )
    log.info("audit %s %s by %s", event, request_id, actor)


def audit_trail(request_id: str | None = None) -> list[dict]:
    rows = [v for _, v in store.kv_list(AUDIT_NAMESPACE)]
    rows.sort(key=lambda r: r["at"])
    if request_id:
        rows = [r for r in rows if r["request_id"] == request_id]
    return rows


# ---------- queue ----------


def create(
    action: str,
    arguments: dict,
    reason: str,
    risk: Risk,
    rendered: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    requested_by: str = "agent",
) -> ApprovalRequest:
    """Queue a request, or return the existing one for the same action.

    Deduplication matters because a retried agent run must not queue the same
    $48,000 refund twice — an approver who sees two identical requests either
    approves both or trusts the queue less. Both are bad.
    """
    request = ApprovalRequest(
        id=f"req_{uuid.uuid4().hex[:12]}",
        action=action,
        arguments=arguments,
        reason=reason,
        risk=risk,
        rendered=rendered,
        expires_at=time.time() + ttl_seconds,
    )

    fingerprint = request.fingerprint()
    for existing in pending():
        if existing.fingerprint() == fingerprint:
            log.info("duplicate request for %s, reusing %s", action, existing.id)
            audit(existing.id, "duplicate_suppressed", requested_by,
                  {"action": action})
            return existing

    store.kv_set(REQUESTS_NAMESPACE, request.id, request.as_dict())
    audit(request.id, "requested", requested_by,
          {"action": action, "risk": risk.value, "reason": reason})
    return request


def get(request_id: str) -> ApprovalRequest | None:
    row = store.kv_get(REQUESTS_NAMESPACE, request_id)
    if not row:
        return None
    request = ApprovalRequest.from_dict(row)
    if request.expired:
        return _expire(request)
    return request


def _expire(request: ApprovalRequest) -> ApprovalRequest:
    request.status = Status.expired
    store.kv_set(REQUESTS_NAMESPACE, request.id, request.as_dict())
    audit(request.id, "expired", "system",
          {"age_seconds": round(time.time() - request.created_at)})
    return request


def all_requests(status: Status | None = None) -> list[ApprovalRequest]:
    rows = [ApprovalRequest.from_dict(v) for _, v in store.kv_list(REQUESTS_NAMESPACE)]
    rows = [_expire(r) if r.expired else r for r in rows]
    rows.sort(key=lambda r: -r.created_at)
    return [r for r in rows if status is None or r.status is status]


def pending() -> list[ApprovalRequest]:
    return all_requests(Status.pending)


def decide(
    request_id: str,
    approved: bool,
    actor: str,
    note: str = "",
    supplied_context: dict | None = None,
) -> ApprovalRequest:
    """Record a human decision. Idempotent, and refuses to re-decide.

    A second decision on a settled request is either a double-click or an
    attempt to overturn one quietly. Neither should mutate the record: the
    original decision stands and the attempt is audited.
    """
    request = get(request_id)
    if request is None:
        raise KeyError(f"no approval request {request_id}")

    if request.status is not Status.pending:
        audit(request_id, "decision_rejected", actor,
              {"reason": f"already {request.status.value}", "attempted": approved})
        log.warning("refusing to re-decide %s (already %s)", request_id, request.status.value)
        return request

    request.status = Status.approved if approved else Status.rejected
    request.decided_at = time.time()
    request.decided_by = actor
    request.decision_note = note
    request.supplied_context = supplied_context or {}
    store.kv_set(REQUESTS_NAMESPACE, request.id, request.as_dict())

    audit(
        request_id,
        "approved" if approved else "rejected",
        actor,
        {
            "note": note,
            "supplied_context": request.supplied_context,
            "seconds_pending": round(request.decided_at - request.created_at, 2),
        },
    )
    return request


def mark_executed(request_id: str, result: str, actor: str = "agent") -> ApprovalRequest:
    """Close the loop. An approved-but-never-executed request is a silent
    failure, so execution is a distinct recorded state, not an assumption."""
    request = get(request_id)
    if request is None:
        raise KeyError(f"no approval request {request_id}")
    if request.status is not Status.approved:
        raise ValueError(
            f"cannot execute {request_id}: status is {request.status.value}, not approved"
        )
    request.status = Status.executed
    request.executed_at = time.time()
    request.result = result
    store.kv_set(REQUESTS_NAMESPACE, request.id, request.as_dict())
    audit(request_id, "executed", actor, {"result": result[:300]})
    return request


def stats() -> dict:
    rows = all_requests()
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
    decided = [r for r in rows if r.decided_at]
    return {
        "total": len(rows),
        "by_status": by_status,
        "audit_entries": len(audit_trail()),
        "mean_seconds_to_decide": (
            round(sum(r.decided_at - r.created_at for r in decided) / len(decided), 2)
            if decided
            else None
        ),
    }
