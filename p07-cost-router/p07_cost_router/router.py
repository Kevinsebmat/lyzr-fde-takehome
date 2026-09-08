"""The router: classify, pick a tier, run, escalate only when it pays.

Design decisions, each with a cost consequence:

**Classification is heuristic first.** An LLM call to decide which model to use
is added to *every* request. Measured (`economics.classifier_verdict`), a small
Haiku classifier costs 4-5% of the saving while the cheap tier handles 80-90% of
traffic, but 60% of it once the success rate drops near the 20% break-even — the
overhead bites hardest exactly when routing is already marginal. Heuristics cost
nothing and add no latency, so they run first; the LLM classifier is an opt-in
for genuinely ambiguous inputs.

**Escalation is a decision, not a reflex.** A cascade only saves money when the
cheap model succeeds often enough (see `economics`). So escalation requires a
concrete reason — low self-reported confidence, a refusal, an output that fails
its schema — and never fires on a hunch.

**The budget is enforced before the call, not after.** A budget checked after
the fact is a report, not a control.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum

from agentcore import LLM, Budget, BudgetExceeded, ParseError, Usage, default_llm, store, tracing
from pydantic import BaseModel, Field

from .economics import break_even, measured_savings

log = logging.getLogger("p07.router")

HISTORY_NAMESPACE = "p07_decisions"


class Complexity(str, Enum):
    simple = "simple"        # classification, extraction, formatting
    moderate = "moderate"    # summarisation, drafting, single-hop reasoning
    hard = "hard"            # multi-step reasoning, ambiguity, judgement calls


TIER_FOR: dict[Complexity, str] = {
    Complexity.simple: "claude-haiku-4-5",
    Complexity.moderate: "claude-sonnet-5",
    Complexity.hard: "claude-opus-5",
}


class Answer(BaseModel):
    """Every routed task returns this, so confidence is always available to
    the escalation decision rather than inferred from prose."""

    model_config = {"extra": "forbid"}

    answer: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    #: True when the model believes the task exceeded it. The cheapest possible
    #: escalation signal: it costs nothing extra to ask for.
    needs_stronger_model: bool = False


SYSTEM = """Answer the task directly and concisely.

Report `confidence` honestly — it is used to decide whether to re-run this on a
more capable model, so overstating it produces wrong answers that are never
checked, and understating it wastes money. If the task is genuinely beyond you,
set `needs_stronger_model` and say what is missing."""


# ---------- classification ----------

_HARD_CUES = re.compile(
    r"\b(compare|trade-?offs?|why|analy[sz]e|evaluate|design|strateg|recommend|"
    r"implications?|root cause|reconcile|forecast|justify|weigh)\b",
    re.I,
)
_SIMPLE_CUES = re.compile(
    r"\b(extract|list|classify|categoris|categoriz|format|convert|translate|"
    r"count|label|tag|parse|yes or no)\b",
    re.I,
)


@dataclass
class Classification:
    complexity: Complexity
    reasons: list[str] = field(default_factory=list)
    method: str = "heuristic"


def classify(task: str) -> Classification:
    """Free complexity estimate from surface features.

    Not clever, and does not need to be: it only has to be right often enough
    that the escalation path catches the rest. Being wrong in the cheap
    direction costs one extra call; being wrong in the expensive direction
    costs the whole saving on that request, so the tie-break favours cheap.
    """
    reasons: list[str] = []
    score = 0

    words = len(task.split())
    if words > 220:
        score += 2
        reasons.append(f"long input ({words} words)")
    elif words > 90:
        score += 1
        reasons.append(f"medium input ({words} words)")

    # Count *distinct* cues rather than presence. One "why" is weak evidence;
    # "compare … analyse … trade-offs … recommend … why" is a different kind
    # of request, and collapsing both to a single +2 mis-routes the second one.
    hard_cues = {m.lower() for m in _HARD_CUES.findall(task)}
    if hard_cues:
        score += min(3, len(hard_cues))
        reasons.append(
            f"{len(hard_cues)} analysis cue(s): {', '.join(sorted(hard_cues)[:4])}"
        )

    simple_cues = {m.lower() for m in _SIMPLE_CUES.findall(task)}
    if simple_cues:
        score -= 2
        reasons.append(f"mechanical verb ({', '.join(sorted(simple_cues)[:3])})")

    questions = task.count("?")
    if questions > 2:
        score += 1
        reasons.append(f"{questions} distinct questions")

    if re.search(r"\bstep[- ]by[- ]step\b|\bthen\b.*\bthen\b", task, re.I):
        score += 1
        reasons.append("multi-step phrasing")

    if score >= 3:
        complexity = Complexity.hard
    elif score >= 1:
        complexity = Complexity.moderate
    else:
        complexity = Complexity.simple

    return Classification(complexity=complexity, reasons=reasons or ["no strong signals"])


# ---------- routing ----------


@dataclass
class Attempt:
    model: str
    ok: bool
    confidence: float
    escalated_because: str | None = None
    cost_usd: float = 0.0
    duration_ms: float = 0.0


@dataclass
class RouteResult:
    task: str
    answer: str
    classification: Classification
    attempts: list[Attempt] = field(default_factory=list)
    final_model: str = ""
    confidence: float = 0.0
    usages: list[Usage] = field(default_factory=list)
    cost_usd: float = 0.0
    early_exit: bool = False
    budget_exceeded: bool = False

    @property
    def escalated(self) -> bool:
        return len(self.attempts) > 1

    def summary(self) -> dict:
        return {
            "complexity": self.classification.complexity.value,
            "final_model": self.final_model,
            "attempts": len(self.attempts),
            "escalated": self.escalated,
            "early_exit": self.early_exit,
            "confidence": round(self.confidence, 3),
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class CostAwareRouter:
    llm: LLM = None  # type: ignore[assignment]
    #: Confidence at or above which the answer is accepted and the cascade stops.
    confidence_floor: float = 0.7
    #: Per-task ceiling. Enforced before each call, not reported after.
    task_budget_usd: float = 0.05
    max_escalations: int = 2
    baseline_model: str = "claude-opus-5"
    #: Opt-in: pay for an LLM classifier. Off, because for typical task shapes
    #: the classifier call costs more than the routing saves.
    llm_classifier: bool = False

    def __post_init__(self):
        if self.llm is None:
            self.llm = default_llm("p07-cost-router")

    def route(self, task: str) -> RouteResult:
        with tracing.run("p07-cost-router", "route", task=task[:120]):
            classification = (
                self._classify_with_llm(task) if self.llm_classifier else classify(task)
            )
            model = TIER_FOR[classification.complexity]

            result = RouteResult(task=task, answer="", classification=classification)
            budget = Budget(limit_usd=self.task_budget_usd)
            self.llm.budget = budget

            reason: str | None = None
            for attempt_no in range(self.max_escalations + 1):
                started = time.perf_counter()
                spent_before = budget.spent_usd
                try:
                    answer, _ = self.llm.parse(
                        task, Answer, system=SYSTEM, model=model,
                        name=f"p07.answer.{model}",
                    )
                except BudgetExceeded as exc:
                    # The ceiling is the point: stop, keep whatever we have.
                    log.warning("task budget exhausted: %s", exc)
                    result.budget_exceeded = True
                    if not result.answer:
                        result.answer = (
                            "Could not answer within the task budget of "
                            f"${self.task_budget_usd:.4f}."
                        )
                    break
                except ParseError:
                    result.attempts.append(
                        Attempt(model=model, ok=False, confidence=0.0,
                                escalated_because="output failed schema validation",
                                cost_usd=budget.spent_usd - spent_before)
                    )
                    reason = "output failed schema validation"
                    if (nxt := _next_tier(model)) is None:
                        result.answer = "No model produced a valid answer."
                        break
                    model = nxt
                    continue

                cost = budget.spent_usd - spent_before
                result.answer = answer.answer
                result.confidence = answer.confidence
                result.final_model = model
                result.usages = list(self.llm.usages)

                accepted = (
                    answer.confidence >= self.confidence_floor
                    and not answer.needs_stronger_model
                )
                result.attempts.append(
                    Attempt(
                        model=model, ok=True, confidence=answer.confidence,
                        escalated_because=None if accepted else _why(answer, self),
                        cost_usd=cost,
                        duration_ms=(time.perf_counter() - started) * 1000,
                    )
                )

                if accepted:
                    # Early exit. The cheap model was enough; stop paying.
                    result.early_exit = attempt_no == 0
                    break

                reason = _why(answer, self)
                nxt = _next_tier(model)
                if nxt is None:
                    log.info("already at the strongest tier; accepting %.2f confidence",
                             answer.confidence)
                    break
                log.info("escalating %s -> %s: %s", model, nxt, reason)
                model = nxt

            result.cost_usd = budget.spent_usd
            _record(result, self.baseline_model)
            return result

    def _classify_with_llm(self, task: str) -> Classification:
        """Opt-in classifier. Costs a call on every request — see the module
        docstring for why that is usually the wrong trade."""

        class ComplexityCall(BaseModel):
            model_config = {"extra": "forbid"}

            complexity: Complexity
            reason: str = Field(min_length=1, max_length=200)

        try:
            call, _ = self.llm.parse(
                f"Classify this task's difficulty.\n\nTASK\n{task}",
                ComplexityCall,
                model="claude-haiku-4-5",
                name="p07.classify",
            )
            return Classification(
                complexity=call.complexity, reasons=[call.reason], method="llm"
            )
        except (ParseError, BudgetExceeded):
            log.warning("llm classifier failed; falling back to heuristics")
            return classify(task)


def _next_tier(model: str) -> str | None:
    from agentcore import next_model_up

    return next_model_up(model)


def _why(answer: Answer, router: CostAwareRouter) -> str:
    if answer.needs_stronger_model:
        return "the model said the task exceeded it"
    return (
        f"confidence {answer.confidence:.2f} below the "
        f"{router.confidence_floor:.2f} floor"
    )


# ---------- analytics ----------


def _record(result: RouteResult, baseline_model: str) -> None:
    store.kv_set(
        HISTORY_NAMESPACE,
        str(time.time_ns()),
        {
            "task": result.task[:200],
            "complexity": result.classification.complexity.value,
            "final_model": result.final_model,
            "escalated": result.escalated,
            "early_exit": result.early_exit,
            "confidence": result.confidence,
            "cost_usd": result.cost_usd,
            "usages": [
                {"model": u.model, "input_tokens": u.input_tokens,
                 "output_tokens": u.output_tokens}
                for u in result.usages
            ],
        },
    )


def history(limit: int = 500) -> list[dict]:
    return [v for _, v in store.kv_list(HISTORY_NAMESPACE)[:limit]]


def reset_history() -> None:
    """Clear the decision log. Used by the demo and by tests that measure a
    rate — a rate computed over a polluted history is not the rate you think."""
    with store.connect() as conn:
        conn.execute("DELETE FROM kv WHERE namespace = ?", (HISTORY_NAMESPACE,))


def analytics(baseline_model: str = "claude-opus-5") -> dict:
    """Cost per decision, and whether routing is actually paying.

    Reports the observed cheap-model success rate against the computed
    break-even. If it is below, the recommendation is to stop routing — a
    cascade that escalates too often costs more than pinning the strong model.
    """
    rows = history()
    if not rows:
        return {"decisions": 0}

    usages = [
        Usage(u["model"], input_tokens=u["input_tokens"], output_tokens=u["output_tokens"])
        for r in rows
        for u in r["usages"]
    ]
    savings = measured_savings(usages, baseline_model)

    started_cheap = [r for r in rows if r["usages"] and
                     r["usages"][0]["model"] != baseline_model]
    unaided = [r for r in started_cheap if not r["escalated"]]
    success_rate = round(len(unaided) / len(started_cheap), 3) if started_cheap else 0.0

    be = break_even("claude-haiku-4-5", baseline_model)
    total_cost = sum(r["cost_usd"] for r in rows)

    return {
        "decisions": len(rows),
        "cost_per_decision_usd": round(total_cost / len(rows), 6),
        "escalation_rate": round(
            sum(1 for r in rows if r["escalated"]) / len(rows), 3
        ),
        "early_exit_rate": round(
            sum(1 for r in rows if r["early_exit"]) / len(rows), 3
        ),
        "cheap_success_rate": success_rate,
        "break_even_success_rate": be.required_success_rate,
        "routing_pays": success_rate > be.required_success_rate if started_cheap else None,
        "verdict": be.verdict(success_rate) if started_cheap else "no cheap-tier traffic yet",
        **savings,
    }
