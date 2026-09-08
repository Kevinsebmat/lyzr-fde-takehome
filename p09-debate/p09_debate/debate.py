"""Multi-agent debate: proposers, a critic, a vote, an aggregator.

The reason to run several agents instead of one is *independence* — several
correlated opinions are one opinion that costs five times as much. Everything
here is aimed at keeping the proposals genuinely independent and then being
honest about how much they actually agreed.

**Proposers do not see each other's work.** The moment one proposal is in the
prompt, the next is anchored to it and the debate collapses into agreement with
whoever went first. They get different personas instead, so they disagree for
structural reasons rather than by chance.

**Consensus is measured, not assumed.** Unanimity among agents primed to think
alike is not evidence. The aggregate reports how much of the vote the winner
took and whether the critic found a blocking flaw, and low agreement lowers the
reported confidence rather than being smoothed away.

**A tie is a result.** Splitting 2-2 is information: the question is genuinely
contested and a human should see it. Breaking ties by arbitrary ordering
manufactures a decision nobody made.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from agentcore import LLM, Budget, ParseError, default_llm, tracing
from pydantic import BaseModel, Field

log = logging.getLogger("p09.debate")


class Verdict(str, Enum):
    consensus = "consensus"           # a clear winner the critic did not block
    majority = "majority"             # a winner, but a real minority existed
    contested = "contested"           # tied or near-tied: send it to a human
    blocked = "blocked"               # the critic found a disqualifying flaw
    failed = "failed"


@dataclass(frozen=True)
class Persona:
    """A structural reason to disagree.

    Personas are not flavour text. Three agents given the same prompt produce
    three samples from the same distribution; three given genuinely different
    priorities produce arguments that can actually conflict.
    """

    key: str
    name: str
    brief: str


PERSONAS: tuple[Persona, ...] = (
    Persona(
        key="pragmatist",
        name="Delivery engineer",
        brief="You optimise for shipping something that works this quarter. You "
              "are sceptical of rewrites and of solutions that need a new system "
              "to be stood up. Call out where a proposal's cost lands on the team.",
    ),
    Persona(
        key="risk",
        name="Risk and compliance",
        brief="You optimise for what happens when this fails at 3am, and for what "
              "an auditor asks afterwards. You care about blast radius, data "
              "handling and reversibility. You would rather be slow than sorry.",
    ),
    Persona(
        key="economist",
        name="Cost owner",
        brief="You optimise for total cost of ownership: infrastructure, tokens, "
              "and the engineer-hours to run the thing for two years. You are "
              "hostile to solutions whose cost scales with usage.",
    ),
    Persona(
        key="user_advocate",
        name="User advocate",
        brief="You optimise for the person on the other end. You care about "
              "latency, clarity, and what happens to them when the system is "
              "wrong. You are hostile to solutions that are elegant internally "
              "and confusing outside.",
    ),
)

PROPOSE_SYSTEM = """You are one of several independent advisors answering the
same question. You have not seen the others' answers and should not speculate
about them.

Give one clear recommendation from your own perspective. State the strongest
argument against your own position — an advisor who cannot name the downside of
their recommendation has not thought about it."""

CRITIC_SYSTEM = """You review competing proposals for flaws.

For each, name concrete problems, not stylistic preferences. A `fatal` flaw is
one that makes the proposal actively unsafe or certain to fail — not one that
makes it merely worse than another proposal. Be sparing with `fatal`: marking
everything fatal blocks the decision and marking nothing fatal is useless.

Judge each proposal on its own merits, in the order given. Do not let a strong
first proposal set the standard for the rest."""

VOTE_SYSTEM = """You are voting on competing proposals, having read the critic.

Pick the single proposal you would actually back and say why in one sentence.
Vote for the best proposal on the merits — not the most popular-sounding, and
not a compromise between them."""

SYNTHESIS_SYSTEM = """You write the final recommendation from a debate.

Lead with the decision. Then give the strongest reason for it and the strongest
reason against, drawn from the actual proposals and critique.

If the vote was close or the critic found serious problems, say so plainly in
the recommendation. A reader must not come away more confident than the debate
warrants."""


class Proposal(BaseModel):
    model_config = {"extra": "forbid"}

    recommendation: str = Field(min_length=10, max_length=600)
    reasoning: str = Field(min_length=10, max_length=1200)
    strongest_counterargument: str = Field(min_length=5, max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)


class Flaw(BaseModel):
    proposal_key: str
    issue: str = Field(min_length=5, max_length=400)
    fatal: bool = Field(description="Actively unsafe or certain to fail.")


class Critique(BaseModel):
    model_config = {"extra": "forbid"}

    flaws: list[Flaw] = Field(default_factory=list)
    note: str = Field(default="", max_length=600)


class Vote(BaseModel):
    model_config = {"extra": "forbid"}

    choice: str = Field(description="The persona key of the proposal being backed.")
    reason: str = Field(min_length=5, max_length=400)


@dataclass
class DebateResult:
    question: str
    verdict: Verdict
    recommendation: str
    proposals: dict[str, Proposal] = field(default_factory=dict)
    critique: Critique | None = None
    votes: dict[str, str] = field(default_factory=dict)   # voter key -> chosen key
    tally: dict[str, int] = field(default_factory=dict)
    winner: str | None = None
    confidence: float = 0.0
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def agreement(self) -> float:
        """Share of the vote the winner took. 1.0 is unanimity."""
        total = sum(self.tally.values())
        return round(max(self.tally.values()) / total, 3) if total else 0.0

    @property
    def decided(self) -> bool:
        return self.verdict in (Verdict.consensus, Verdict.majority)

    def summary(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "winner": self.winner,
            "agreement": self.agreement,
            "confidence": round(self.confidence, 3),
            "proposals": len(self.proposals),
            "fatal_flaws": sum(1 for f in (self.critique.flaws if self.critique else [])
                               if f.fatal),
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class DebateSystem:
    llm: LLM = None  # type: ignore[assignment]
    personas: tuple[Persona, ...] = PERSONAS
    #: Below this share of the vote, the result is contested rather than decided.
    majority_threshold: float = 0.5
    #: Above this, it is consensus rather than a bare majority.
    consensus_threshold: float = 0.75
    #: Cheaper model for proposals; the aggregator gets the strong one. Four
    #: proposals on the top tier is where a debate's cost runs away.
    proposer_model: str = "claude-sonnet-5"

    def __post_init__(self):
        if self.llm is None:
            self.llm = default_llm("p09-debate", budget=Budget(limit_usd=0.80))

    def run(self, question: str) -> DebateResult:
        with tracing.run("p09-debate", "debate", question=question[:120]):
            before = self.llm.budget.spent_usd
            notes: list[str] = []

            proposals = self._propose(question, notes)
            if len(proposals) < 2:
                return DebateResult(
                    question=question,
                    verdict=Verdict.failed,
                    recommendation="Too few usable proposals to hold a debate.",
                    proposals=proposals,
                    cost_usd=self.llm.budget.spent_usd - before,
                    notes=notes,
                )

            critique = self._critique(question, proposals, notes)
            votes, tally = self._vote(question, proposals, critique, notes)

            return self._decide(
                question, proposals, critique, votes, tally, notes, before
            )

    # ---------- stages ----------

    def _propose(self, question: str, notes: list[str]) -> dict[str, Proposal]:
        """Each persona answers independently.

        Deliberately sequential calls with no shared history: nothing from one
        proposal may reach another's prompt.
        """
        proposals: dict[str, Proposal] = {}
        for persona in self.personas:
            system = f"{PROPOSE_SYSTEM}\n\nYOUR PERSPECTIVE\n{persona.name}: {persona.brief}"
            try:
                proposal, _ = self.llm.parse(
                    f"QUESTION\n{question}",
                    Proposal,
                    system=system,
                    model=self.proposer_model,
                    name=f"p09.propose.{persona.key}",
                )
                proposals[persona.key] = proposal
            except ParseError as exc:
                # One advisor failing is a smaller debate, not a failed one.
                log.warning("proposer %s produced unusable output: %s", persona.key, exc)
                notes.append(f"proposer `{persona.key}` failed and was dropped")
        return proposals

    def _critique(
        self, question: str, proposals: dict[str, Proposal], notes: list[str]
    ) -> Critique:
        rendered = "\n\n".join(
            f"[{key}] {p.recommendation}\nReasoning: {p.reasoning}\n"
            f"Their own counterargument: {p.strongest_counterargument}"
            for key, p in proposals.items()
        )
        try:
            critique, _ = self.llm.parse(
                f"QUESTION\n{question}\n\nPROPOSALS\n{rendered}",
                Critique,
                system=CRITIC_SYSTEM,
                name="p09.critique",
            )
        except ParseError:
            notes.append("critic failed; voting proceeded without a critique")
            return Critique()

        # A critic that flags every proposal as fatal has blocked the decision
        # without informing it. Treat that as a failed critique, not a verdict.
        fatal_keys = {f.proposal_key for f in critique.flaws if f.fatal}
        if fatal_keys >= set(proposals) and len(proposals) > 1:
            notes.append(
                "critic marked every proposal fatally flawed — treating the "
                "critique as uninformative rather than blocking the decision"
            )
            for flaw in critique.flaws:
                flaw.fatal = False
        return critique

    def _vote(
        self,
        question: str,
        proposals: dict[str, Proposal],
        critique: Critique,
        notes: list[str],
    ) -> tuple[dict[str, str], dict[str, int]]:
        rendered = "\n\n".join(
            f"[{key}] {p.recommendation}" for key, p in proposals.items()
        )
        flaws = "\n".join(
            f"- [{f.proposal_key}]{' FATAL' if f.fatal else ''}: {f.issue}"
            for f in critique.flaws
        ) or "(no flaws raised)"

        votes: dict[str, str] = {}
        for persona in self.personas:
            if persona.key not in proposals:
                continue
            system = (
                f"{VOTE_SYSTEM}\n\nYOUR PERSPECTIVE\n{persona.name}: {persona.brief}"
            )
            try:
                vote, _ = self.llm.parse(
                    f"QUESTION\n{question}\n\nPROPOSALS\n{rendered}\n\n"
                    f"CRITIC'S FINDINGS\n{flaws}\n\n"
                    f"Valid choices: {', '.join(proposals)}",
                    Vote,
                    system=system,
                    model=self.proposer_model,
                    name=f"p09.vote.{persona.key}",
                )
            except ParseError:
                notes.append(f"voter `{persona.key}` failed and was skipped")
                continue

            if vote.choice not in proposals:
                # A vote for something that does not exist cannot be counted,
                # and guessing what they meant invents a result.
                notes.append(
                    f"voter `{persona.key}` chose `{vote.choice}`, which is not a "
                    "proposal — discarded"
                )
                continue
            votes[persona.key] = vote.choice

        tally: dict[str, int] = {key: 0 for key in proposals}
        for choice in votes.values():
            tally[choice] += 1
        return votes, tally

    def _decide(
        self, question, proposals, critique, votes, tally, notes, before
    ) -> DebateResult:
        result = DebateResult(
            question=question,
            verdict=Verdict.failed,
            recommendation="",
            proposals=proposals,
            critique=critique,
            votes=votes,
            tally=tally,
            notes=notes,
        )

        if not votes:
            result.verdict = Verdict.failed
            result.recommendation = "No valid votes were cast."
            result.cost_usd = self.llm.budget.spent_usd - before
            return result

        top = max(tally.values())
        leaders = [key for key, count in tally.items() if count == top]

        fatal = {f.proposal_key for f in critique.flaws if f.fatal}
        surviving = [key for key in leaders if key not in fatal]

        if not surviving:
            # The winner is disqualified. Report that rather than silently
            # promoting the runner-up nobody voted for.
            result.verdict = Verdict.blocked
            result.winner = None
            result.confidence = 0.1
            result.recommendation = (
                "No recommendation. The proposal that won the vote "
                f"(`{leaders[0]}`) was found to have a disqualifying flaw: "
                + "; ".join(f.issue for f in critique.flaws if f.proposal_key in leaders)
            )
            result.cost_usd = self.llm.budget.spent_usd - before
            return result

        if len(surviving) > 1:
            # A tie is information: the question is genuinely contested.
            result.verdict = Verdict.contested
            result.winner = None
            result.confidence = 0.35
            result.recommendation = self._synthesise(
                question, proposals, critique, tally, winner=None
            )
            result.cost_usd = self.llm.budget.spent_usd - before
            return result

        winner = surviving[0]
        agreement = top / sum(tally.values())
        result.winner = winner

        if agreement < self.majority_threshold:
            result.verdict = Verdict.contested
        elif agreement >= self.consensus_threshold:
            result.verdict = Verdict.consensus
        else:
            result.verdict = Verdict.majority

        # Confidence tracks measured agreement, discounted by the proposers'
        # own confidence and by any non-fatal flaws found in the winner.
        winner_flaws = sum(1 for f in critique.flaws if f.proposal_key == winner)
        mean_self = sum(p.confidence for p in proposals.values()) / len(proposals)
        result.confidence = round(
            max(0.0, min(1.0, 0.6 * agreement + 0.4 * mean_self - 0.05 * winner_flaws)),
            3,
        )
        result.recommendation = self._synthesise(
            question, proposals, critique, tally, winner
        )
        result.cost_usd = self.llm.budget.spent_usd - before
        return result

    def _synthesise(self, question, proposals, critique, tally, winner) -> str:
        rendered = "\n\n".join(
            f"[{key}] {p.recommendation}\nReasoning: {p.reasoning}\n"
            f"Their counterargument: {p.strongest_counterargument}"
            for key, p in proposals.items()
        )
        flaws = "\n".join(
            f"- [{f.proposal_key}]{' FATAL' if f.fatal else ''}: {f.issue}"
            for f in critique.flaws
        ) or "(none)"
        outcome = (
            f"`{winner}` won the vote {tally}."
            if winner
            else f"The vote was tied: {tally}. There is no winner."
        )
        prompt = (
            f"QUESTION\n{question}\n\nPROPOSALS\n{rendered}\n\n"
            f"CRITIC\n{flaws}\n\nOUTCOME\n{outcome}\n\n"
            "Write the final recommendation."
        )
        try:
            return self.llm.complete(
                prompt, system=SYNTHESIS_SYSTEM, name="p09.synthesise"
            ).text.strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("synthesis failed: %s", exc)
            if winner:
                return proposals[winner].recommendation
            return "The debate did not reach a decision."
