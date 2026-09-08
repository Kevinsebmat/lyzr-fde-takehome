"""Tool registry with capabilities and permission scoping.

Two rules the design turns on:

**Permission is enforced at invocation, not by filtering the menu.** Hiding a
tool from the list shown to the model is not access control — the model can
name a tool it was never shown, prompt injection can tell it to, and a retried
plan from a different scope can reference one. The list is a *hint*; the check
at `invoke` is the control. Anything else is security by suggestion.

**Capabilities, not names, are how work is routed.** Asking for "the tool
called `get_invoice_v2`" couples the planner to today's tool names. Asking for
"something that can read billing" survives a tool being renamed, replaced, or
having a second implementation added — which is what a dynamic registry is for.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from agentcore import tracing

log = logging.getLogger("p04.registry")


class PermissionDenied(RuntimeError):
    """A caller invoked a tool outside its granted scopes."""


class UnknownTool(KeyError):
    pass


@dataclass(frozen=True)
class Scope:
    """A permission a caller may hold, e.g. `billing:read`."""

    name: str

    def covers(self, required: str) -> bool:
        """`billing:*` covers `billing:read`; `*` covers everything."""
        if self.name == "*":
            return True
        if self.name.endswith(":*"):
            return required.startswith(self.name[:-1])
        return self.name == required


@dataclass
class Tool:
    name: str
    description: str
    #: What this tool can do, e.g. {"billing", "read"}. Routing matches on these.
    capabilities: frozenset[str]
    #: Scope a caller must hold to invoke it.
    required_scope: str
    fn: Callable[..., object]
    #: Rough latency, used to order parallel batches sensibly.
    typical_ms: int = 50
    #: Lower number wins a conflict. A system of record outranks a cache.
    authority: int = 100
    calls: int = 0
    failures: int = 0


@dataclass
class ToolResult:
    tool: str
    ok: bool
    value: object = None
    error: str | None = None
    duration_ms: float = 0.0
    authority: int = 100

    def as_dict(self) -> dict:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "value": self.value,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "authority": self.authority,
        }


@dataclass
class Caller:
    """Who is asking, and what they are allowed to do."""

    name: str
    scopes: tuple[Scope, ...] = ()

    def may(self, required_scope: str) -> bool:
        return any(s.covers(required_scope) for s in self.scopes)

    @classmethod
    def of(cls, name: str, *scopes: str) -> Caller:
        return cls(name=name, scopes=tuple(Scope(s) for s in scopes))


@dataclass
class Registry:
    """Tools can be registered and removed at runtime — hence 'dynamic'."""

    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> Registry:
        if tool.name in self.tools:
            log.info("replacing existing tool %s", tool.name)
        self.tools[tool.name] = tool
        return self

    def unregister(self, name: str) -> bool:
        return self.tools.pop(name, None) is not None

    def get(self, name: str) -> Tool:
        if name not in self.tools:
            raise UnknownTool(name)
        return self.tools[name]

    # ---------- routing ----------

    def find(self, capabilities: set[str], caller: Caller | None = None) -> list[Tool]:
        """Tools covering every requested capability, best authority first.

        When `caller` is given, tools they cannot use are excluded — a useful
        hint for planning. It is only a hint: `invoke` re-checks.
        """
        matches = [
            t for t in self.tools.values() if capabilities.issubset(t.capabilities)
        ]
        if caller is not None:
            matches = [t for t in matches if caller.may(t.required_scope)]
        return sorted(matches, key=lambda t: (t.authority, t.typical_ms))

    def route(self, capabilities: set[str], caller: Caller) -> Tool | None:
        """The single best tool for a capability set, or None."""
        found = self.find(capabilities, caller)
        return found[0] if found else None

    def describe(self, caller: Caller | None = None) -> str:
        tools = self.tools.values()
        if caller is not None:
            tools = [t for t in tools if caller.may(t.required_scope)]
        return "\n".join(
            f"- {t.name} [{', '.join(sorted(t.capabilities))}]: {t.description}"
            for t in sorted(tools, key=lambda t: t.name)
        )

    # ---------- invocation ----------

    def invoke(self, name: str, caller: Caller, **kwargs) -> ToolResult:
        """Run a tool. This is where permission is actually enforced."""
        tool = self.get(name)

        if not caller.may(tool.required_scope):
            # Deliberately not a filtered-out no-op: a denied call must be
            # visible in the trace and in the caller's result, or an
            # over-broad plan looks identical to a correct one.
            log.warning(
                "permission denied: %s lacks %s for %s",
                caller.name, tool.required_scope, name,
            )
            with tracing.span(f"tool.{name}", kind="tool") as sp:
                sp.status = "error"
                sp.attrs.update(denied=True, required_scope=tool.required_scope)
            raise PermissionDenied(
                f"{caller.name} lacks scope {tool.required_scope!r} for tool {name!r}"
            )

        tool.calls += 1
        started = time.perf_counter()
        with tracing.span(f"tool.{name}", kind="tool", caller=caller.name) as sp:
            try:
                value = tool.fn(**kwargs)
            except Exception as exc:  # noqa: BLE001
                tool.failures += 1
                sp.status = "error"
                duration = (time.perf_counter() - started) * 1000
                log.warning("tool %s failed: %s", name, exc)
                return ToolResult(
                    tool=name, ok=False, error=f"{type(exc).__name__}: {exc}",
                    duration_ms=duration, authority=tool.authority,
                )
            duration = (time.perf_counter() - started) * 1000
            sp.attrs["duration_ms"] = round(duration, 2)
            return ToolResult(
                tool=name, ok=True, value=value, duration_ms=duration,
                authority=tool.authority,
            )

    def stats(self) -> dict:
        return {
            "tools": len(self.tools),
            "calls": sum(t.calls for t in self.tools.values()),
            "failures": sum(t.failures for t in self.tools.values()),
        }
