# P8 — Event-Triggered Automation Agent

**Failure mode this project exists to survive:** issuing the same refund twice
because the webhook was delivered twice — during an incident, when the retries
are thickest.

Status: **working end-to-end.** 27 tests, all offline.

## Setup & run

```bash
make install    # from the repo root
python -m p08_event_automation.cli demo     # ← the whole story
python -m p08_event_automation.cli send payment.succeeded \
    -d '{"account":"ACC-1001","amount":4820}' -k pay_88 --times 3
python -m p08_event_automation.cli work
python -m p08_event_automation.cli events
python -m p08_event_automation.cli dead --replay dl_xxxx
python ../scripts/smoke.py p08 && python -m pytest tests -q
```

```
a flaky sender delivers the same payment three times
  delivery 1: accepted → evt_f93a50642545
  delivery 2: duplicate suppressed → evt_f93a50642545
  delivery 3: duplicate suppressed → evt_f93a50642545
worker
worker {'processed': 3, 'succeeded': 1, 'retried': 0, 'dead_lettered': 2}
ledger {'ACC-1001': 4820.0}          ← delivered three times, credited once
```

## Every real webhook source delivers at least once

Which means sometimes twice. Stripe, GitHub, Slack and every broker worth using
redeliver on a timeout, a 500, or a network blip they saw and you didn't.

So **idempotency is the schema, not a feature**: a `UNIQUE INDEX` on
`idempotency_key`. A check-then-insert would race with itself under exactly the
concurrent redelivery it exists to stop — there's a test that fires eight
simultaneous deliveries through a barrier and asserts one event lands.

Enqueueing a duplicate **does not raise**. A sender that gets an error for a
redelivery keeps redelivering; returning the original event is what makes it
stop. The webhook endpoint returns 202 for new work and 200 for a duplicate, so
a sender can tell the difference without treating it as a failure.

The key itself: **sender-supplied wins**, because only the sender knows whether
two structurally identical events are one retry or two genuine occurrences.
Hashing the payload is the fallback, and it's a real trade-off — two legitimately
identical events would collapse into one. That's the safer error for money
movement and the wrong one for page views, so the choice is documented rather
than hidden.

## Retrying the wrong thing is worse than not retrying

Failures are classified before they're retried:

| | Handling | Why |
|---|---|---|
| `PermanentFailure` | dead-letter **immediately** | a malformed payload is malformed all three times |
| `TransientFailure` | retry, exponential backoff | it might work in a second |
| unclassified | retry once, then dead-letter | unknown, so assume recoverable once |
| no handler registered | dead-letter immediately | a config problem, not a transient one |

`queue.kill()` dead-letters directly rather than looping `fail()` until the
attempts run out. Both reach the same state, but looping writes *"retrying in
1.0s"* into the log for something that isn't being retried — and someone on call
at 3am will believe it.

Retries are bounded and backed off. Infinite retry against a permanently broken
payload is how a queue becomes a denial-of-service attack on its own downstream.

## Claiming is atomic

Two workers polling the same table must not both get the same event. The claim
is a conditional `UPDATE ... RETURNING`, so the database decides. A
SELECT-then-UPDATE would race precisely under the load that makes you want two
workers in the first place.

## Dead letters are a destination, not a log line

Failures you can't inspect and replay are failures you'll never fix. The table
keeps the payload, the attempt count and the last error.

**Replay uses a new idempotency key** — slightly counter-intuitive, and
essential: replaying under the original key would hit the unique index and be
suppressed as a duplicate, so the replay would silently do nothing. There's a
test that fixes a broken handler, replays, and asserts the event now succeeds.

## Defence in depth on money

The payment handler is idempotent at the *business* level too. The queue stops a
duplicate delivery; the handler stops a duplicate effect if the same payment
somehow arrives under two different keys. Money movement deserves both.

## API

- `POST /api/p08/webhook` — `Idempotency-Key` header; 202 new, 200 duplicate
- `POST /api/p08/drain` — run the worker once
- `GET /api/p08/events`, `GET /api/p08/dead-letters`
- `POST /api/p08/dead-letters/{id}/replay`

## Where the code lives

| File | |
|---|---|
| `queue.py` | schema, idempotency, atomic claim, backoff, dead letters, replay |
| `worker.py` | failure classification, the router, the demo handlers |
