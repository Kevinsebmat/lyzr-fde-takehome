# P5 — Memory-Enabled Conversational Agent

**Failure mode this project exists to survive:** amnesia across sessions — and
its more dangerous twin, confidently remembering something that is no longer
true.

Status: **working end-to-end.** 23 tests, all offline.

## Setup & run

```bash
make install    # from the repo root
python -m p05_memory_agent.cli chat --user wexler-ops "Our data has to stay in the EU."
python -m p05_memory_agent.cli chat --user wexler-ops "Where is our data hosted?"
python -m p05_memory_agent.cli facts --user wexler-ops --all
python -m p05_memory_agent.cli demo        # two sessions and a correction
python ../scripts/smoke.py p05
python -m pytest tests -q
```

## Storing text is not the hard part

Three things are.

### What is worth keeping

"Thanks, that helps" is not a memory. Store every turn and you get a store where
the useful facts are outnumbered by pleasantries — and retrieval then returns
pleasantries. Only durable facts are promoted, in five kinds: `identity`,
`preference`, `constraint`, `decision`, `context`.

Extraction is a **separate structured call**, not a side effect of the answering
prompt, with an explicit contract: durable means it will still matter in a
month, and *most messages contain nothing durable — an empty list is the common,
correct answer.*

### What to do when a fact changes

A user who says "we moved to Frankfurt" after previously saying "we're in
Dublin" has not given you two facts. If both stay retrievable, the agent will
eventually cite the stale one — and **a confidently wrong recollection is worse
than no memory at all**, because it is much harder to catch than an admission of
ignorance.

So the extractor is shown existing facts *with their ids* and can mark one
`supersedes`. Without ids it can only ever add, and contradictions accumulate.

Superseded facts are **kept, not deleted**. They never reach a prompt again, but
`--all` still shows them: "what did we believe on 3 March, and why" is a real
question during an incident, and an audit trail with holes isn't one.

`forget()` is a separate hard delete — a correction should leave a trail, a
right-to-erasure request must not.

### When to forget the conversation

The short-term buffer is token-bounded. Past the budget, older turns are
summarised and the **last four are kept verbatim** — pronouns and follow-ups
resolve against them, so summarising "it" out of the last exchange breaks the
next question.

## Relevance is not just similarity

```
score = 0.75 × cosine similarity + 0.25 × recency
```

with a recency half-life of about a quarter. Two facts can be equally on-topic
while one is six months stale. Usage counts are tracked per fact, so
`never_used` makes dead memory visible — that number is how you find out a store
has filled with noise.

## Cross-session, and what that does and doesn't mean

Everything is keyed by `user_id` in sqlite, so a new process, a new conversation
or a second device reads the same rows. `end_session()` clears the transcript
and leaves the facts — the next session starts with no transcript and full
knowledge of the user. That's the line this project is about.

Genuinely distributed multi-region sync is a different problem and is **not**
claimed here.

Recall filters on `user_id` before anything reaches a prompt. A memory leak
between users is a data breach, not a bug, and there's a test for it.

## Graceful degradation

A failed extraction logs and continues. Losing an extraction costs one fact;
failing the turn costs the conversation. Memory is an enhancement, not the
critical path.

## Where the code lives

| File | |
|---|---|
| `memory.py` | buffer, facts, supersession, recall scoring |
| `agent.py` | the per-turn loop: recall → answer → extract → compress |
