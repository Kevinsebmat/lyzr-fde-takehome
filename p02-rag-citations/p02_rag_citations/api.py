"""FastAPI router for P2, mounted by `server/main.py` at /api/p02."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .agent import RagAgent, index_corpus
from .corpus import ANSWERABLE, UNANSWERABLE, all_chunks

router = APIRouter(prefix="/api/p02", tags=["p02-rag-citations"])


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    k: int = Field(default=4, ge=1, le=10)
    allow_search_fallback: bool = True
    answer_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class ClaimOut(BaseModel):
    text: str
    citation_ids: list[str]
    verified: bool
    support_score: float
    reason: str


class AskResponse(BaseModel):
    question: str
    action: str
    answered: bool
    answer: str
    claims: list[ClaimOut]
    citations: list[dict]
    confidence: dict | None
    search_results: list[dict]
    retrieved: int
    cost_usd: float


@router.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    agent = RagAgent(k=req.k, allow_search_fallback=req.allow_search_fallback)
    if req.answer_threshold is not None:
        # Exposed so the console can show the threshold moving the decision —
        # the clearest way to make "this is a product choice, not a constant"
        # legible to a non-technical viewer.
        agent.answer_threshold = req.answer_threshold
    result = agent.ask(req.question)

    return AskResponse(
        question=result.question,
        action=result.action.value,
        answered=result.answered,
        answer=result.answer,
        claims=[
            ClaimOut(
                text=c.text,
                citation_ids=c.citation_ids,
                verified=c.verified,
                support_score=c.support_score,
                reason=c.reason,
            )
            for c in result.claims
        ],
        citations=result.citations,
        confidence=result.confidence.as_dict() if result.confidence else None,
        search_results=[r.as_dict() for r in result.search_results],
        retrieved=result.retrieved,
        cost_usd=round(result.cost_usd, 6),
    )


@router.get("/corpus")
def corpus() -> dict:
    """The chunks, and the demo question sets.

    `unanswerable` is shipped as first-class data: knowing what a knowledge base
    does *not* cover is part of describing it honestly.
    """
    return {
        "indexed": index_corpus(),
        "chunks": [
            {"id": c.id, "title": c.title, "heading": c.heading, "source": c.source}
            for c in all_chunks()
        ],
        "answerable": list(ANSWERABLE),
        "unanswerable": list(UNANSWERABLE),
    }
