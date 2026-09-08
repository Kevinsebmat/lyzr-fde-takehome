"""Token accounting and budget enforcement.

Every LLM call in every project passes through here, so `Budget` is the one
place a runaway loop gets stopped on spend rather than on iteration count.
P7 is built on it directly; P3/P9/P10 use it as a second safety net behind
their iteration caps.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .models import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER, spec


class BudgetExceeded(RuntimeError):
    """Raised *before* a call that would breach the budget, never after.

    Carries the numbers so callers can degrade gracefully and report why.
    """

    def __init__(self, spent_usd: float, limit_usd: float, projected_usd: float):
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        self.projected_usd = projected_usd
        super().__init__(
            f"budget exceeded: ${spent_usd:.4f} spent, next call projected at "
            f"${projected_usd:.4f}, limit ${limit_usd:.4f}"
        )


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def cost_usd(self) -> float:
        s = spec(self.model)
        inp = s.input_per_mtok / 1_000_000
        out = s.output_per_mtok / 1_000_000
        return (
            self.input_tokens * inp
            + self.output_tokens * out
            + self.cache_read_tokens * inp * CACHE_READ_MULTIPLIER
            + self.cache_write_tokens * inp * CACHE_WRITE_MULTIPLIER
        )

    @classmethod
    def from_response(cls, model: str, usage) -> Usage:
        """Build from an SDK `response.usage`, tolerating absent cache fields."""
        return cls(
            model=model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )


@dataclass
class Budget:
    """A spend ceiling for one task, with a pre-flight check.

    `limit_usd=None` means unlimited — used by the CLIs, never by the API.
    """

    limit_usd: float | None = None
    spent_usd: float = 0.0
    calls: int = 0
    by_model: dict[str, float] = field(default_factory=dict)

    def check(self, projected_usd: float = 0.0) -> None:
        """Refuse a call whose projected cost would breach the limit."""
        if self.limit_usd is None:
            return
        projected_total = self.spent_usd + projected_usd
        if projected_total > self.limit_usd:
            raise BudgetExceeded(self.spent_usd, self.limit_usd, projected_total)

    def charge(self, usage: Usage) -> Usage:
        self.spent_usd += usage.cost_usd
        self.calls += 1
        self.by_model[usage.model] = self.by_model.get(usage.model, 0.0) + usage.cost_usd
        return usage

    @property
    def remaining_usd(self) -> float | None:
        return None if self.limit_usd is None else max(0.0, self.limit_usd - self.spent_usd)

    def snapshot(self) -> dict:
        return {
            "spent_usd": round(self.spent_usd, 6),
            "limit_usd": self.limit_usd,
            "remaining_usd": (
                None if self.remaining_usd is None else round(self.remaining_usd, 6)
            ),
            "calls": self.calls,
            "by_model": {k: round(v, 6) for k, v in self.by_model.items()},
        }


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Price a hypothetical call. Used for the pre-flight `Budget.check`."""
    return Usage(model=model, input_tokens=input_tokens, output_tokens=output_tokens).cost_usd


def baseline_comparison(usages: list[Usage], baseline_model: str) -> dict:
    """What the same token volume would have cost on a single model.

    This is P7's headline metric: routed spend vs. always-Opus spend.
    """
    actual = sum(u.cost_usd for u in usages)
    baseline = sum(
        replace(u, model=baseline_model).cost_usd for u in usages
    )
    saved = baseline - actual
    return {
        "actual_usd": round(actual, 6),
        "baseline_usd": round(baseline, 6),
        "baseline_model": baseline_model,
        "saved_usd": round(saved, 6),
        "saved_pct": round(100 * saved / baseline, 2) if baseline else 0.0,
        "calls": len(usages),
    }
