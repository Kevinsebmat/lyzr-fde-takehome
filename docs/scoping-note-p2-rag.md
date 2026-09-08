# Grounded Document Q&A — should you fund it?

**For:** Northwind Cloud, Support & Operations · **Pattern:** RAG with citation grounding\
**Recommendation:** fund a two-week pilot, with one condition (below).

---

### The problem, plainly

Your support team answers the same questions out of the same documents all day —
refund windows, SLA credits, retention periods, SSO setup — and still answers them
inconsistently. An assistant that reads those documents is the obvious fix.

The reason it isn't already done is the risk: **an AI assistant that is wrong is
wrong confidently, and nobody downstream can tell which answers to trust.** One
wrong refund figure quoted to a customer costs more than the tool saves in a
month, and it ends the team's willingness to use it at all.

Citation grounding does not buy better answers. It buys **the ability to tell a
good answer from a bad one** — before the customer sees it.

### What "production-ready" means here, and what a demo hides

A demo retrieves a passage, writes a fluent answer and attaches a source. It takes
a day and it looks finished. Four things separate it from something customer-facing:

| | Demo | Production |
|---|---|---|
| **Citations** | attached | **verified** — a claim whose source doesn't support it is removed |
| **Not knowing** | answers anyway | **refuses, and says why**; "not in our docs" is a correct answer |
| **Accuracy** | unknown | **measured** against questions with agreed right answers |
| **Documents** | one clean folder | your real ones, contradictions and stale copies included |

The naive version **fails silently**: with no way to say "I don't know", it always
produces something. You find out it was wrong when a customer does.

### Three things to weigh before committing budget

1. **Your documents set the ceiling, and they are your responsibility.** Where two
   policies contradict, the system faithfully cites the wrong one; no engineering
   fixes that. You need a named owner for the document set — its absence is the
   commonest reason these projects underdeliver.

2. **A citation proves provenance, not correctness.** Verification catches the
   common, expensive error — a figure the source doesn't contain, "14 days" against
   "30 days" — but not an answer that paraphrases its source while inverting the
   meaning. Closing that gap roughly doubles the per-answer cost; decide after the
   pilot, on real data.

3. **Launch is not the finish.** Documents change and accuracy decays quietly.
   Budget for ongoing evaluation and re-indexing, or in six months you have a tool
   people have quietly stopped trusting.

### Two-week scope

**Week 1 — measurement before engineering.** Ingest and tune retrieval on your real
documents, and build a **50-question evaluation set** with agreed correct answers.
This is the deliverable that de-risks the rest: it is how both of us find out
whether this works.

**Week 2 — calibration and pilot.** Tune citation verification and the refusal
threshold against that set. The trade-off between "answers more" and "is wrong
less" is your decision; we bring the numbers. Then a pilot with 5–10 agents, and
handover.

**In scope:** one document set, one internal interface, English, read-only.
**Explicitly out:** other languages, taking actions for a customer, live document
sync, permission-scoped answers. Each is a sensible phase two; adding one now puts
the pilot at risk. **From you:** a document-set owner, 5–10 pilot users, and about
three hours of a subject expert's time to agree the evaluation answers.

### The condition

Fund the pilot, and treat **the week-one evaluation set as the go/no-go gate.** If
accuracy on your own documents comes in low, the problem is the documents, not the
model — and the right next spend is fixing them, not more engineering. We would
rather tell you that in week two than in month six.

*Lyzr FDE take-home. A working implementation — verified citations, confidence
scoring, the refusal path — is in `p02-rag-citations/`.*
