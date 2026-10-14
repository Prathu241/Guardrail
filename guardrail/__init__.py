"""Pre-execution guardrails that ground agent tool calls in a reference knowledge base."""

from importlib.metadata import PackageNotFoundError, version

from .guard import Guardrail, feedback_for_agent
from .knowledge_base import KnowledgeBase
from .models import (
    AgentContext,
    Decision,
    ExecutionResult,
    Outcome,
    ProposedAction,
    Verdict,
)
from .reviewers import ClaudeReviewer, HeuristicReviewer
from .risk import RiskRouter

try:
    __version__ = version("guardrail")
except PackageNotFoundError:  # running from a source tree that was not installed
    __version__ = "0.0.0+source"

__all__ = [
    "AgentContext",
    "ClaudeReviewer",
    "Decision",
    "ExecutionResult",
    "Guardrail",
    "HeuristicReviewer",
    "KnowledgeBase",
    "Outcome",
    "ProposedAction",
    "RiskRouter",
    "Verdict",
    "feedback_for_agent",
]
