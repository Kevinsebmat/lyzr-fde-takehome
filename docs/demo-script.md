# Demo walkthrough — under 10 minutes

A route through the strongest builds. Every command runs offline; no API key,
no cost. Timings are generous.

```bash
make install    # once
make smoke      # ~2s — also generates the traces P11 reads
```

Run the console alongside if you want the visual version:

```bash
make dev        # FastAPI :8000, console :3000
```

---

## 0 · The frame (30s)

> Every one of these projects is defined by **the failure it survives**, not by
> its feature list. So the demo is: trigger the failure, watch the guard hold.

```bash
make smoke
```

Eleven pass/fail lines. This is the check behind the README's triage table — no
project is described as working unless it passes here.

---

## 1 · P2 — RAG that refuses (2 min) · *flagship, and the memo's subject*

```bash
cd p02-rag-citations
python -m p02_rag_citations.cli demo
```

Five covered questions answer at 0.76–0.83 confidence. Four uncovered ones
**refuse** at 0.03–0.10.

> A citation the system never checks is decoration. Models cite fluently and
> wrongly, and a wrongly-cited answer is *worse* than an uncited one because it
> defeats the reader's own scepticism.

Then show the per-claim check:

```bash
python -m p02_rag_citations.cli ask "How long are audit logs kept?"
```

Point at: the ✓ per claim, the support score, and the confidence breakdown —
grounding weighted 0.50 because it's the only signal computed *from the
evidence*; the model's own confidence 0.20 because it's the weakest.

**The line to land:** refusing is a feature. It's what a customer is actually
buying when they buy "grounded", and it's the thing a demo never shows.

---

## 2 · P7 — the arithmetic behind the savings claim (2 min) · *flagship*

```bash
cd ../p07-cost-router
python -m p07_cost_router.cli model --tasks 50000
```

> When the cheap model fails you pay for **both** calls. So a cascade only saves
> money above a break-even success rate — and that rate is just the price ratio.
> Haiku is a fifth of Opus, so it must handle 20% of traffic unaided.

The table shows 10% and 20% in red — *costs more*. The brief's "40–60% savings"
needs the cheap tier handling 60–85% of traffic. That's a claim about the
customer's task mix, not about the router.

```bash
python -m p07_cost_router.cli route "Extract the invoice number from: INV-8842, \$1,204.00"
python -m p07_cost_router.cli route "Compare batch against streaming, analyse the trade-offs, and recommend which to fund. Why is the other defensible?"
```

$0.00015 on Haiku versus $0.00317 on Opus — a 21× spread, decided by a free
heuristic.

**The line to land:** the classifier overhead table. It eats 4% of the saving
when routing works and 60% when it's marginal — it bites hardest exactly when
you can least absorb it.

---

## 3 · P6 — a pause that survives a restart (1.5 min)

Three **separate processes**. That's the demonstration, not a convenience.

```bash
cd ../p06-hitl-approval
python -m p06_hitl_approval.cli handle "Refund \$48,200 to Wexler Industries for the two-hour outage on ORD-2."
# note the request id, then in a fresh command:
python -m p06_hitl_approval.cli approve req_XXXX --actor finance-lead \
    --note "SLA calc gives 9640" --set-amount 9640
python -m p06_hitl_approval.cli trail req_XXXX
```

> An agent that blocks a coroutine waiting for a human works perfectly in a demo
> and loses every pending decision the first time the process restarts. A deploy
> during business hours is not an edge case.

**The line to land:** the approver corrected $48,200 → $9,640 and their number
won. They're the authority the pause existed to consult.

---

## 4 · P8 — delivered three times, credited once (1 min)

```bash
cd ../p08-event-automation
python -m p08_event_automation.cli demo
```

> Every real webhook source delivers at least once, so sometimes twice.
> Idempotency here isn't a check, it's a `UNIQUE INDEX` — a check-then-insert
> races with itself under exactly the concurrent redelivery it exists to stop.

Ledger: `{'ACC-1001': 4820.0}`. Then the dead-letter table, and replay under a
*new* key — under the original it would be suppressed as a duplicate and
silently do nothing.

---

## 5 · P11 — the dashboard over everything else (2 min) · *flagship*

```bash
cd ../p11-observability
python -m p11_observability.cli dashboard
python -m p11_observability.cli alerts
python -m p11_observability.cli canary --demo
```

> These are ~800 real spans from the other ten projects, because every call in
> this repo goes through one tracer. This isn't a trace I generated to have
> something to draw.

Point at: p95 against mean; errors grouped by *shape*; and `SCHEMA_DECAY` — the
rule that catches a regression producing **zero errors and 2–3× the cost**.

The canary demo: promotion refused at 5 observations ("unknown, not healthy"),
granted at 25, and a bad release rolled back automatically after 20 requests.

---

## 6 · Close (30s)

```bash
make test    # 326 tests, offline, deterministic
```

> Mock mode isn't a shortcut. You cannot ask a real model to produce a malformed
> response, a refusal, or three timeouts in a row on demand — so the failure
> modes these projects are graded on are *scripted*, which is how the repair
> loop and the iteration caps are verified rather than asserted.

Finish on the README triage table and `docs/scoping-note-p2-rag.pdf`.

---

## If asked "what would you do next?"

1. Wire P11's exporter to LangSmith — the schema already matches.
2. Add the LLM-judge tier to P2's verification, and measure whether the doubled
   cost is worth it on real data rather than assuming.
3. Build the held-out eval set that P2's thresholds actually want; the current
   ones are calibrated for the offline embedder and are honest about it.
