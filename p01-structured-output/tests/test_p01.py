"""P1 tests.

Weighted towards failure modes: the happy path is one test, because a schema
that validates correct output is not what this project is graded on.
"""

from __future__ import annotations

import json

import pytest
from agentcore import mock, store
from p01_structured_output.agent import StructuredAgent, failure_log, failure_stats
from p01_structured_output.schemas import LookupAccountOutput, TicketTriage
from p01_structured_output.tools import (
    ToolContractError,
    build_account_tool,
    build_broken_tool,
)
from pydantic import ValidationError

VALID = {
    "severity": "high",
    "category": "bug",
    "summary": "Password reset emails are not delivered to Outlook addresses.",
    "customer_sentiment": -0.4,
    "affected_users": 250,
    "action_items": [
        {"description": "Check the SMTP relay reputation", "owner_team": "platform",
         "due_within_hours": 8}
    ],
    "requires_human_review": False,
    "refund_amount_usd": None,
}


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("AGENTCORE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AGENTCORE_TRACE_FILE", str(tmp_path / "t.jsonl"))
    mock.PROVIDER.reset()
    yield
    mock.PROVIDER.reset()
    store.reset()


# ---------- extraction ----------


def test_clean_extraction_takes_one_attempt():
    mock.PROVIDER.queue(json.dumps(VALID))
    result = StructuredAgent().extract("ticket text")
    assert result.ok and result.attempts == 1
    assert not result.repaired
    assert result.value.severity.value == "high"


@pytest.mark.parametrize(
    "mutation, expect_in_repair",
    [
        ({"customer_sentiment": -45.0}, "customer_sentiment"),
        ({"affected_users": -3}, "affected_users"),
        ({"action_items": []}, "action_items"),
        ({"severity": "catastrophic"}, "severity"),
        ({"summary": "short"}, "summary"),
    ],
)
def test_each_constraint_triggers_a_targeted_repair(mutation, expect_in_repair):
    """Every constraint must produce a repair prompt naming the field it broke."""
    mock.PROVIDER.queue(json.dumps({**VALID, **mutation}), json.dumps(VALID))
    result = StructuredAgent().extract("ticket")
    assert result.ok and result.repaired
    repair_prompt = mock.PROVIDER.calls[1]["messages"][-1]["content"]
    assert expect_in_repair in repair_prompt


def test_business_rule_violation_is_repaired_too():
    """Type-valid but useless output — the case pure schema checking misses."""
    narrated = {**VALID, "summary": "The ticket says password resets are failing."}
    mock.PROVIDER.queue(json.dumps(narrated), json.dumps(VALID))
    result = StructuredAgent().extract("ticket")
    assert result.ok and result.repaired
    assert not result.value.summary.lower().startswith("the ticket")


def test_extra_fields_are_rejected_not_silently_dropped():
    mock.PROVIDER.queue(json.dumps({**VALID, "invented_field": "hi"}), json.dumps(VALID))
    result = StructuredAgent().extract("ticket")
    assert result.repaired, "an invented field must not pass silently"


def test_prose_wrapped_json_is_recovered_without_a_retry():
    mock.PROVIDER.queue("Here you go:\n" + json.dumps(VALID))
    result = StructuredAgent().extract("ticket")
    assert result.ok and result.attempts == 1


def test_exhaustion_returns_no_partial_object():
    mock.PROVIDER.queue(*["garbage"] * 3)
    result = StructuredAgent(max_attempts=3).extract("ticket")
    assert not result.ok
    assert result.value is None, "a partial object looks like an answer; it must be None"
    assert result.error and "3 attempts" in result.error


def test_escalation_moves_up_the_ladder():
    from agentcore import LLM

    mock.PROVIDER.queue("garbage", "garbage", json.dumps(VALID))
    agent = StructuredAgent(llm=LLM(model="claude-haiku-4-5", project="p01"), escalate=True)
    assert agent.extract("ticket").ok
    models = [c["model"] for c in mock.PROVIDER.calls]
    assert models == ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"]


def test_escalation_is_off_by_default_when_disabled():
    from agentcore import LLM

    mock.PROVIDER.queue("garbage", json.dumps(VALID))
    agent = StructuredAgent(llm=LLM(model="claude-haiku-4-5", project="p01"), escalate=False)
    agent.extract("ticket")
    assert {c["model"] for c in mock.PROVIDER.calls} == {"claude-haiku-4-5"}


# ---------- failure logging ----------


def test_failures_are_persisted_with_enough_detail_to_reproduce():
    mock.PROVIDER.queue(json.dumps({**VALID, "affected_users": -1}), json.dumps(VALID))
    StructuredAgent().extract("ticket")
    rows = failure_log()
    assert rows, "a validation failure that is not recorded cannot be measured"
    assert rows[0]["schema"] == "TicketTriage"
    assert "affected_users" in rows[0]["error"]


def test_failure_stats_aggregate_by_schema():
    mock.PROVIDER.queue(json.dumps({**VALID, "affected_users": -1}), json.dumps(VALID))
    StructuredAgent().extract("ticket")
    stats = failure_stats()
    assert stats["total"] >= 1
    assert stats["by_schema"]["TicketTriage"] >= 1


def test_clean_run_records_nothing():
    mock.PROVIDER.queue(json.dumps(VALID))
    StructuredAgent().extract("ticket")
    assert failure_stats()["total"] == 0


# ---------- tool contracts ----------


def test_valid_tool_call_returns_a_validated_model():
    outcome = build_account_tool().invoke({"account_id": "ACC-1001"})
    assert outcome.ok
    assert isinstance(outcome.value, LookupAccountOutput)
    assert outcome.value.is_enterprise is True


def test_bad_tool_arguments_are_recoverable_by_the_model():
    tool = build_account_tool()
    outcome = tool.invoke({"account_id": "nope"})
    assert not outcome.ok
    assert outcome.model_recoverable, "bad arguments are the model's to fix"
    assert "account_id" in outcome.error
    assert tool.input_failures


def test_bad_tool_arguments_render_as_an_error_tool_result():
    outcome = build_account_tool().invoke({"account_id": "nope"})
    block = outcome.as_tool_result("toolu_123")
    assert block["is_error"] is True
    assert block["tool_use_id"] == "toolu_123"
    assert "ValidationError" in block["content"]


def test_non_json_arguments_are_rejected_cleanly():
    outcome = build_account_tool().invoke("{not json")
    assert not outcome.ok and outcome.model_recoverable


def test_tool_contract_violation_raises_and_is_not_shown_to_the_model():
    """Our bug, not the model's. Passing it through teaches the model to
    work around a broken integration."""
    tool = build_broken_tool()
    with pytest.raises(ToolContractError) as exc:
        tool.invoke({"account_id": "ACC-1001"})
    assert "monthly_spend_usd" in str(exc.value)
    assert tool.output_failures


def test_tool_crash_is_reported_without_raising():
    tool = build_account_tool()
    outcome = tool.invoke({"account_id": "ACC-9999"})  # KeyError inside the tool
    assert not outcome.ok
    assert "KeyError" in outcome.error


def test_strict_schema_forbids_extra_arguments():
    schema = build_account_tool().anthropic_schema()
    assert schema["strict"] is True
    assert schema["input_schema"]["additionalProperties"] is False


# ---------- schema itself ----------


def test_schema_rejects_out_of_range_sentiment():
    with pytest.raises(ValidationError):
        TicketTriage(**{**VALID, "customer_sentiment": 2.0})


def test_schema_requires_at_least_one_action_item():
    with pytest.raises(ValidationError):
        TicketTriage(**{**VALID, "action_items": []})
