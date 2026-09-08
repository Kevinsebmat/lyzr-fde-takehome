"""End-to-end smoke for P8.

The first check is the one the project exists for: a redelivered event must be
a no-op. Everything else is about failures ending up somewhere you can act on.
"""

from __future__ import annotations

import time

from . import queue
from .queue import Status
from .worker import Worker, default_router, ledger, reset_ledger


def smoke() -> dict:
    results: dict[str, object] = {}
    queue.purge()
    reset_ledger()
    worker = Worker(default_router())

    # 1. THE check: at-least-once delivery means duplicates. A payment
    #    delivered three times must be credited once.
    payload = {"account": "ACC-1001", "amount": 4820.00, "payment_id": "pay_88"}
    first = queue.enqueue("payment.succeeded", payload, idempotency_key="pay_88")
    dupes = [
        queue.enqueue("payment.succeeded", payload, idempotency_key="pay_88")
        for _ in range(2)
    ]
    assert not first.duplicate
    assert all(d.duplicate for d in dupes)
    assert all(d.event.id == first.event.id for d in dupes)

    worker.drain()
    assert ledger()["ACC-1001"] == 4820.00, ledger()
    results["duplicate_delivery_credited_once"] = True

    # 2. A duplicate arriving *after* processing is still a no-op.
    late = queue.enqueue("payment.succeeded", payload, idempotency_key="pay_88")
    worker.drain()
    assert late.duplicate
    assert ledger()["ACC-1001"] == 4820.00, "a late redelivery double-credited"
    results["late_redelivery_safe"] = True

    # 3. A transient failure retries with backoff rather than dying.
    queue.enqueue("sync.requested", {"resource": "contacts", "upstream_down": True},
                  idempotency_key="sync-1")
    stats = worker.drain()
    assert stats.retried == 1, stats.as_dict()
    event = next(e for e in queue.all_events() if e.type == "sync.requested")
    assert event.status is Status.failed
    assert event.next_attempt_at > time.time(), "a retry must be scheduled, not immediate"
    results["transient_retries_with_backoff"] = True

    # 4. A permanent failure skips the retries entirely.
    queue.enqueue("ticket.created", {"no_subject_here": True}, idempotency_key="tkt-1")
    worker.drain()
    ticket = next(e for e in queue.all_events() if e.type == "ticket.created")
    assert ticket.status is Status.dead
    assert ticket.attempts == 1, "a permanent failure must not burn three attempts"
    results["permanent_failure_skips_retries"] = True

    # 5. Exhausted retries land in the dead-letter table, not a log line.
    letters = queue.dead_letters()
    assert letters, "a failure you cannot inspect is one you will never fix"
    assert any(dl["type"] == "ticket.created" for dl in letters)
    results["dead_lettered"] = len(letters)

    # 6. A dead letter can be replayed — under a NEW key, or the unique index
    #    would suppress the replay as a duplicate and it would silently do
    #    nothing.
    target = next(dl for dl in letters if dl["type"] == "ticket.created")
    replayed = queue.replay(target["id"])
    assert not replayed.duplicate, "replaying under the original key does nothing"
    assert queue.dead_letters() == [dl for dl in queue.dead_letters()
                                    if dl["id"] != target["id"]]
    results["replayed"] = True

    # 7. An unroutable event is a config problem, not a transient one.
    queue.enqueue("nobody.handles.this", {"x": 1}, idempotency_key="unrouted-1")
    worker.drain()
    unrouted = next(e for e in queue.all_events() if e.type == "nobody.handles.this")
    assert unrouted.status is Status.dead
    assert "no handler" in unrouted.last_error
    results["unhandled_event_dead_lettered"] = True

    # 8. Two workers must never claim the same event.
    queue.purge()
    for i in range(6):
        queue.enqueue("sync.requested", {"resource": f"r{i}"}, idempotency_key=f"c{i}")
    a = queue.claim("worker-a", limit=6)
    b = queue.claim("worker-b", limit=6)
    assert len(a) == 6 and len(b) == 0, (len(a), len(b))
    assert len({e.id for e in a}) == 6
    results["claims_are_exclusive"] = True

    queue.purge()
    reset_ledger()
    return results
