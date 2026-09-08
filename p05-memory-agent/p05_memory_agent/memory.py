"""Memory: a short-term buffer, a long-term store, and the rules between them.

Storing text is not the hard part. Three things are:

**What is worth keeping.** "Thanks, that helps" is not a memory. Storing every
turn produces a store where the useful facts are outnumbered by pleasantries,
and retrieval then returns pleasantries. Only durable facts are promoted —
identity, preferences, constraints, decisions.

**What to do when a fact changes.** A user who says "we moved to Frankfurt"
after previously saying "we're in Dublin" has not given you two facts. If both
are retrievable the agent will eventually cite the stale one, and that is worse
than having no memory at all — a confidently wrong recollection is harder to
catch than an admission of ignorance. Superseding is the whole game.

**When to forget the conversation.** The short-term buffer has to be compressed
before it eats the context window, and compression must not silently drop the
facts that were already promoted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from agentcore import store, tracing

log = logging.getLogger("p05.memory")

FACTS_COLLECTION = "p05_facts"
BUFFER_NAMESPACE = "p05_buffers"

#: Roughly four characters per token. Precise counting needs the API's
#: count_tokens; this only has to decide when to compress.
CHARS_PER_TOKEN = 4


class FactKind(str, Enum):
    identity = "identity"        # who they are, their org, their role
    preference = "preference"    # how they want things done
    constraint = "constraint"    # what they cannot do — budget, policy, deadline
    decision = "decision"        # what was agreed
    context = "context"          # durable situational background


@dataclass
class Fact:
    id: str
    user_id: str
    kind: FactKind
    text: str
    #: Set when a later fact replaces this one. Superseded facts are kept, not
    #: deleted: "what did we believe on 3 March, and why" is a real question
    #: during an incident, and an audit trail with holes is not one.
    superseded_by: str | None = None
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    uses: int = 0

    @property
    def active(self) -> bool:
        return self.superseded_by is None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "kind": self.kind.value,
            "text": self.text,
            "superseded_by": self.superseded_by,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "uses": self.uses,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Fact:
        return cls(
            id=d["id"],
            user_id=d["user_id"],
            kind=FactKind(d["kind"]),
            text=d["text"],
            superseded_by=d.get("superseded_by"),
            created_at=d.get("created_at", 0.0),
            last_used_at=d.get("last_used_at", 0.0),
            uses=d.get("uses", 0),
        )


@dataclass
class Turn:
    role: str
    content: str
    at: float = field(default_factory=time.time)

    @property
    def tokens(self) -> int:
        return max(1, len(self.content) // CHARS_PER_TOKEN)


@dataclass
class ScoredFact:
    fact: Fact
    similarity: float
    recency: float
    score: float


def _facts_namespace(user_id: str) -> str:
    return f"p05_facts::{user_id}"


class MemoryStore:
    """Per-user memory that survives the process.

    Everything lands in sqlite keyed by `user_id`, which is all "cross-session
    sync" needs to mean for a single deployment: a new process, a new
    conversation, or a second device reads the same rows. Genuinely distributed
    sync is a different problem and is not claimed here.
    """

    def __init__(self, user_id: str, buffer_token_budget: int = 1200):
        self.user_id = user_id
        self.buffer_token_budget = buffer_token_budget
        self.embedder = store.Embedder()

    # ---------- short-term ----------

    def buffer(self) -> list[Turn]:
        raw = store.kv_get(BUFFER_NAMESPACE, self.user_id, default={"turns": [], "summary": ""})
        return [Turn(**t) for t in raw["turns"]]

    def summary(self) -> str:
        raw = store.kv_get(BUFFER_NAMESPACE, self.user_id, default={"turns": [], "summary": ""})
        return raw["summary"]

    def append(self, role: str, content: str) -> None:
        raw = store.kv_get(BUFFER_NAMESPACE, self.user_id, default={"turns": [], "summary": ""})
        raw["turns"].append({"role": role, "content": content, "at": time.time()})
        store.kv_set(BUFFER_NAMESPACE, self.user_id, raw)

    def buffer_tokens(self) -> int:
        return sum(t.tokens for t in self.buffer())

    def needs_compression(self) -> bool:
        return self.buffer_tokens() > self.buffer_token_budget

    def compress(self, summary: str, keep_last: int = 4) -> None:
        """Replace older turns with a summary, keeping the most recent verbatim.

        The recent turns stay literal because pronouns and follow-ups resolve
        against them — summarising "it" out of the last exchange breaks the
        next question.
        """
        raw = store.kv_get(BUFFER_NAMESPACE, self.user_id, default={"turns": [], "summary": ""})
        kept = raw["turns"][-keep_last:] if keep_last else []
        previous = raw["summary"]
        raw["summary"] = f"{previous}\n{summary}".strip() if previous else summary
        raw["turns"] = kept
        store.kv_set(BUFFER_NAMESPACE, self.user_id, raw)
        log.info("compressed buffer for %s, kept %d turns", self.user_id, len(kept))

    def clear_buffer(self) -> None:
        """End a session. Long-term facts are untouched — that is the point."""
        store.kv_set(BUFFER_NAMESPACE, self.user_id, {"turns": [], "summary": ""})

    # ---------- long-term ----------

    def all_facts(self, include_superseded: bool = False) -> list[Fact]:
        rows = [Fact.from_dict(v) for _, v in store.kv_list(_facts_namespace(self.user_id))]
        rows.sort(key=lambda f: f.created_at)
        return rows if include_superseded else [f for f in rows if f.active]

    def remember(self, kind: FactKind, text: str, supersedes: str | None = None) -> Fact:
        fact = Fact(
            id=f"f{time.time_ns()}",
            user_id=self.user_id,
            kind=kind,
            text=text.strip(),
        )
        with tracing.span("memory.remember", kind="step", fact_kind=kind.value):
            if supersedes:
                self._supersede(supersedes, fact.id)
            store.kv_set(_facts_namespace(self.user_id), fact.id, fact.as_dict())
            store.add_documents(
                FACTS_COLLECTION,
                [{
                    "id": f"{self.user_id}::{fact.id}",
                    "text": text,
                    "title": kind.value,
                    "metadata": {"user_id": self.user_id, "fact_id": fact.id},
                }],
                self.embedder,
            )
        return fact

    def _supersede(self, old_id: str, new_id: str) -> None:
        row = store.kv_get(_facts_namespace(self.user_id), old_id)
        if not row:
            log.warning("cannot supersede unknown fact %s", old_id)
            return
        row["superseded_by"] = new_id
        store.kv_set(_facts_namespace(self.user_id), old_id, row)
        log.info("fact %s superseded by %s", old_id, new_id)

    def forget(self, fact_id: str) -> bool:
        """Hard delete, for a deletion request rather than a correction.

        Distinct from superseding on purpose: a correction should leave a
        trail, and a right-to-erasure request must not.
        """
        namespace = _facts_namespace(self.user_id)
        if not store.kv_get(namespace, fact_id):
            return False
        with store.connect() as conn:
            conn.execute("DELETE FROM kv WHERE namespace = ? AND key = ?", (namespace, fact_id))
            conn.execute(
                "DELETE FROM documents WHERE id = ?", (f"{self.user_id}::{fact_id}",)
            )
        return True

    # ---------- recall ----------

    def recall(self, query: str, k: int = 4, min_score: float = 0.12) -> list[ScoredFact]:
        """Retrieve relevant active facts, ranked by similarity and recency.

        Recency matters independently of similarity: two facts can be equally
        on-topic while one is six months stale. Superseded facts are excluded
        here — they remain readable through `all_facts(include_superseded=True)`
        for audit, but must never reach a prompt.
        """
        with tracing.span("memory.recall", kind="step", query=query[:100]) as sp:
            active = {f.id: f for f in self.all_facts()}
            if not active:
                sp.attrs["hits"] = 0
                return []

            hits = store.search(FACTS_COLLECTION, query, self.embedder, k=k * 4)
            now = time.time()
            scored: list[ScoredFact] = []

            for hit in hits:
                if hit.metadata.get("user_id") != self.user_id:
                    continue  # never leak one user's memory into another's prompt
                fact = active.get(hit.metadata.get("fact_id"))
                if fact is None:
                    continue  # superseded or deleted

                age_days = max(0.0, (now - fact.created_at) / 86400)
                recency = 0.5 ** (age_days / 90)  # half-life of about a quarter
                score = 0.75 * hit.score + 0.25 * recency
                if score >= min_score:
                    scored.append(
                        ScoredFact(fact=fact, similarity=round(hit.score, 3),
                                   recency=round(recency, 3), score=round(score, 3))
                    )

            scored.sort(key=lambda s: -s.score)
            selected = scored[:k]
            self._mark_used([s.fact for s in selected])
            sp.attrs["hits"] = len(selected)
            return selected

    def _mark_used(self, facts: list[Fact]) -> None:
        """Usage counts make unused memory visible, which is what tells you a
        store has filled up with noise."""
        namespace = _facts_namespace(self.user_id)
        for fact in facts:
            row = store.kv_get(namespace, fact.id)
            if row:
                row["uses"] = row.get("uses", 0) + 1
                row["last_used_at"] = time.time()
                store.kv_set(namespace, fact.id, row)

    def stats(self) -> dict:
        every = self.all_facts(include_superseded=True)
        active = [f for f in every if f.active]
        return {
            "user_id": self.user_id,
            "facts_active": len(active),
            "facts_superseded": len(every) - len(active),
            "never_used": sum(1 for f in active if f.uses == 0),
            "buffer_turns": len(self.buffer()),
            "buffer_tokens": self.buffer_tokens(),
            "has_summary": bool(self.summary()),
        }
