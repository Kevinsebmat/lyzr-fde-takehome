"""Tools for the ReAct loop.

Includes tools that fail, return nothing, and return the same thing forever —
because an agent that only ever sees successful tool calls never has to prove
it can terminate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from agentcore import tracing

log = logging.getLogger("p03.tools")


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[[str], str]
    calls: int = 0

    def __call__(self, arg: str) -> str:
        self.calls += 1
        with tracing.span(f"tool.{self.name}", kind="tool", arg=arg[:100]) as sp:
            try:
                result = self.fn(arg)
            except Exception as exc:  # noqa: BLE001
                # A tool failure is an observation, not a crash. The agent has
                # to be able to read it, reason about it, and try something
                # else — that is the whole point of the reflect step.
                sp.status = "error"
                log.warning("tool %s failed: %s", self.name, exc)
                return f"ERROR: {type(exc).__name__}: {exc}"
            sp.attrs["chars"] = len(result)
            return result


@dataclass
class Registry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, tool: Tool) -> Registry:
        self.tools[tool.name] = tool
        return self

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def describe(self) -> str:
        return "\n".join(f"- {t.name}: {t.description}" for t in self.tools.values())

    @property
    def names(self) -> list[str]:
        return list(self.tools)

    def total_calls(self) -> int:
        return sum(t.calls for t in self.tools.values())


# ---------- the demo toolset ----------

_ORDERS = {
    "ORD-4417": {"customer": "Wexler Industries", "total": 12480.00, "status": "shipped",
                 "region": "EU", "items": 3},
    "ORD-4418": {"customer": "Calder Foods", "total": 890.50, "status": "processing",
                 "region": "US", "items": 1},
    "ORD-4419": {"customer": "Wexler Industries", "total": 3100.00, "status": "cancelled",
                 "region": "EU", "items": 2},
}

_POLICY = {
    "refund": "Orders under $1,000 refund automatically. Orders of $1,000 or more "
              "need manager approval. Cancelled orders are never refunded.",
    "shipping": "EU orders ship from Rotterdam in 2-4 days. US orders ship from "
                "Columbus in 1-3 days.",
    "escalation": "Any single order above $10,000 escalates to the enterprise desk.",
}


def _lookup_order(order_id: str) -> str:
    order = _ORDERS.get(order_id.strip().upper())
    if not order:
        return f"No order found with id {order_id!r}. Known ids: {', '.join(_ORDERS)}."
    return "; ".join(f"{k}={v}" for k, v in order.items())


def _policy(topic: str) -> str:
    key = topic.strip().lower()
    for name, text in _POLICY.items():
        if name in key:
            return text
    return f"No policy found for {topic!r}. Topics: {', '.join(_POLICY)}."


def _calculator(expression: str) -> str:
    """Arithmetic only. `eval` on model-supplied text is a remote code execution
    hole; the character allowlist is the control that closes it."""
    allowed = set("0123456789+-*/(). ")
    if not expression or set(expression) - allowed:
        return "ERROR: only digits and + - * / ( ) . are permitted"
    try:
        return str(round(eval(expression, {"__builtins__": {}}, {}), 4))  # noqa: S307
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"


def _always_empty(_: str) -> str:
    """Returns nothing useful, forever.

    Exists so the no-progress path is exercised by a real tool rather than a
    mock: an agent that keeps calling this must notice and stop.
    """
    return "No results."


def _always_fails(_: str) -> str:
    raise ConnectionError("upstream service unavailable")


def default_registry() -> Registry:
    return (
        Registry()
        .add(Tool("lookup_order", "Look up an order by id, e.g. ORD-4417.", _lookup_order))
        .add(Tool("policy", "Read policy on a topic: refund, shipping, escalation.", _policy))
        .add(Tool("calculator", "Evaluate arithmetic, e.g. 12480 * 0.15.", _calculator))
        .add(Tool("search_archive", "Search the historical archive.", _always_empty))
        .add(Tool("legacy_crm", "Query the legacy CRM (frequently down).", _always_fails))
    )
