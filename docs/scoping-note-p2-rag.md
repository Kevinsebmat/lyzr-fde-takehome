# Grounded Document Q&A: should you fund it?

**For:** Northwind Cloud, Support & Operations · **Pattern:** RAG with citation grounding\
**Recommendation:** fund a two-week pilot, with one condition (below).

---

### The problem

Your support team answers the same questions out of the same documents all day.
Refund windows, SLA credits, retention periods, SSO setup. They still answer them
inconsistently. An assistant that reads those documents is the obvious fix.

The reason it isn't already done is the risk. An AI assistant that is wrong is
usually wrong confidently, and nobody downstream can tell which answers to trust.
One wrong refund figure quoted to a customer costs more than the tool saves in a
month, and it ends the team's willingness to use it.

Citation grounding doesn't buy you better answers. It buys you the ability to
tell a good answer from a bad one, before the customer sees it.

### What "production-ready" means here

A demo retrieves a passage, writes a fluent answer and attaches a source. It
takes a day and looks finished. Four things separate that from something you can
put in front of customers:

| | Demo | Production |
|---|---|---|
| **Citations** | attached | **verified**, so a claim whose source doesn't support it gets removed |
| **Not knowing** | answers anyway | **refuses and says why**; "not in our docs" is a correct answer |
| **Accuracy** | unknown | **measured** against questions with agreed right answers |
| **Documents** | one clean folder | your real ones, contradictions and stale copies included |

The naive version fails silently. With no way to say "I don't know" it always
produces something, and you find out it was wrong when a customer does.

### Three things to weigh before committing budget

1. **Your documents set the ceiling, and they're your responsibility.** Where two
   policies contradict, the system will faithfully cite the wrong one, and no
   amount of engineering fixes that. You'll need a named owner for the document
   set; not having one is the commonest reason these projects underdeliver.

2. **A citation proves provenance, not correctness.** Our verification catches
   the expensive error, where a claim states a figure its source doesn't contain:
   "14 days" against "30 days". It won't catch an answer that paraphrases its
   source while inverting the meaning. Closing that gap roughly doubles the cost
   per answer, so we'd rather decide it after the pilot, on real data.

3. **Launch isn't the finish.** Documents change and accuracy decays quietly.
   Budget for ongoing evaluation and re-indexing, or in six months you'll have a
   tool people have stopped trusting without anyone able to say when.

### Two-week scope

**Week 1, measurement before engineering.** We ingest your real documents and
tune retrieval on them, and build a 50-question evaluation set with agreed correct
answers. That set de-risks everything after it, because it's how both of us find
out whether this works.

**Week 2, calibration and pilot.** We tune citation verification and the refusal
threshold against that set. Whether it should answer more or be wrong less is your
call, and we'll bring the numbers. Then a pilot with five to ten agents, and
handover.

**In scope:** one document set, one internal interface, English, read-only.
**Out of scope:** other languages, acting on a customer's behalf, live document
sync, permission-scoped answers. Each is a sensible phase two, and adding one now
puts the pilot at risk. **From you:** a document-set owner, five to ten pilot
users, and three hours of a subject expert's time to agree the evaluation answers.

### The condition

Fund the pilot, but treat the week-one evaluation set as a go/no-go gate. If
accuracy on your own documents comes back low, the documents are the problem
rather than the model, and fixing them is where the next money should go. We'd
rather tell you that in week two than in month six.

*Lyzr FDE take-home. A working implementation is in `p02-rag-citations/`.*
