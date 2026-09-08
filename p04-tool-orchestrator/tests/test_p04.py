"""P4 tests."""

from __future__ import annotations

import time

import pytest
from agentcore import store
from p04_tool_orchestrator.orchestrator import Orchestrator, merge
from p04_tool_orchestrator.registry import (
    Caller,
    PermissionDenied,
    Registry,
    Scope,
    Tool,
    ToolResult,
    UnknownTool,
)
from p04_tool_orchestrator.tools import default_registry

ANALYST = Caller.of("analyst", "billing:read", "crm:read", "analytics:read")
READONLY = Caller.of("readonly", "crm:read")
ADMIN = Caller.of("admin", "*")


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCORE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AGENTCORE_TRACE_FILE", str(tmp_path / "t.jsonl"))
    yield
    store.reset()


def tool(name, value, *, authority=100, scope="x:read", caps=("x",), fn=None):
    return Tool(
        name=name, description=name, capabilities=frozenset(caps),
        required_scope=scope, authority=authority,
        fn=fn or (lambda: value),
    )


# ---------- scopes ----------


@pytest.mark.parametrize(
    "held, required, expected",
    [
        ("billing:read", "billing:read", True),
        ("billing:read", "billing:write", False),
        ("billing:*", "billing:write", True),
        ("billing:*", "crm:read", False),
        ("*", "anything:at:all", True),
        ("billing", "billing:read", False),
    ],
)
def test_scope_matching(held, required, expected):
    assert Scope(held).covers(required) is expected


def test_caller_holds_any_of_its_scopes():
    caller = Caller.of("c", "a:read", "b:write")
    assert caller.may("a:read") and caller.may("b:write")
    assert not caller.may("c:read")


# ---------- permission enforcement ----------


def test_permission_is_enforced_at_invoke():
    """Filtering the menu is not access control: the model can name a tool it
    was never shown, and prompt injection can tell it to."""
    with pytest.raises(PermissionDenied, match="billing:read"):
        default_registry().invoke("billing_service", READONLY, account="ACC-1001")


def test_a_hidden_tool_is_still_protected():
    registry = default_registry()
    assert "apply_credit" not in registry.describe(ANALYST), "not on the analyst's menu"
    with pytest.raises(PermissionDenied):
        registry.invoke("apply_credit", ANALYST, account="ACC-1001", amount=1)


def test_wildcard_scope_permits_everything():
    assert default_registry().invoke(
        "apply_credit", ADMIN, account="ACC-1001", amount=50
    ).ok


def test_unknown_tool_raises_distinctly():
    with pytest.raises(UnknownTool):
        default_registry().invoke("nope", ADMIN)


# ---------- capability routing ----------


def test_routing_prefers_the_most_authoritative_tool():
    """A system of record outranks a cache, whatever order they registered in."""
    best = default_registry().route({"billing", "balance"}, ANALYST)
    assert best.name == "billing_service"


def test_routing_requires_every_capability():
    registry = default_registry()
    assert registry.route({"billing", "write"}, ANALYST) is None, "analyst cannot write"
    assert registry.route({"billing", "write"}, ADMIN).name == "apply_credit"


def test_routing_excludes_tools_the_caller_cannot_use():
    names = [t.name for t in default_registry().find({"billing", "balance"}, READONLY)]
    assert names == []


def test_routing_survives_a_tool_being_replaced():
    """The point of capability routing: names change, capabilities don't."""
    registry = default_registry()
    registry.unregister("billing_service")
    registry.register(tool(
        "billing_service_v2", {"balance_usd": 1.0}, authority=5,
        scope="billing:read", caps=("billing", "read", "balance"),
    ))
    assert registry.route({"billing", "balance"}, ANALYST).name == "billing_service_v2"


def test_registry_is_dynamic():
    registry = Registry()
    registry.register(tool("a", 1))
    assert registry.get("a")
    assert registry.unregister("a") is True
    assert registry.unregister("a") is False
    with pytest.raises(UnknownTool):
        registry.get("a")


# ---------- parallel execution ----------


def test_one_failure_does_not_sink_the_batch():
    batch = Orchestrator(default_registry()).run_batch_sync(
        [
            ("billing_service", {"account": "ACC-1001"}),
            ("billing_legacy", {"account": "ACC-1001"}),  # raises
            ("crm", {"account": "ACC-1001"}),
        ],
        ANALYST,
    )
    assert len(batch.ok) == 2
    assert len(batch.failed) == 1
    assert "ConnectionError" in batch.failed[0].error


def test_denied_and_unknown_tools_are_results_not_exceptions():
    batch = Orchestrator(default_registry()).run_batch_sync(
        [
            ("crm", {"account": "ACC-1001"}),
            ("apply_credit", {"account": "ACC-1001", "amount": 1}),
            ("does_not_exist", {}),
        ],
        ANALYST,
    )
    assert len(batch.ok) == 1
    assert batch.denied == ["apply_credit"]
    assert any("no tool named" in (r.error or "") for r in batch.failed)


def test_calls_actually_run_concurrently():
    registry = Registry()
    for i in range(5):
        registry.register(tool(f"slow{i}", None, fn=lambda: time.sleep(0.1) or {"v": 1}))
    started = time.perf_counter()
    Orchestrator(registry, max_concurrency=5).run_batch_sync(
        [(f"slow{i}", {}) for i in range(5)], Caller.of("c", "x:read")
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 0.35, f"{elapsed:.2f}s — five 0.1s calls did not overlap"


def test_concurrency_is_capped():
    """A wide plan must not exhaust downstream connection pools."""
    live = {"now": 0, "peak": 0}

    def track():
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.05)
        live["now"] -= 1
        return {"v": 1}

    registry = Registry()
    for i in range(10):
        registry.register(tool(f"t{i}", None, fn=track))
    Orchestrator(registry, max_concurrency=3).run_batch_sync(
        [(f"t{i}", {}) for i in range(10)], Caller.of("c", "x:read")
    )
    assert live["peak"] <= 3


def test_a_timeout_bounds_wall_clock_time():
    """`wait_for` cannot cancel a blocking call. Without a private executor
    that is never joined, this batch returns in 0.2s and then blocks for
    another 1.8s at shutdown."""
    registry = Registry()
    registry.register(tool("stuck", None, fn=lambda: time.sleep(2) or {"v": 1}))
    registry.register(tool("quick", {"v": 2}))

    orchestrator = Orchestrator(registry, per_tool_timeout=0.2)
    started = time.perf_counter()
    batch = orchestrator.run_batch_sync(
        [("stuck", {}), ("quick", {})], Caller.of("c", "x:read")
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"{elapsed:.2f}s — the abandoned thread was joined"
    assert len(batch.ok) == 1
    assert "timed out" in batch.failed[0].error
    orchestrator.close()


def test_abandoned_workers_are_counted():
    """A rising count is the signal a dependency is sick, and it is invisible
    if you only look at timeouts."""
    registry = Registry()
    registry.register(tool("stuck", None, fn=lambda: time.sleep(2)))
    orchestrator = Orchestrator(registry, per_tool_timeout=0.1)
    orchestrator.run_batch_sync([("stuck", {})], Caller.of("c", "x:read"))
    assert orchestrator.abandoned == 1
    assert "abandoned" in orchestrator.run_batch_sync(
        [("stuck", {})], Caller.of("c", "x:read")
    ).failed[0].error
    assert orchestrator.abandoned == 2
    orchestrator.close()


def test_an_empty_batch_is_fine():
    batch = Orchestrator(default_registry()).run_batch_sync([], ANALYST)
    assert batch.results == [] and batch.merged == {}


# ---------- conflict resolution ----------


def test_authority_decides_a_conflict():
    merged, conflicts = merge([
        ToolResult("cache", True, {"balance": 12000.0}, authority=50),
        ToolResult("system_of_record", True, {"balance": 48200.0}, authority=10),
    ])
    assert merged["balance"] == 48200.0
    assert conflicts[0].winner == "system_of_record"
    assert not conflicts[0].escalate


def test_resolution_is_deterministic_regardless_of_order():
    """Delegating this to the model makes the answer depend on prompt phrasing."""
    a = ToolResult("cache", True, {"balance": 12000.0}, authority=50)
    b = ToolResult("sor", True, {"balance": 48200.0}, authority=10)
    assert merge([a, b])[0] == merge([b, a])[0]


def test_agreement_is_not_a_conflict():
    merged, conflicts = merge([
        ToolResult("a", True, {"region": "EU"}, authority=10),
        ToolResult("b", True, {"region": "EU"}, authority=50),
    ])
    assert conflicts == [] and merged["region"] == "EU"


def test_equal_authority_disagreement_escalates():
    """Quietly picking one hides a data integrity problem."""
    merged, conflicts = merge([
        ToolResult("a", True, {"amount": 100.0}, authority=10),
        ToolResult("b", True, {"amount": 200.0}, authority=10),
    ])
    assert conflicts[0].escalate
    assert conflicts[0].winner is None
    assert merged["amount"] is None, "must not invent a winner"


def test_non_conflicting_fields_merge_cleanly():
    merged, conflicts = merge([
        ToolResult("billing", True, {"balance": 1.0}, authority=10),
        ToolResult("crm", True, {"plan": "enterprise"}, authority=10),
    ])
    assert merged == {"balance": 1.0, "plan": "enterprise"}
    assert conflicts == []


def test_scalar_results_do_not_break_the_merge():
    merged, _ = merge([
        ToolResult("a", True, "just a string", authority=10),
        ToolResult("b", True, {"ok": True}, authority=10),
    ])
    assert merged == {"ok": True}


def test_unhashable_values_are_compared_not_crashed_on():
    merged, conflicts = merge([
        ToolResult("a", True, {"items": [1, 2]}, authority=10),
        ToolResult("b", True, {"items": [1, 2]}, authority=50),
    ])
    assert conflicts == [] and merged["items"] == [1, 2]


def test_conflict_records_every_value_for_the_audit():
    _, conflicts = merge([
        ToolResult("cache", True, {"balance": 12000.0}, authority=50),
        ToolResult("sor", True, {"balance": 48200.0}, authority=10),
    ])
    assert set(conflicts[0].values) == {"cache", "sor"}
    assert "authority" in conflicts[0].resolution


def test_end_to_end_conflict_through_the_orchestrator():
    batch = Orchestrator(default_registry()).run_batch_sync(
        [("billing_service", {"account": "ACC-1001"}),
         ("billing_cache", {"account": "ACC-1001"})],
        ANALYST,
    )
    assert batch.merged["balance_usd"] == 48200.0
    assert any(c.field == "balance_usd" for c in batch.conflicts)
