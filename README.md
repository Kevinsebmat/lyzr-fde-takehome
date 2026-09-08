# Lyzr FDE Take-Home

Eleven agent projects sharing one core, plus the Part 1 scoping note.

I built each project around the specific failure it has to survive rather than
around a feature list. That decision shaped the code, the tests and the demo
console.

```bash
make install    # creates the `lyzer` conda env, installs everything editable
make smoke      # runs all 11 projects end to end. No API key, no cost.
make test       # 326 tests, offline and deterministic
make dev        # FastAPI on :8000, console on :3000
```

---

## What's done

`make smoke` runs every project end to end and prints a pass/fail line for each.
I didn't mark anything below as working unless it passes there, so the table and
the code can't quietly drift apart.

| # | Project | Status | Tests | Notes |
|---|---------|--------|------:|-------|
| P1 | Structured Output | ✅ working | 24 | Repair turns that name the failing field, model escalation, two-sided tool contracts |
| P2 | RAG with Citation Grounding | ✅ working | 28 | Verified citations and a real refusal path. Flagship, and the memo's subject. |
| P3 | ReAct Planner | ✅ working | 20 | Four independent stopping conditions. Every exit still answers. |
| P4 | Multi-Tool Orchestrator | ✅ working | 32 | Scope enforced at invocation, deterministic conflict resolution |
| P5 | Memory Agent | ✅ working | 23 | Supersession: a corrected fact stops being retrievable |
| P6 | Human-in-the-Loop Approval | ✅ working | 29 | A pause that survives a restart, append-only audit |
| P7 | Cost-Aware Router | ✅ working | 34 | Break-even arithmetic that says when routing *doesn't* pay. Flagship. |
| P8 | Event Automation | ✅ working | 27 | Idempotency as a unique index, replayable dead letters |
| P9 | Multi-Agent Debate | ✅ working | 20 | Independent proposers. A tie is reported as a tie. |
| P10 | Self-Reflective Auto-Eval | ✅ working | 24 | Keeps the best attempt rather than the last, with improvement logged |
| P11 | Production Observability | ✅ working | 35 | Dashboards over ~400 real spans from P1–P10. Flagship. |
| P12 | OSS Framework Contribution | ⛔ skipped | — | Bonus only. Reasoning below. |

All eleven required projects run end to end. 326 tests: 296 across the projects,
30 on the shared core. `make lint` is clean.

## What's partial

Three things fall short of what I'd actually ship. All three were deliberate.

**No LangSmith or Arize export in P11.** The trace schema already carries
`run_id`, `parent_span_id`, model, tokens, cost and status, which is what those
exporters want, so the adapter would be small. I left it out because it needs an
API key, and an integration that can't run in the offline demo isn't really
finished.

**P2's citation checking is lexical.** It catches the expensive case, where a
claim states a figure its source doesn't contain. It won't catch an answer that
paraphrases its source while inverting the meaning. Fixing that means an LLM
judge per claim and roughly double the cost per answer. I put that tradeoff in
the scoping note rather than quietly leaving it out.

**Four console panels show placeholders offline.** P3, P5, P9 and P10 build
their prompts from accumulated conversation state, so I can't key a recorded
response to them. Their CLIs and tests cover them properly, and the console works
fully once you set an API key.

## Why I skipped P12

It's bonus credit with no penalty attached. Getting a PR merged upstream depends
on a maintainer's schedule, which doesn't fit inside ten days, and the time would
have come out of the eleven projects that actually count. I'd rather say that
than leave a silent gap.

---

## Part 1 — the scoping note

[`docs/scoping-note-p2-rag.pdf`](docs/scoping-note-p2-rag.pdf), one page, on P2.
The markdown source sits next to it and `make memo` rebuilds the PDF. That target
fails if the output runs to two pages, since the brief caps it at one.

The recommendation is to fund a two-week pilot, but gated on the week-one
evaluation set. If accuracy on the customer's own documents comes back low, the
documents are the problem and that's where the next money should go, not into
more engineering.

## Demo

[`docs/demo-script.md`](docs/demo-script.md) is a walkthrough under ten minutes
covering the strongest builds, with the exact commands. Everything runs offline.
I ran each command as written before committing the script, which is how I found
two of the bugs listed below.

---

## A few decisions worth explaining

### Why there's a shared `core/`

The brief asks for one subfolder per project with its own README and setup, and
the repo does that. But eleven private copies of an LLM client, a retry loop, a
cost meter and a trace writer is how you end up with eleven things that each
half-work.

So `core/agentcore/` holds the shared infrastructure while each project stays
independently runnable. Two things fall out of that which per-project copies
couldn't give you. P7 can reroute by cost because model selection lives in one
place, so the router is a policy change instead of eleven edits. And P11 has real
data to work with, because every call in every project emits a trace span, which
turns the observability project into a dashboard over the actual submission
instead of a toy trace of its own.

### Why mock mode exists

`LLM_PROVIDER=mock` is the default for tests and smoke, and it replays cassettes
recorded from real calls.

The obvious reason is that a dead key or a rate limit can't kill the demo. Clone
the repo, run it, and all eleven projects work at zero cost.

The better reason is that it makes failure modes testable. These projects are
judged on surviving malformed output, refusals, timeouts and runaway loops, and
you can't ask a real model to produce any of those on demand. The mock provider
scripts them exactly, which is how the repair loop and the iteration caps are
actually verified instead of just asserted.

### Bugs worth mentioning

Four things I got wrong, all found by running the code rather than reasoning
about it. Each is documented where it happened.

**`asyncio.wait_for` can't cancel a blocking call.** P4's batch took 10,015ms
with a 200ms timeout set. `to_thread` uses the loop's default executor and
`asyncio.run` joins it on the way out, so the timeout bounded the wait and
nothing else. A private executor that's never joined fixed it. 205ms now, with a
test.

**The offline embedder ranked the wrong chunk first.** It summed a random
gaussian per token, which spreads every token across all dimensions with random
signs. Feature hashing replaced it, so cosine similarity tracks shared vocabulary
the way you'd expect.

**P11's loop detector flagged three P3 runs that were working correctly.** It
counted every tool and LLM span, and a ReAct planner legitimately calls `think`
once per iteration. It now requires the inputs to repeat too.

**I was wrong about classifier overhead.** I'd written that a per-request LLM
classifier "consumes most of the saving" in P7. When I measured it, it's 4–5% at
a healthy success rate. The interesting part is that the classifier's cost stays
fixed while the saving shrinks, so at a 25% success rate it eats 60%. The
overhead hurts most exactly when routing is already marginal.

---

## Layout

```
core/agentcore/    shared: llm, cost, tracing, retry, store, mock provider
p01-…/ … p11-…/    one folder per project, each with its own README and tests
server/            one FastAPI app that mounts every project
web/               the console, which is the consolidated demo surface
scripts/smoke.py   the check behind the table above
docs/              the brief, the scoping note, the demo script
```

## Requirements

Python 3.11 in the `lyzer` conda env, plus Node 22 and pnpm if you want the
console. You don't need an API key for `make test`, `make smoke` or the console.
`.env.example` lists what a live run wants.
