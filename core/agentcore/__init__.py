"""agentcore — shared infrastructure for the eleven agent projects.

Every project imports from here rather than re-implementing an LLM client,
a retry loop, a cost meter and a trace writer eleven times. See the top-level
README for why a shared core coexists with the one-folder-per-project layout.
"""

from .cost import Budget, BudgetExceeded, Usage, baseline_comparison, estimate_cost
from .llm import LLM, Reply, default_llm
from .models import DEFAULT_MODEL, LADDER, MODEL_REGISTRY, ModelSpec, next_model_up, spec
from .retry import (
    AgentError,
    FatalError,
    ParseError,
    PolicyError,
    TransientError,
    classify,
    retry_call,
)

__all__ = [
    "LLM", "Reply", "default_llm",
    "Budget", "BudgetExceeded", "Usage", "estimate_cost", "baseline_comparison",
    "MODEL_REGISTRY", "LADDER", "DEFAULT_MODEL", "ModelSpec", "spec", "next_model_up",
    "AgentError", "TransientError", "ParseError", "PolicyError", "FatalError",
    "classify", "retry_call",
]
