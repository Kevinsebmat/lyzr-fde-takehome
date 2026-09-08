"""The rubric, and the judge that applies it.

The rubric is the actual engineering here. "Rate this 1-5" produces a number
that drifts between calls and means nothing across runs; every level needs a
concrete, checkable anchor before a score can be compared to last week's.

Known limitation, stated rather than hidden: a model judging output from the
same model family inflates its scores. The mitigations here are anchored
levels, a judge that must quote the text it is criticising, and a separate
model tier for judging. That narrows the bias; it does not remove it. The real
fix is a human-labelled calibration set, which is scoped work in an engagement,
not something a demo can assert.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class Dimension:
    key: str
    name: str
    weight: float
    anchors: dict[int, str]

    def render(self) -> str:
        levels = "\n".join(f"    {score} = {text}" for score, text in sorted(self.anchors.items()))
        return f"{self.key} — {self.name} (weight {self.weight})\n{levels}"


DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(
        key="accuracy",
        name="Factual accuracy against the case notes",
        weight=0.35,
        anchors={
            1: "States something the case notes contradict.",
            3: "Nothing contradicted, but adds a detail the notes do not contain.",
            5: "Every factual statement traces to the case notes. Nothing invented.",
        },
    ),
    Dimension(
        key="completeness",
        name="Answers everything the customer asked",
        weight=0.25,
        anchors={
            1: "Ignores at least one of the customer's questions.",
            3: "Addresses every question, but leaves one answer vague.",
            5: "Every question answered specifically, with a number or a date where one exists.",
        },
    ),
    Dimension(
        key="tone",
        name="Tone appropriate to an angry customer",
        weight=0.20,
        anchors={
            1: "Defensive, blames the customer, or is coldly transactional.",
            3: "Polite but generic — could have been sent to anyone.",
            5: "Acknowledges the specific impact on this customer without grovelling "
               "or over-apologising.",
        },
    ),
    Dimension(
        key="actionability",
        name="Says what happens next",
        weight=0.20,
        anchors={
            1: "No next step, or an unowned one ('someone will look into it').",
            3: "A next step with no owner or no date.",
            5: "A named owner and a specific date or timeframe for each next step.",
        },
    ),
)

TARGET_SCORE = 4.2
MAX_SCORE = 5.0


class DimensionScore(BaseModel):
    key: str
    score: int = Field(ge=1, le=5)
    #: The judge must quote what it is judging. A critique that cannot point at
    #: the text is not actionable, and is usually the judge inventing a flaw.
    evidence: str = Field(min_length=1, max_length=300)
    fix: str = Field(min_length=1, max_length=300, description="One specific change.")


class Judgement(BaseModel):
    model_config = {"extra": "forbid"}

    scores: list[DimensionScore] = Field(min_length=1)
    overall_note: str = Field(min_length=1, max_length=400)

    def weighted(self) -> float:
        by_key = {d.key: d for d in DIMENSIONS}
        total = weight = 0.0
        for s in self.scores:
            dim = by_key.get(s.key)
            if dim is None:
                continue  # a dimension the rubric doesn't define scores nothing
            total += s.score * dim.weight
            weight += dim.weight
        return round(total / weight, 3) if weight else 0.0

    def weakest(self, n: int = 2) -> list[DimensionScore]:
        known = {d.key for d in DIMENSIONS}
        return sorted(
            (s for s in self.scores if s.key in known), key=lambda s: s.score
        )[:n]


def render_rubric() -> str:
    return "\n\n".join(d.render() for d in DIMENSIONS)


JUDGE_SYSTEM = f"""You score a drafted customer reply against a fixed rubric.

Score each dimension 1-5 using these anchors. Do not invent intermediate
criteria; use the anchors as written.

{render_rubric()}

For every dimension you must quote the specific text you are scoring in
`evidence`, and give one concrete change in `fix`. A critique you cannot
attach to actual text is not a critique.

Score what is on the page, not what you assume the writer meant. Be strict:
a 5 means there is nothing left to fix on that dimension."""
