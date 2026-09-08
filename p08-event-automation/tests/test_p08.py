"""P8 tests.

Idempotency is the theme. Everything else is about failures landing somewhere
a human can act on them.
"""

from __future__ import annotations

import threading
import time

import pytest
from agentcore import store
from p08_event_automation import queue
from p08_event_automation.queue import Status
from p08_event_automation.worker import (
    PermanentFailure,
    Router,
    TransientFailure,
    Worker,
    default_router,
    ledger,
    reset_ledger,
)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCORE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AGENTCORE_TRACE_FILE", str(tmp_path / "t.jsonl"))
    queue.purge()
    reset_ledger()
    yield
    reset_ledger()
    store.reset()


PAYMENT = {"account": "ACC-1001", "amount": 4820.00}


# ---------- idempotency ----------


def test_a_redelivered_event_is_a_no_op():
    """Every real webhook source delivers at least once, so sometimes twice."""
    first = queue.enqueue("payment.succeeded", PAYMENT, idempotency_key="pay_88")
    second = queue.enqueue("payment.succeeded", PAYMENT, idempotency_key="pay_88")
    assert not first.duplicate and second.duplicate
    assert second.event.id == first.event.id
    assert len(queue.all_events()) == 1


def test_a_duplicate_does_not_raise():
    """A sender that gets an error for a duplicate keeps retrying it. Returning
    the original is what makes the redelivery stop."""
    queue.enqueue("payment.succeeded", PAYMENT, idempotency_key="pay_88")
    result = queue.enqueue("payment.succeeded", PAYMENT, idempotency_key="pay_88")
    assert result.duplicate is True


def test_duplicate_delivery_produces_one_effect():
    for _ in range(5):
        queue.enqueue("payment.succeeded", PAYMENT, idempotency_key="pay_88")
    Worker(default_router()).drain()
    assert ledger()["ACC-1001"] == 4820.00


def test_a_duplicate_after_processing_is_still_suppressed():
    queue.enqueue("payment.succeeded", PAYMENT, idempotency_key="pay_88")
    Worker(default_router()).drain()
    late = queue.enqueue("payment.succeeded", PAYMENT, idempotency_key="pay_88")
    Worker(default_router()).drain()
    assert late.duplicate
    assert ledger()["ACC-1001"] == 4820.00


def test_derived_keys_collapse_identical_payloads():
    queue.enqueue("payment.succeeded", PAYMENT)
    assert queue.enqueue("payment.succeeded", PAYMENT).duplicate


def test_different_payloads_are_different_events():
    queue.enqueue("payment.succeeded", {"account": "A", "amount": 1})
    assert not queue.enqueue("payment.succeeded", {"account": "A", "amount": 2}).duplicate


def test_an_explicit_key_beats_the_payload_hash():
    """Only the sender knows whether two identical events are one retry or two
    genuine occurrences."""
    a = queue.enqueue("payment.succeeded", PAYMENT, idempotency_key="attempt-1")
    b = queue.enqueue("payment.succeeded", PAYMENT, idempotency_key="attempt-2")
    assert not a.duplicate and not b.duplicate
    assert len(queue.all_events()) == 2


def test_idempotency_survives_concurrent_delivery():
    """The unique index has to hold under exactly the concurrency a
    check-then-insert would race on."""
    results = []
    barrier = threading.Barrier(8)

    def deliver():
        barrier.wait()
        results.append(queue.enqueue("payment.succeeded", PAYMENT,
                                     idempotency_key="pay_concurrent"))

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(queue.all_events()) == 1
    assert sum(1 for r in results if not r.duplicate) == 1


# ---------- claiming ----------


def test_two_workers_never_claim_the_same_event():
    for i in range(5):
        queue.enqueue("sync.requested", {"i": i}, idempotency_key=f"e{i}")
    a = queue.claim("worker-a", limit=5)
    b = queue.claim("worker-b", limit=5)
    assert len(a) == 5 and len(b) == 0
    assert len({e.id for e in a}) == 5


def test_claiming_an_empty_queue_returns_nothing():
    assert queue.claim("worker-a", limit=5) == []


def test_an_event_scheduled_for_later_is_not_claimed():
    queue.enqueue("sync.requested", {"upstream_down": True}, idempotency_key="s1")
    Worker(default_router()).drain()          # fails, schedules a retry
    assert queue.claim("worker-a") == [], "a backed-off event must not be claimed early"


# ---------- retries ----------


def test_a_transient_failure_retries_with_backoff():
    queue.enqueue("sync.requested", {"upstream_down": True}, idempotency_key="s1")
    stats = Worker(default_router()).drain()
    assert stats.retried == 1
    event = queue.all_events()[0]
    assert event.status is Status.failed
    assert event.next_attempt_at > time.time()


def test_backoff_grows_between_attempts():
    result = queue.enqueue("sync.requested", {"x": 1}, idempotency_key="s1")
    first = queue.fail(result.event.id, "boom").next_attempt_at
    second = queue.fail(result.event.id, "boom").next_attempt_at
    assert second - first > 0.5, "the second retry must wait longer than the first"


def test_retries_are_bounded():
    """Infinite retry against a broken payload is a denial-of-service against
    your own downstream."""
    result = queue.enqueue("report.generate", {"x": 1}, idempotency_key="r1",
                           max_attempts=3)
    for _ in range(3):
        queue.fail(result.event.id, "boom")
    assert queue.get(result.event.id).status is Status.dead


def test_a_permanent_failure_does_not_burn_retries():
    """A malformed payload will be malformed all three times. Retrying it just
    delays the dead-letter and burns slots a recoverable event needed."""
    queue.enqueue("ticket.created", {"no_subject": True}, idempotency_key="t1")
    Worker(default_router()).drain()
    event = queue.all_events()[0]
    assert event.status is Status.dead
    assert event.attempts == 1


def test_an_unclassified_failure_is_retried_then_dead_lettered():
    queue.enqueue("report.generate", {"x": 1}, idempotency_key="r1")
    worker = Worker(default_router())
    for _ in range(3):
        queue.get(queue.all_events()[0].id)
        with store_time_travel():
            worker.drain()
    assert queue.all_events()[0].status is Status.dead


class store_time_travel:
    """Make backed-off events immediately due, so retry paths are testable
    without sleeping through the backoff."""

    def __enter__(self):
        with store.connect() as conn:
            conn.execute("UPDATE events SET next_attempt_at = 0")
        return self

    def __exit__(self, *exc):
        return False


def test_an_unroutable_event_is_a_config_problem_not_a_transient_one():
    queue.enqueue("nobody.handles.this", {"x": 1}, idempotency_key="u1")
    Worker(default_router()).drain()
    event = queue.all_events()[0]
    assert event.status is Status.dead
    assert "no handler" in event.last_error
    assert event.attempts == 1


# ---------- dead letters ----------


def test_a_dead_event_lands_in_the_dead_letter_table():
    """A failure you cannot inspect is one you will never fix."""
    queue.enqueue("ticket.created", {"no_subject": True}, idempotency_key="t1")
    Worker(default_router()).drain()
    letters = queue.dead_letters()
    assert len(letters) == 1
    assert letters[0]["type"] == "ticket.created"
    assert letters[0]["last_error"]
    assert letters[0]["payload"]


def test_replay_uses_a_new_key_or_it_would_silently_do_nothing():
    queue.enqueue("ticket.created", {"no_subject": True}, idempotency_key="t1")
    Worker(default_router()).drain()
    letter = queue.dead_letters()[0]

    replayed = queue.replay(letter["id"])
    assert not replayed.duplicate, "the original key would be suppressed as a duplicate"
    assert replayed.event.status is Status.pending


def test_replaying_marks_the_letter_handled():
    queue.enqueue("ticket.created", {"no_subject": True}, idempotency_key="t1")
    Worker(default_router()).drain()
    letter = queue.dead_letters()[0]
    queue.replay(letter["id"])
    assert queue.dead_letters() == []
    assert len(queue.dead_letters(include_replayed=True)) == 1


def test_a_fixed_handler_makes_the_replay_succeed():
    """The point of replay: fix the bug, then re-run the traffic it broke on."""
    queue.enqueue("ticket.created", {"no_subject": True}, idempotency_key="t1")
    Worker(default_router()).drain()
    letter = queue.dead_letters()[0]

    fixed = Router()

    @fixed.on("ticket.created")
    def _handle(payload: dict) -> str:
        return "handled by the fixed version"

    queue.replay(letter["id"])
    Worker(fixed).drain()
    replayed_event = [e for e in queue.all_events() if e.status is Status.done]
    assert replayed_event and "fixed version" in replayed_event[0].result


def test_replaying_an_unknown_letter_raises():
    with pytest.raises(KeyError):
        queue.replay("dl_nope")


# ---------- handlers ----------


def test_a_successful_handler_completes_the_event():
    queue.enqueue("payment.succeeded", PAYMENT, idempotency_key="p1")
    stats = Worker(default_router()).drain()
    assert stats.succeeded == 1
    event = queue.all_events()[0]
    assert event.status is Status.done
    assert "credited" in event.result


def test_the_handler_contract_is_narrow():
    """Raise to fail: a handler cannot accidentally acknowledge work it did not
    finish."""
    router = Router()

    @router.on("x")
    def _handler(payload: dict) -> str:
        raise TransientFailure("nope")

    queue.enqueue("x", {}, idempotency_key="x1")
    assert Worker(router).drain().retried == 1


def test_permanent_and_transient_are_handled_differently():
    router = Router()

    @router.on("perm")
    def _perm(payload: dict) -> str:
        raise PermanentFailure("never")

    @router.on("trans")
    def _trans(payload: dict) -> str:
        raise TransientFailure("later")

    queue.enqueue("perm", {}, idempotency_key="p1")
    queue.enqueue("trans", {}, idempotency_key="t1")
    Worker(router).drain()

    events = {e.type: e for e in queue.all_events()}
    assert events["perm"].status is Status.dead
    assert events["trans"].status is Status.failed


def test_drain_is_bounded():
    for i in range(20):
        queue.enqueue("payment.succeeded", {"account": "A", "amount": i},
                      idempotency_key=f"p{i}")
    assert Worker(default_router()).drain(max_events=5).processed == 5


def test_stats_summarise_the_queue():
    queue.enqueue("payment.succeeded", PAYMENT, idempotency_key="p1")
    queue.enqueue("ticket.created", {"no_subject": True}, idempotency_key="t1")
    Worker(default_router()).drain()
    s = queue.stats()
    assert s["total"] == 2
    assert s["by_status"]["done"] == 1
    assert s["dead_letters_pending"] == 1
