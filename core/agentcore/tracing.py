"""Structured trace events, written as JSONL.

Every LLM call and every agent step in all eleven projects emits a span here.
P11 is then a dashboard over real traffic from the rest of the submission
rather than a toy trace of its own — which is the point of building it last.

Deliberately a file, not a service: the demo has to run from a clone with no
infrastructure. `AGENTCORE_TRACE_FILE` redirects it; tests point it at tmp.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_TRACE_LOCK = threading.Lock()

#: Current run and parent span, so nested steps stitch into a tree without
#: every call site threading an id through its signature.
_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)
_project: ContextVar[str | None] = ContextVar("project", default=None)
_parent_span: ContextVar[str | None] = ContextVar("parent_span", default=None)


def trace_path() -> Path:
    return Path(os.environ.get("AGENTCORE_TRACE_FILE", "traces.jsonl")).expanduser()


@dataclass
class Span:
    span_id: str
    run_id: str
    parent_span_id: str | None
    project: str | None
    name: str
    kind: str  # llm | tool | step | run
    started_at: float
    ended_at: float | None = None
    duration_ms: float | None = None
    status: str = "ok"  # ok | error | degraded
    error_type: str | None = None
    error_message: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


def emit(span: Span) -> None:
    """Append one span. Tracing must never take down the agent it observes."""
    try:
        path = trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(span), default=str)
        with _TRACE_LOCK, path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 — observability failure is not agent failure
        pass


@contextlib.contextmanager
def run(project: str, name: str = "run", **attrs: Any) -> Iterator[str]:
    """Open a top-level run. Everything emitted inside shares its run_id."""
    rid = uuid.uuid4().hex[:16]
    tok_run, tok_proj = _run_id.set(rid), _project.set(project)
    try:
        with span(name, kind="run", **attrs):
            yield rid
    finally:
        _run_id.reset(tok_run)
        _project.reset(tok_proj)


@contextlib.contextmanager
def span(name: str, kind: str = "step", **attrs: Any) -> Iterator[Span]:
    """Time a unit of work and record how it ended.

    Yielded so callers can attach results after the fact:
        with span("retrieve") as s: s.attrs["hits"] = len(docs)
    """
    s = Span(
        span_id=uuid.uuid4().hex[:16],
        run_id=_run_id.get() or uuid.uuid4().hex[:16],
        parent_span_id=_parent_span.get(),
        project=_project.get(),
        name=name,
        kind=kind,
        started_at=time.time(),
        attrs=dict(attrs),
    )
    token = _parent_span.set(s.span_id)
    try:
        yield s
    except Exception as exc:  # record, then let it propagate
        s.status = "error"
        s.error_type = type(exc).__name__
        s.error_message = str(exc)[:500]
        raise
    finally:
        _parent_span.reset(token)
        s.ended_at = time.time()
        s.duration_ms = round((s.ended_at - s.started_at) * 1000, 3)
        emit(s)


def read_spans(path: Path | None = None) -> list[dict]:
    """Load spans for analysis. Skips malformed lines rather than failing —
    a half-written final line from a killed process is normal."""
    p = path or trace_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def current_run_id() -> str | None:
    return _run_id.get()
