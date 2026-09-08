"""The demo toolset.

Deliberately includes two tools that answer the same question differently —
a live billing service and a stale cache — because conflict resolution cannot
be demonstrated with tools that always agree.
"""

from __future__ import annotations

import time

from .registry import Registry, Tool

_ACCOUNTS = {
    "ACC-1001": {"account": "ACC-1001", "name": "Wexler Industries",
                 "plan": "enterprise", "region": "EU"},
    "ACC-2002": {"account": "ACC-2002", "name": "Calder Foods",
                 "plan": "starter", "region": "US"},
}


def _billing_service(account: str) -> dict:
    """System of record. Slow, authoritative."""
    time.sleep(0.02)
    if account not in _ACCOUNTS:
        raise KeyError(f"unknown account {account}")
    return {"account": account, "balance_usd": 48200.0, "currency": "USD"}


def _billing_cache(account: str) -> dict:
    """Fast, and sometimes stale — which is the point."""
    return {"account": account, "balance_usd": 12000.0, "currency": "USD"}


def _crm(account: str) -> dict:
    time.sleep(0.02)
    if account not in _ACCOUNTS:
        raise KeyError(f"unknown account {account}")
    return dict(_ACCOUNTS[account])


def _usage(account: str) -> dict:
    time.sleep(0.02)
    return {"account": account, "api_calls_30d": 1_284_000, "seats_active": 42}


def _partner_ledger(account: str) -> dict:
    """A third party that also claims to know the balance, with equal
    authority to another third party — used to demonstrate escalation."""
    return {"account": account, "credit_rating": "A-"}


def _flaky(account: str) -> dict:
    raise ConnectionError("billing-legacy is unreachable")


def _slow(account: str) -> dict:
    time.sleep(10)
    return {"account": account, "never": "returned"}


def default_registry() -> Registry:
    return (
        Registry()
        .register(Tool(
            name="billing_service",
            description="Authoritative balance from the billing system of record.",
            capabilities=frozenset({"billing", "read", "balance"}),
            required_scope="billing:read",
            fn=_billing_service, typical_ms=20, authority=10,
        ))
        .register(Tool(
            name="billing_cache",
            description="Fast cached balance. May be stale.",
            capabilities=frozenset({"billing", "read", "balance"}),
            required_scope="billing:read",
            fn=_billing_cache, typical_ms=1, authority=50,
        ))
        .register(Tool(
            name="crm",
            description="Account name, plan and region.",
            capabilities=frozenset({"crm", "read", "account"}),
            required_scope="crm:read",
            fn=_crm, typical_ms=20, authority=10,
        ))
        .register(Tool(
            name="usage_analytics",
            description="API call volume and active seats over 30 days.",
            capabilities=frozenset({"analytics", "read", "usage"}),
            required_scope="analytics:read",
            fn=_usage, typical_ms=20, authority=20,
        ))
        .register(Tool(
            name="partner_ledger",
            description="Third-party credit rating.",
            capabilities=frozenset({"billing", "read", "credit"}),
            required_scope="partner:read",
            fn=_partner_ledger, typical_ms=5, authority=90,
        ))
        .register(Tool(
            name="billing_legacy",
            description="Legacy billing system (frequently unreachable).",
            capabilities=frozenset({"billing", "read", "balance"}),
            required_scope="billing:read",
            fn=_flaky, typical_ms=30, authority=70,
        ))
        .register(Tool(
            name="slow_report",
            description="Generates a report. Very slow — used to show timeouts.",
            capabilities=frozenset({"reporting", "read"}),
            required_scope="reporting:read",
            fn=_slow, typical_ms=10_000, authority=80,
        ))
        .register(Tool(
            name="apply_credit",
            description="Apply an account credit. Writes.",
            capabilities=frozenset({"billing", "write", "credit"}),
            required_scope="billing:write",
            fn=lambda account, amount: {"account": account, "credited": amount},
            typical_ms=40, authority=10,
        ))
    )
