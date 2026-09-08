"""Model registry.

Pricing and capability facts are kept in exactly one place because three
projects depend on them being right: P7 routes on price, P11 reports on price,
and every project's trace carries a cost computed from this table.

Prices are Anthropic first-party USD per million tokens, current as of
2026-09-07. `make verify-pricing` re-checks them against the live docs.
"""

from __future__ import annotations

from dataclasses import dataclass

# Standard Anthropic cache multipliers against the base input rate.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


@dataclass(frozen=True)
class ModelSpec:
    id: str
    tier: str  # small | mid | large — P7 routes on this, not on the id
    context: int
    max_output: int
    input_per_mtok: float
    output_per_mtok: float
    supports_effort: bool
    #: True  -> thinking={"type": "adaptive"}
    #: False -> thinking={"type": "enabled", "budget_tokens": N}
    adaptive_thinking: bool

    @property
    def display(self) -> str:
        return f"{self.id} (${self.input_per_mtok}/${self.output_per_mtok} per Mtok)"


MODEL_REGISTRY: dict[str, ModelSpec] = {
    "claude-haiku-4-5": ModelSpec(
        id="claude-haiku-4-5",
        tier="small",
        context=200_000,
        max_output=64_000,
        input_per_mtok=1.00,
        output_per_mtok=5.00,
        supports_effort=False,  # `effort` raises on Haiku 4.5
        adaptive_thinking=False,  # still takes budget_tokens
    ),
    "claude-sonnet-5": ModelSpec(
        id="claude-sonnet-5",
        tier="mid",
        context=1_000_000,
        max_output=128_000,
        input_per_mtok=2.00,
        output_per_mtok=10.00,
        supports_effort=True,
        adaptive_thinking=True,
    ),
    "claude-opus-5": ModelSpec(
        id="claude-opus-5",
        tier="large",
        context=1_000_000,
        max_output=128_000,
        input_per_mtok=5.00,
        output_per_mtok=25.00,
        supports_effort=True,
        adaptive_thinking=True,
    ),
}

#: Cheapest-first. P7's escalation ladder walks this in order.
LADDER: tuple[str, ...] = ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5")

DEFAULT_MODEL = "claude-opus-5"


def spec(model: str) -> ModelSpec:
    try:
        return MODEL_REGISTRY[model]
    except KeyError:
        known = ", ".join(MODEL_REGISTRY)
        raise ValueError(f"unknown model {model!r}; known models: {known}") from None


def next_model_up(model: str) -> str | None:
    """The next rung of the escalation ladder, or None at the top.

    Used by P1 (escalate after repeated schema failures) and P7 (escalate when
    the cheap model reports low confidence).
    """
    idx = LADDER.index(model)
    return LADDER[idx + 1] if idx + 1 < len(LADDER) else None
