"""P11 tests."""

from __future__ import annotations

import pytest
from agentcore import store
from p11_observability import canary as canary_mod
from p11_observability.alerts import Severity, evaluate
from p11_observability.analysis import (
    LatencyProfile,
    by_model,
    by_project,
    costliest_runs,
    error_shapes,
    overview,
    percentile,
    slowest_runs,
    trace,
)
from p11_observability.canary import GuardRails, State
from p11_observability.smoke import span


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCORE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AGENTCORE_TRACE_FILE", str(tmp_path / "t.jsonl"))
    yield
    store.reset()


# ---------- percentiles ----------


def test_percentiles_expose_what_the_mean_hides():
    """A mean of 1020ms and a p95 of 5000ms describe very different systems."""
    values = [10, 20, 30, 40, 5000]
    profile = LatencyProfile.of(values)
    assert profile.p50 == 30
    assert profile.p95 == 5000
    assert profile.mean == 1020
    assert profile.p95 > profile.mean * 4


def test_percentile_of_nothing_is_zero_not_a_crash():
    assert percentile([], 95) == 0.0
    assert LatencyProfile.of([]).count == 0


def test_percentile_uses_a_real_observation():
    """Interpolating invents a number no request actually took."""
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 95) in values


# ---------- overview ----------


def test_overview_counts_the_shape_of_traffic():
    spans = [
        span("p01", "run", kind="run", cost=0),
        span("p01", "llm.parse", cost=0.002),
        span("p01", "tool.x", kind="tool", cost=0),
        span("p01", "llm.parse", status="error", cost=0.001),
    ]
    o = overview(spans)
    assert o["runs"] == 1 and o["llm_calls"] == 2 and o["tool_calls"] == 1
    assert o["total_cost_usd"] == pytest.approx(0.003)
    assert o["error_rate"] == 0.25


def test_overview_of_nothing_does_not_divide_by_zero():
    assert overview([])["error_rate"] == 0.0


def test_by_project_reports_cost_per_run():
    spans = [
        span("p01", "run", kind="run", cost=0),
        span("p01", "llm", cost=0.01),
        span("p02", "run", kind="run", cost=0),
        span("p02", "llm", cost=0.05),
    ]
    rows = {r["project"]: r for r in by_project(spans)}
    assert rows["p02"]["cost_per_run_usd"] == pytest.approx(0.05)
    assert by_project(spans)[0]["project"] == "p02", "costliest first"


def test_by_model_shows_where_the_money_goes():
    spans = [span("p07", "llm", model="claude-haiku-4-5", cost=0.001) for _ in range(10)]
    spans += [span("p07", "llm", model="claude-opus-5", cost=0.05)]
    rows = by_model(spans)
    assert rows[0]["model"] == "claude-opus-5"
    assert rows[0]["calls"] == 1 and rows[0]["cost_usd"] == pytest.approx(0.05)


# ---------- error grouping ----------


def test_errors_are_grouped_by_shape_not_counted():
    """'47 errors' is not actionable; '41 of them are the same one' is."""
    spans = [
        span("p01", "llm", status="error", error_type="ValidationError",
             error_message=f"affected_users must be >= 0, got {-i}")
        for i in range(1, 42)
    ] + [
        span("p01", "llm", status="error", error_type="TimeoutError",
             error_message="request timed out"),
    ]
    shapes = error_shapes(spans)
    assert len(shapes) == 2, "42 errors must collapse into 2 shapes"
    assert shapes[0]["count"] == 41
    assert "<n>" in shapes[0]["shape"]


def test_ids_and_quoted_values_collapse_into_the_same_shape():
    spans = [
        span("p01", "llm", status="error", error_type="KeyError",
             error_message=f"no order 'ORD-{i}' with id a1b2c3d4e5f6")
        for i in range(20)
    ]
    assert len(error_shapes(spans)) == 1


def test_the_shape_carries_an_example_to_start_from():
    spans = [span("p01", "llm", status="error", error_type="X",
                  error_message="something specific broke")]
    assert error_shapes(spans)[0]["example"] == "something specific broke"
    assert error_shapes(spans)[0]["example_span"]


# ---------- alerts ----------


def test_a_repeated_action_raises_a_critical_loop_alert():
    spans = [
        span("p03", "tool.search", kind="tool", run_id="r1", attrs={"arg": "same"})
        for _ in range(5)
    ]
    alert = next(a for a in evaluate(spans) if a.rule == "LOOP")
    assert alert.severity is Severity.critical
    assert alert.evidence["repeats"] == 5
    assert alert.evidence["run_id"] == "r1"


def test_varied_actions_are_not_a_loop():
    spans = [
        span("p03", "tool.search", kind="tool", run_id="r1", attrs={"arg": f"q{i}"})
        for i in range(5)
    ]
    assert not [a for a in evaluate(spans) if a.rule == "LOOP"]


def test_error_alerts_carry_the_dominant_shape():
    """The alert has to start the investigation, not begin it from scratch."""
    spans = [
        span("p01", "llm", status="error", error_type="ValidationError",
             error_message=f"field bad {i}")
        for i in range(20)
    ] + [span("p01", "llm") for _ in range(5)]
    alert = next(a for a in evaluate(spans) if a.rule == "ERROR_RATE")
    assert alert.severity is Severity.critical
    assert alert.evidence["dominant_error"] == "ValidationError"
    assert alert.evidence["dominant_count"] == 20


def test_a_small_sample_does_not_page_anyone():
    """Two failures out of three is not a 67% error rate worth waking someone."""
    spans = [span("p01", "llm", status="error") for _ in range(2)]
    spans.append(span("p01", "llm"))
    assert not [a for a in evaluate(spans) if a.rule == "ERROR_RATE"]


def test_error_rate_severity_escalates():
    warn = [span("p01", "llm", status="error") for _ in range(1)]
    warn += [span("p01", "llm") for _ in range(14)]
    crit = [span("p02", "llm", status="error") for _ in range(8)]
    crit += [span("p02", "llm") for _ in range(12)]
    alerts = {a.evidence["project"]: a for a in evaluate(warn + crit)
              if a.rule == "ERROR_RATE"}
    assert alerts["p01"].severity is Severity.warning
    assert alerts["p02"].severity is Severity.critical


def test_schema_decay_is_invisible_to_error_monitoring():
    """These calls all succeed. They just cost two or three times as much."""
    spans = [
        span("p01", "llm.parse", attrs={"attempts": 3, "schema": "TicketTriage"})
        for _ in range(6)
    ] + [
        span("p01", "llm.parse", attrs={"attempts": 1, "schema": "TicketTriage"})
        for _ in range(6)
    ]
    assert overview(spans)["error_rate"] == 0.0, "no errors at all"
    alert = next(a for a in evaluate(spans) if a.rule == "SCHEMA_DECAY")
    assert alert.evidence["worst_schema"] == "TicketTriage"


def test_cost_spike_is_relative_to_the_project_median():
    spans = [span("p07", "llm", cost=0.001, run_id=f"r{i}") for i in range(10)]
    spans.append(span("p07", "llm", cost=0.5, run_id="expensive"))
    alert = next(a for a in evaluate(spans) if a.rule == "COST_SPIKE")
    assert alert.evidence["run_id"] == "expensive"


def test_a_spike_below_the_floor_is_not_an_alert():
    """5x of nothing is nothing."""
    spans = [span("p07", "llm", cost=0.000001, run_id=f"r{i}") for i in range(10)]
    spans.append(span("p07", "llm", cost=0.0001, run_id="tiny-spike"))
    assert not [a for a in evaluate(spans) if a.rule == "COST_SPIKE"]


def test_silent_degradation_gets_its_own_rule():
    """Degraded outcomes are successes by every conventional metric."""
    spans = [span("p02", "llm", status="degraded") for _ in range(5)]
    spans += [span("p02", "llm") for _ in range(10)]
    assert overview(spans)["error_rate"] == 0.0
    alert = next(a for a in evaluate(spans) if a.rule == "SILENT_DEGRADE")
    assert alert.evidence["degraded"] == 5


def test_a_healthy_system_raises_nothing():
    spans = [span("p01", "llm") for _ in range(50)]
    spans += [span("p01", "run", kind="run", duration=100) for _ in range(10)]
    assert evaluate(spans) == []


def test_alerts_are_sorted_by_severity():
    spans = [
        span("p03", "tool.x", kind="tool", run_id="r1", attrs={"arg": "same"})
        for _ in range(5)
    ]
    spans += [span("p02", "llm", status="degraded") for _ in range(5)]
    spans += [span("p02", "llm") for _ in range(10)]
    alerts = evaluate(spans)
    assert alerts[0].severity is Severity.critical


# ---------- traces ----------


def test_a_run_rebuilds_into_a_tree():
    root = span("p01", "run", kind="run", run_id="r1", cost=0)
    child = span("p01", "llm", run_id="r1", parent=root["span_id"], cost=0.01)
    grandchild = span("p01", "tool", kind="tool", run_id="r1",
                      parent=child["span_id"], cost=0)
    node = trace([root, child, grandchild], "r1")
    assert node.span["name"] == "run"
    assert node.children[0].children[0].span["name"] == "tool"
    assert node.total_cost == pytest.approx(0.01)


def test_an_unknown_run_is_none():
    assert trace([span("p01", "x")], "nope") is None


def test_slowest_and_costliest_runs_are_findable():
    spans = [span("p01", "run", kind="run", run_id=f"r{i}", duration=i * 100, cost=i * 0.01)
             for i in range(1, 6)]
    assert slowest_runs(spans, 1)[0]["run_id"] == "r5"
    assert costliest_runs(spans, 1)[0]["run_id"] == "r5"


# ---------- canary ----------


def rails(**kwargs) -> GuardRails:
    return GuardRails(**{"min_samples": 20, **kwargs})


def feed(canary, arm, n, ok=True, latency=100.0, cost=0.001):
    for _ in range(n):
        canary.record(arm, ok=ok, latency_ms=latency, cost_usd=cost)


def test_a_thin_canary_is_unknown_not_healthy():
    """Promoting on three good requests is how a bad release reaches prod."""
    c = canary_mod.start("c1", "v1", "v2", rails=rails())
    feed(c, "baseline", 30)
    feed(c, "canary", 5)
    assert c.state is State.pending
    ok, why = c.promote()
    assert not ok and "unknown, not healthy" in why


def test_a_good_canary_promotes():
    c = canary_mod.start("c1", "v1", "v2", rails=rails())
    feed(c, "baseline", 30)
    feed(c, "canary", 25)
    assert c.state is State.healthy
    ok, _ = c.promote()
    assert ok and c.state is State.promoted


def test_a_bad_error_rate_rolls_back_without_a_human():
    c = canary_mod.start("c1", "v1", "v2", rails=rails())
    feed(c, "baseline", 40)
    for i in range(25):
        c.record("canary", ok=(i % 4 != 0), latency_ms=100, cost_usd=0.001)
    assert c.state is State.rolled_back
    assert "error rate" in c.rollback_reason
    assert c.traffic_pct == 0.0


def test_guard_rails_are_relative_to_the_baseline():
    """4% errors is fine against a 4% baseline, and an emergency against 0.1%."""
    tolerant = canary_mod.start("c1", "v1", "v2", rails=rails())
    for i in range(40):
        tolerant.record("baseline", ok=(i % 10 != 0), latency_ms=100, cost_usd=0.001)
    for i in range(25):
        tolerant.record("canary", ok=(i % 10 != 0), latency_ms=100, cost_usd=0.001)
    assert tolerant.state is State.healthy


def test_a_latency_regression_rolls_back():
    c = canary_mod.start("c1", "v1", "v2", rails=rails())
    feed(c, "baseline", 30, latency=100)
    feed(c, "canary", 25, latency=400)
    assert c.state is State.rolled_back
    assert "latency" in c.rollback_reason


def test_a_cost_regression_rolls_back():
    c = canary_mod.start("c1", "v1", "v2", rails=rails())
    feed(c, "baseline", 30, cost=0.001)
    feed(c, "canary", 25, cost=0.01)
    assert c.state is State.rolled_back
    assert "cost" in c.rollback_reason


def test_a_rolled_back_canary_serves_baseline_and_cannot_promote():
    c = canary_mod.start("c1", "v1", "v2", rails=rails())
    feed(c, "baseline", 30)
    feed(c, "canary", 25, ok=False)
    assert c.route("anything") == "baseline"
    ok, why = c.promote()
    assert not ok and "rolled-back" in why


def test_a_rolled_back_canary_ignores_further_observations():
    c = canary_mod.start("c1", "v1", "v2", rails=rails())
    feed(c, "baseline", 30)
    feed(c, "canary", 25, ok=False)
    before = len(c.canary)
    feed(c, "canary", 5)
    assert len(c.canary) == before


def test_routing_is_stable_per_request():
    """A retry must land in the same arm, or it smears the comparison."""
    c = canary_mod.start("c1", "v1", "v2", traffic_pct=50.0, rails=rails())
    assert c.route("req-abc") == c.route("req-abc")


def test_canary_state_survives_a_restart():
    c = canary_mod.start("c1", "v1", "v2", rails=rails())
    feed(c, "baseline", 30)
    feed(c, "canary", 25, ok=False)
    reloaded = canary_mod.load("c1")
    assert reloaded.state is State.rolled_back
    assert reloaded.rollback_reason == c.rollback_reason
    assert len(reloaded.canary) == len(c.canary)


def test_loading_an_unknown_canary_is_none():
    assert canary_mod.load("nope") is None
