# P5 — Memory-Enabled Conversational Agent

**The failure this survives:** amnesia across sessions, and its more dangerous
twin, confidently remembering something that's no longer true.

Status: working end to end. 23 tests, all offline.

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

## Storing text isn't the hard part

Three other things are.

### Deciding what's worth keeping

"Thanks, that helps" isn't a memory. Store every turn and you end up with a store
where pleasantries outnumber the useful facts, so retrieval starts returning
pleasantries. Only durable facts get promoted, in five kinds: `identity`,
`preference`, `constraint`, `decision` and `context`.

Extraction is a separate structured call rather than a side effect of the
answering prompt, with an explicit contract. Durable means it'll still matter in
a month, and most messages contain nothing durable, so an empty list is the
common and correct answer.

### Handling a fact that changes

A user who says "we moved to Frankfurt" after previously saying "we're in Dublin"
hasn't given you two facts. If both stay retrievable, the agent will eventually
cite the stale one, and a confidently wrong recollection is worse than no memory
at all. It's much harder to catch than an admission of ignorance.

So the extractor sees existing facts with their ids and can mark one
`supersedes`. Without ids it can only ever add, and contradictions pile up.

Superseded facts are kept rather than deleted. They never reach a prompt again,
but `--all` still shows them. "What did we believe on 3 March, and why" is a real
question during an incident, and an audit trail with holes doesn't answer it.

`forget()` is a separate hard delete. A correction should leave a trail; a
right-to-erasure request mustn't.

### Knowing when to forget the conversation

The short-term buffer is token-bounded. Past the budget, older turns get
summarised and the last four are kept verbatim, because pronouns and follow-ups
resolve against them. Summarising "it" out of the last exchange breaks the next
question.

## Relevance isn't just similarity

```
score = 0.75 × cosine similarity + 0.25 × recency
```

with a recency half-life of roughly a quarter. Two facts can be equally on-topic
while one of them is six months stale. Usage counts are tracked per fact, so
`never_used` makes dead memory visible. That number is how you find out a store
has filled up with noise.

## What "cross-session" does and doesn't mean here

Everything is keyed by `user_id` in sqlite, so a new process, a new conversation
or a second device reads the same rows. `end_session()` clears the transcript and
leaves the facts, so the next session starts with no transcript and full
knowledge of the user. That's the line this project is about.

Genuinely distributed multi-region sync is a different problem and I'm not
claiming it.

Recall filters on `user_id` before anything reaches a prompt. A memory leak
between users is a data breach rather than a bug, and there's a test for it.

## Graceful degradation

A failed extraction logs and carries on. Losing an extraction costs you one fact;
failing the turn costs you the conversation. Memory is an enhancement, not the
critical path.

## Where the code lives

| File | |
|---|---|
| `memory.py` | buffer, facts, supersession, recall scoring |
| `agent.py` | the per-turn loop: recall, answer, extract, compress |
