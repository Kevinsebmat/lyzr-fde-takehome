# Architecture

How this repo is put together, and the reasoning behind the parts that are not
obvious from reading one file. Start with the top-level `README.md` for what is
built and what is skipped; this is the map for anyone changing the code.

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
`scripts/smoke.py` calls — and a `cli.py`, which is its canonical interface.

**Server and console.** `server/main.py` mounts every project into one FastAPI
app. P1, P2 and P8 own routers inside their own packages (P8's webhook *is* the
product); the rest live in `server/routers.py`, because a near-empty `api.py` per
project is filing, not structure. `web/` is a Next.js console driving them all —
the consolidated demo surface, not the primary interface. `server/main.py`
inlines its `sys.path` setup rather than importing `conftest`: an import sorter
will move an import statement, and moving that one breaks every project import
below it.

**Cassettes.** `scripts/seed_cassettes.py` (`make seed`) writes the demo
fixtures, keyed on the exact request each agent builds. It validates payloads
against their schemas and fails loudly when a corpus edit leaves a P2 cassette
citing a chunk retrieval no longer returns. Re-run it after changing a corpus, a
system prompt, or a chunker — a stale cassette silently degrades a demo to
placeholder text rather than erroring.

**The memo.** `docs/scoping-note-p2-rag.md` is the source of truth; `make memo`
renders the PDF and *fails* if it runs to two pages, because the brief caps it
at one.

## Conventions

Each project is defined by **the failure mode it must survive**, not by its
feature list — that framing drives the code, the tests and the console. The
per-project READMEs name the failure each one handles.

- **Shared core, independent projects.** Each `pNN-*/` folder stays independently
  runnable from its own README, but they share `core/agentcore` rather than each
  carrying a private copy of an LLM client, retry loop, cost meter and tracer.
  That was a deliberate reversal of the obvious "duplicate for isolation" rule:
  the duplication risk is eleven half-working copies, and the shared chokepoint
  is what makes P7 (reroute by cost) and P11 (traces from every project)
  possible at all. Projects still must not import *each other*.
- **Caps are the feature, not a defence.** In P3, P7, P9 and P10 the limit *is*
  the thing being demonstrated — max iterations, token budget, consensus cutoff,
  regeneration cap. Keep them explicit constructor parameters with a stated
  reason, never implicit constants buried in a loop.
- **Log what you would need at 3am.** Validation failures with the offending
  output, retries with the reason, cost per call. P1, P7 and P11 depend on those
  records existing; a failure you cannot reproduce from the log is not
  actionable.
- Model ids are `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5` — no date
  suffixes. Pricing and per-model capability flags live in
  `core/agentcore/models.py` and were verified against the current price list
  rather than recalled; P7's whole business case is those numbers, so re-verify
  rather than edit from memory.
