"""The single entry point for every model call in this repo.

Nothing calls the Anthropic SDK directly. Routing everything through one class
is what makes three of the eleven projects possible at all: P7 can reroute a
call because it owns model selection here, P11 has traces from every project
because every call emits one here, and mock mode works everywhere because the
switch is here.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from . import mock, tracing
from .cost import Budget, Usage, estimate_cost
from .models import DEFAULT_MODEL, spec
from .retry import ParseError, retry_call

log = logging.getLogger("agentcore.llm")

M = TypeVar("M", bound=BaseModel)

#: Conservative pre-flight estimate for the budget check. Real usage replaces
#: it after the call; this only has to be good enough to refuse an obviously
#: unaffordable request before spending anything.
_PREFLIGHT_OUTPUT_TOKENS = 1_000


@dataclass
class Reply:
    text: str
    model: str
    usage: Usage
    stop_reason: str = "end_turn"
    tool_calls: list[dict] = field(default_factory=list)
    attempts: int = 1
    #: Validation errors from earlier attempts, oldest first. Non-empty on a
    #: successful `parse` means the repair loop did work worth reporting.
    failures: list[str] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


@dataclass
class LLM:
    """A budgeted, traced, retrying model client.

    One instance per task, not per process — the budget and the usage log are
    per-task state that P7 and P11 both read.
    """

    model: str = DEFAULT_MODEL
    project: str | None = None
    budget: Budget = field(default_factory=Budget)
    effort: str | None = None
    max_tokens: int = 8_000
    thinking: bool = True
    temperature_unsupported: bool = True  # 4.6+ models reject sampling params
    usages: list[Usage] = field(default_factory=list)
    _client: Any = None

    # ---------- provider plumbing ----------

    def _anthropic(self):
        if self._client is None:
            import anthropic  # imported lazily so mock mode needs no SDK

            self._client = anthropic.Anthropic()
        return self._client

    def _request_kwargs(self, model: str) -> dict:
        """Per-model request shape.

        Haiku 4.5 and the 5-series diverge on both thinking and effort, and
        getting it wrong is a 400 rather than a degraded response — so the
        capability flags live in the registry and are applied here once.
        """
        s = spec(model)
        kwargs: dict[str, Any] = {"max_tokens": min(self.max_tokens, s.max_output)}
        if self.thinking:
            if s.adaptive_thinking:
                kwargs["thinking"] = {"type": "adaptive"}
            else:
                budget = max(1024, min(4096, kwargs["max_tokens"] - 1024))
                if budget >= 1024:
                    kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if self.effort and s.supports_effort:
            kwargs["output_config"] = {"effort": self.effort}
        return kwargs

    # ---------- public API ----------

    def complete(
        self,
        prompt: str | list,
        *,
        system: str | None = None,
        model: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        name: str = "llm.complete",
    ) -> Reply:
        """One text completion, traced and charged."""
        model = model or self.model
        messages = (
            [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
        )
        return self._call(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            schema=None,
            max_tokens=max_tokens,
            name=name,
        )

    def parse(
        self,
        prompt: str | list,
        schema: type[M],
        *,
        system: str | None = None,
        model: str | None = None,
        max_attempts: int = 3,
        escalate: bool = False,
        name: str = "llm.parse",
    ) -> tuple[M, Reply]:
        """A completion validated against a Pydantic model.

        On a validation failure the error text is fed back as a repair turn —
        a bare retry re-rolls the same dice, while showing the model exactly
        which field it got wrong usually fixes it on the next attempt. This is
        the mechanism P1 is built to demonstrate; every other project that
        needs structured data inherits it.
        """
        model = model or self.model
        messages = (
            [{"role": "user", "content": prompt}] if isinstance(prompt, str) else list(prompt)
        )
        system = _schema_system_prompt(system, schema)
        failures: list[str] = []

        with tracing.span(name, kind="llm", schema=schema.__name__) as sp:
            for attempt in range(1, max_attempts + 1):
                reply = self._call(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=None,
                    schema=schema,
                    max_tokens=None,
                    name=f"{name}.attempt{attempt}",
                )
                try:
                    parsed = schema.model_validate_json(_extract_json(reply.text))
                except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                    detail = str(exc)[:800]
                    failures.append(detail)
                    # Logged with the offending output — a validation failure you
                    # cannot reproduce from the log is not actionable.
                    log.warning(
                        "schema validation failed (%s, attempt %d/%d): %s | raw=%.300s",
                        schema.__name__, attempt, max_attempts, detail, reply.text,
                    )
                    if attempt == max_attempts:
                        sp.status = "error"
                        sp.attrs["failures"] = failures
                        raise ParseError(
                            f"{schema.__name__} validation failed after "
                            f"{max_attempts} attempts",
                            raw_output=reply.text,
                            attempts=attempt,
                        ) from exc
                    messages = messages + [
                        {"role": "assistant", "content": reply.text},
                        {"role": "user", "content": _repair_prompt(detail, schema)},
                    ]
                    if escalate:
                        from .models import next_model_up

                        if (up := next_model_up(model)) is not None:
                            log.info("escalating %s -> %s after parse failure", model, up)
                            model = up
                    continue

                reply.attempts = attempt
                reply.failures = list(failures)
                sp.attrs.update(attempts=attempt, failures=failures, model=model)
                return parsed, reply

        raise AssertionError("unreachable")

    # ---------- internals ----------

    def _call(
        self,
        *,
        model: str,
        system: str | None,
        messages: list,
        tools: list[dict] | None,
        schema: Any,
        max_tokens: int | None,
        name: str,
    ) -> Reply:
        self.budget.check(estimate_cost(model, _rough_tokens(messages, system), _PREFLIGHT_OUTPUT_TOKENS))

        with tracing.span(name, kind="llm", model=model) as sp:
            if mock.is_mock_mode():
                reply = self._call_mock(model, system, messages, schema)
            else:
                reply = retry_call(
                    lambda: self._call_api(model, system, messages, tools, max_tokens)
                )
            self.usages.append(reply.usage)
            self.budget.charge(reply.usage)
            sp.model = model
            sp.input_tokens = reply.usage.input_tokens
            sp.output_tokens = reply.usage.output_tokens
            sp.cost_usd = round(reply.usage.cost_usd, 6)
            sp.attrs["stop_reason"] = reply.stop_reason
            if reply.refused:
                sp.status = "degraded"
            return reply

    def _call_mock(self, model, system, messages, schema) -> Reply:
        resp = mock.PROVIDER.complete(
            model=model, system=system, messages=messages, schema=schema
        )
        return Reply(
            text=resp.text,
            model=model,
            usage=Usage(
                model=model,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
            ),
            stop_reason=resp.stop_reason,
            tool_calls=resp.tool_calls,
        )

    def _call_api(self, model, system, messages, tools, max_tokens) -> Reply:
        client = self._anthropic()
        kwargs = self._request_kwargs(model)
        if max_tokens:
            kwargs["max_tokens"] = min(max_tokens, spec(model).max_output)
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        resp = client.messages.create(model=model, messages=messages, **kwargs)

        text = "".join(b.text for b in resp.content if b.type == "text")
        tool_calls = [
            {"id": b.id, "name": b.name, "input": b.input}
            for b in resp.content
            if b.type == "tool_use"
        ]
        reply = Reply(
            text=text,
            model=model,
            usage=Usage.from_response(model, resp.usage),
            stop_reason=resp.stop_reason or "end_turn",
            tool_calls=tool_calls,
        )
        if mock.is_recording():
            mock.PROVIDER.record(
                mock.request_key(model, system, messages),
                {
                    "text": text,
                    "input_tokens": reply.usage.input_tokens,
                    "output_tokens": reply.usage.output_tokens,
                    "stop_reason": reply.stop_reason,
                    "tool_calls": tool_calls,
                },
            )
        return reply


# ---------- helpers ----------


def _schema_system_prompt(system: str | None, schema: type[BaseModel]) -> str:
    instruction = (
        "Respond with a single JSON object matching this schema exactly. "
        "Emit no prose, no markdown fences, no commentary.\n\n"
        f"{json.dumps(schema.model_json_schema(), indent=2)}"
    )
    return f"{system}\n\n{instruction}" if system else instruction


def _repair_prompt(error: str, schema: type[BaseModel]) -> str:
    return (
        f"That response failed validation against {schema.__name__}:\n\n{error}\n\n"
        "Return corrected JSON only. Fix the named fields; change nothing else."
    )


def _extract_json(text: str) -> str:
    """Recover JSON from a response that wrapped it in prose or fences.

    Being lenient here is deliberate: an unparseable-but-recoverable response
    should not burn a retry, while a genuinely malformed one still fails
    validation downstream and gets the repair turn it needs.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t[3:]
        t = t[4:] if t.lower().startswith("json") else t
        t = t.strip()
    if t.startswith(("{", "[")):
        return t
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = t.find(opener), t.rfind(closer)
        if start != -1 and end > start:
            return t[start : end + 1]
    return t


def _rough_tokens(messages: list, system: str | None) -> int:
    """~4 chars per token. Only used for the pre-flight budget check; real
    counts come back with the response."""
    chars = len(system or "")
    for m in messages:
        c = m.get("content")
        chars += len(c) if isinstance(c, str) else len(json.dumps(c, default=str))
    return chars // 4


def default_llm(project: str, **kwargs) -> LLM:
    """Construct an LLM configured from the environment.

    `AGENT_MODEL` and `AGENT_EFFORT` let the demo switch tiers without edits.
    """
    return LLM(
        model=os.environ.get("AGENT_MODEL", DEFAULT_MODEL),
        effort=os.environ.get("AGENT_EFFORT") or None,
        project=project,
        **kwargs,
    )
