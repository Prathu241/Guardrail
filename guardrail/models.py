"""Core data types shared by every layer of the guardrail."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def _action_id() -> str:
    return f"act-{uuid.uuid4().hex[:12]}"


class Outcome(str, Enum):
    """Result of a single deterministic check."""

    PASS = "pass"
    WARN = "warn"  # suspicious but not disqualifying; raises the risk score
    NEEDS_APPROVAL = "needs_approval"  # allowed only with a human sign-off
    FAIL = "fail"  # contradicts the source of truth; never execute


class Decision(str, Enum):
    ALLOW = "allow"
    ESCALATE = "escalate"  # park for a human; do not execute
    BLOCK = "block"


@dataclass(frozen=True)
class ProposedAction:
    """A tool call the agent wants to make, captured *before* it runs."""

    tool: str
    args: dict[str, Any]
    rationale: str = ""
    id: str = field(default_factory=_action_id)


@dataclass
class AgentContext:
    """What the agent was actually told. Used to ground intent, not facts."""

    conversation: list[str] = field(default_factory=list)
    customer_id: str | None = None  # the authenticated customer, if any
    conversation_id: str = "default"

    @property
    def text(self) -> str:
        return "\n".join(self.conversation)


@dataclass
class CheckResult:
    check: str
    outcome: Outcome
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewResult:
    """Verdict of a model-based (or simulated) grounding reviewer."""

    reviewer: str
    grounded: bool
    confidence: float
    concerns: list[str] = field(default_factory=list)
    cost: float = 0.0


@dataclass
class RiskAssessment:
    score: float
    factors: list[str] = field(default_factory=list)


@dataclass
class Verdict:
    action: ProposedAction
    decision: Decision
    checks: list[CheckResult] = field(default_factory=list)
    risk: RiskAssessment | None = None
    reviews: list[ReviewResult] = field(default_factory=list)
    caught_by: str | None = None  # "deterministic" | "commit-recheck" | reviewer name | None if allowed

    @property
    def route(self) -> list[str]:
        return ["deterministic"] + [r.reviewer for r in self.reviews]

    @property
    def reasons(self) -> list[str]:
        out = [
            f"[{c.check}] {c.message}"
            for c in self.checks
            if c.outcome in (Outcome.FAIL, Outcome.NEEDS_APPROVAL)
        ]
        if self.decision is not Decision.ALLOW:
            for r in self.reviews:
                if not r.grounded:
                    out.extend(f"[{r.reviewer}] {c}" for c in r.concerns)
        return out

    @property
    def warnings(self) -> list[str]:
        return [f"[{c.check}] {c.message}" for c in self.checks if c.outcome is Outcome.WARN]

    @property
    def review_cost(self) -> float:
        return sum(r.cost for r in self.reviews)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action.id,
            "tool": self.action.tool,
            "args": self.action.args,
            "decision": self.decision.value,
            "caught_by": self.caught_by,
            "risk": round(self.risk.score, 3) if self.risk else None,
            "route": self.route,
            "reasons": self.reasons,
            "warnings": self.warnings,
        }


@dataclass
class ExecutionResult:
    verdict: Verdict
    executed: bool
    output: Any = None
    approval_id: str | None = None
