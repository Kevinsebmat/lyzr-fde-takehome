"""One FastAPI app mounting every project.

This is the surface the Next.js console drives and the demo walks through. It
exists so a reviewer can see all eleven projects without eleven terminals — the
CLIs remain the canonical interface for each project on its own.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Inlined rather than `import conftest`, because an import statement is exactly
# the thing an import sorter will move — and moving this below the project
# imports would break them. A plain loop cannot be reordered into a bug.
for _path in [ROOT, ROOT / "core", *sorted(ROOT.glob("p[0-9][0-9]-*"))]:
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from agentcore import bootstrap  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("server")

MODE = bootstrap(quiet=True)

app = FastAPI(
    title="Lyzr FDE take-home",
    description=(
        "Eleven agent projects over one shared core. Every endpoint runs "
        "offline in mock mode; set ANTHROPIC_API_KEY for real calls."
    ),
    version="0.1.0",
)

# The console runs on :3000 in development. Wide open because this is a local
# demo app with no auth and no data worth protecting — worth saying explicitly
# rather than leaving someone to assume it was considered.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PROJECTS = [
    ("p01", "Structured Output", "unparseable or untrustworthy model output"),
    ("p02", "RAG with Citation Grounding", "a confident answer the sources don't support"),
    ("p03", "ReAct Planner", "the agent that never stops"),
    ("p04", "Multi-Tool Orchestrator", "unauthorised calls, and sources that disagree"),
    ("p05", "Memory Agent", "amnesia, and confidently remembering what changed"),
    ("p06", "Human-in-the-Loop Approval", "an unauthorised irreversible action"),
    ("p07", "Cost-Aware Router", "a cascade everyone believes is saving money"),
    ("p08", "Event Automation", "the same refund issued twice"),
    ("p09", "Multi-Agent Debate", "four agents agreeing because they were primed to"),
    ("p10", "Self-Reflective Auto-Eval", "shipping a worse draft than you started with"),
    ("p11", "Observability", "misbehaviour every dashboard reports as healthy"),
]


@app.get("/api/health")
def health() -> dict:
    """Mode is reported so nobody mistakes a mock answer for a live one."""
    return {
        "ok": True,
        "mode": MODE,
        "live": MODE == "live",
        "note": (
            "running offline against recorded responses — set ANTHROPIC_API_KEY "
            "for real model calls" if MODE == "mock" else "calling the real API"
        ),
    }


@app.get("/api/projects")
def projects() -> dict:
    return {
        "projects": [
            {"slug": slug, "name": name, "failure_mode": failure}
            for slug, name, failure in PROJECTS
        ]
    }


def _mount() -> None:
    """Mount each project's router, reporting anything that fails to import.

    A project that cannot be imported must not take the whole app down — the
    console should still show the other ten and say which one is broken.
    """
    from server.routers import router as demo_router

    app.include_router(demo_router)

    owned = [
        ("p01", "p01_structured_output.api"),
        ("p02", "p02_rag_citations.api"),
        ("p08", "p08_event_automation.api"),
    ]
    for slug, module_path in owned:
        try:
            module = __import__(module_path, fromlist=["router"])
            app.include_router(module.router)
        except Exception as exc:  # noqa: BLE001
            log.error("could not mount %s (%s): %s", slug, module_path, exc)


_mount()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server.main:app", host="127.0.0.1", port=8000, reload=True)
