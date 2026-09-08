"""Web-search fallback for questions the corpus does not cover.

Absent by design when unconfigured. With no `TAVILY_API_KEY` this returns
nothing, and the agent refuses — which is the correct behaviour, not a
degraded one. A fallback that invents results to avoid an empty hand is worse
than the refusal it was meant to replace.

Results are always labelled as external and never merged into the grounded
answer, because they carry none of the verification the corpus path applies.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from agentcore import mock, tracing

log = logging.getLogger("p02.search")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str = "web"

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
        }


#: Canned results for the demo's known-uncovered questions, so the fallback
#: path is visible offline. Keyed by a distinctive term rather than the whole
#: question, so paraphrases still hit.
_MOCK_RESULTS: dict[str, list[SearchResult]] = {
    "hipaa": [
        SearchResult(
            title="Northwind Cloud — Compliance",
            url="https://example.com/northwind/compliance",
            snippet="SOC 2 Type II and ISO 27001 certified. HIPAA BAAs are "
                    "available on Enterprise agreements by request.",
        )
    ],
    "on-premise": [
        SearchResult(
            title="Northwind Cloud — Deployment options",
            url="https://example.com/northwind/deployment",
            snippet="Northwind Cloud is offered as a managed service only. A "
                    "self-hosted edition is not currently available.",
        )
    ],
}


def available() -> bool:
    return bool(os.environ.get("TAVILY_API_KEY")) or mock.is_mock_mode()


def web_search(query: str, max_results: int = 3) -> list[SearchResult]:
    """Search the web, or return nothing if no backend is configured."""
    with tracing.span("web_search", kind="tool", query=query[:120]) as sp:
        if mock.is_mock_mode():
            results = _mock_search(query)
            sp.attrs.update(results=len(results), backend="mock")
            return results[:max_results]

        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            log.info("no TAVILY_API_KEY — search fallback unavailable, will refuse")
            sp.attrs["backend"] = "none"
            return []

        try:
            results = _tavily_search(query, api_key, max_results)
            sp.attrs.update(results=len(results), backend="tavily")
            return results
        except Exception as exc:  # noqa: BLE001
            # A failed fallback must degrade to refusal, never take down the
            # request that was already going to refuse anyway.
            log.warning("search fallback failed, refusing instead: %s", exc)
            sp.status = "degraded"
            sp.attrs["error"] = str(exc)[:200]
            return []


def _mock_search(query: str) -> list[SearchResult]:
    lowered = query.lower()
    for key, results in _MOCK_RESULTS.items():
        if key in lowered:
            return results
    return []


def _tavily_search(query: str, api_key: str, max_results: int) -> list[SearchResult]:
    import httpx

    resp = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return [
        SearchResult(
            title=r.get("title", ""),
            url=r.get("url", ""),
            snippet=(r.get("content") or "")[:300],
        )
        for r in resp.json().get("results", [])
    ]
