"""Tests for the shared core.

These matter more than any single project's tests: eleven projects inherit
this behaviour, so a bug here is eleven bugs.
"""

from __future__ import annotations

import json

import pytest
from agentcore import (
    LLM,
    Budget,
    BudgetExceeded,
    ParseError,
    TransientError,
    Usage,
    baseline_comparison,
    mock,
    next_model_up,
    retry_call,
    spec,
    store,
    tracing,
)
from agentcore.mock import MockResponse
from pydantic import BaseModel, Field


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Every test gets its own db, trace file and clean mock provider."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("AGENTCORE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("AGENTCORE_TRACE_FILE", str(tmp_path / "traces.jsonl"))
    monkeypatch.setenv("AGENTCORE_FIXTURES", str(tmp_path / "fixtures"))
    mock.PROVIDER.reset()
    yield
    mock.PROVIDER.reset()
    store.reset()


class Contact(BaseModel):
    name: str
    email: str
    score: int = Field(ge=0, le=100)


# ---------- cost ----------


def test_cost_uses_registry_pricing():
    # Opus 5 at $5/$25 per Mtok: 1M in + 1M out = $30.
    u = Usage(model="claude-opus-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert u.cost_usd == pytest.approx(30.0)


def test_cache_reads_are_a_tenth_of_input():
    read = Usage(model="claude-opus-5", cache_read_tokens=1_000_000).cost_usd
    assert read == pytest.approx(0.5)  # 10% of $5


def test_budget_refuses_before_spending():
    b = Budget(limit_usd=0.01)
    b.charge(Usage(model="claude-opus-5", input_tokens=1000, output_tokens=1000))
    with pytest.raises(BudgetExceeded) as exc:
        b.check(projected_usd=1.0)
    # The exception has to carry the numbers, or a caller cannot degrade
    # gracefully and explain itself.
    assert exc.value.limit_usd == 0.01
    assert exc.value.spent_usd > 0


def test_unlimited_budget_never_raises():
    Budget().check(projected_usd=10_000.0)


def test_baseline_comparison_reports_savings():
    usages = [Usage(model="claude-haiku-4-5", input_tokens=10_000, output_tokens=1_000)] * 5
    result = baseline_comparison(usages, baseline_model="claude-opus-5")
    assert result["actual_usd"] < result["baseline_usd"]
    assert 0 < result["saved_pct"] < 100
    assert result["calls"] == 5


def test_ladder_escalates_then_stops():
    assert next_model_up("claude-haiku-4-5") == "claude-sonnet-5"
    assert next_model_up("claude-sonnet-5") == "claude-opus-5"
    assert next_model_up("claude-opus-5") is None


def test_unknown_model_names_the_known_ones():
    with pytest.raises(ValueError, match="claude-opus-5"):
        spec("gpt-4")


# ---------- retry ----------


def test_retry_recovers_from_transient():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("rate limited")
        return "ok"

    assert retry_call(flaky, attempts=3, sleep=lambda _: None) == "ok"
    assert calls["n"] == 3


def test_retry_does_not_retry_fatal():
    calls = {"n": 0}

    def bad_request():
        calls["n"] += 1
        raise ValueError("malformed")

    with pytest.raises(ValueError):
        retry_call(bad_request, attempts=3, sleep=lambda _: None)
    assert calls["n"] == 1, "a non-transient error must not be retried"


def test_retry_exhausts_and_reraises():
    with pytest.raises(TransientError):
        retry_call(lambda: (_ for _ in ()).throw(TransientError("down")),
                   attempts=2, sleep=lambda _: None)


# ---------- structured output / repair loop ----------


def test_parse_returns_validated_model():
    mock.PROVIDER.queue(json.dumps({"name": "Jane", "email": "j@co.com", "score": 90}))
    llm = LLM(project="test")
    parsed, reply = llm.parse("extract", Contact)
    assert isinstance(parsed, Contact)
    assert parsed.name == "Jane"
    assert reply.attempts == 1


def test_parse_repairs_malformed_output():
    """The core failure mode: first response is not valid JSON at all."""
    mock.PROVIDER.queue(
        "Sure! Here's the contact: name is Jane.",  # prose, unparseable
        json.dumps({"name": "Jane", "email": "j@co.com", "score": 90}),
    )
    parsed, reply = LLM(project="test").parse("extract", Contact)
    assert parsed.email == "j@co.com"
    assert reply.attempts == 2, "should have taken a repair turn"


def test_parse_repairs_schema_violation():
    """Valid JSON, invalid against the schema — score out of range."""
    mock.PROVIDER.queue(
        json.dumps({"name": "Jane", "email": "j@co.com", "score": 5000}),
        json.dumps({"name": "Jane", "email": "j@co.com", "score": 50}),
    )
    parsed, _ = LLM(project="test").parse("extract", Contact)
    assert parsed.score == 50


def test_parse_gives_up_with_the_raw_output_attached():
    mock.PROVIDER.queue(*["not json"] * 3)
    with pytest.raises(ParseError) as exc:
        LLM(project="test").parse("extract", Contact, max_attempts=3)
    # Without the offending output the log entry is not actionable.
    assert exc.value.raw_output == "not json"
    assert exc.value.attempts == 3


def test_parse_strips_markdown_fences():
    fenced = "```json\n" + json.dumps({"name": "A", "email": "a@b.c", "score": 1}) + "\n```"
    mock.PROVIDER.queue(fenced)
    parsed, reply = LLM(project="test").parse("extract", Contact)
    assert parsed.name == "A"
    assert reply.attempts == 1, "fence recovery must not burn a repair turn"


def test_repair_turn_shows_the_model_its_error():
    mock.PROVIDER.queue(
        json.dumps({"name": "Jane", "email": "j@co.com", "score": 5000}),
        json.dumps({"name": "Jane", "email": "j@co.com", "score": 50}),
    )
    LLM(project="test").parse("extract", Contact)
    repair = mock.PROVIDER.calls[1]["messages"][-1]["content"]
    assert "score" in repair, "the repair prompt must name the failing field"


def test_parse_escalates_model_when_asked():
    mock.PROVIDER.queue("not json", json.dumps({"name": "A", "email": "a@b.c", "score": 1}))
    LLM(model="claude-haiku-4-5", project="test").parse("x", Contact, escalate=True)
    assert mock.PROVIDER.calls[0]["model"] == "claude-haiku-4-5"
    assert mock.PROVIDER.calls[1]["model"] == "claude-sonnet-5"


def test_mock_synthesizes_schema_shaped_output_on_a_fresh_clone():
    """No cassette, nothing queued: smoke must still pass."""
    parsed, _ = LLM(project="test").parse("extract", Contact)
    assert isinstance(parsed, Contact)


# ---------- budget enforcement through the client ----------


def test_llm_charges_the_budget():
    mock.PROVIDER.queue(MockResponse(text="hi", input_tokens=1000, output_tokens=500))
    llm = LLM(project="test", budget=Budget(limit_usd=1.0))
    llm.complete("hello")
    assert llm.budget.spent_usd > 0
    assert llm.budget.calls == 1


def test_llm_stops_at_the_budget_ceiling():
    llm = LLM(project="test", budget=Budget(limit_usd=0.0001))
    llm.budget.charge(Usage(model="claude-opus-5", input_tokens=100_000, output_tokens=10_000))
    with pytest.raises(BudgetExceeded):
        llm.complete("this call must never be made")


# ---------- tracing ----------


def test_every_call_emits_a_costed_span():
    mock.PROVIDER.queue("hello")
    with tracing.run("test-project"):
        LLM(project="test").complete("hi")
    spans = tracing.read_spans()
    llm_spans = [s for s in spans if s["kind"] == "llm"]
    assert llm_spans, "an LLM call with no trace is invisible to P11"
    assert llm_spans[0]["cost_usd"] is not None
    assert llm_spans[0]["project"] == "test-project"


def test_spans_share_a_run_id_and_nest():
    with tracing.run("proj") as run_id:
        with tracing.span("outer"):
            with tracing.span("inner"):
                pass
    spans = {s["name"]: s for s in tracing.read_spans()}
    assert all(s["run_id"] == run_id for s in spans.values())
    assert spans["inner"]["parent_span_id"] == spans["outer"]["span_id"]


def test_errors_are_recorded_then_reraised():
    with pytest.raises(ValueError):
        with tracing.run("proj"):
            with tracing.span("failing"):
                raise ValueError("boom")
    failing = next(s for s in tracing.read_spans() if s["name"] == "failing")
    assert failing["status"] == "error"
    assert failing["error_type"] == "ValueError"


def test_tracing_never_breaks_the_agent(tmp_path, monkeypatch):
    """Observability failure must not become agent failure.

    Trace file placed *under a regular file*, so every write raises
    NotADirectoryError. The agent must still return its answer.
    """
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("AGENTCORE_TRACE_FILE", str(blocker / "traces.jsonl"))
    mock.PROVIDER.queue("fine")
    assert LLM(project="test").complete("hi").text == "fine"
    assert tracing.read_spans() == []


# ---------- store ----------


def test_search_ranks_the_relevant_chunk_first():
    emb = store.Embedder()
    store.add_documents(
        "docs",
        [
            {"id": "1", "text": "refund policy window is thirty days", "title": "Refunds"},
            {"id": "2", "text": "the office parking garage closes at ten", "title": "Parking"},
        ],
        emb,
    )
    hits = store.search("docs", "refund policy window", emb, k=2)
    assert hits[0].id == "1"
    assert hits[0].title == "Refunds"


def test_min_score_can_return_nothing():
    """An empty result is a valid answer — P2's refusal path depends on it."""
    emb = store.Embedder()
    store.add_documents("docs", [{"id": "1", "text": "unrelated content here"}], emb)
    assert store.search("docs", "quantum chromodynamics", emb, min_score=0.99) == []


def test_search_on_an_empty_collection_is_empty():
    assert store.search("nothing", "q", store.Embedder()) == []


def test_embeddings_are_cached_and_deterministic():
    emb = store.Embedder()
    first = emb.embed(["stable text"])
    second = emb.embed(["stable text"])
    assert (first == second).all()

    with store.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM embedding_cache").fetchone()["n"]
    assert n == 1, "the second call must hit the cache, not re-embed"


def test_kv_roundtrip_and_listing():
    store.kv_set("approvals", "req-1", {"status": "pending", "amount": 500})
    assert store.kv_get("approvals", "req-1")["status"] == "pending"
    assert store.kv_get("approvals", "missing", default={}) == {}
    store.kv_set("approvals", "req-2", {"status": "approved"})
    assert len(store.kv_list("approvals")) == 2


def test_kv_survives_a_reconnect():
    """P6 pauses durably: the approval must outlive the process."""
    store.kv_set("approvals", "req-1", {"status": "pending"})
    # Drop the cached connection entirely, as a process restart would.
    conns = getattr(store._LOCAL, "conns", {})
    for conn in list(conns.values()):
        conn.close()
    conns.clear()
    assert store.kv_get("approvals", "req-1") == {"status": "pending"}
