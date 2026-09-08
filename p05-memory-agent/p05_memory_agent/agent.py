"""The conversational agent that uses the memory.

The loop per turn: recall relevant facts → answer with them in context →
extract anything durable the user just said → supersede what it contradicts →
compress the buffer if it has grown too large.

Extraction is the step that decides whether the memory is useful or is
landfill, so it is a separate, structured call with an explicit contract about
what counts as durable — not a side effect of the answering prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from agentcore import LLM, Budget, ParseError, default_llm, tracing
from pydantic import BaseModel, Field

from .memory import Fact, FactKind, MemoryStore, ScoredFact

log = logging.getLogger("p05.agent")

CHAT_SYSTEM = """You are an ongoing assistant to a returning customer.

Use the remembered facts naturally — do not announce that you are consulting
memory, and do not re-ask for something already remembered. If a remembered
fact conflicts with what the user just said, believe the user and say plainly
that you have updated it.

Never state a remembered fact you were not given."""

EXTRACT_SYSTEM = """Extract only durable facts from the user's latest message.

Durable means it will still matter in a month: identity, a stated preference, a
constraint, or a decision.

Not durable, and must not be extracted: pleasantries, one-off questions,
restatements of something you already know, or anything the user asked about
rather than asserted.

If the message contradicts or updates an existing fact, set `supersedes` to
that fact's id. Do not emit a new fact that merely rephrases an existing one.

Most messages contain nothing durable. An empty list is the common, correct
answer."""


class ExtractedFact(BaseModel):
    kind: FactKind
    text: str = Field(min_length=3, max_length=300,
                      description="One self-contained fact, readable without context.")
    supersedes: str | None = Field(
        default=None, description="Id of the existing fact this replaces, if any."
    )


class Extraction(BaseModel):
    model_config = {"extra": "forbid"}

    facts: list[ExtractedFact] = Field(default_factory=list)


@dataclass
class TurnResult:
    reply: str
    recalled: list[ScoredFact] = field(default_factory=list)
    learned: list[Fact] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)
    compressed: bool = False
    cost_usd: float = 0.0

    def summary(self) -> dict:
        return {
            "recalled": len(self.recalled),
            "learned": len(self.learned),
            "superseded": len(self.superseded),
            "compressed": self.compressed,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class MemoryAgent:
    user_id: str
    llm: LLM = None  # type: ignore[assignment]
    memory: MemoryStore = None  # type: ignore[assignment]
    recall_k: int = 4
    extract: bool = True

    def __post_init__(self):
        if self.llm is None:
            self.llm = default_llm("p05-memory-agent", budget=Budget(limit_usd=0.40))
        if self.memory is None:
            self.memory = MemoryStore(self.user_id)

    def chat(self, message: str) -> TurnResult:
        with tracing.run("p05-memory-agent", "turn", user=self.user_id):
            before = self.llm.budget.spent_usd

            recalled = self.memory.recall(message, k=self.recall_k)
            reply = self._answer(message, recalled)

            self.memory.append("user", message)
            self.memory.append("assistant", reply)

            learned, superseded = ([], [])
            if self.extract:
                learned, superseded = self._learn(message)

            compressed = False
            if self.memory.needs_compression():
                self._compress()
                compressed = True

            return TurnResult(
                reply=reply,
                recalled=recalled,
                learned=learned,
                superseded=superseded,
                compressed=compressed,
                cost_usd=self.llm.budget.spent_usd - before,
            )

    # ---------- steps ----------

    def _answer(self, message: str, recalled: list[ScoredFact]) -> str:
        parts = []
        if recalled:
            parts.append(
                "REMEMBERED ABOUT THIS USER\n"
                + "\n".join(f"- [{s.fact.kind.value}] {s.fact.text}" for s in recalled)
            )
        if (summary := self.memory.summary()):
            parts.append(f"EARLIER IN THIS CONVERSATION (summarised)\n{summary}")
        recent = self.memory.buffer()[-6:]
        if recent:
            parts.append(
                "RECENT TURNS\n" + "\n".join(f"{t.role}: {t.content}" for t in recent)
            )
        parts.append(f"USER\n{message}")
        return self.llm.complete(
            "\n\n".join(parts), system=CHAT_SYSTEM, name="p05.answer"
        ).text.strip()

    def _learn(self, message: str) -> tuple[list[Fact], list[str]]:
        """Extract durable facts and apply supersession.

        Existing facts are shown with their ids so the model can point at the
        one it is replacing. Without that it can only ever add, and the store
        accumulates contradictions.
        """
        existing = self.memory.all_facts()
        known = "\n".join(f"[{f.id}] ({f.kind.value}) {f.text}" for f in existing) or "(none)"
        prompt = f"EXISTING FACTS\n{known}\n\nUSER'S LATEST MESSAGE\n{message}"

        try:
            extraction, _ = self.llm.parse(
                prompt, Extraction, system=EXTRACT_SYSTEM, name="p05.extract"
            )
        except ParseError as exc:
            # Losing an extraction costs one fact; failing the turn costs the
            # conversation. Memory is an enhancement, not the critical path.
            log.warning("fact extraction failed, continuing without it: %s", exc)
            return [], []

        valid_ids = {f.id for f in existing}
        learned: list[Fact] = []
        superseded: list[str] = []

        for item in extraction.facts:
            target = item.supersedes
            if target and target not in valid_ids:
                # A hallucinated id would silently supersede nothing. Store the
                # fact anyway — dropping a real update is the worse failure.
                log.warning("extraction referenced unknown fact id %s", target)
                target = None
            fact = self.memory.remember(item.kind, item.text, supersedes=target)
            learned.append(fact)
            if target:
                superseded.append(target)

        return learned, superseded

    def _compress(self) -> None:
        turns = self.memory.buffer()
        transcript = "\n".join(f"{t.role}: {t.content}" for t in turns[:-4])
        if not transcript.strip():
            return
        prompt = (
            f"Summarise this conversation segment in under 120 words. Keep "
            f"decisions, numbers, names and open questions. Drop pleasantries "
            f"and anything already superseded.\n\n{transcript}"
        )
        with tracing.span("memory.compress", kind="step", turns=len(turns)):
            summary = self.llm.complete(prompt, name="p05.compress").text.strip()
        self.memory.compress(summary)

    # ---------- session boundary ----------

    def end_session(self) -> None:
        """Close the conversation. Facts survive; the buffer does not.

        This is the line the whole project is about: the next session starts
        with no transcript and full knowledge of the user.
        """
        self.memory.clear_buffer()
