"""Error taxonomy and retry.

The taxonomy matters more than the backoff: retrying a 400 is a bug, and
retrying a schema-parse failure the *same way* is a worse bug — a parse
failure needs the validation error fed back into the next attempt, which is
what P1 does on top of this.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

log = logging.getLogger("agentcore.retry")

T = TypeVar("T")


class AgentError(RuntimeError):
    """Base for errors this codebase raises deliberately."""


class TransientError(AgentError):
    """Rate limit, timeout, 5xx. Retry with backoff."""


class ParseError(AgentError):
    """Model output failed schema validation. Retry with the error as context."""

    def __init__(self, message: str, raw_output: str = "", attempts: int = 0):
        self.raw_output = raw_output
        self.attempts = attempts
        super().__init__(message)


class PolicyError(AgentError):
    """Refused, blocked, or out of permitted scope. Do not retry."""


class FatalError(AgentError):
    """Bad request, unknown model, missing credential. Do not retry."""


def classify(exc: BaseException) -> type[AgentError]:
    """Map an SDK exception onto the taxonomy.

    Matches on class *name* rather than importing anthropic types, so the core
    stays importable when the SDK is absent (mock mode, CI without extras).
    """
    if isinstance(exc, AgentError):
        return type(exc)
    name = type(exc).__name__
    if name in {
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
        "APIStatusError",
    }:
        return TransientError
    if name in {"PermissionDeniedError", "AuthenticationError"}:
        return PolicyError
    if name in {"BadRequestError", "NotFoundError", "UnprocessableEntityError"}:
        return FatalError
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return TransientError
    return FatalError


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry `fn` on transient failures only, with jittered exponential backoff.

    `sleep` is injectable so tests exercise the backoff path without waiting.
    """
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 — classified immediately
            kind = classify(exc)
            last = exc
            if kind is not TransientError or attempt == attempts:
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay *= 0.5 + random.random()  # jitter, so parallel agents desync
            log.warning(
                "transient %s on attempt %d/%d, retrying in %.2fs: %s",
                type(exc).__name__, attempt, attempts, delay, exc,
            )
            sleep(delay)
    raise last  # unreachable; satisfies type checkers
