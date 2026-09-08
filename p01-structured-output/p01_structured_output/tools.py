"""Tools with a validated contract on both sides.

Two different failures wear the same costume, and separating them is the whole
point of this module:

- **Bad tool input** — the model produced arguments that don't fit the schema.
  Recoverable. Hand the validation error back as an error `tool_result` and the
  model usually fixes it on the next turn.
- **Bad tool output** — your own function returned something that violates its
  declared contract. Not recoverable by the model, and never the model's fault.
  Raising it into the conversation teaches the model to work around your bug;
  it has to be logged loudly and surfaced as an integration failure instead.

Most agent code validates neither, and then debugging is a guess about which
half broke.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentcore import tracing
from pydantic import BaseModel, ValidationError

log = logging.getLogger("p01.tools")


class ToolContractError(RuntimeError):
    """A tool returned output violating its own declared schema."""


@dataclass
class ToolOutcome:
    ok: bool
    value: BaseModel | None = None
    error: str | None = None
    #: True when the failure is the model's to fix (bad arguments).
    model_recoverable: bool = False

    def as_tool_result(self, tool_use_id: str) -> dict:
        """Render for the Messages API `tool_result` block."""
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "is_error": not self.ok,
            "content": (
                self.value.model_dump_json() if self.ok else f"ValidationError: {self.error}"
            ),
        }


@dataclass
class ValidatedTool:
    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    fn: Callable[..., Any]
    calls: int = 0
    input_failures: list[str] = field(default_factory=list)
    output_failures: list[str] = field(default_factory=list)

    def anthropic_schema(self) -> dict:
        """Tool definition with `strict: true`.

        Strict mode makes the API itself guarantee schema-valid arguments, so
        the input-validation path below becomes a belt-and-braces check rather
        than the primary defence. It still earns its place: strict mode cannot
        express Pydantic's cross-field rules, and it does nothing at all in
        mock mode or behind a proxy.
        """
        schema = self.input_schema.model_json_schema()
        schema["additionalProperties"] = False
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": schema,
        }

    def invoke(self, raw_input: dict | str) -> ToolOutcome:
        self.calls += 1
        with tracing.span(f"tool.{self.name}", kind="tool") as sp:
            if isinstance(raw_input, str):
                try:
                    raw_input = json.loads(raw_input)
                except json.JSONDecodeError as exc:
                    return self._input_failure(sp, f"arguments were not JSON: {exc}")

            try:
                args = self.input_schema.model_validate(raw_input)
            except ValidationError as exc:
                return self._input_failure(sp, _terse(exc))

            try:
                raw_output = self.fn(**args.model_dump())
            except Exception as exc:  # noqa: BLE001 — tool crash, reported as such
                sp.status = "error"
                log.error("tool %s raised: %s", self.name, exc)
                return ToolOutcome(ok=False, error=f"tool raised {type(exc).__name__}: {exc}")

            try:
                value = self.output_schema.model_validate(raw_output)
            except ValidationError as exc:
                # Loud: this is our bug, and the model must not be asked to
                # paper over it.
                detail = _terse(exc)
                self.output_failures.append(detail)
                sp.status = "error"
                sp.attrs["contract_violation"] = detail
                log.error(
                    "TOOL CONTRACT VIOLATION: %s returned output failing %s: %s | raw=%.300s",
                    self.name, self.output_schema.__name__, detail, raw_output,
                )
                raise ToolContractError(
                    f"{self.name} violated its output contract: {detail}"
                ) from exc

            sp.attrs["ok"] = True
            return ToolOutcome(ok=True, value=value)

    def _input_failure(self, sp, detail: str) -> ToolOutcome:
        self.input_failures.append(detail)
        sp.status = "degraded"  # expected and recoverable, not an error
        sp.attrs["input_validation_failed"] = detail
        log.warning("tool %s got invalid arguments: %s", self.name, detail)
        return ToolOutcome(ok=False, error=detail, model_recoverable=True)


def _terse(exc: ValidationError) -> str:
    """Compact Pydantic errors into something a model can act on.

    The full repr is mostly noise; `field: message` is the actionable part, and
    a shorter repair prompt is a cheaper and more reliable one.
    """
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts[:8])


# ---------- the demo tool ----------

_ACCOUNTS = {
    "ACC-1001": {
        "account_id": "ACC-1001",
        "plan": "enterprise",
        "monthly_spend_usd": 48200.0,
        "open_tickets": 3,
        "is_enterprise": True,
    },
    "ACC-2002": {
        "account_id": "ACC-2002",
        "plan": "starter",
        "monthly_spend_usd": 49.0,
        "open_tickets": 1,
        "is_enterprise": False,
    },
}


def _lookup_account(account_id: str) -> dict:
    if account_id not in _ACCOUNTS:
        raise KeyError(f"no such account {account_id}")
    return _ACCOUNTS[account_id]


def build_account_tool() -> ValidatedTool:
    from .schemas import LookupAccountInput, LookupAccountOutput

    return ValidatedTool(
        name="lookup_account",
        description="Look up billing and plan details for an account id like ACC-1001.",
        input_schema=LookupAccountInput,
        output_schema=LookupAccountOutput,
        fn=_lookup_account,
    )


def build_broken_tool() -> ValidatedTool:
    """A tool that violates its own contract — returns spend as a string.

    Kept in the source rather than in a test so the demo can show the
    integration-failure path deliberately. This is the bug class that otherwise
    surfaces three layers downstream as an inexplicable model answer.
    """
    from .schemas import LookupAccountInput, LookupAccountOutput

    def broken(account_id: str) -> dict:
        return {
            "account_id": account_id,
            "plan": "enterprise",
            "monthly_spend_usd": "forty-eight thousand",  # contract says float
            "open_tickets": -2,  # contract says >= 0
            "is_enterprise": True,
        }

    return ValidatedTool(
        name="lookup_account_broken",
        description="Deliberately broken variant used to demonstrate contract enforcement.",
        input_schema=LookupAccountInput,
        output_schema=LookupAccountOutput,
        fn=broken,
    )
