"""Alerting on the failure modes agents actually have.

Generic infrastructure monitoring does not catch these. CPU and memory look
fine while an agent loops; the p99 looks fine while a schema regression sends
every third request through three repair attempts. So the rules here are
agent-shaped:

    LOOP            a run repeating the same action
    COST_SPIKE      a run costing far more than the norm for its project
    ERROR_RATE      a project's failure rate above threshold
    SCHEMA_DECAY    validation failures climbing — a prompt or model regression
    LATENCY         p95 above threshold
    SILENT_DEGRADE  a rising share of `degraded` outcomes

**Every alert carries the evidence to act on it.** An alert that says "error
rate high" makes the on-call engineer start the investigation from scratch. One
that names the run id, the error shape and the count starts them halfway
through it.

The thresholds are deliberately explicit constants rather than learned
baselines. A learned baseline quietly normalises a regression that has been
running for a week, which is precisely when you most want to be told.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum

from .analysis import _shape, percentile


class Severity(str, Enum):
    critical = "critical"
    warning = "warning"
    info = "info"


@dataclass
class Alert:
    rule: str
    severity: Severity
    title: str
    detail: str
    #: Ids, counts and examples — enough to start work without re-deriving them.
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity.value,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class Thresholds:
    #: Identical action signatures in one run before it is called a loop.
    loop_repeats: int = 3
    #: A run costing this multiple of its project's median is a spike.
    cost_spike_multiple: float = 5.0
    #: Ignore spikes below this absolute cost — 5x of nothing is nothing.
    cost_spike_floor_usd: float = 0.01
    error_rate_warning: float = 0.05
    error_rate_critical: float = 0.20
    p95_latency_ms: float = 10_000.0
    schema_failure_rate: float = 0.10
    degraded_rate: float = 0.15
    #: Below this, rates are noise. Two failures out of three is not a 67%
    #: error rate worth paging someone about.
    min_sample: int = 10


def evaluate(spans: list[dict], thresholds: Thresholds | None = None) -> list[Alert]:
    t = thresholds or Thresholds()
    alerts: list[Alert] = []
    alerts += _loops(spans, t)
    alerts += _cost_spikes(spans, t)
    alerts += _error_rates(spans, t)
    alerts += _schema_decay(spans, t)
    alerts += _latency(spans, t)
    alerts += _degradation(spans, t)
    order = {Severity.critical: 0, Severity.warning: 1, Severity.info: 2}
    return sorted(alerts, key=lambda a: order[a.severity])


def _loops(spans: list[dict], t: Thresholds) -> list[Alert]:
    """A run repeating the same action *with the same arguments*.

    Detected from traces rather than from inside the agent, so it catches loops
    in projects that have no loop detection of their own.

    Only spans carrying an argument are considered. An earlier version counted
    every tool and LLM span, which flagged every multi-step agent as looping —
    a ReAct planner legitimately calls `think` once per iteration, and with no
    argument to distinguish them those calls all collapse into one signature.
    Repetition is only evidence of a loop when the *inputs* repeat too.
    """
    by_run: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        if s["kind"] == "tool" and str(s.get("attrs", {}).get("arg", "")).strip():
            by_run[s["run_id"]].append(s)

    alerts = []
    for run_id, run_spans in by_run.items():
        signatures = Counter(
            (s["name"], str(s.get("attrs", {}).get("arg", "")).strip().lower())
            for s in run_spans
        )
        worst, count = signatures.most_common(1)[0] if signatures else (None, 0)
        if count >= t.loop_repeats:
            alerts.append(Alert(
                rule="LOOP",
                severity=Severity.critical,
                title=f"possible loop in {run_spans[0]['project']}",
                detail=(
                    f"`{worst[0]}` called {count} times with identical arguments "
                    "in a single run — generic infrastructure monitoring will "
                    "not catch this"
                ),
                evidence={
                    "run_id": run_id,
                    "action": worst[0],
                    "repeats": count,
                    "run_cost_usd": round(
                        sum(s.get("cost_usd") or 0 for s in run_spans), 6
                    ),
                },
            ))
    return alerts


def _cost_spikes(spans: list[dict], t: Thresholds) -> list[Alert]:
    cost_by_run: dict[str, float] = defaultdict(float)
    project_of: dict[str, str] = {}
    for s in spans:
        cost_by_run[s["run_id"]] += s.get("cost_usd") or 0
        if s["project"]:
            project_of[s["run_id"]] = s["project"]

    by_project: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for run_id, cost in cost_by_run.items():
        by_project[project_of.get(run_id, "unknown")].append((run_id, cost))

    alerts = []
    for project, runs in by_project.items():
        costs = sorted(c for _, c in runs)
        if len(costs) < 3:
            continue
        median = costs[len(costs) // 2]
        if median <= 0:
            continue
        for run_id, cost in runs:
            if cost >= t.cost_spike_floor_usd and cost > median * t.cost_spike_multiple:
                alerts.append(Alert(
                    rule="COST_SPIKE",
                    severity=Severity.warning,
                    title=f"cost spike in {project}",
                    detail=(
                        f"one run cost ${cost:.4f}, {cost / median:.1f}x the "
                        f"${median:.4f} median for this project"
                    ),
                    evidence={"run_id": run_id, "cost_usd": round(cost, 6),
                              "median_usd": round(median, 6)},
                ))
    return alerts


def _error_rates(spans: list[dict], t: Thresholds) -> list[Alert]:
    by_project: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        if s["project"]:
            by_project[s["project"]].append(s)

    alerts = []
    for project, project_spans in by_project.items():
        if len(project_spans) < t.min_sample:
            continue  # a rate over three samples is noise, not a signal
        errors = [s for s in project_spans if s["status"] == "error"]
        rate = len(errors) / len(project_spans)
        if rate < t.error_rate_warning:
            continue
        shapes = Counter(
            (s.get("error_type"), _shape(s.get("error_message"))) for s in errors
        )
        (error_type, shape), count = shapes.most_common(1)[0]
        alerts.append(Alert(
            rule="ERROR_RATE",
            severity=(Severity.critical if rate >= t.error_rate_critical
                      else Severity.warning),
            title=f"{project} error rate {rate:.1%}",
            detail=(
                f"{len(errors)} of {len(project_spans)} spans failed; "
                f"{count} of them are the same {error_type}"
            ),
            # The dominant shape is the actionable part — it turns "47 errors"
            # into one ticket.
            evidence={
                "project": project,
                "error_rate": round(rate, 4),
                "dominant_error": error_type,
                "dominant_shape": shape,
                "dominant_count": count,
            },
        ))
    return alerts


def _schema_decay(spans: list[dict], t: Thresholds) -> list[Alert]:
    """Repair attempts climbing means a prompt or model regression.

    Invisible to latency and error-rate monitoring: the requests still succeed,
    they just cost two or three times as much to do it.
    """
    parses = [s for s in spans if s["name"].startswith("llm.parse")
              and s["kind"] == "llm" and "attempt" not in s["name"]]
    if len(parses) < t.min_sample:
        return []

    repaired = [s for s in parses if (s.get("attrs", {}).get("attempts") or 1) > 1]
    rate = len(repaired) / len(parses)
    if rate < t.schema_failure_rate:
        return []

    schemas = Counter(s.get("attrs", {}).get("schema") for s in repaired)
    worst, count = schemas.most_common(1)[0]
    return [Alert(
        rule="SCHEMA_DECAY",
        severity=Severity.warning,
        title=f"{rate:.0%} of structured calls needed a repair turn",
        detail=(
            f"{len(repaired)} of {len(parses)} parses took more than one "
            f"attempt; {count} were `{worst}`. Latency and error-rate "
            "monitoring will not show this — the calls succeed, they just cost "
            "two or three times as much."
        ),
        evidence={"repair_rate": round(rate, 4), "worst_schema": worst,
                  "count": count},
    )]


def _latency(spans: list[dict], t: Thresholds) -> list[Alert]:
    by_project: dict[str, list[float]] = defaultdict(list)
    for s in spans:
        if s["kind"] == "run" and s["project"] and s.get("duration_ms"):
            by_project[s["project"]].append(s["duration_ms"])

    alerts = []
    for project, durations in by_project.items():
        if len(durations) < 5:
            continue
        p95 = percentile(durations, 95)
        if p95 > t.p95_latency_ms:
            alerts.append(Alert(
                rule="LATENCY",
                severity=Severity.warning,
                title=f"{project} p95 {p95:.0f}ms",
                # Deliberately reported against the mean, because a mean that
                # looks fine is exactly how a bad p95 stays hidden.
                detail=f"p95 {p95:.0f}ms against a mean of "
                       f"{sum(durations) / len(durations):.0f}ms",
                evidence={"project": project, "p95_ms": p95,
                          "p99_ms": percentile(durations, 99),
                          "samples": len(durations)},
            ))
    return alerts


def _degradation(spans: list[dict], t: Thresholds) -> list[Alert]:
    """A rising share of degraded outcomes — refusals, partial answers.

    These are successes by every conventional metric, which is exactly why they
    need their own rule: the system is quietly doing less than it used to.
    """
    by_project: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        if s["project"]:
            by_project[s["project"]].append(s)

    alerts = []
    for project, project_spans in by_project.items():
        if len(project_spans) < t.min_sample:
            continue
        degraded = [s for s in project_spans if s["status"] == "degraded"]
        rate = len(degraded) / len(project_spans)
        if rate >= t.degraded_rate:
            alerts.append(Alert(
                rule="SILENT_DEGRADE",
                severity=Severity.info,
                title=f"{project} degrading on {rate:.0%} of spans",
                detail=(
                    f"{len(degraded)} spans returned a degraded result. These "
                    "count as successes everywhere else, which is why they need "
                    "their own rule."
                ),
                evidence={"project": project, "degraded": len(degraded),
                          "rate": round(rate, 4)},
            ))
    return alerts
