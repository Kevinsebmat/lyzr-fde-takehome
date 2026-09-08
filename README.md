# Lyzr FDE Take-Home

Eleven agent projects over one shared core, plus a client scoping note.

Every project is defined by **the failure it survives**, not by its feature
list. That is the organising idea of the code, the tests, and the console.

```bash
make install    # creates the `lyzer` conda env, installs everything editable
make smoke      # runs all 11 projects end-to-end — no API key, no cost
make test       # 326 tests, deterministic and offline
make dev        # FastAPI :8000 + the console on :3000
```

---

## Triage — what works, what's partial, what's skipped

`make smoke` executes every project end-to-end and prints a pass/fail line each.
**Nothing below is marked working unless it passes there**, so the claim and the
check cannot drift apart.

| # | Project | Status | Tests | What it demonstrates |
|---|---------|--------|------:|----------------------|
| P1 | Structured Output | ✅ working | 24 | Repair turns naming the failing field; model escalation; two-sided tool contracts |
| P2 | RAG with Citation Grounding | ✅ working | 28 | Verified citations, and a real refusal path — **flagship, and the memo's subject** |
| P3 | ReAct Planner | ✅ working | 20 | Four independent stopping conditions; every exit still answers |
| P4 | Multi-Tool Orchestrator | ✅ working | 32 | Scope enforced at invocation; deterministic conflict resolution |
| P5 | Memory Agent | ✅ working | 23 | Supersession — a corrected fact stops being retrievable |
| P6 | Human-in-the-Loop Approval | ✅ working | 29 | A pause that survives a restart; append-only audit |
| P7 | Cost-Aware Router | ✅ working | 34 | Break-even arithmetic that says when routing does **not** pay — **flagship** |
| P8 | Event Automation | ✅ working | 27 | Idempotency as a unique index; replayable dead letters |
| P9 | Multi-Agent Debate | ✅ working | 20 | Independent proposers; a tie reported as a tie |
| P10 | Self-Reflective Auto-Eval | ✅ working | 24 | Keeps the best attempt, not the last; logged improvement metrics |
| P11 | Production Observability | ✅ working | 35 | Dashboards over ~800 real spans from P1–P10 — **flagship** |
| P12 | OSS Framework Contribution | ⛔ **skipped** | — | Bonus only. Reasoning below. |

**11 of 11 required projects run end-to-end.** 326 tests — 296 across the
projects above plus 30 on the shared core, all offline and deterministic.
`make lint` is clean.

### Partial, and stated plainly

Three things are less than they could be. Each is a deliberate cut, not an
oversight:

- **P11 does not export to LangSmith or Arize.** The trace schema (`run_id`,
  `parent_span_id`, model, tokens, cost, status) is the shape those exporters
  want and the adapter is small, but a key-gated integration that cannot run in
  the offline demo would be a claim rather than a feature.
- **P2's citation verification is lexical.** It catches a claim stating a figure
  its source doesn't contain — the common, expensive error — but not one that
  paraphrases its source while inverting the meaning. Catching that needs an LLM
  judge per claim and roughly doubles the cost. The trade-off is in the scoping
  note rather than hidden.
- **The offline console shows placeholders for P3, P5, P9 and P10.** Their
  prompts depend on accumulated conversation state, so they cannot be keyed to a
  recorded response deterministically. Their CLIs and tests exercise them fully;
  with an API key the console does too.

### Why P12 is skipped

It is explicitly bonus credit and carries no penalty. A merged upstream PR
depends on a maintainer's review timeline, which does not fit inside a ten-day
window, and the time would come out of the eleven projects that are actually
graded. Naming the cut is the point; going quiet on it is what the brief
penalises.

---

## Part 1 — Client scoping note

**[`docs/scoping-note-p2-rag.pdf`](docs/scoping-note-p2-rag.pdf)** — one page, on
P2 (RAG with citation grounding). Markdown source alongside it; `make memo`
rebuilds the PDF and fails if it runs to two pages.

It recommends funding a two-week pilot **conditional on the week-one evaluation
set**: if accuracy on the customer's own documents comes in low, the problem is
the documents and the right next spend is fixing them, not more engineering.

---

## Three decisions worth explaining

### Why there is a shared `core/` despite one-folder-per-project

The brief asks for one subfolder per project, each with its own README and setup
— and this repo honours that. But eleven private copies of an LLM client, a
retry loop, a cost meter and a trace writer is the surest way to end up with
eleven things that each half-work.

So `core/agentcore/` holds the shared infrastructure and each project stays
independently runnable. The core also buys two things no per-project copy could:

- **P7 can route.** Model selection lives in one place, so a cost router is a
  policy change rather than eleven edits.
- **P11 has real data.** Every call in every project emits a trace span, so the
  observability project is a dashboard over the actual submission rather than a
  toy trace of its own.

### Why mock mode exists

`LLM_PROVIDER=mock` (the default for tests and smoke) replays cassettes recorded
from real calls.

- **The demo cannot be killed by a dead key or a rate limit.** A reviewer clones
  the repo and all eleven projects run, at zero cost.
- **Failure modes become testable.** These projects are graded on surviving
  malformed output, refusals, timeouts and runaway loops. You cannot ask a real
  model to produce those on demand; the mock provider scripts them exactly,
  which is how the repair loop and the iteration caps are actually verified
  rather than asserted.

### Where the interesting bugs were

Four things were found by *running* the code rather than reasoning about it, and
each is documented where it happened:

- **`asyncio.wait_for` cannot cancel a blocking call.** P4's batch took 10,015ms
  with a 200ms timeout set, because `to_thread` uses the loop's default executor
  and `asyncio.run` joins it on the way out. Fixed with a private executor that
  is never joined; 205ms now, tested.
- **The offline embedder ranked the wrong chunk first.** It summed a random
  gaussian per token, which spreads every token across all dimensions with
  random signs. Replaced with feature hashing, so cosine tracks shared
  vocabulary.
- **P11's loop rule flagged three correctly-working P3 runs.** It counted every
  tool *and LLM* span, and a ReAct planner legitimately calls `think` once per
  iteration. It now requires the inputs to repeat too.
- **A claim I made about classifier overhead was wrong.** I wrote that a
  per-request LLM classifier "consumes most of the saving". Measured, it is 4–5%
  at a healthy success rate. The real insight is that its cost is fixed while
  the saving shrinks, so at 25% success it eats 60% — the overhead bites hardest
  exactly when routing is already marginal.

---

## Layout

```
core/agentcore/    shared: llm, cost, tracing, retry, store, mock provider
p01-…/ … p11-…/    one folder per project, each with its own README + tests
server/            one FastAPI app mounting every project
web/               the console — the consolidated demo surface
scripts/smoke.py   the check behind the triage table above
docs/              the assignment brief and the Part 1 scoping note
```

## Requirements

Python 3.11 (conda env `lyzer`), Node 22 + pnpm for the console. No API key is
needed for `make test`, `make smoke`, or the console — see `.env.example` for
what a live run wants.
