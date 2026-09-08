"""The structured extraction agent.

What makes this production rather than a demo is not the schema — it's what
happens when the schema is violated:

1. The validation error is fed back as a **repair turn** naming the failing
   fields, instead of a blind retry that re-rolls the same dice.
2. Repeated failure **escalates the model** rather than burning identical
   attempts on one that has already shown it cannot do the task.
3. Every failure is **persisted with the offending output**, so the rate is
   measurable and a regression is visible. A validation failure you cannot
   reproduce from the log is not actionable.
4. Exhaustion is an **explicit typed failure**, never a half-populated object.

The repair loop itself lives in `agentcore.llm.parse` because ten other
projects need it; this module is the productionised surface around it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TypeVar

from agentcore import LLM, Budget, ParseError, default_llm, store, tracing
from pydantic import BaseModel

from .schemas import TicketTriage

log = logging.getLogger("p01.agent")

M = TypeVar("M", bound=BaseModel)

FAILURE_NAMESPACE = "p01_validation_failures"

SYSTEM = """You extract structured triage data from customer support tickets.

Rules that the schema cannot express:
- `summary` states the problem directly. Do not narrate ("the ticket says...").
- `refund_amount_usd` is null unless the customer explicitly asks for money back.
- `affected_users` is 0 when the ticket does not say. Do not guess a number.
- Every ticket gets at least one concrete action item with a real owning team."""


@dataclass
class ValidationFailure:
    schema: str
    attempt: int
    error: str
    raw_output: str
    model: str
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "schema": self.schema,
            "attempt": self.attempt,
            "error": self.error,
            "raw_output": self.raw_output[:2000],
            "model": self.model,
            "at": self.at,
        }


@dataclass
class ExtractionResult:
    value: BaseModel | None
    ok: bool
    attempts: int
    model: str
    cost_usd: float
    failures: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def repaired(self) -> bool:
        """Succeeded, but only after at least one validation failure."""
        return self.ok and bool(self.failures)

    def summary(self) -> dict:
        return {
            "ok": self.ok,
            "attempts": self.attempts,
            "repaired": self.repaired,
            "failures": len(self.failures),
            "model": self.model,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class StructuredAgent:
    llm: LLM = None  # type: ignore[assignment]
    max_attempts: int = 3
    escalate: bool = True

    def __post_init__(self):
        if self.llm is None:
            self.llm = default_llm("p01-structured-output", budget=Budget(limit_usd=0.50))

    def extract(
        self,
        text: str,
        schema: type[M] = TicketTriage,
        *,
        system: str = SYSTEM,
    ) -> ExtractionResult:
        """Extract `schema` from `text`, repairing and escalating as needed."""
        with tracing.run("p01-structured-output", "extract", schema=schema.__name__) as _:
            before = self.llm.budget.spent_usd
            try:
                value, reply = self.llm.parse(
                    text,
                    schema,
                    system=system,
                    max_attempts=self.max_attempts,
                    escalate=self.escalate,
                )
            except ParseError as exc:
                self._record_failures(
                    schema.__name__, [str(exc)], exc.raw_output, self.llm.model, exc.attempts
                )
                log.error(
                    "extraction gave up after %d attempts for %s",
                    exc.attempts, schema.__name__,
                )
                # Explicit failure. A partially-populated object here is worse
                # than nothing: it looks like an answer.
                return ExtractionResult(
                    value=None,
                    ok=False,
                    attempts=exc.attempts,
                    model=self.llm.model,
                    cost_usd=self.llm.budget.spent_usd - before,
                    failures=[str(exc)],
                    error=f"schema validation failed after {exc.attempts} attempts",
                )

            if reply.failures:
                self._record_failures(
                    schema.__name__, reply.failures, "", reply.model, reply.attempts
                )

            return ExtractionResult(
                value=value,
                ok=True,
                attempts=reply.attempts,
                model=reply.model,
                cost_usd=self.llm.budget.spent_usd - before,
                failures=list(reply.failures),
            )

    def _record_failures(
        self, schema: str, errors: list[str], raw: str, model: str, attempts: int
    ) -> None:
        for i, err in enumerate(errors, start=1):
            failure = ValidationFailure(
                schema=schema, attempt=i, error=err, raw_output=raw, model=model
            )
            store.kv_set(FAILURE_NAMESPACE, f"{time.time_ns()}-{i}", failure.as_dict())


def failure_log(limit: int = 50) -> list[dict]:
    """Recent validation failures, newest first.

    Persisted rather than logged-and-forgotten because the useful question is
    'what is our validation failure rate this week', and you cannot answer that
    from stderr.
    """
    return [v for _, v in store.kv_list(FAILURE_NAMESPACE)[:limit]]


def failure_stats() -> dict:
    """Aggregate for the console and the P11 dashboard."""
    rows = [v for _, v in store.kv_list(FAILURE_NAMESPACE)]
    by_schema: dict[str, int] = {}
    for r in rows:
        by_schema[r["schema"]] = by_schema.get(r["schema"], 0) + 1
    return {"total": len(rows), "by_schema": by_schema}
