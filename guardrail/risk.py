"""Risk scoring and fast/careful reviewer routing."""

from __future__ import annotations

from dataclasses import dataclass

from .knowledge_base import KnowledgeBase
from .models import (
    AgentContext,
    CheckResult,
    Outcome,
    ProposedAction,
    ReviewResult,
    RiskAssessment,
)
from .reviewers import Reviewer, ReviewRequest
from .tools import ToolSpec

WARN_WEIGHT = 0.2


def assess_risk(
    spec: ToolSpec,
    action: ProposedAction,
    ctx: AgentContext,
    checks: list[CheckResult],
    kb: KnowledgeBase,
) -> RiskAssessment:
    """Blast radius of the action if it turns out to be wrong. Range 0..1."""
    score = spec.base_risk
    factors = [f"base risk of {spec.name}: {spec.base_risk:.2f}"]

    def add(amount: float, why: str) -> None:
        nonlocal score
        score += amount
        factors.append(f"+{amount:.2f} {why}")

    if spec.irreversible:
        add(0.15, "irreversible side effect")
    if action.tool == "issue_refund":
        limit = kb.param("refund_approval_limit")
        add(0.4 * min(action.args["amount"] / limit, 1.0), f"${action.args['amount']:.2f} of ${limit:.0f} limit")
    if action.tool == "update_inventory":
        limit = kb.param("inventory_max_adjustment")
        add(0.2 * min(abs(action.args["delta"]) / limit, 1.0), f"{abs(action.args['delta'])} units moved")
    if action.tool == "send_email" and not action.args["to"].lower().endswith("@" + kb.internal_domain):
        add(0.05, "leaves the company")
    for w in (c for c in checks if c.outcome is Outcome.WARN):
        add(WARN_WEIGHT, f"warning: {w.check}")
    if action.tool in ("issue_refund", "send_email", "answer_customer") and not ctx.customer_id:
        add(0.1, "no verified customer identity in context")
    return RiskAssessment(min(round(score, 3), 1.0), factors)


@dataclass
class RiskRouter:
    """Send low-risk actions to a cheap reviewer and high-risk ones to a careful one.

    Low-risk actions start on the fast tier; if it objects or is unsure, the same
    request is re-reviewed by the careful tier, whose verdict is final.
    """

    fast: Reviewer
    careful: Reviewer
    threshold: float = 0.5
    cascade_below_confidence: float = 0.75

    def review(self, request: ReviewRequest) -> list[ReviewResult]:
        if request.risk.score >= self.threshold:
            return [self.careful.review(request)]
        first = self.fast.review(request)
        if first.grounded and first.confidence >= self.cascade_below_confidence:
            return [first]
        return [first, self.careful.review(request)]
