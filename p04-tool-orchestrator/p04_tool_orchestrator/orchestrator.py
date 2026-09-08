"""Parallel execution and conflict resolution.

**Failure isolation.** One tool failing must not take down the batch. A
`gather` without `return_exceptions=True` loses every sibling result to one
timeout, and the agent then has nothing to reason with instead of three
useful answers and one error.

**Conflict resolution is deterministic, not delegated.** When the billing
service says $48,200 and a cached snapshot says $12,000, asking the model to
pick makes the answer depend on prompt phrasing and vary between runs. The
registry declares an authority ordering — system of record beats cache beats
third party — and the code applies it. The model is told what the conflict was
and which source won, but it does not get a vote.

The one case worth escalating rather than resolving: two sources of *equal*
authority disagreeing. That is a data integrity problem, and quietly picking
one hides it.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from agentcore import tracing

from .registry import Caller, PermissionDenied, Registry, ToolResult, UnknownTool

log = logging.getLogger("p04.orchestrator")


@dataclass
class Conflict:
    field: str
    values: dict[str, object]      # tool name -> value
    winner: str | None             # tool whose value was taken
    resolution: str                # how it was decided
    escalate: bool = False         # equal authority: a human should look

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "values": {k: str(v) for k, v in self.values.items()},
            "winner": self.winner,
            "resolution": self.resolution,
            "escalate": self.escalate,
        }


@dataclass
class BatchResult:
    results: list[ToolResult] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    merged: dict = field(default_factory=dict)
    duration_ms: float = 0.0

    @property
    def ok(self) -> list[ToolResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[ToolResult]:
        return [r for r in self.results if not r.ok]

    @property
    def needs_escalation(self) -> bool:
        return any(c.escalate for c in self.conflicts)

    def summary(self) -> dict:
        return {
            "called": len(self.results),
            "ok": len(self.ok),
            "failed": len(self.failed),
            "denied": len(self.denied),
            "conflicts": len(self.conflicts),
            "escalate": self.needs_escalation,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class Orchestrator:
    """Runs tool batches concurrently on its own thread pool.

    **The pool is deliberately owned here rather than borrowed.** A timeout in
    Python cannot cancel a blocking call — `wait_for` around `to_thread`
    returns on schedule, but the worker thread runs the call to completion
    whatever you do. Two consequences follow, and both need handling rather
    than hoping:

    1. `asyncio.to_thread` uses the loop's *default* executor, and `asyncio.run`
       joins that executor on the way out. A 200ms timeout on a 10-second tool
       therefore produces a batch that returns in 200ms and then blocks for
       another 9.8 seconds at shutdown. A private executor that is never joined
       is what makes the timeout actually bound wall-clock time.
    2. The abandoned thread is still consuming a worker. A bounded pool stops
       that starving everything else in the process, and `abandoned` is tracked
       and reported — a rising count is the signal that a downstream dependency
       is sick, and it is invisible if you only look at the timeouts.

    The real fix is tools that are async or cooperatively cancellable. Until
    they are, this bounds the damage and makes it measurable.
    """

    registry: Registry
    #: Cap on genuinely concurrent calls, so a wide plan cannot exhaust
    #: connection pools downstream.
    max_concurrency: int = 5
    per_tool_timeout: float = 5.0
    #: Total worker threads. Larger than max_concurrency so a few abandoned
    #: threads do not immediately stall the next batch.
    pool_size: int = 16
    #: Calls whose thread was still running when we stopped waiting.
    abandoned: int = 0
    _executor: ThreadPoolExecutor | None = field(default=None, repr=False)

    def _pool(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.pool_size, thread_name_prefix="p04-tool"
            )
        return self._executor

    def close(self) -> None:
        """Release the pool without waiting for abandoned work."""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    async def run_batch(
        self, calls: list[tuple[str, dict]], caller: Caller
    ) -> BatchResult:
        """Invoke tools concurrently, isolating every failure mode.

        A denied, unknown, failing or slow tool becomes a recorded result, not
        an exception that kills the batch — the orchestrator's job is to come
        back with as much as it could get, plus an honest account of the rest.
        """
        started = time.perf_counter()
        semaphore = asyncio.Semaphore(self.max_concurrency)
        loop = asyncio.get_running_loop()
        batch = BatchResult()

        async def one(name: str, kwargs: dict) -> ToolResult:
            async with semaphore:
                future = loop.run_in_executor(
                    self._pool(),
                    functools.partial(self.registry.invoke, name, caller, **kwargs),
                )
                try:
                    # shield: the timeout must stop *us* waiting, and must not
                    # pretend to have cancelled work that is still running.
                    return await asyncio.wait_for(
                        asyncio.shield(future), timeout=self.per_tool_timeout
                    )
                except PermissionDenied as exc:
                    batch.denied.append(name)
                    return ToolResult(tool=name, ok=False, error=str(exc))
                except UnknownTool:
                    return ToolResult(tool=name, ok=False, error=f"no tool named {name!r}")
                except (TimeoutError, asyncio.TimeoutError):
                    self.abandoned += 1
                    future.add_done_callback(_drain)  # never leave it unretrieved
                    log.warning(
                        "tool %s exceeded %.2fs; the thread is still running and "
                        "has been abandoned (total abandoned: %d)",
                        name, self.per_tool_timeout, self.abandoned,
                    )
                    return ToolResult(
                        tool=name, ok=False,
                        error=f"timed out after {self.per_tool_timeout}s "
                              "(worker abandoned, not cancelled)",
                    )

        with tracing.span("orchestrate.batch", kind="step", calls=len(calls)) as sp:
            batch.results = list(
                await asyncio.gather(*(one(name, kwargs) for name, kwargs in calls))
            )
            batch.duration_ms = (time.perf_counter() - started) * 1000
            sp.attrs.update(batch.summary(), abandoned=self.abandoned)

        batch.merged, batch.conflicts = merge(batch.ok)
        return batch

    def run_batch_sync(self, calls: list[tuple[str, dict]], caller: Caller) -> BatchResult:
        return asyncio.run(self.run_batch(calls, caller))


def _drain(future) -> None:
    """Consume an abandoned future's outcome so it is not logged as unretrieved."""
    with contextlib.suppress(Exception):
        future.result()


def merge(results: list[ToolResult]) -> tuple[dict, list[Conflict]]:
    """Combine dict-shaped tool results, resolving disagreements by authority.

    Only dict results participate — a scalar has no field to merge on. Values
    that agree are not conflicts, however many tools reported them.
    """
    contributions: dict[str, list[tuple[ToolResult, object]]] = {}
    for result in results:
        if not isinstance(result.value, dict):
            continue
        for key, value in result.value.items():
            contributions.setdefault(key, []).append((result, value))

    merged: dict = {}
    conflicts: list[Conflict] = []

    for field_name, entries in contributions.items():
        distinct = {_hashable(v) for _, v in entries}
        if len(distinct) == 1:
            merged[field_name] = entries[0][1]
            continue

        ranked = sorted(entries, key=lambda e: e[0].authority)
        best_authority = ranked[0][0].authority
        tied = [e for e in ranked if e[0].authority == best_authority]
        tied_values = {_hashable(v) for _, v in tied}

        if len(tied) > 1 and len(tied_values) > 1:
            # Equal authority, different answers. Picking one would hide a data
            # integrity problem behind a confident number.
            winner, resolution, escalate = None, (
                f"{len(tied)} sources of equal authority ({best_authority}) disagree"
            ), True
            merged[field_name] = None
        else:
            winner_result, winner_value = ranked[0]
            winner = winner_result.tool
            resolution = (
                f"{winner} has authority {winner_result.authority}, "
                f"ahead of {', '.join(r.tool for r, _ in ranked[1:])}"
            )
            escalate = False
            merged[field_name] = winner_value

        conflicts.append(
            Conflict(
                field=field_name,
                values={r.tool: v for r, v in entries},
                winner=winner,
                resolution=resolution,
                escalate=escalate,
            )
        )

    return merged, conflicts


def _hashable(value: object) -> object:
    """Compare unhashable values by their repr rather than crashing on them."""
    try:
        hash(value)
        return value
    except TypeError:
        return repr(value)
