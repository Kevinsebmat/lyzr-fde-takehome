"""Routers for the projects whose demo surface is thin enough to inline.

P1, P2 and P8 own richer routers in their own packages, because their API is
part of the project (P8's webhook *is* the product). The rest get their demo
endpoint here rather than a near-empty `api.py` in each folder — a file per
project that exists only to satisfy symmetry is not structure, it is filing.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(tags=["demos"])


# ---------- p03 ----------


class ReActRequest(BaseModel):
    task: str = Field(min_length=1)
    max_iterations: int = Field(default=8, ge=1, le=20)
    max_repeats: int = Field(default=2, ge=0, le=10)
    reflect: bool = True


@router.post("/api/p03/run")
def p03_run(req: ReActRequest) -> dict:
    from p03_react_planner.agent import ReActAgent

    result = ReActAgent(
        max_iterations=req.max_iterations,
        max_repeats=req.max_repeats,
        reflect=req.reflect,
    ).run(req.task)
    return {
        "outcome": result.outcome.value,
        "answer": result.answer,
        "reason": result.reason,
        "solved": result.solved,
        "degraded": result.degraded,
        "steps": [s.as_dict() for s in result.steps],
        **result.summary(),
    }


# ---------- p04 ----------


class GatherRequest(BaseModel):
    account: str = "ACC-1001"
    caller: str = "analyst"
    timeout: float = Field(default=0.5, gt=0, le=10)


@router.post("/api/p04/gather")
def p04_gather(req: GatherRequest) -> dict:
    from p04_tool_orchestrator.orchestrator import Orchestrator
    from p04_tool_orchestrator.registry import Caller
    from p04_tool_orchestrator.tools import default_registry

    callers = {
        "analyst": Caller.of("analyst", "billing:read", "crm:read", "analytics:read"),
        "readonly": Caller.of("readonly", "crm:read"),
        "admin": Caller.of("admin", "*"),
    }
    who = callers.get(req.caller, callers["readonly"])
    orchestrator = Orchestrator(default_registry(), per_tool_timeout=req.timeout)
    batch = orchestrator.run_batch_sync(
        [
            ("billing_service", {"account": req.account}),
            ("billing_cache", {"account": req.account}),
            ("crm", {"account": req.account}),
            ("usage_analytics", {"account": req.account}),
            ("billing_legacy", {"account": req.account}),
            ("apply_credit", {"account": req.account, "amount": 100}),
        ],
        who,
    )
    out = {
        "results": [r.as_dict() for r in batch.results],
        "conflicts": [c.as_dict() for c in batch.conflicts],
        "merged": batch.merged,
        "denied": batch.denied,
        "abandoned_workers": orchestrator.abandoned,
        **batch.summary(),
    }
    orchestrator.close()
    return out


# ---------- p05 ----------


class ChatRequest(BaseModel):
    user_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


@router.post("/api/p05/chat")
def p05_chat(req: ChatRequest) -> dict:
    from p05_memory_agent.agent import MemoryAgent

    result = MemoryAgent(req.user_id).chat(req.message)
    return {
        "reply": result.reply,
        "recalled": [
            {"kind": s.fact.kind.value, "text": s.fact.text,
             "similarity": s.similarity, "recency": s.recency, "score": s.score}
            for s in result.recalled
        ],
        "learned": [{"id": f.id, "kind": f.kind.value, "text": f.text}
                    for f in result.learned],
        "superseded": result.superseded,
        **result.summary(),
    }


@router.get("/api/p05/facts/{user_id}")
def p05_facts(user_id: str, include_superseded: bool = False) -> dict:
    from p05_memory_agent.memory import MemoryStore

    memory = MemoryStore(user_id)
    return {
        "stats": memory.stats(),
        "facts": [f.as_dict() for f in memory.all_facts(include_superseded)],
    }


@router.post("/api/p05/end-session/{user_id}")
def p05_end_session(user_id: str) -> dict:
    from p05_memory_agent.agent import MemoryAgent
    from p05_memory_agent.memory import MemoryStore

    MemoryAgent(user_id).end_session()
    return {"buffer_cleared": True, "facts_kept": len(MemoryStore(user_id).all_facts())}


# ---------- p06 ----------


class HandleRequest(BaseModel):
    case: str = Field(min_length=1)


class DecideRequest(BaseModel):
    approved: bool
    actor: str = Field(min_length=1)
    note: str = ""
    set_amount: float | None = None


@router.post("/api/p06/handle")
def p06_handle(req: HandleRequest) -> dict:
    from p06_hitl_approval.agent import ApprovalAgent

    result = ApprovalAgent().handle(req.case)
    return {
        "outcome": result.outcome.value,
        "result": result.result,
        "escalation_reasons": result.escalation_reasons,
        "request": result.request.as_dict() if result.request else None,
    }


@router.get("/api/p06/queue")
def p06_queue(all_statuses: bool = False) -> dict:
    from p06_hitl_approval import approvals

    rows = approvals.all_requests() if all_statuses else approvals.pending()
    return {"stats": approvals.stats(), "requests": [r.as_dict() for r in rows]}


@router.post("/api/p06/requests/{request_id}/decide")
def p06_decide(request_id: str, req: DecideRequest) -> dict:
    from p06_hitl_approval import approvals
    from p06_hitl_approval.agent import ApprovalAgent

    context = {"amount": req.set_amount} if req.set_amount is not None else {}
    approvals.decide(request_id, approved=req.approved, actor=req.actor,
                     note=req.note, supplied_context=context)
    result = ApprovalAgent().resume(request_id)
    return {"outcome": result.outcome.value, "result": result.result,
            "request": result.request.as_dict() if result.request else None}


@router.get("/api/p06/audit")
def p06_audit(request_id: str | None = None) -> dict:
    from p06_hitl_approval import approvals

    return {"trail": approvals.audit_trail(request_id), "stats": approvals.stats()}


# ---------- p07 ----------


class RouteRequest(BaseModel):
    task: str = Field(min_length=1)
    budget_usd: float = Field(default=0.05, gt=0)


@router.post("/api/p07/route")
def p07_route(req: RouteRequest) -> dict:
    from p07_cost_router.router import CostAwareRouter

    result = CostAwareRouter(task_budget_usd=req.budget_usd).route(req.task)
    return {
        "answer": result.answer,
        "classification": {
            "complexity": result.classification.complexity.value,
            "reasons": result.classification.reasons,
            "method": result.classification.method,
        },
        "attempts": [a.__dict__ for a in result.attempts],
        "budget_exceeded": result.budget_exceeded,
        **result.summary(),
    }


@router.get("/api/p07/economics")
def p07_economics(tasks: int = 1000) -> dict:
    from p07_cost_router.economics import (
        break_even,
        classifier_verdict,
        price_table,
        projected_cost,
    )
    from p07_cost_router.router import analytics

    return {
        "prices": price_table(),
        "break_even": break_even("claude-haiku-4-5", "claude-opus-5").__dict__,
        "projections": [
            projected_cost("claude-haiku-4-5", "claude-opus-5", rate, tasks=tasks)
            for rate in (0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 0.95)
        ],
        "classifier_overhead": [
            {"success_rate": rate,
             **classifier_verdict("claude-haiku-4-5", "claude-opus-5", rate)}
            for rate in (0.9, 0.8, 0.5, 0.25)
        ],
        "measured": analytics(),
    }


# ---------- p09 ----------


class DebateRequest(BaseModel):
    question: str = Field(min_length=1)


@router.post("/api/p09/debate")
def p09_debate(req: DebateRequest) -> dict:
    from p09_debate.debate import DebateSystem

    result = DebateSystem().run(req.question)
    return {
        "verdict": result.verdict.value,
        "recommendation": result.recommendation,
        "winner": result.winner,
        "agreement": result.agreement,
        "tally": result.tally,
        "votes": result.votes,
        "notes": result.notes,
        "proposals": {
            k: {"recommendation": p.recommendation,
                "counterargument": p.strongest_counterargument,
                "confidence": p.confidence}
            for k, p in result.proposals.items()
        },
        "flaws": [f.model_dump() for f in (result.critique.flaws if result.critique else [])],
        **result.summary(),
    }


# ---------- p10 ----------


class ReflectRequest(BaseModel):
    task: str | None = None
    max_iterations: int = Field(default=3, ge=1, le=6)
    target_score: float = Field(default=4.2, ge=1.0, le=5.0)


@router.post("/api/p10/reflect")
def p10_reflect(req: ReflectRequest) -> dict:
    from p10_self_reflective.agent import SelfReflectiveAgent
    from p10_self_reflective.smoke import CASE_NOTES, TASK

    result = SelfReflectiveAgent(
        max_iterations=req.max_iterations, target_score=req.target_score
    ).run(req.task or TASK, CASE_NOTES)
    return {
        "attempts": [a.as_dict() for a in result.attempts],
        "best": result.best.as_dict() if result.best else None,
        "best_text": result.best.text if result.best else "",
        "trajectory": result.trajectory,
        "regressed": result.regressed,
        **result.summary(),
    }


@router.get("/api/p10/metrics")
def p10_metrics() -> dict:
    from p10_self_reflective.agent import aggregate, history

    return {"aggregate": aggregate(), "recent": history(20)}


# ---------- p11 ----------


@router.get("/api/p11/dashboard")
def p11_dashboard() -> dict:
    from p11_observability.alerts import evaluate
    from p11_observability.analysis import (
        by_model,
        by_project,
        costliest_runs,
        error_shapes,
        load,
        overview,
        slowest_runs,
    )

    spans = load()
    return {
        "overview": overview(spans),
        "by_project": by_project(spans),
        "by_model": by_model(spans),
        "error_shapes": error_shapes(spans),
        "alerts": [a.as_dict() for a in evaluate(spans)],
        "slowest_runs": slowest_runs(spans),
        "costliest_runs": costliest_runs(spans),
    }


@router.get("/api/p11/trace/{run_id}")
def p11_trace(run_id: str) -> dict:
    from p11_observability.analysis import load, trace

    node = trace(load(), run_id)
    if node is None:
        return {"found": False}

    def render(n) -> dict:
        return {
            "name": n.span["name"],
            "kind": n.span["kind"],
            "status": n.span["status"],
            "duration_ms": n.span["duration_ms"],
            "cost_usd": n.span.get("cost_usd"),
            "model": n.span.get("model"),
            "error_type": n.span.get("error_type"),
            "children": [render(c) for c in n.children],
        }

    return {"found": True, "total_cost_usd": round(node.total_cost, 6), "tree": render(node)}


@router.get("/api/p11/canaries")
def p11_canaries() -> dict:
    from p11_observability import canary as canary_mod

    return {"canaries": [c.as_dict() for c in canary_mod.all_canaries()]}


@router.post("/api/p11/canaries/demo")
def p11_canary_demo() -> dict:
    """Run a good release and a bad one, so the console can show both."""
    from p11_observability import canary as canary_mod

    rails = canary_mod.GuardRails(min_samples=20)
    out = {}

    good = canary_mod.start("demo-good", "v1", "v2", traffic_pct=10.0, rails=rails)
    for _ in range(40):
        good.record("baseline", ok=True, latency_ms=120, cost_usd=0.002)
    for _ in range(5):
        good.record("canary", ok=True, latency_ms=115, cost_usd=0.002)
    _, out["early_promotion_refused"] = good.promote()
    for _ in range(20):
        good.record("canary", ok=True, latency_ms=115, cost_usd=0.002)
    ok, out["promoted"] = good.promote()
    out["promotion_succeeded"] = ok

    bad = canary_mod.start("demo-bad", "v1", "v3", traffic_pct=10.0, rails=rails)
    for _ in range(40):
        bad.record("baseline", ok=True, latency_ms=120, cost_usd=0.002)
    for i in range(25):
        bad.record("canary", ok=(i % 4 != 0), latency_ms=120, cost_usd=0.002)
        if bad.state is canary_mod.State.rolled_back:
            out["rolled_back_after_requests"] = i + 1
            break
    out["rollback_reason"] = bad.rollback_reason
    out["routing_now"] = bad.route("any")
    out["canaries"] = [good.as_dict(), bad.as_dict()]
    return out
