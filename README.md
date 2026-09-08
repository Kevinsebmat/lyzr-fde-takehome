# Lyzr FDE Take-Home

Eleven agent projects over one shared core, plus a client scoping note.

**Status: in progress.** The triage table below is the honest current state and
is regenerated from `make smoke`, not written from memory. Nothing is claimed as
working unless the smoke run passes it.

```bash
make install    # creates the `lyzer` conda env, installs everything editable
make smoke      # runs all 11 projects end-to-end — no API key, no cost
make test       # full test suite, deterministic and offline
```

---

## Triage — what works, what's partial, what's skipped

| # | Project | Status | Notes |
|---|---------|--------|-------|
| P1 | Structured Output Agent | ✅ working | Repair loop, model escalation, two-sided tool contracts. 24 tests. |
| P2 | RAG with Citation Grounding | ✅ working | Verified citations, 4-way answer/caveat/fallback/refuse. 28 tests. Flagship + scoping-note subject. |
| P3 | ReAct Planning Agent | ✅ working | Four independent stopping conditions; every exit still answers. 20 tests. |
| P4 | Multi-Tool Orchestrator | ✅ working | Scope enforced at invoke; deterministic conflict resolution. 32 tests. |
| P5 | Memory-Enabled Agent | ✅ working | Durable facts with supersession; a corrected fact stops being retrievable. 23 tests. |
| P6 | Human-in-the-Loop Approval | ✅ working | Pause is a DB row, not an await; survives restart. Append-only audit. 29 tests. |
| P7 | Cost-Aware Router | ✅ working | Break-even arithmetic; says plainly when routing does NOT pay. 34 tests. Flagship. |
| P8 | Event-Triggered Automation | ✅ working | Idempotency enforced by a unique index; dead letters replayable. 27 tests. |
| P9 | Multi-Agent Debate | ✅ working | Independent proposers; a tie is reported as a tie. 20 tests. |
| P10 | Self-Reflective Auto-Eval | ✅ working | Keeps the best attempt, not the last; logged improvement metrics. 24 tests. |
| P11 | Production Observability | ⏳ building | Flagship; reads traces from P1–P10 |
| P12 | OSS Framework Contribution | ⛔ skipped | Bonus only. Deliberately cut — see below. |

**Part 1 — Client scoping note:** `docs/scoping-note-p2-rag.pdf` *(pending)*

### Why P12 is skipped

It is explicitly bonus credit and carries no penalty. A merged upstream PR
depends on a maintainer's review timeline, which does not fit inside a ten-day
window, and the time it would consume is better spent making the eleven required
projects actually run. Naming the cut is the point; going quiet on it is what
the brief penalises.

---

## Two decisions worth explaining

### Why there is a shared `core/` despite one-folder-per-project

The brief asks for one subfolder per project, each with its own README and setup
instructions — and this repo honours that. But eleven private copies of an LLM
client, a retry loop, a cost meter and a trace writer is the surest way to end up
with eleven things that each half-work.

So `core/agentcore/` holds the shared infrastructure, and each project stays
independently runnable: its README's setup step installs the core alongside it,
and its CLI runs standalone. The core also buys two things no per-project copy
could:

- **P7 can route.** Model selection lives in one place, so a cost router is a
  policy change rather than eleven edits.
- **P11 has real data.** Every LLM call in every project emits a trace span, so
  the observability project is a dashboard over the actual submission rather than
  a toy trace of its own.

### Why mock mode exists

`LLM_PROVIDER=mock` (the default for tests and smoke) replays cassettes recorded
from real calls. Two reasons, both load-bearing:

- **The demo cannot be killed by a dead key or a rate limit.** A reviewer clones
  the repo and all eleven projects run, at zero cost.
- **Failure modes become testable.** These projects are graded on surviving
  malformed output, refusals, timeouts and runaway loops. You cannot ask a real
  model to produce those on demand; the mock provider scripts them exactly, which
  is how the repair loop and the iteration caps are actually verified rather than
  asserted.

---

## Layout

```
core/agentcore/    shared: llm, cost, tracing, retry, store, mock provider
p01-…/ … p11-…/    one folder per project, each with its own README + tests
server/            one FastAPI app mounting every project's router
web/               Next.js console — the consolidated demo surface
scripts/smoke.py   the check behind the triage table above
docs/              assignment brief + the Part 1 scoping note
```

## Requirements

Python 3.11 (conda env `lyzer`), Node 22 + pnpm for the console. No API key is
needed for `make test` or `make smoke`; real runs need `ANTHROPIC_API_KEY` (see
`.env.example`).
