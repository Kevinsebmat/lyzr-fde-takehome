"""Analysis over the traces the other ten projects emit.

This project has an advantage worth stating: it is not looking at a toy trace
of its own making. Every LLM call, tool call and agent step in P1-P10 goes
through `agentcore.tracing`, so `make smoke` produces ~400 real spans across
eight projects and the dashboards below are computed from actual traffic.

Two principles run through the metrics:

**Percentiles, not means.** A mean latency of 800ms hides the 5% of requests
taking nine seconds, and those are the ones the customer complains about. Every
latency figure here is p50/p95/p99.

**Errors are grouped by shape, not counted.** "47 errors" is not actionable;
"41 of them are the same ValidationError on TicketTriage.affected_users" is a
ticket someone can pick up.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from agentcore import tracing


def load(path=None) -> list[dict]:
    return tracing.read_spans(path)


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile.

    Deliberately not interpolating: with the span counts here, an interpolated
    p99 invents a value between two real observations, and it is more useful to
    be able to point at the actual slow request.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(p / 100 * len(ordered)) - 1)
    return round(ordered[min(index, len(ordered) - 1)], 3)


@dataclass
class LatencyProfile:
    count: int
    p50: float
    p95: float
    p99: float
    max: float
    mean: float

    @classmethod
    def of(cls, values: list[float]) -> LatencyProfile:
        if not values:
            return cls(0, 0, 0, 0, 0, 0)
        return cls(
            count=len(values),
            p50=percentile(values, 50),
            p95=percentile(values, 95),
            p99=percentile(values, 99),
            max=round(max(values), 3),
            mean=round(statistics.fmean(values), 3),
        )

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def overview(spans: list[dict]) -> dict:
    """Top-line health: volume, spend, latency, error rate."""
    runs = [s for s in spans if s["kind"] == "run"]
    llm = [s for s in spans if s["kind"] == "llm"]
    errors = [s for s in spans if s["status"] == "error"]

    return {
        "spans": len(spans),
        "runs": len(runs),
        "llm_calls": len(llm),
        "tool_calls": sum(1 for s in spans if s["kind"] == "tool"),
        "projects": len({s["project"] for s in spans if s["project"]}),
        "total_cost_usd": round(sum(s.get("cost_usd") or 0 for s in spans), 6),
        "input_tokens": sum(s.get("input_tokens") or 0 for s in spans),
        "output_tokens": sum(s.get("output_tokens") or 0 for s in spans),
        "error_rate": round(len(errors) / len(spans), 4) if spans else 0.0,
        "degraded": sum(1 for s in spans if s["status"] == "degraded"),
        "run_latency_ms": LatencyProfile.of(
            [s["duration_ms"] for s in runs if s.get("duration_ms")]
        ).as_dict(),
        "llm_latency_ms": LatencyProfile.of(
            [s["duration_ms"] for s in llm if s.get("duration_ms")]
        ).as_dict(),
    }


def by_project(spans: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        if s["project"]:
            grouped[s["project"]].append(s)

    rows = []
    for project, project_spans in grouped.items():
        runs = [s for s in project_spans if s["kind"] == "run"]
        errors = [s for s in project_spans if s["status"] == "error"]
        rows.append({
            "project": project,
            "runs": len(runs),
            "spans": len(project_spans),
            "cost_usd": round(sum(s.get("cost_usd") or 0 for s in project_spans), 6),
            "cost_per_run_usd": round(
                sum(s.get("cost_usd") or 0 for s in project_spans) / len(runs), 6
            ) if runs else 0.0,
            "error_rate": round(len(errors) / len(project_spans), 4),
            "p95_run_ms": percentile(
                [s["duration_ms"] for s in runs if s.get("duration_ms")], 95
            ),
        })
    return sorted(rows, key=lambda r: -r["cost_usd"])


def by_model(spans: list[dict]) -> list[dict]:
    """Where the money goes, and what each tier actually costs per call."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        if s.get("model"):
            grouped[s["model"]].append(s)

    rows = []
    for model, model_spans in grouped.items():
        costs = [s.get("cost_usd") or 0 for s in model_spans]
        rows.append({
            "model": model,
            "calls": len(model_spans),
            "cost_usd": round(sum(costs), 6),
            "mean_cost_usd": round(statistics.fmean(costs), 6) if costs else 0.0,
            "output_tokens": sum(s.get("output_tokens") or 0 for s in model_spans),
            "p95_ms": percentile(
                [s["duration_ms"] for s in model_spans if s.get("duration_ms")], 95
            ),
        })
    return sorted(rows, key=lambda r: -r["cost_usd"])


def error_shapes(spans: list[dict], limit: int = 10) -> list[dict]:
    """Group errors by (type, message shape) rather than counting them.

    "47 errors" is not actionable. "41 of them are the same ValidationError on
    the same field" is a ticket someone can pick up this afternoon.
    """
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for s in spans:
        if s["status"] != "error":
            continue
        # Fall back to the span name when there is no message, so spans that
        # merely marked themselves failed still group by *where* they failed
        # rather than collapsing into one useless "(no message)" bucket.
        shape = _shape(s.get("error_message")) if s.get("error_message") else f"at {s['name']}"
        grouped[(s.get("error_type") or "unknown", shape)].append(s)

    rows = [
        {
            "error_type": error_type,
            "shape": shape,
            "count": len(group),
            "projects": sorted({s["project"] for s in group if s["project"]}),
            "example": (group[0].get("error_message") or "")[:200],
            "example_span": group[0]["span_id"],
        }
        for (error_type, shape), group in grouped.items()
    ]
    return sorted(rows, key=lambda r: -r["count"])[:limit]


def _shape(message: str | None) -> str:
    """Collapse a message to its shape by removing the varying parts.

    Two errors that differ only in an id or a number are one problem, and
    counting them separately buries it under its own instances.
    """
    if not message:
        return "(no message)"
    import re

    shape = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", message)
    shape = re.sub(r"-?\d+\.?\d*", "<n>", shape)
    shape = re.sub(r"'[^']{0,60}'", "'<v>'", shape)
    return shape[:160]


# ---------- traces ----------


@dataclass
class TraceNode:
    span: dict
    children: list[TraceNode] = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return (self.span.get("cost_usd") or 0) + sum(c.total_cost for c in self.children)


def trace(spans: list[dict], run_id: str) -> TraceNode | None:
    """Rebuild one run as a tree, so a slow run can be read rather than guessed at."""
    in_run = [s for s in spans if s["run_id"] == run_id]
    if not in_run:
        return None

    nodes = {s["span_id"]: TraceNode(span=s) for s in in_run}
    root: TraceNode | None = None
    for s in in_run:
        node = nodes[s["span_id"]]
        parent = nodes.get(s.get("parent_span_id"))
        if parent is None:
            root = root or node
        else:
            parent.children.append(node)
    for node in nodes.values():
        node.children.sort(key=lambda n: n.span["started_at"])
    return root


def slowest_runs(spans: list[dict], limit: int = 5) -> list[dict]:
    runs = [s for s in spans if s["kind"] == "run" and s.get("duration_ms")]
    runs.sort(key=lambda s: -s["duration_ms"])
    return [
        {
            "run_id": s["run_id"],
            "project": s["project"],
            "duration_ms": s["duration_ms"],
            "status": s["status"],
        }
        for s in runs[:limit]
    ]


def costliest_runs(spans: list[dict], limit: int = 5) -> list[dict]:
    by_run: dict[str, float] = Counter()
    project_of: dict[str, str] = {}
    for s in spans:
        by_run[s["run_id"]] += s.get("cost_usd") or 0
        if s["project"]:
            project_of[s["run_id"]] = s["project"]
    return [
        {"run_id": run_id, "project": project_of.get(run_id), "cost_usd": round(cost, 6)}
        for run_id, cost in by_run.most_common(limit)
    ]
