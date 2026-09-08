"""Handlers and the worker that drains the queue.

Handler failures are classified before they are retried, because retrying the
wrong thing is worse than not retrying at all. A malformed payload will be
malformed on all three attempts — retrying it just delays the dead-letter by
seven seconds and burns three slots that a recoverable event needed. So:

    permanent  -> dead-letter immediately, do not retry
    transient  -> retry with backoff
    unhandled  -> treat as transient once, then dead-letter

The handler contract is deliberately narrow — take a payload, return a string,
raise to fail — so a handler cannot accidentally acknowledge an event it did
not finish.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from agentcore import tracing

from . import queue
from .queue import Event, Status

log = logging.getLogger("p08.worker")


class PermanentFailure(Exception):
    """The event will never succeed. Do not retry it."""


class TransientFailure(Exception):
    """Might succeed later. Retry with backoff."""


Handler = Callable[[dict], str]


@dataclass
class Router:
    handlers: dict[str, Handler] = field(default_factory=dict)

    def on(self, event_type: str):
        def register(fn: Handler) -> Handler:
            self.handlers[event_type] = fn
            return fn

        return register

    def get(self, event_type: str) -> Handler | None:
        return self.handlers.get(event_type)


@dataclass
class WorkerStats:
    processed: int = 0
    succeeded: int = 0
    retried: int = 0
    dead_lettered: int = 0
    skipped_unhandled: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Worker:
    router: Router
    name: str = "worker-1"
    batch_size: int = 10

    def drain(self, max_events: int = 100) -> WorkerStats:
        """Process everything currently due, then stop.

        Bounded rather than an infinite loop so it is testable and so a flood
        of retries cannot monopolise the process.
        """
        stats = WorkerStats()
        while stats.processed < max_events:
            batch = queue.claim(self.name, limit=min(self.batch_size,
                                                     max_events - stats.processed))
            if not batch:
                break
            for event in batch:
                self._process(event, stats)
        return stats

    def _process(self, event: Event, stats: WorkerStats) -> None:
        stats.processed += 1
        handler = self.router.get(event.type)

        if handler is None:
            # An unroutable event is a configuration problem, not a transient
            # one. Retrying it three times just delays finding that out.
            stats.skipped_unhandled += 1
            queue.kill(event.id, f"no handler registered for event type {event.type!r}")
            stats.dead_lettered += 1
            return

        with tracing.span(f"handle.{event.type}", kind="step", event=event.id) as sp:
            try:
                result = handler(event.payload)
            except PermanentFailure as exc:
                # Skip straight to dead: it will fail identically every time.
                sp.status = "error"
                queue.kill(event.id, f"permanent: {exc}")
                stats.dead_lettered += 1
                return
            except TransientFailure as exc:
                sp.status = "error"
                updated = queue.fail(event.id, f"transient: {exc}")
                _count(updated, stats)
                return
            except Exception as exc:  # noqa: BLE001
                sp.status = "error"
                log.warning("unclassified failure on %s: %s", event.id, exc)
                updated = queue.fail(event.id, f"{type(exc).__name__}: {exc}")
                _count(updated, stats)
                return

            queue.complete(event.id, result)
            stats.succeeded += 1
            sp.attrs["result"] = result[:120]

def _count(updated: Event, stats: WorkerStats) -> None:
    if updated.status is Status.dead:
        stats.dead_lettered += 1
    else:
        stats.retried += 1


# ---------- the demo handlers ----------

_LEDGER: dict[str, float] = {}


def default_router() -> Router:
    router = Router()

    @router.on("payment.succeeded")
    def _payment(payload: dict) -> str:
        """Idempotent at the business level too, not just at the queue.

        Belt and braces: the queue stops a duplicate *delivery*, and this stops
        a duplicate *effect* if the same payment somehow arrives under two
        different keys. Money movement deserves both.
        """
        account = payload["account"]
        amount = float(payload["amount"])
        _LEDGER[account] = _LEDGER.get(account, 0.0) + amount
        return f"credited {account} ${amount:,.2f} (balance ${_LEDGER[account]:,.2f})"

    @router.on("ticket.created")
    def _ticket(payload: dict) -> str:
        if "subject" not in payload:
            raise PermanentFailure("payload has no `subject`; it never will")
        return f"triaged ticket: {payload['subject'][:60]}"

    @router.on("sync.requested")
    def _sync(payload: dict) -> str:
        if payload.get("upstream_down"):
            raise TransientFailure("upstream sync service is unavailable")
        return f"synced {payload.get('resource', 'unknown')}"

    @router.on("report.generate")
    def _report(payload: dict) -> str:
        raise RuntimeError("report renderer crashed")  # unclassified on purpose

    return router


def ledger() -> dict[str, float]:
    return dict(_LEDGER)


def reset_ledger() -> None:
    _LEDGER.clear()
