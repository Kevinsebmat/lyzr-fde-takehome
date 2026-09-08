"""Self-reflective agent: generate → judge → critique → regenerate.

Two things make this more than a loop with an LLM in it.

**Keep the best, not the last.** Regeneration frequently makes output *worse* —
the model over-corrects the flaw it was shown and breaks something that was
already fine. A loop that returns its final iteration ships that regression.
This one scores every attempt and returns the highest-scoring one, so an extra
iteration can never leave you worse off than stopping early would have.

**The metric is the deliverable.** "It reflects and improves" is a claim; a
logged trajectory — per-dimension scores per iteration, the delta, which
attempt won, how much each point of improvement cost — is evidence. Without it
you cannot tell whether reflection helped or just spent money, and the honest
answer on some tasks is that it spent money.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from agentcore import LLM, Budget, BudgetExceeded, ParseError, default_llm, store, tracing

from .rubric import (
    DIMENSIONS,
    JUDGE_SYSTEM,
    TARGET_SCORE,
    Judgement,
    render_rubric,
)

log = logging.getLogger("p10.agent")

HISTORY_NAMESPACE = "p10_runs"

WRITER_SYSTEM = """You draft replies to customer emails for a B2B SaaS support team.

Use only the case notes supplied. Never invent an account detail, a date, a
figure or a commitment that is not in the notes — if something is unknown, say
who will find out and by when.

Write the email only. No preamble, no subject line, no sign-off placeholder."""


class Stop(str, Enum):
    target_reached = "target_reached"
    max_iterations = "max_iterations"
    no_improvement = "no_improvement"
    budget_exhausted = "budget_exhausted"
    failed = "failed"


@dataclass
class Attempt:
    iteration: int
    text: str
    score: float
    per_dimension: dict[str, int] = field(default_factory=dict)
    critique: str = ""
    fixes: list[str] = field(default_factory=list)
    cost_usd: float = 0.0

    def as_dict(self) -> dict:
        return {
            "iteration": self.iteration,
            "score": self.score,
            "per_dimension": self.per_dimension,
            "critique": self.critique,
            "fixes": self.fixes,
            "cost_usd": round(self.cost_usd, 6),
            "chars": len(self.text),
        }


@dataclass
class ReflectionResult:
    task: str
    best: Attempt | None
    attempts: list[Attempt] = field(default_factory=list)
    stop: Stop = Stop.failed
    cost_usd: float = 0.0

    @property
    def trajectory(self) -> list[float]:
        return [a.score for a in self.attempts]

    @property
    def improvement(self) -> float:
        """Best score minus first score. Negative means every rewrite hurt."""
        if not self.attempts:
            return 0.0
        return round(max(self.trajectory) - self.trajectory[0], 3)

    @property
    def regressed(self) -> bool:
        """True when a later attempt scored worse than the best.

        Worth surfacing rather than hiding: it is the evidence that
        keep-the-best is load-bearing and not decoration.
        """
        if len(self.attempts) < 2:
            return False
        best_at = self.trajectory.index(max(self.trajectory))
        return any(s < max(self.trajectory) for s in self.trajectory[best_at + 1:])

    @property
    def cost_per_point(self) -> float | None:
        """Dollars per point of rubric improvement — the number that decides
        whether reflection is worth switching on for a given task."""
        if self.improvement <= 0:
            return None
        return round(self.cost_usd / self.improvement, 6)

    def summary(self) -> dict:
        return {
            "stop": self.stop.value,
            "iterations": len(self.attempts),
            "first_score": self.trajectory[0] if self.attempts else 0.0,
            "best_score": max(self.trajectory) if self.attempts else 0.0,
            "improvement": self.improvement,
            "regressed": self.regressed,
            "cost_usd": round(self.cost_usd, 6),
            "cost_per_point": self.cost_per_point,
        }


@dataclass
class SelfReflectiveAgent:
    llm: LLM = None  # type: ignore[assignment]
    max_iterations: int = 3
    target_score: float = TARGET_SCORE
    #: Minimum gain that counts as improvement. Below this it is judge noise,
    #: and spending another generation to chase it is waste.
    min_gain: float = 0.15
    #: Judge on a separate tier from the writer. Does not eliminate
    #: self-evaluation bias, but stops it being the same request lineage.
    judge_model: str = "claude-sonnet-5"

    def __post_init__(self):
        if self.llm is None:
            self.llm = default_llm("p10-self-reflective", budget=Budget(limit_usd=0.60))

    def run(self, task: str, case_notes: str) -> ReflectionResult:
        with tracing.run("p10-self-reflective", "reflect", task=task[:120]):
            before = self.llm.budget.spent_usd
            attempts: list[Attempt] = []
            previous: Attempt | None = None

            for i in range(1, self.max_iterations + 1):
                spent_at_start = self.llm.budget.spent_usd
                try:
                    text = (
                        self._write(task, case_notes)
                        if previous is None
                        else self._rewrite(task, case_notes, previous)
                    )
                    judgement = self._judge(task, case_notes, text)
                except BudgetExceeded as exc:
                    log.warning("stopping on budget: %s", exc)
                    return self._finish(task, attempts, Stop.budget_exhausted, before)
                except ParseError as exc:
                    log.error("iteration %d produced unusable output: %s", i, exc)
                    if attempts:
                        # Keep what already worked rather than losing the run.
                        return self._finish(task, attempts, Stop.failed, before)
                    return self._finish(task, [], Stop.failed, before)

                attempt = Attempt(
                    iteration=i,
                    text=text,
                    score=judgement.weighted(),
                    per_dimension={s.key: s.score for s in judgement.scores},
                    critique=judgement.overall_note,
                    fixes=[s.fix for s in judgement.weakest()],
                    cost_usd=self.llm.budget.spent_usd - spent_at_start,
                )
                attempts.append(attempt)
                log.info("iteration %d scored %.2f", i, attempt.score)

                if attempt.score >= self.target_score:
                    return self._finish(task, attempts, Stop.target_reached, before)

                if previous is not None and attempt.score - max(
                    a.score for a in attempts[:-1]
                ) < self.min_gain:
                    # The rewrite did not move the needle. Another one is
                    # unlikely to, and the best attempt is already banked.
                    return self._finish(task, attempts, Stop.no_improvement, before)

                previous = attempt

            return self._finish(task, attempts, Stop.max_iterations, before)

    # ---------- steps ----------

    def _write(self, task: str, case_notes: str) -> str:
        prompt = f"CASE NOTES\n{case_notes}\n\nTASK\n{task}"
        return self.llm.complete(prompt, system=WRITER_SYSTEM, name="p10.write").text.strip()

    def _rewrite(self, task: str, case_notes: str, previous: Attempt) -> str:
        """Regenerate under explicit constraints drawn from the critique.

        Two rules do the work: fix only what was named, and change nothing
        else. Without them the model rewrites wholesale and breaks the
        dimensions that already scored well — which is how a reflection loop
        produces a *worse* draft on iteration three.
        """
        weak = "\n".join(f"- {fix}" for fix in previous.fixes)
        prompt = (
            f"CASE NOTES\n{case_notes}\n\nTASK\n{task}\n\n"
            f"PREVIOUS DRAFT (scored {previous.score:.2f} of 5)\n{previous.text}\n\n"
            f"REVIEWER'S NOTE\n{previous.critique}\n\n"
            f"REQUIRED CHANGES\n{weak}\n\n"
            "Rewrite the email applying exactly these changes. Keep everything "
            "the reviewer did not criticise — including wording that already "
            "works. Do not restructure for its own sake, and do not add "
            "commitments the case notes do not support."
        )
        return self.llm.complete(prompt, system=WRITER_SYSTEM, name="p10.rewrite").text.strip()

    def _judge(self, task: str, case_notes: str, draft: str) -> Judgement:
        prompt = (
            f"CASE NOTES\n{case_notes}\n\nTASK\n{task}\n\nDRAFT TO SCORE\n{draft}\n\n"
            f"Score every dimension:\n{', '.join(d.key for d in DIMENSIONS)}"
        )
        judgement, _ = self.llm.parse(
            prompt,
            Judgement,
            system=JUDGE_SYSTEM,
            model=self.judge_model,
            name="p10.judge",
        )
        return judgement

    def _finish(
        self, task: str, attempts: list[Attempt], stop: Stop, before: float
    ) -> ReflectionResult:
        best = max(attempts, key=lambda a: a.score) if attempts else None
        result = ReflectionResult(
            task=task,
            best=best,
            attempts=attempts,
            stop=stop,
            cost_usd=self.llm.budget.spent_usd - before,
        )
        if result.regressed:
            log.info(
                "a later iteration scored below the best (%s) — returning iteration %d",
                result.trajectory, best.iteration if best else -1,
            )
        _record(result)
        return result


def _record(result: ReflectionResult) -> None:
    """Persist the trajectory. Improvement is only a claim until it is logged
    across runs and can be compared."""
    import time

    store.kv_set(
        HISTORY_NAMESPACE,
        str(time.time_ns()),
        {
            "task": result.task[:200],
            "trajectory": result.trajectory,
            **result.summary(),
        },
    )


def history(limit: int = 50) -> list[dict]:
    return [v for _, v in store.kv_list(HISTORY_NAMESPACE)[:limit]]


def aggregate() -> dict:
    """Does reflection actually pay on this workload?

    The question the metric exists to answer. If mean improvement is near zero,
    the honest recommendation is to switch the loop off for this task and spend
    the budget on a better first-draft prompt.
    """
    rows = history(500)
    if not rows:
        return {"runs": 0}
    improvements = [r["improvement"] for r in rows]
    return {
        "runs": len(rows),
        "mean_improvement": round(sum(improvements) / len(improvements), 3),
        "runs_that_improved": sum(1 for i in improvements if i > 0),
        "runs_that_regressed": sum(1 for r in rows if r.get("regressed")),
        "mean_iterations": round(sum(r["iterations"] for r in rows) / len(rows), 2),
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
    }


__all__ = [
    "Attempt", "ReflectionResult", "SelfReflectiveAgent", "Stop",
    "aggregate", "history", "render_rubric",
]
