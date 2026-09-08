"""Citation verification and confidence scoring.

A citation the system never checks is decoration. Models cite fluently and
wrongly: they attach a plausible chunk id to a claim that chunk does not
support, and the answer then looks *more* trustworthy than an uncited one.
That is worse than no citations at all, because it defeats the reader's own
scepticism.

So every claim is verified against the text it cites before the answer is
returned, and the verification result — not the model's self-assessment — is
what drives the refuse/answer decision.

Verification is lexical here: content-word overlap between the claim and the
cited chunk, with numbers and money amounts weighted heavily because those are
what get hallucinated and what cost the customer money when wrong. It is
deliberately cheap and deterministic. The honest limitation, stated in the
scoping note too: lexical overlap catches a claim citing the wrong chunk, but
not a claim that paraphrases its chunk while inverting the meaning. Catching
that needs an LLM judge (`verify_with_judge`) and costs a second call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Words that carry no evidentiary weight; overlap on these means nothing.
_STOPWORDS = frozenset(
    """a an and are as at be been by can could do does for from had has have how i if in
    is it its may must not of on or our shall should than that the their there these they
    this to was we were what when where which who will with would you your""".split()
)

#: Tokens that must match when present. A claim about "14 days" citing a chunk
#: that says "30 days" is the failure that matters most.
_NUMERIC = re.compile(r"^[\$€£]?\d[\d,._%]*$")


@dataclass
class Claim:
    """One assertion in the answer, with the chunks it says support it."""

    text: str
    citation_ids: list[str]
    verified: bool = False
    support_score: float = 0.0
    reason: str = ""


@dataclass
class Grounding:
    claims: list[Claim] = field(default_factory=list)
    unsupported: list[Claim] = field(default_factory=list)
    #: Fraction of claims whose citations actually support them.
    grounded_fraction: float = 0.0
    #: Claims citing a chunk id that was never retrieved — a fabricated source.
    fabricated_citations: list[str] = field(default_factory=list)

    @property
    def fully_grounded(self) -> bool:
        return bool(self.claims) and not self.unsupported and not self.fabricated_citations


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9._%$,\-]*", text.lower())


def stem(token: str) -> str:
    """Strip the inflections that break exact matching.

    Without this, a claim saying "refunds" citing a source that says "refund"
    scores as unsupported. Over-refusing is a real failure too — a system that
    refuses correct answers gets switched off just as fast as one that
    hallucinates — so the cheapest fix for the commonest false negative earns
    its place. Deliberately not a full Porter stemmer: the failure mode of
    aggressive stemming is collapsing distinct terms together, which would make
    verification *more* permissive, in the wrong direction.
    """
    for suffix in ("ies",):
        if len(token) > 4 and token.endswith(suffix):
            return token[: -len(suffix)] + "y"
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def content_terms(text: str) -> set[str]:
    return {stem(t) for t in tokenize(text) if t not in _STOPWORDS and len(t) > 1}


def numeric_terms(text: str) -> set[str]:
    return {t.rstrip(".,") for t in tokenize(text) if _NUMERIC.match(t)}


def support_score(claim: str, evidence: str) -> tuple[float, str]:
    """How well `evidence` supports `claim`, in [0, 1], with a reason.

    Numbers are checked separately and gate the result: a claim that states a
    figure the evidence does not contain is unsupported no matter how much
    prose it shares. This is the single highest-value check in the module —
    "14 days" vs "30 days" is the error that reaches a customer as a wrong
    answer with a citation attached.
    """
    claim_terms = content_terms(claim)
    if not claim_terms:
        return 0.0, "claim has no content words"

    evidence_terms = content_terms(evidence)
    overlap = claim_terms & evidence_terms
    lexical = len(overlap) / len(claim_terms)

    claim_numbers = numeric_terms(claim)
    if claim_numbers:
        evidence_numbers = numeric_terms(evidence)
        missing = {n for n in claim_numbers if not _number_present(n, evidence_numbers)}
        if missing:
            return (
                min(lexical, 0.25),
                f"figures not present in the cited text: {', '.join(sorted(missing))}",
            )

    if lexical >= 0.5:
        return lexical, "supported"
    return lexical, f"only {lexical:.0%} of the claim's terms appear in the cited text"


def _number_present(number: str, evidence_numbers: set[str]) -> bool:
    """Match numbers modulo formatting: 10000, 10,000 and $10,000 are one figure."""
    def normalise(n: str) -> str:
        return n.lstrip("$€£").replace(",", "").rstrip("%").rstrip(".")

    target = normalise(number)
    return any(normalise(e) == target for e in evidence_numbers)


def verify(
    claims: list[Claim], evidence_by_id: dict[str, str], *, threshold: float = 0.5
) -> Grounding:
    """Check every claim against the text it cites.

    Three distinct outcomes, kept distinct because they mean different things:
    a claim citing nothing, a claim citing a chunk that was never retrieved
    (fabricated), and a claim citing a real chunk that does not support it.
    """
    grounding = Grounding()

    for claim in claims:
        grounding.claims.append(claim)

        if not claim.citation_ids:
            claim.reason = "no citation"
            grounding.unsupported.append(claim)
            continue

        unknown = [cid for cid in claim.citation_ids if cid not in evidence_by_id]
        if unknown:
            # The model invented a source id. Never treat this as a near-miss.
            grounding.fabricated_citations.extend(unknown)
            claim.reason = f"cites unknown source(s): {', '.join(unknown)}"
            grounding.unsupported.append(claim)
            continue

        best, best_reason = 0.0, ""
        for cid in claim.citation_ids:
            score, reason = support_score(claim.text, evidence_by_id[cid])
            if score > best:
                best, best_reason = score, reason

        claim.support_score = round(best, 3)
        claim.reason = best_reason
        claim.verified = best >= threshold
        if not claim.verified:
            grounding.unsupported.append(claim)

    total = len(grounding.claims)
    verified = sum(1 for c in grounding.claims if c.verified)
    grounding.grounded_fraction = round(verified / total, 3) if total else 0.0
    return grounding


# ---------- confidence ----------


@dataclass
class Confidence:
    score: float
    retrieval: float
    grounding: float
    self_reported: float
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "retrieval": round(self.retrieval, 3),
            "grounding": round(self.grounding, 3),
            "self_reported": round(self.self_reported, 3),
            "reasons": self.reasons,
        }


def score_confidence(
    retrieval_scores: list[float],
    grounding: Grounding,
    self_reported: float,
) -> Confidence:
    """Combine three independent signals into one number.

    Weighted towards grounding because it is the only signal computed from the
    evidence rather than asserted. Retrieval score says we found *something*;
    self-reported confidence is the model's opinion of its own work and is the
    least reliable of the three, so it gets the smallest weight — but dropping
    it entirely loses the one signal that catches a well-grounded answer to a
    question the user did not actually ask.
    """
    retrieval = max(retrieval_scores) if retrieval_scores else 0.0
    grounded = grounding.grounded_fraction

    score = 0.30 * retrieval + 0.50 * grounded + 0.20 * self_reported

    reasons: list[str] = []
    if not retrieval_scores:
        reasons.append("nothing retrieved from the corpus")
    elif retrieval < 0.35:
        reasons.append(f"weak retrieval (best chunk scored {retrieval:.2f})")
    if grounding.fabricated_citations:
        # A fabricated source is disqualifying on its own, whatever else scored.
        score = min(score, 0.2)
        reasons.append(
            f"fabricated citation(s): {', '.join(sorted(set(grounding.fabricated_citations)))}"
        )
    if grounding.unsupported:
        reasons.append(
            f"{len(grounding.unsupported)} of {len(grounding.claims)} claims "
            "not supported by their cited text"
        )
    if not grounding.claims:
        reasons.append("answer made no verifiable claims")

    return Confidence(
        score=round(min(max(score, 0.0), 1.0), 3),
        retrieval=retrieval,
        grounding=grounded,
        self_reported=self_reported,
        reasons=reasons,
    )
