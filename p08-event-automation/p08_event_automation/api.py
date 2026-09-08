"""Webhook endpoint for P8, mounted by `server/main.py` at /api/p08.

The endpoint returns **200 for a duplicate**, which is the entire contract. A
sender that receives an error for a redelivery will keep redelivering; 200 with
the original event id is what makes it stop.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, Field

from . import queue
from .queue import Status
from .worker import Worker, default_router, ledger

router = APIRouter(prefix="/api/p08", tags=["p08-event-automation"])


class WebhookPayload(BaseModel):
    type: str = Field(min_length=1)
    data: dict = Field(default_factory=dict)


@router.post("/webhook")
def webhook(
    payload: WebhookPayload,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Accept an event. Idempotent by the `Idempotency-Key` header."""
    result = queue.enqueue(payload.type, payload.data, idempotency_key=idempotency_key)
    # 200 either way, and 202 only for genuinely new work, so a sender can tell
    # the difference without treating a duplicate as a failure.
    response.status_code = 200 if result.duplicate else 202
    return {
        "event_id": result.event.id,
        "duplicate": result.duplicate,
        "status": result.event.status.value,
        "idempotency_key": result.event.idempotency_key,
    }


@router.post("/drain")
def drain(max_events: int = 50) -> dict:
    """Run the worker once. In production this is a loop or a cron."""
    stats = Worker(default_router()).drain(max_events=max_events)
    return {"worker": stats.as_dict(), "queue": queue.stats(), "ledger": ledger()}


@router.get("/events")
def events(status: str | None = None, limit: int = 50) -> dict:
    parsed = Status(status) if status else None
    return {
        "stats": queue.stats(),
        "events": [e.as_dict() for e in queue.all_events(parsed, limit)],
    }


@router.get("/dead-letters")
def dead_letters(include_replayed: bool = False) -> dict:
    return {"dead_letters": queue.dead_letters(include_replayed)}


@router.post("/dead-letters/{dead_letter_id}/replay")
def replay(dead_letter_id: str) -> dict:
    result = queue.replay(dead_letter_id)
    return {"event_id": result.event.id, "duplicate": result.duplicate}
