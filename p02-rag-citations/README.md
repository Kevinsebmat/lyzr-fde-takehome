# P2 — RAG Agent with Citation Grounding

**Failure mode this project exists to survive:** a fluent, confident, cited
answer that the sources do not actually support.

Status: **working end-to-end.** 28 tests, all offline. This is the flagship and
the subject of the Part 1 scoping note (`docs/scoping-note-p2-rag.pdf`).

## Setup

```bash
make install            # from the repo root
# or: pip install -e ../core -e '..[dev]'
```

No API key required — `LLM_PROVIDER=mock` replays recorded responses.

## Run

```bash
python -m p02_rag_citations.cli demo          # ← start here
python -m p02_rag_citations.cli ask "How long are audit logs kept?"
python -m p02_rag_citations.cli ask "Do you sign a HIPAA BAA?" --no-search
python -m p02_rag_citations.cli chunks        # how the corpus was split
python ../scripts/smoke.py p02
python -m pytest tests -q
```

`demo` runs nine questions: five the corpus covers, four it deliberately does
not. The second half is the point.

```
covered by the knowledge base
ANSWERED               0.83  How long do customers have to request a refund…
ANSWERED               0.80  What uptime do we commit to…
ANSWERED               0.79  How long are audit logs kept?
ANSWERED               0.77  Which SSO protocols work on the Business plan?
ANSWERED               0.76  What happens when a customer exceeds their rate limit?
NOT covered — the interesting half
REFUSED                0.08  Do you sign a HIPAA business associate agreement?
REFUSED                0.10  What does the Enterprise plan cost per seat per year?
REFUSED                0.06  Can Northwind Cloud be deployed on-premise…?
REFUSED                0.03  What is the CEO's direct phone number?
```

## The central claim

**A citation the system never checks is decoration.** Models cite fluently and
wrongly — they attach a plausible chunk id to a claim that chunk doesn't
support, and the answer then looks *more* trustworthy than an uncited one. That
is worse than no citations, because it defeats the reader's own scepticism.

So every claim is verified against the text it cites *before* the answer is
returned, and the verification result — not the model's self-assessment — drives
the decision.

## Four outcomes, chosen on measured evidence

| | When | What the user gets |
|---|---|---|
| `ANSWER` | confident and fully grounded | the answer with sources |
| `ANSWER_CAVEATED` | grounded but weak | verified claims only; unverified ones **removed**, and the removal disclosed |
| `FALLBACK_SEARCH` | corpus doesn't cover it | external results, explicitly labelled unverified |
| `REFUSE` | nothing trustworthy available | a refusal that says *why* |

Unsupported claims are **dropped, not hedged**. Keeping a claim behind a "may"
still ships it as fact to a reader who is skimming.

## How confidence is computed

Three independent signals, weighted by how much they can be trusted:

```
0.30 × retrieval score      did we find anything relevant
0.50 × grounded fraction    do the citations actually support the claims
0.20 × model self-report    the model's own opinion — the weakest signal
```

Grounding gets the most weight because it is the only one *computed from the
evidence* rather than asserted. A **fabricated citation caps the score at 0.2**
regardless of everything else: inventing a source id is disqualifying, never a
near miss.

## Verification, and its honest limits

`support_score()` measures content-word overlap between a claim and the text it
cites, with **numbers gating the result**: a claim stating a figure the evidence
doesn't contain is unsupported no matter how much prose it shares. "14 days" vs
"60 days" is the error that reaches a customer as a wrong answer with a citation
attached, and it's the single highest-value check here.

Light stemming is applied, because over-refusing is also a failure — a system
that rejects correct answers gets switched off as fast as one that hallucinates.

**What this does not catch:** a claim that paraphrases its source while
inverting the meaning ("refunds are *not* available within 14 days" shares every
content word with its evidence). Catching that needs an LLM judge and a second
call per claim. That trade-off is stated in the scoping note rather than hidden:
lexical verification is cheap, deterministic and catches the common case; the
judge is a scoped upgrade with a real cost attached.

## Retrieval score is not the safety mechanism

Measured on this corpus, retrieval scores for answerable and unanswerable
questions **overlap** — 0.27–0.46 against 0.10–0.31. A floor set high enough to
reject the unanswerable ones also rejects real questions. So `RETRIEVAL_FLOOR`
is a cheap pre-filter ("is there anything here at all", worth skipping an LLM
call for), and citation verification does the real work.

Absolute scores are embedding-backend specific. The current values are
calibrated for the offline lexical embedder and want re-tuning against a
held-out set when real embeddings are switched on — scoped work in the
engagement, not a constant anyone can guess.

## Chunking

Split on `##` headings, each section kept whole, with the heading prefixed into
the chunk text. Fixed-width chunking splits a policy mid-sentence and then cites
half a rule — exactly what makes citations untrustworthy. The heading prefix
also means a chunk shown to a user carries the context it came from.

## The corpus is deliberately incomplete

`corpus.py` covers refunds, SLA, retention, SSO and rate limits, and says
nothing about HIPAA, on-premise deployment or pricing. Most RAG demos choose
questions the corpus answers well, which proves nothing. `UNANSWERABLE` is
shipped as first-class data next to `ANSWERABLE`.

## Search fallback

Absent by design when unconfigured: with no `TAVILY_API_KEY` it returns nothing
and the agent refuses. A fallback that invents results to avoid an empty hand is
worse than the refusal it replaced. Results are always labelled external and
never merged into the grounded answer — they carry none of the verification the
corpus path applies.

## API

- `POST /ask` — `{question, k, allow_search_fallback, answer_threshold}` → the
  action, per-claim verification, citations, and the confidence breakdown
- `GET /corpus` — chunks plus both demo question sets

## Where the code lives

| File | |
|---|---|
| `corpus.py` | documents, heading-aware chunking, the two question sets |
| `grounding.py` | `support_score`, `verify`, `score_confidence` — the trust machinery |
| `agent.py` | retrieve → draft → verify → decide |
| `search.py` | the fallback, and its deliberate absence |

Demo cassettes are regenerated by `scripts/seed_p02_cassettes.py`, which fails
loudly if a corpus edit leaves a cassette citing a chunk retrieval no longer
returns.
