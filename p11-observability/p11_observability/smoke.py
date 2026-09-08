"""End-to-end smoke for P11.

Runs against synthetic spans so the assertions are deterministic, but the CLI
and dashboard read the real `traces.jsonl` that P1-P10 produce — `make smoke`
alone generates around 400 spans across eight projects.
"""

from __future__ import annotations

import time
import uuid

from . import canary as canary_mod
from .alerts import Severity, evaluate
from .analysis import error_shapes, overview
from .canary import GuardRails


def span(project, name, kind="llm", status="ok", duration=100.0, cost=0.001,
         run_id=None, parent=None, model="claude-opus-5", error_type=None,
         error_message=None, attrs=None) -> dict:
    return {
        "span_id": uuid.uuid4().hex[:16],
        "run_id": run_id or uuid.uuid4().hex[:16],
        "parent_span_id": parent,
        "project": project,
        "name": name,
        "kind": kind,
        "started_at": time.time(),
        "ended_at": time.time(),
        "duration_ms": duration,
        "status": status,
        "error_type": error_type,
        "error_message": error_message,
        "model": model,
        "input_tokens": 1000,
        "output_tokens": 300,
        "cost_usd": cost,
        "attrs": attrs or {},
    }


def smoke() -> dict:
    results: dict[str, object] = {}

    # ---------- analysis ----------
    spans = [span("p01", "llm.parse", duration=d) for d in (10, 20, 30, 40, 5000)]
    profile = overview(spans)["llm_latency_ms"]
    assert profile["p95"] == 5000, profile
    assert profile["mean"] < 1100
    # The whole reason to report percentiles: the mean hides the outlier that
    # the customer actually experiences.
    assert profile["p95"] > profile["mean"] * 4
    results["p95_beats_the_mean"] = True

    # Errors grouped by shape, not counted.
    noisy = [
        span("p01", "llm.parse", status="error", error_type="ValidationError",
             error_message=f"affected_users must be >= 0, got {-i}")
        for i in range(1, 25)
    ] + [
        span("p01", "llm.parse", status="error", error_type="TimeoutError",
             error_message="request timed out")
    ]
    shapes = error_shapes(noisy)
    assert shapes[0]["count"] == 24, shapes
    assert "affected_users" in shapes[0]["shape"]
    assert "<n>" in shapes[0]["shape"], "varying numbers must collapse into one shape"
    results["grouped_25_errors_into"] = len(shapes)

    # ---------- alerts ----------
    run_id = "loop-run"
    looping = [
        span("p03", "tool.search_archive", kind="tool", run_id=run_id,
             attrs={"arg": "same-query"})
        for _ in range(6)
    ]
    # A multi-step agent whose planner runs once per iteration is not looping.
    looping += [
        span("p03", "react.think", kind="llm", run_id="normal-run")
        for _ in range(8)
    ]
    alerts = evaluate(looping)
    loop_alert = next(a for a in alerts if a.rule == "LOOP")
    assert loop_alert.severity is Severity.critical
    assert loop_alert.evidence["repeats"] == 6
    assert loop_alert.evidence["run_id"] == run_id, "an alert must name the run"
    assert len([a for a in alerts if a.rule == "LOOP"]) == 1, \
        "a planner that runs once per iteration is not a loop"
    results["loop_detected_from_traces"] = True

    # Error rate, with the dominant shape attached so it becomes one ticket.
    mixed = noisy + [span("p01", "llm.parse") for _ in range(10)]
    rate_alert = next(a for a in evaluate(mixed) if a.rule == "ERROR_RATE")
    assert rate_alert.severity is Severity.critical
    assert rate_alert.evidence["dominant_count"] == 24
    results["error_alert_names_the_ticket"] = True

    # A small sample must not raise an alert.
    tiny = [span("p01", "x", status="error"), span("p01", "x")]
    assert not [a for a in evaluate(tiny) if a.rule == "ERROR_RATE"], \
        "two failures out of three is not a signal"
    results["small_samples_do_not_page"] = True

    # Schema decay: succeeds, but costs 2-3x. Invisible to error monitoring.
    decaying = [
        span("p01", "llm.parse", attrs={"attempts": 3, "schema": "TicketTriage"})
        for _ in range(6)
    ] + [span("p01", "llm.parse", attrs={"attempts": 1, "schema": "TicketTriage"})
         for _ in range(6)]
    decay = next(a for a in evaluate(decaying) if a.rule == "SCHEMA_DECAY")
    assert decay.evidence["worst_schema"] == "TicketTriage"
    results["schema_decay_detected"] = True

    # Cost spike against the project's own median.
    normal = [span("p07", "llm", cost=0.001, run_id=f"r{i}") for i in range(10)]
    spike = [span("p07", "llm", cost=0.5, run_id="expensive")]
    cost_alert = next(a for a in evaluate(normal + spike) if a.rule == "COST_SPIKE")
    assert cost_alert.evidence["run_id"] == "expensive"
    results["cost_spike_detected"] = True

    # ---------- canary ----------
    canary_mod.store.kv_set(canary_mod.NAMESPACE, "_reset", {})
    rails = GuardRails(min_samples=20, max_error_rate_multiple=2.0)
    c = canary_mod.start("agent-v2", "v1", "v2", traffic_pct=10.0, rails=rails)

    for _ in range(40):
        c.record("baseline", ok=True, latency_ms=100, cost_usd=0.001)

    # Under-observed is *unknown*, not healthy — and must not promote.
    for _ in range(5):
        c.record("canary", ok=True, latency_ms=100, cost_usd=0.001)
    ok, why = c.promote()
    assert not ok and "unknown, not healthy" in why
    results["refuses_to_promote_on_thin_evidence"] = True

    # Enough good traffic: healthy, and promotable.
    for _ in range(20):
        c.record("canary", ok=True, latency_ms=100, cost_usd=0.001)
    assert c.state is canary_mod.State.healthy
    ok, why = c.promote()
    assert ok, why
    results["promoted_on_evidence"] = True

    # A bad release rolls back automatically, no human in the path.
    bad = canary_mod.start("agent-v3", "v1", "v3", traffic_pct=10.0, rails=rails)
    for _ in range(40):
        bad.record("baseline", ok=True, latency_ms=100, cost_usd=0.001)
    for i in range(25):
        bad.record("canary", ok=(i % 4 != 0), latency_ms=100, cost_usd=0.001)
    assert bad.state is canary_mod.State.rolled_back, bad.state
    assert "error rate" in bad.rollback_reason
    assert bad.traffic_pct == 0.0
    assert bad.route("any-request") == "baseline", "rolled back must serve baseline"
    results["auto_rolled_back"] = True

    # And it survives a restart, because it is persisted.
    reloaded = canary_mod.load("agent-v3")
    assert reloaded.state is canary_mod.State.rolled_back
    results["canary_state_is_durable"] = True

    # ---------- real traces ----------
    real = overview(_real_spans())
    results["real_spans_available"] = real["spans"]

    return results


def _real_spans() -> list[dict]:
    """Whatever the other projects have emitted in this process, if anything."""
    from agentcore import tracing

    return tracing.read_spans()
