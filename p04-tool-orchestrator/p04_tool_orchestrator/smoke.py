"""End-to-end smoke for P4.

The four checks that matter: permission is enforced at invocation rather than
by hiding the menu, one failure does not sink the batch, parallel really is
parallel, and conflicts resolve deterministically.
"""

from __future__ import annotations

import time

from .orchestrator import Orchestrator
from .registry import Caller, PermissionDenied, Registry, Tool
from .tools import default_registry

# `reporting:read` is granted so slow_report actually runs and times out —
# without it the call is denied first and the timeout path is never exercised.
ANALYST = Caller.of("analyst", "billing:read", "crm:read", "analytics:read", "reporting:read")
READONLY = Caller.of("readonly", "crm:read")
ADMIN = Caller.of("admin", "*")


def smoke() -> dict:
    results: dict[str, object] = {}
    registry = default_registry()
    orchestrator = Orchestrator(registry)

    # 1. Capability routing picks the most authoritative matching tool.
    best = registry.route({"billing", "balance"}, ANALYST)
    assert best is not None and best.name == "billing_service", best
    results["routed_to"] = best.name

    # 2. Permission is enforced at invoke, not by filtering the menu. This is
    #    the call a filtered list would have silently allowed.
    try:
        registry.invoke("billing_service", READONLY, account="ACC-1001")
        raise AssertionError("a scope check that only filters the menu is not a check")
    except PermissionDenied:
        results["permission_enforced_at_invoke"] = True

    # 3. Parallel execution with failure isolation: one tool raises, one times
    #    out, one is denied — the rest still return.
    orchestrator.per_tool_timeout = 0.2
    started = time.perf_counter()
    batch = orchestrator.run_batch_sync(
        [
            ("billing_service", {"account": "ACC-1001"}),
            ("crm", {"account": "ACC-1001"}),
            ("usage_analytics", {"account": "ACC-1001"}),
            ("billing_legacy", {"account": "ACC-1001"}),   # raises
            ("slow_report", {"account": "ACC-1001"}),      # times out
            ("apply_credit", {"account": "ACC-1001", "amount": 10}),  # denied
            ("no_such_tool", {}),                          # unknown
        ],
        ANALYST,
    )
    elapsed = (time.perf_counter() - started) * 1000

    assert len(batch.ok) == 3, [r.tool for r in batch.ok]
    assert len(batch.failed) == 4
    assert "apply_credit" in batch.denied
    timed_out = next(r for r in batch.failed if r.tool == "slow_report")
    assert "timed out" in timed_out.error, timed_out.error
    results["survived_partial_failure"] = f"{len(batch.ok)} ok, {len(batch.failed)} failed"

    # slow_report sleeps for 10s and is cut off at 200ms. Run serially the
    # batch could not finish under 10s at all, so this is the parallelism proof.
    assert elapsed < 1000, f"took {elapsed:.0f}ms — that is not parallel"
    assert elapsed >= 150, f"took {elapsed:.0f}ms — the timeout cannot have fired"
    results["batch_ms"] = round(elapsed)

    # 4. Conflicting balances resolve by authority, deterministically.
    conflict_batch = orchestrator.run_batch_sync(
        [("billing_service", {"account": "ACC-1001"}),
         ("billing_cache", {"account": "ACC-1001"})],
        ANALYST,
    )
    balance = next(c for c in conflict_batch.conflicts if c.field == "balance_usd")
    assert balance.winner == "billing_service", balance.winner
    assert conflict_batch.merged["balance_usd"] == 48200.0
    assert not balance.escalate
    results["conflict_resolved_by_authority"] = True

    # 5. Equal authority disagreeing is escalated, not quietly decided.
    tied = Registry()
    for name in ("source_a", "source_b"):
        value = 100.0 if name == "source_a" else 200.0
        tied.register(Tool(
            name=name, description="", capabilities=frozenset({"x"}),
            required_scope="x:read", authority=10,
            fn=lambda v=value: {"amount": v},
        ))
    tied_batch = Orchestrator(tied).run_batch_sync(
        [("source_a", {}), ("source_b", {})], Caller.of("c", "x:read")
    )
    assert tied_batch.needs_escalation
    assert tied_batch.merged["amount"] is None, "must not invent a winner"
    results["equal_authority_escalates"] = True

    # 6. Agreement across tools is not a conflict.
    agreed = Registry()
    for name in ("a", "b"):
        agreed.register(Tool(
            name=name, description="", capabilities=frozenset({"x"}),
            required_scope="x:read", authority=10, fn=lambda: {"region": "EU"},
        ))
    agreed_batch = Orchestrator(agreed).run_batch_sync(
        [("a", {}), ("b", {})], Caller.of("c", "x:read")
    )
    assert agreed_batch.conflicts == []
    assert agreed_batch.merged["region"] == "EU"
    results["agreement_is_not_conflict"] = True

    # 7. The registry is dynamic.
    registry.unregister("billing_cache")
    assert registry.route({"billing", "balance"}, ANALYST).name == "billing_service"
    assert not any(t.name == "billing_cache" for t in registry.find({"billing"}))
    results["registry_is_dynamic"] = True

    return results
