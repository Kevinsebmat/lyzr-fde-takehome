"""The RAG agent: retrieve, answer with citations, verify, then decide.

The decision at the end is the project. A RAG system that always answers has
not solved hallucination — it has moved it behind a citation. This agent has
four outcomes and picks between them on measured evidence:

    ANSWER          confident and fully grounded
    ANSWER_CAVEATED grounded, but weak enough that the user is told so
    FALLBACK_SEARCH the corpus does not cover it; go outside, and label it
    REFUSE          no trustworthy answer available; say so and stop

Refusing is a feature. It is the behaviour a customer is actually buying when
they buy "grounded" RAG, and the one a demo never shows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from agentcore import LLM, Budget, ParseError, default_llm, store, tracing
from pydantic import BaseModel, Field

from .corpus import COLLECTION, all_chunks
from .grounding import Claim, Confidence, Grounding, score_confidence, verify
from .search import SearchResult, web_search

log = logging.getLogger("p02.agent")


class Action(str, Enum):
    answer = "answer"
    answer_caveated = "answer_caveated"
    fallback_search = "fallback_search"
    refuse = "refuse"


# These are product decisions, not constants: a legal-research customer wants
# them higher, a brainstorming tool lower. They are constructor parameters for
# exactly that reason.
ANSWER_THRESHOLD = 0.62
CAVEAT_THRESHOLD = 0.45

# Deliberately low. Measured on this corpus, retrieval scores for answerable
# and unanswerable questions *overlap* (0.27–0.46 vs 0.10–0.31), so a floor set
# high enough to reject the unanswerable ones also rejects real questions.
# Retrieval score is therefore a cheap pre-filter — "is there anything here at
# all", worth skipping an LLM call for — and not the safety mechanism. Citation
# verification is the safety mechanism.
#
# Absolute scores are also embedding-backend specific: this is calibrated for
# the offline lexical embedder, and wants re-tuning against a held-out set when
# real embeddings are switched on. That re-tuning is scoped work in the P2
# engagement, not a constant someone can guess.
RETRIEVAL_FLOOR = 0.15

SYSTEM = """You answer questions strictly from the supplied sources.

Break your answer into individual claims. Every claim must cite the id of at
least one source that directly states it.

Hard rules:
- Never state anything the sources do not contain. Not background, not context,
  not common knowledge.
- Never cite a source id that is not in the supplied list.
- If the sources do not answer the question, return an empty claims list and
  set answerable to false. This is a correct and expected outcome.
- Copy figures, dates and amounts exactly as the sources give them.
- `self_confidence` is your honest estimate that the sources fully answer the
  question. Be harsh: partial coverage is not full coverage."""


class DraftClaim(BaseModel):
    text: str = Field(min_length=1, description="A single factual assertion.")
    citation_ids: list[str] = Field(
        default_factory=list, description="Source ids that directly state this claim."
    )


class DraftAnswer(BaseModel):
    model_config = {"extra": "forbid"}

    answerable: bool = Field(description="False when the sources do not cover the question.")
    claims: list[DraftClaim] = Field(default_factory=list)
    self_confidence: float = Field(ge=0.0, le=1.0)


@dataclass
class RagResult:
    question: str
    action: Action
    answer: str
    claims: list[Claim] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    confidence: Confidence | None = None
    grounding: Grounding | None = None
    search_results: list[SearchResult] = field(default_factory=list)
    cost_usd: float = 0.0
    retrieved: int = 0

    @property
    def answered(self) -> bool:
        return self.action in (Action.answer, Action.answer_caveated)

    def summary(self) -> dict:
        return {
            "action": self.action.value,
            "confidence": round(self.confidence.score, 3) if self.confidence else 0.0,
            "claims": len(self.claims),
            "grounded": round(self.grounding.grounded_fraction, 3) if self.grounding else 0.0,
            "retrieved": self.retrieved,
            "cost_usd": round(self.cost_usd, 6),
        }


def build_prompt(question: str, hits: list[store.Hit]) -> str:
    """Render retrieved chunks into the generation prompt.

    A module-level function rather than a method so the cassette seeder can
    reproduce the exact request the agent makes. A recorded response keyed on a
    prompt built a slightly different way would silently never match.
    """
    sources = "\n\n".join(
        f"[{h.id}] {h.title} — {h.metadata.get('heading', '')}\n{h.text}" for h in hits
    )
    return f"SOURCES\n\n{sources}\n\nQUESTION\n\n{question}"


def index_corpus(force: bool = False) -> int:
    """Embed and store the corpus. Idempotent — the embedding cache means a
    re-index of unchanged text costs nothing."""
    chunks = all_chunks()
    if not force and store.count(COLLECTION) >= len(chunks):
        return store.count(COLLECTION)
    with tracing.span("index_corpus", chunks=len(chunks)):
        store.add_documents(
            COLLECTION, [c.as_document() for c in chunks], store.Embedder()
        )
    return len(chunks)


@dataclass
class RagAgent:
    llm: LLM = None  # type: ignore[assignment]
    k: int = 4
    answer_threshold: float = ANSWER_THRESHOLD
    caveat_threshold: float = CAVEAT_THRESHOLD
    retrieval_floor: float = RETRIEVAL_FLOOR
    allow_search_fallback: bool = True
    embedder: store.Embedder = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.llm is None:
            self.llm = default_llm("p02-rag-citations", budget=Budget(limit_usd=0.50))
        if self.embedder is None:
            self.embedder = store.Embedder()
        index_corpus()

    def ask(self, question: str) -> RagResult:
        with tracing.run("p02-rag-citations", "ask", question=question[:120]):
            before = self.llm.budget.spent_usd

            hits = self._retrieve(question)
            if not hits or max(h.score for h in hits) < self.retrieval_floor:
                # Nothing worth reading. Going to the model here would produce a
                # fluent answer from its own parameters — the exact failure the
                # citation machinery exists to prevent — so short-circuit before
                # spending a token.
                return self._no_evidence(question, hits, before)

            evidence = {h.id: h.text for h in hits}
            try:
                draft = self._draft(question, hits)
            except ParseError as exc:
                log.error("answer generation failed schema validation: %s", exc)
                return self._finalise(
                    question, Action.refuse,
                    "I could not produce a well-formed grounded answer to that.",
                    hits, None, None, before,
                )

            if not draft.answerable or not draft.claims:
                # The model itself says the corpus does not cover this.
                return self._uncovered(question, hits, draft, before)

            claims = [Claim(text=c.text, citation_ids=c.citation_ids) for c in draft.claims]
            grounding = verify(claims, evidence)
            confidence = score_confidence(
                [h.score for h in hits], grounding, draft.self_confidence
            )

            return self._decide(question, hits, grounding, confidence, before)

    # ---------- steps ----------

    def _retrieve(self, question: str) -> list[store.Hit]:
        with tracing.span("retrieve", kind="step") as sp:
            hits = store.search(COLLECTION, question, self.embedder, k=self.k)
            sp.attrs.update(hits=len(hits), best=round(hits[0].score, 3) if hits else 0.0)
            return hits

    def _draft(self, question: str, hits: list[store.Hit]) -> DraftAnswer:
        draft, _ = self.llm.parse(
            build_prompt(question, hits), DraftAnswer, system=SYSTEM, name="rag.draft"
        )
        return draft

    def _decide(
        self,
        question: str,
        hits: list[store.Hit],
        grounding: Grounding,
        confidence: Confidence,
        before: float,
    ) -> RagResult:
        supported = [c for c in grounding.claims if c.verified]

        if confidence.score >= self.answer_threshold and grounding.fully_grounded:
            answer = " ".join(c.text for c in supported)
            return self._finalise(
                question, Action.answer, answer, hits, grounding, confidence, before
            )

        if confidence.score >= self.caveat_threshold and supported:
            # Partial answer with the unverified parts removed, not softened.
            # Keeping an unsupported claim and hedging it is how a hedge becomes
            # a citation the reader trusts anyway.
            dropped = len(grounding.claims) - len(supported)
            answer = (
                " ".join(c.text for c in supported)
                + f"\n\n[Low confidence: {dropped} statement(s) could not be verified "
                "against the cited sources and were removed. Please confirm before "
                "relying on this.]"
            )
            return self._finalise(
                question, Action.answer_caveated, answer, hits, grounding, confidence, before
            )

        if self.allow_search_fallback:
            return self._search_fallback(question, hits, grounding, confidence, before)

        return self._finalise(
            question,
            Action.refuse,
            self._refusal_text(confidence),
            hits, grounding, confidence, before,
        )

    def _search_fallback(
        self, question, hits, grounding, confidence, before
    ) -> RagResult:
        with tracing.span("search_fallback", kind="step") as sp:
            results = web_search(question)
            sp.attrs["results"] = len(results)

        if not results:
            # No search backend configured, or nothing found. Refusing is the
            # correct outcome — not a softer answer from the same weak evidence.
            result = self._finalise(
                question, Action.refuse, self._refusal_text(confidence),
                hits, grounding, confidence, before,
            )
            return result

        body = "\n".join(f"- {r.title}: {r.snippet} ({r.url})" for r in results[:3])
        answer = (
            "This is not covered by the internal knowledge base. From external "
            f"sources, which are not verified against our documentation:\n\n{body}"
        )
        result = self._finalise(
            question, Action.fallback_search, answer, hits, grounding, confidence, before
        )
        result.search_results = results
        return result

    def _no_evidence(self, question, hits, before) -> RagResult:
        confidence = score_confidence([h.score for h in hits], Grounding(), 0.0)
        if self.allow_search_fallback:
            return self._search_fallback(question, hits, Grounding(), confidence, before)
        return self._finalise(
            question, Action.refuse,
            "I have nothing in the knowledge base relevant to that question.",
            hits, Grounding(), confidence, before,
        )

    def _uncovered(self, question, hits, draft, before) -> RagResult:
        grounding = Grounding()
        confidence = score_confidence([h.score for h in hits], grounding, draft.self_confidence)
        if self.allow_search_fallback:
            return self._search_fallback(question, hits, grounding, confidence, before)
        return self._finalise(
            question, Action.refuse,
            "The knowledge base does not cover that. I would rather say so than guess.",
            hits, grounding, confidence, before,
        )

    def _refusal_text(self, confidence: Confidence | None) -> str:
        base = "I can't answer that reliably from the knowledge base."
        if confidence and confidence.reasons:
            return base + " Why: " + "; ".join(confidence.reasons) + "."
        return base

    def _finalise(
        self, question, action, answer, hits, grounding, confidence, before
    ) -> RagResult:
        cited = {c for cl in (grounding.claims if grounding else []) for c in cl.citation_ids}
        return RagResult(
            question=question,
            action=action,
            answer=answer,
            claims=grounding.claims if grounding else [],
            citations=[
                {
                    "id": h.id,
                    "title": h.title,
                    "source": h.source,
                    "heading": h.metadata.get("heading", ""),
                    "score": round(h.score, 3),
                    "text": h.text,
                    "used": h.id in cited,
                }
                for h in hits
            ],
            confidence=confidence,
            grounding=grounding,
            cost_usd=self.llm.budget.spent_usd - before,
            retrieved=len(hits),
        )
