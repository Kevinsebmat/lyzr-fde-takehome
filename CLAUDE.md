# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Environment is the conda env **`lyzer`** (Python 3.11). `make install` creates it.

```bash
make install      # create `lyzer`, install core + workspace editable
make test         # full pytest suite — offline, deterministic, no API key
make smoke        # run all 11 projects end-to-end in mock mode
make lint         # ruff
make dev          # FastAPI :8000 + Next.js console :3000
make demo-reset   # wipe traces.jsonl + agentcore.db for a clean demo
```

Direct interpreter: `/opt/homebrew/Caskroom/miniforge/base/envs/lyzer/bin/python`

Single test / single project:

```bash
<py> -m pytest core/tests/test_core.py::test_parse_repairs_malformed_output
<py> -m pytest p02-rag-citations/tests -q
<py> scripts/smoke.py p02 p07          # smoke a subset
SMOKE_TRACEBACK=1 <py> scripts/smoke.py p02   # full traceback on failure
```

`make smoke` is the gate behind every "fully working" claim in the top-level
README — update the triage table from its output, never from memory.

## Architecture

Eleven projects over one shared core. `core/agentcore/` is the spine:

- **`llm.py` — the only place the Anthropic SDK is called.** Every project goes
  through `LLM.complete()` / `LLM.parse()`. This single chokepoint is what makes
  three projects possible: P7 reroutes models because selection lives here, P11
  gets traces from all ten other projects because every call emits one here, and
  mock mode works everywhere because the switch is here. Do not call `anthropic`
  directly from a project.
- **`models.py`** — the model registry: ids, tiers, context, per-Mtok pricing,
  and the per-model capability flags (`supports_effort`, `adaptive_thinking`).
  Haiku 4.5 and the 5-series diverge on thinking and effort, and getting it wrong
  is a 400, not a degraded response — so `LLM._request_kwargs` applies it from the
  registry. Pricing is load-bearing for P7 and P11: verify against the
  `claude-api` skill rather than recalling it.
- **`tracing.py`** — JSONL spans with contextvar-propagated run/parent ids.
  Deliberately a file, not a service, so a clone runs with no infrastructure.
  Tracing failures are swallowed: observability must never take down the agent.
- **`cost.py`** — `Usage` (priced from the registry) and `Budget`, which refuses
  a call *before* it is made. `baseline_comparison()` is P7's headline metric.
- **`retry.py`** — the error taxonomy matters more than the backoff. Transient
  retries; parse failures instead get a repair turn (in `llm.parse`); policy and
  fatal errors never retry.
- **`store.py`** — sqlite + exact numpy cosine, plus an embedding cache. Not a
  vector DB on purpose: these corpora are hundreds of chunks, exact beats
  approximate, and `git clone && make smoke` needs no service.
- **`mock.py`** — cassette replay plus `PROVIDER.queue(...)` for scripting exact
  failures in tests. Recording is `AGENTCORE_RECORD=1` with a real key.

**Project layout.** Folders are hyphenated (`p01-structured-output/`) per the
brief; the importable package inside is underscored (`p01_structured_output/`).
Root `conftest.py` bridges the two onto `sys.path`. Each project exposes
`smoke.py:smoke()` returning a summary dict and raising on failure — that is what
`scripts/smoke.py` calls.

## What this repository is for

The Lyzr Forward Deployed Engineer take-home. Two deliverables, **weighted 40% / 60%**:

1. **Part 1 — Client scoping note** (max 1 page, PDF or doc): pick *one* of the 12 projects and write a fund/don't-fund memo for a non-technical stakeholder. Must cover the customer problem in plain language, what "production-ready" means vs. a demo, 2–3 risks/trade-offs, and scope + timeline for a 2-week paid engagement. Graded on client-facing solutioning (40%).
2. **Part 2 — Build**: attempt as many of the 12 projects as possible. P1–P11 are the real target; **P12 is bonus only and is never penalized if skipped**. Graded on technical execution (60%): breadth (how many run end-to-end), depth (does each handle the failure mode implied by its "what it shows"), code quality, production thinking (error handling, logging, retries, tests), and how prioritization trade-offs are communicated.

Explicit grading stance: *"A small thing that works end-to-end beats a large thing that half-works."* Both deliverables are needed — a great build with no memo, or a great memo with no code, both fall short.

## Required repository layout

The submission format is fixed by the assignment, so keep to it:

```
README.md                 # top-level: what is FULLY WORKING / PARTIAL / SKIPPED and why
docs/                     # assignment source; scoping note (PDF or doc) lands here
p01-.../  p02-.../  ...   # ONE SUBFOLDER PER PROJECT ATTEMPTED
  └── README.md           # per-project: setup + how to run
```

The top-level README's done/partial/skipped triage table is itself graded — it is not boilerplate. Skipped projects must be named and justified, not silently omitted.

## The 12 projects, by track

Each project is defined by the failure mode it must survive, not by its feature list. Build to the failure mode.

**Track 1 — Reliability & Safety**
- **P1 Structured Output Agent** — Pydantic/JSON schema enforcement, tool-response validation, retry on parse errors, logged validation failures. *Failure mode: nondeterministic LLM output.*
- **P3 ReAct Planning Agent** — observe → think → act → reflect, max-iteration limits, self-critique, graceful degradation. *Failure mode: infinite loops.*
- **P6 Human-in-the-Loop Approval Agent** — uncertainty detection → pause → request human input → resume with validated context, full audit trail. *Failure mode: unsafe autonomous action.*

**Track 2 — Knowledge & Memory**
- **P2 RAG Agent with Citation Grounding** — retrieval, answers with sources, low-confidence flagging, fallback to search. *Failure mode: hallucination.*
- **P5 Memory-Enabled Conversational Agent** — short-term buffer + long-term vector recall, context compression, relevance scoring, cross-session sync. *Failure mode: amnesia across sessions.*

**Track 3 — Orchestration & Multi-Agent**
- **P4 Multi-Tool Orchestrator Agent** — dynamic tool registry, capability-based routing, permission scoping, parallel execution, conflict resolution.
- **P9 Multi-Agent Debate System** — proposers, a critic, voting/consensus, aggregator synthesis with confidence.

**Track 4 — Production & Cost Ops**
- **P7 Cost-Aware Agent Router** — per-task token budgeting, model routing by complexity/cost, early exit on confidence, cost-per-decision analytics.
- **P8 Event-Triggered Automation Agent** — webhook/queue listeners, idempotent execution, dead-letter handling, retry logic.
- **P11 Production Agent with Observability** — tracing (LangSmith/Arize), latency/cost dashboards, alerting on loops/failures, canary testing, rollback.

**Track 5 — Self-Improvement & Community**
- **P10 Self-Reflective Agent with Auto-Eval** — execute → LLM-as-judge evaluation → critique → regenerate under constraints, logged improvement metrics.
- **P12 Open Source Agent Framework Contribution** *(bonus)* — extend LangGraph/CrewAI/AutoGen with a new pattern, docs + demo, published benchmarks, PR + tutorial.

## Working conventions for this repo

- **Per-project isolation.** Each `pNN-*/` folder must be independently runnable from its own README with its own dependencies. Cross-project imports create a single point of failure across the whole submission — prefer duplicating a small helper over coupling two projects.
- **Cost and iteration caps are features here, not defenses.** P3, P7, P9, and P10 are each graded on the limit itself (max iterations, token budget, consensus cutoff, regeneration cap). Make caps explicit and configurable rather than implicit.
- **Structured logging is graded.** "Evidence of production thinking (error handling, logging, retries, tests)" is a scoring line. Log validation failures, retries, and cost per call in the projects where that is the point (P1, P7, P11).
- When building agents against Claude, default to the latest models (Opus 5 `claude-opus-5`, Sonnet 5 `claude-sonnet-5`, Haiku 4.5 `claude-haiku-4-5-20251001`) — P7's cost router in particular depends on real current pricing tiers, so verify pricing rather than recalling it.

## Deadline

10 calendar days from receipt. Partial completion is expected; going quiet on unfinished projects is explicitly penalized relative to naming them.
