"""A durable event queue with idempotency, retries and a dead-letter table.

The whole design follows from one fact: **every real webhook source delivers at
least once, and therefore sometimes twice.** Stripe, GitHub, Slack and every
message broker worth using will redeliver on a timeout, a 500, or a network
blip they saw and you didn't. An automation that is not idempotent will
eventually issue the same refund twice, and it will do it during an incident
when the retries are thickest.

So idempotency is not a feature here, it is the schema. The unique index on
`idempotency_key` makes a duplicate impossible at the database level rather
than by a check-then-insert that races with itself.

Three more properties that separate this from a demo:

- **Claiming is atomic.** Two workers polling the same table must not both get
  the same event. The claim is a conditional UPDATE, so the database decides.
- **Retries are bounded and backed off.** Infinite retry against a permanently
  broken payload is how a queue becomes a denial-of-service against its own
  downstream.
- **The dead-letter table is a first-class destination**, not a log line.
  Failures you cannot inspect and replay are failures you will never fix.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from agentcore import store, tracing

log = logging.getLogger("p08.queue")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id               TEXT PRIMARY KEY,
    idempotency_key  TEXT NOT NULL,
    type             TEXT NOT NULL,
    payload          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    next_attempt_at  REAL NOT NULL DEFAULT 0,
    claimed_by       TEXT,
    claimed_at       REAL,
    last_error       TEXT,
    result           TEXT,
    received_at      REAL NOT NULL,
    completed_at     REAL
);

-- Idempotency enforced by the database, not by a check-then-insert that races
-- with itself under exactly the concurrent redelivery it is meant to stop.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency
    ON events(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS dead_letters (
    id            TEXT PRIMARY KEY,
    event_id      TEXT NOT NULL,
    type          TEXT NOT NULL,
    payload       TEXT NOT NULL,
    attempts      INTEGER NOT NULL,
    last_error    TEXT NOT NULL,
    died_at       REAL NOT NULL,
    replayed_at   REAL
);
"""


class Status(str, Enum):
    pending = "pending"
    in_flight = "in_flight"
    done = "done"
    failed = "failed"       # retryable, waiting for its next attempt
    dead = "dead"           # out of attempts, moved to dead letters


@dataclass
class Event:
    id: str
    idempotency_key: str
    type: str
    payload: dict
    status: Status = Status.pending
    attempts: int = 0
    max_attempts: int = 3
    next_attempt_at: float = 0.0
    last_error: str | None = None
    result: str | None = None
    received_at: float = field(default_factory=time.time)
    completed_at: float | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Event:
        return cls(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            type=row["type"],
            payload=json.loads(row["payload"]),
            status=Status(row["status"]),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            next_attempt_at=row["next_attempt_at"],
            last_error=row["last_error"],
            result=row["result"],
            received_at=row["received_at"],
            completed_at=row["completed_at"],
        )

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["status"] = self.status.value
        return d


def init() -> None:
    with store.connect() as conn:
        conn.executescript(SCHEMA)


def derive_key(event_type: str, payload: dict, explicit: str | None = None) -> str:
    """The idempotency key.

    A sender-supplied key is always preferred — only the sender knows whether
    two structurally identical events are one retry or two genuine occurrences.
    Hashing the payload is the fallback, and it is a real trade-off: two
    legitimately identical events (the same customer, the same amount, twice in
    a second) would collapse into one. That is the safer error for money
    movement, and the wrong one for, say, page views. The choice is documented
    rather than hidden.
    """
    if explicit:
        return f"explicit:{explicit}"
    digest = hashlib.sha256(
        json.dumps({"type": event_type, "payload": payload}, sort_keys=True,
                   default=str).encode()
    ).hexdigest()[:32]
    return f"derived:{digest}"


@dataclass
class EnqueueResult:
    event: Event
    duplicate: bool


def enqueue(
    event_type: str,
    payload: dict,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
) -> EnqueueResult:
    """Accept an event. A redelivery is a no-op that returns the original.

    Note what this does *not* do: raise. A webhook sender that gets an error
    for a duplicate will keep retrying it. Returning 200 with the original
    event is what makes the redelivery stop.
    """
    init()
    key = derive_key(event_type, payload, idempotency_key)
    event = Event(
        id=f"evt_{uuid.uuid4().hex[:12]}",
        idempotency_key=key,
        type=event_type,
        payload=payload,
        max_attempts=max_attempts,
    )

    with store.connect() as conn:
        try:
            conn.execute(
                "INSERT INTO events (id, idempotency_key, type, payload, status, "
                "max_attempts, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event.id, key, event_type, json.dumps(payload, default=str),
                 Status.pending.value, max_attempts, event.received_at),
            )
        except sqlite3.IntegrityError:
            # The unique index did its job. This is the expected path for a
            # redelivery, not an error condition.
            row = conn.execute(
                "SELECT * FROM events WHERE idempotency_key = ?", (key,)
            ).fetchone()
            log.info("duplicate event %s suppressed (key %s)", event_type, key)
            return EnqueueResult(event=Event.from_row(row), duplicate=True)

    return EnqueueResult(event=event, duplicate=False)


def claim(worker: str, limit: int = 1) -> list[Event]:
    """Atomically take ownership of due events.

    A conditional UPDATE, so two workers polling simultaneously cannot both get
    the same row. A SELECT-then-UPDATE would race precisely under the load that
    makes you want two workers.
    """
    init()
    now = time.time()
    claimed: list[Event] = []

    with store.connect() as conn:
        for _ in range(limit):
            cursor = conn.execute(
                """
                UPDATE events SET status = ?, claimed_by = ?, claimed_at = ?
                WHERE id = (
                    SELECT id FROM events
                    WHERE status IN (?, ?) AND next_attempt_at <= ?
                    ORDER BY received_at LIMIT 1
                )
                RETURNING *
                """,
                (Status.in_flight.value, worker, now,
                 Status.pending.value, Status.failed.value, now),
            )
            row = cursor.fetchone()
            if row is None:
                break
            claimed.append(Event.from_row(row))
    return claimed


def complete(event_id: str, result: str) -> None:
    with store.connect() as conn:
        conn.execute(
            "UPDATE events SET status = ?, result = ?, completed_at = ?, "
            "attempts = attempts + 1 WHERE id = ?",
            (Status.done.value, result[:2000], time.time(), event_id),
        )


def fail(event_id: str, error: str, base_delay: float = 1.0) -> Event:
    """Record a failure and either schedule a retry or dead-letter it.

    Backoff is exponential. Retrying immediately and forever against a
    permanently broken payload turns the queue into a denial-of-service attack
    on its own downstream.
    """
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        event = Event.from_row(row)
        attempts = event.attempts + 1

        if attempts >= event.max_attempts:
            conn.execute(
                "UPDATE events SET status = ?, attempts = ?, last_error = ?, "
                "completed_at = ? WHERE id = ?",
                (Status.dead.value, attempts, error[:1000], time.time(), event_id),
            )
            conn.execute(
                "INSERT INTO dead_letters (id, event_id, type, payload, attempts, "
                "last_error, died_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"dl_{uuid.uuid4().hex[:12]}", event_id, event.type,
                 json.dumps(event.payload, default=str), attempts,
                 error[:1000], time.time()),
            )
            log.error("event %s dead-lettered after %d attempts: %s",
                      event_id, attempts, error)
        else:
            delay = base_delay * (2 ** (attempts - 1))
            conn.execute(
                "UPDATE events SET status = ?, attempts = ?, last_error = ?, "
                "next_attempt_at = ?, claimed_by = NULL WHERE id = ?",
                (Status.failed.value, attempts, error[:1000],
                 time.time() + delay, event_id),
            )
            log.warning("event %s failed (attempt %d/%d), retrying in %.1fs: %s",
                        event_id, attempts, event.max_attempts, delay, error)

        return get(event_id)


def kill(event_id: str, error: str) -> Event:
    """Dead-letter an event immediately, without pretending to retry it.

    Used for failures that are certain to recur: a malformed payload, an
    unroutable event type. Looping `fail()` until the attempts run out would
    reach the same end state, but it writes "retrying in 1.0s" into the log for
    something that is not being retried — and someone on call at 3am will
    believe it.
    """
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        event = Event.from_row(row)
        attempts = event.attempts + 1
        conn.execute(
            "UPDATE events SET status = ?, attempts = ?, last_error = ?, "
            "completed_at = ?, next_attempt_at = 0 WHERE id = ?",
            (Status.dead.value, attempts, error[:1000], time.time(), event_id),
        )
        conn.execute(
            "INSERT INTO dead_letters (id, event_id, type, payload, attempts, "
            "last_error, died_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"dl_{uuid.uuid4().hex[:12]}", event_id, event.type,
             json.dumps(event.payload, default=str), attempts, error[:1000], time.time()),
        )
    log.error("event %s dead-lettered without retry (will never succeed): %s",
              event_id, error)
    return get(event_id)


def get(event_id: str) -> Event:
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise KeyError(event_id)
    return Event.from_row(row)


def all_events(status: Status | None = None, limit: int = 200) -> list[Event]:
    init()
    with store.connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM events WHERE status = ? ORDER BY received_at DESC LIMIT ?",
                (status.value, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY received_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [Event.from_row(r) for r in rows]


# ---------- dead letters ----------


def dead_letters(include_replayed: bool = False) -> list[dict]:
    init()
    with store.connect() as conn:
        sql = "SELECT * FROM dead_letters"
        if not include_replayed:
            sql += " WHERE replayed_at IS NULL"
        rows = conn.execute(sql + " ORDER BY died_at DESC").fetchall()
    return [dict(r) for r in rows]


def replay(dead_letter_id: str, max_attempts: int = 3) -> EnqueueResult:
    """Re-enqueue a dead letter under a fresh idempotency key.

    A new key is essential and slightly counter-intuitive: replaying under the
    original key would hit the unique index and be suppressed as a duplicate,
    so the replay would silently do nothing. The link back to the original is
    kept in the payload for the audit trail.
    """
    init()
    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM dead_letters WHERE id = ?", (dead_letter_id,)
        ).fetchone()
        if row is None:
            raise KeyError(dead_letter_id)
        conn.execute(
            "UPDATE dead_letters SET replayed_at = ? WHERE id = ?",
            (time.time(), dead_letter_id),
        )

    payload = json.loads(row["payload"])
    result = enqueue(
        row["type"],
        payload,
        idempotency_key=f"replay:{dead_letter_id}:{time.time_ns()}",
        max_attempts=max_attempts,
    )
    log.info("replayed dead letter %s as event %s", dead_letter_id, result.event.id)
    return result


def stats() -> dict:
    init()
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM events GROUP BY status"
        ).fetchall()
        dead = conn.execute(
            "SELECT COUNT(*) AS n FROM dead_letters WHERE replayed_at IS NULL"
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    return {
        "total": total,
        "by_status": {r["status"]: r["n"] for r in rows},
        "dead_letters_pending": dead,
    }


def purge() -> None:
    """Clear everything. For the demo and for tests."""
    init()
    with store.connect() as conn:
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM dead_letters")


__all__ = [
    "Event", "EnqueueResult", "Status", "all_events", "claim", "complete",
    "dead_letters", "derive_key", "enqueue", "fail", "get", "init", "kill", "purge",
    "replay", "stats", "tracing",
]
