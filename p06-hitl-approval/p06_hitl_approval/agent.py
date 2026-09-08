"""The agent that knows when to stop and ask.

Uncertainty detection runs on two independent signals, because either alone
fails in a way the other catches:

**Deterministic policy.** Rules in code: a refund over $500, anything touching
a cancelled order, any deletion. These fire regardless of what the model
thinks, which is the point — a policy that the model can talk itself out of is
not a policy, it is a suggestion. Every regulated control belongs here.

**Model-assessed uncertainty.** The model's own confidence and its reading of
whether the request is ambiguous. This catches what the rules did not
anticipate — the novel case, the malformed request, the one where the numbers
do not add up.

Policy wins ties. If the rules say escalate, the model's confidence is
irrelevant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from agentcore import LLM, Budget, ParseError, default_llm, tracing
from pydantic import BaseModel, Field

from . import approvals
from .approvals import ApprovalRequest, Risk, Status

log = logging.getLogger("p06.agent")

SYSTEM = """You are a support agent deciding how to handle a customer request.

Propose exactly one action with its arguments. Be honest about confidence: if
the request is ambiguous, the amount is unclear, or the account state does not
obviously support it, say so. Escalating is cheap; a wrong irreversible action
is not.

Set `needs_human` yourself whenever you would want a second pair of eyes."""


class Outcome(str, Enum):
    executed = "executed"
    awaiting_approval = "awaiting_approval"
    rejected = "rejected"
    failed = "failed"


class Proposal(BaseModel):
    model_config = {"extra": "forbid"}

    action: str = Field(min_length=1, description="e.g. issue_refund, close_ticket")
    arguments: dict = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)
    needs_human: bool = Field(description="True if you want a human to check this.")


@dataclass
class PolicyVerdict:
    escalate: bool
    risk: Risk
    reasons: list[str] = field(default_factory=list)


#: Deterministic controls. Amounts in USD.
AUTO_APPROVE_LIMIT = 500.0
HIGH_RISK_LIMIT = 10_000.0
IRREVERSIBLE_ACTIONS = frozenset({"delete_account", "purge_data", "cancel_contract"})
CONFIDENCE_FLOOR = 0.75


def apply_policy(proposal: Proposal) -> PolicyVerdict:
    """The rules that do not care what the model thinks."""
    reasons: list[str] = []
    risk = Risk.low

    amount = _amount(proposal.arguments)
    if amount is not None:
        if amount >= HIGH_RISK_LIMIT:
            reasons.append(f"${amount:,.2f} is at or above the ${HIGH_RISK_LIMIT:,.0f} "
                           "high-risk threshold")
            risk = Risk.high
        elif amount > AUTO_APPROVE_LIMIT:
            reasons.append(f"${amount:,.2f} exceeds the ${AUTO_APPROVE_LIMIT:,.0f} "
                           "auto-approval limit")
            risk = Risk.medium

    if proposal.action in IRREVERSIBLE_ACTIONS:
        reasons.append(f"`{proposal.action}` is irreversible")
        risk = Risk.high

    if str(proposal.arguments.get("order_status", "")).lower() == "cancelled":
        reasons.append("the order is cancelled — policy forbids automatic action")
        risk = Risk.high

    return PolicyVerdict(escalate=bool(reasons), risk=risk, reasons=reasons)


def _amount(arguments: dict) -> float | None:
    for key in ("amount", "amount_usd", "refund_amount", "value"):
        if key in arguments:
            try:
                return float(arguments[key])
            except (TypeError, ValueError):
                # An unparseable amount is itself a reason to escalate rather
                # than to treat the request as amountless.
                return float("inf")
    return None


@dataclass
class HandleResult:
    outcome: Outcome
    proposal: Proposal | None = None
    request: ApprovalRequest | None = None
    result: str = ""
    escalation_reasons: list[str] = field(default_factory=list)
    cost_usd: float = 0.0

    def summary(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "action": self.proposal.action if self.proposal else None,
            "risk": self.request.risk.value if self.request else None,
            "reasons": self.escalation_reasons,
            "cost_usd": round(self.cost_usd, 6),
        }


def render_request(proposal: Proposal, reasons: list[str], case: str) -> str:
    """What the approver sees. Stored verbatim on the request.

    An approver deciding from a bare action name is rubber-stamping. The
    arguments, the agent's own rationale and confidence, and the specific
    reasons it was escalated all have to be on the screen, or the audit record
    documents a decision nobody actually made.
    """
    lines = [
        f"ACTION      {proposal.action}",
        f"ARGUMENTS   {proposal.arguments}",
        f"RATIONALE   {proposal.rationale}",
        f"CONFIDENCE  {proposal.confidence:.2f}",
        "",
        "ESCALATED BECAUSE",
        *[f"  - {r}" for r in reasons],
        "",
        "ORIGINAL REQUEST",
        f"  {case.strip()}",
    ]
    return "\n".join(lines)


@dataclass
class ApprovalAgent:
    llm: LLM = None  # type: ignore[assignment]
    confidence_floor: float = CONFIDENCE_FLOOR
    ttl_seconds: int = approvals.DEFAULT_TTL_SECONDS
    executor: callable = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.llm is None:
            self.llm = default_llm("p06-hitl-approval", budget=Budget(limit_usd=0.30))
        if self.executor is None:
            self.executor = _default_executor

    def handle(self, case: str) -> HandleResult:
        """Propose an action, then either do it or queue it for a human."""
        with tracing.run("p06-hitl-approval", "handle", case=case[:120]):
            before = self.llm.budget.spent_usd
            try:
                proposal, _ = self.llm.parse(case, Proposal, system=SYSTEM, name="p06.propose")
            except ParseError as exc:
                log.error("proposal failed validation: %s", exc)
                return HandleResult(
                    outcome=Outcome.failed,
                    result="Could not form a well-defined action.",
                    cost_usd=self.llm.budget.spent_usd - before,
                )

            verdict = apply_policy(proposal)
            reasons = list(verdict.reasons)

            # The model's own doubt, only after the rules have had their say.
            if proposal.needs_human:
                reasons.append("the agent asked for a human check")
            if proposal.confidence < self.confidence_floor:
                reasons.append(
                    f"confidence {proposal.confidence:.2f} is below the "
                    f"{self.confidence_floor:.2f} floor"
                )

            if not reasons:
                result = self.executor(proposal.action, proposal.arguments)
                return HandleResult(
                    outcome=Outcome.executed,
                    proposal=proposal,
                    result=result,
                    cost_usd=self.llm.budget.spent_usd - before,
                )

            request = approvals.create(
                action=proposal.action,
                arguments=proposal.arguments,
                reason=proposal.rationale,
                risk=verdict.risk,
                rendered=render_request(proposal, reasons, case),
                ttl_seconds=self.ttl_seconds,
            )
            return HandleResult(
                outcome=Outcome.awaiting_approval,
                proposal=proposal,
                request=request,
                escalation_reasons=reasons,
                cost_usd=self.llm.budget.spent_usd - before,
            )

    def resume(self, request_id: str) -> HandleResult:
        """Continue a decided request — from any process, at any later time.

        Nothing here depends on the process that created the request still
        existing. That is what makes the pause durable rather than a coroutine
        holding state it will lose on the next deploy.
        """
        request = approvals.get(request_id)
        if request is None:
            raise KeyError(f"no approval request {request_id}")

        with tracing.run("p06-hitl-approval", "resume", request=request_id):
            if request.status is Status.rejected:
                return HandleResult(
                    outcome=Outcome.rejected,
                    request=request,
                    result=f"Rejected by {request.decided_by}: "
                           f"{request.decision_note or 'no reason given'}",
                )

            if request.status is Status.executed:
                # Idempotent: a replayed resume must not act twice.
                return HandleResult(
                    outcome=Outcome.executed, request=request,
                    result=request.result or "",
                )

            if request.status is not Status.approved:
                return HandleResult(
                    outcome=Outcome.awaiting_approval,
                    request=request,
                    result=f"Still {request.status.value}.",
                )

            # The approver may have corrected an argument. Their context wins:
            # they are the authority the pause existed to consult.
            arguments = {**request.arguments, **request.supplied_context}
            result = self.executor(request.action, arguments)
            approvals.mark_executed(request_id, result, actor=request.decided_by or "agent")
            return HandleResult(
                outcome=Outcome.executed,
                request=approvals.get(request_id),
                result=result,
            )


def _default_executor(action: str, arguments: dict) -> str:
    """Stand-in for the real side effect."""
    return f"executed {action} with {arguments}"
