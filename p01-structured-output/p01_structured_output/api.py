"""FastAPI router for P1, mounted by `server/main.py` at /api/p01."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .agent import StructuredAgent, failure_log, failure_stats
from .schemas import TicketTriage
from .tools import ToolContractError, build_account_tool, build_broken_tool

router = APIRouter(prefix="/api/p01", tags=["p01-structured-output"])


class ExtractRequest(BaseModel):
    text: str = Field(min_length=1)
    max_attempts: int = Field(default=3, ge=1, le=5)
    escalate: bool = True


class ExtractResponse(BaseModel):
    ok: bool
    value: TicketTriage | None
    attempts: int
    repaired: bool
    failures: list[str]
    model: str
    cost_usd: float
    error: str | None = None


@router.post("/extract", response_model=ExtractResponse)
def extract(req: ExtractRequest) -> ExtractResponse:
    result = StructuredAgent(max_attempts=req.max_attempts, escalate=req.escalate).extract(
        req.text, TicketTriage
    )
    return ExtractResponse(
        ok=result.ok,
        value=result.value,
        attempts=result.attempts,
        repaired=result.repaired,
        failures=result.failures,
        model=result.model,
        cost_usd=round(result.cost_usd, 6),
        error=result.error,
    )


@router.get("/failures")
def failures(limit: int = 50) -> dict:
    return {"stats": failure_stats(), "recent": failure_log(limit)}


@router.post("/tools/demo")
def tool_demo(broken: bool = False, account_id: str = "ACC-1001") -> dict:
    """Show both validation paths side by side.

    The console renders this to make the distinction concrete: a rejected
    argument is a normal, recoverable turn; a contract violation is an incident.
    """
    tool = build_broken_tool() if broken else build_account_tool()
    out: dict = {"tool": tool.name}

    try:
        outcome = tool.invoke({"account_id": account_id})
        out["valid_call"] = {
            "ok": outcome.ok,
            "value": outcome.value.model_dump() if outcome.value else None,
        }
    except ToolContractError as exc:
        out["valid_call"] = {
            "ok": False,
            "contract_violation": str(exc),
            "note": "the tool broke its own output contract — an integration bug, "
                    "never surfaced to the model",
        }

    bad = tool.invoke({"account_id": "not-valid"})
    out["invalid_arguments"] = {
        "ok": bad.ok,
        "model_recoverable": bad.model_recoverable,
        "error": bad.error,
        "tool_result_block": bad.as_tool_result("toolu_demo"),
    }
    return out
