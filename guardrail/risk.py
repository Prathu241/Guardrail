"""Risk scoring and fast/careful reviewer routing."""

from __future__ import annotations

from dataclasses import dataclass

from .checks import Session
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
    session: Session | None = None,
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
    blocked = len(session.blocked_tools) if session else 0
    if blocked:
        add(min(0.3, 0.1 * blocked), f"{blocked} earlier blocked action(s) in this conversation")
    return RiskAssessment(min(round(score, 3), 1.0), factors)


@dataclass
class ReviewOutcome:
    reviews: list[ReviewResult]  # every review that ran, in order
    deciding: list[ReviewResult]  # the reviews whose verdict counts

    @property
    def grounded(self) -> bool:
        return all(r.grounded for r in self.deciding)

    @property
    def confidence(self) -> float:
        """Grounded: the least confident approver. Not grounded: the most confident objector."""
        if self.grounded:
            return min(r.confidence for r in self.deciding)
        return max(r.confidence for r in self.deciding if not r.grounded)

    @property
    def decided_by(self) -> str:
        objectors = [r.reviewer for r in self.deciding if not r.grounded]
        return "+".join(objectors or [r.reviewer for r in self.deciding])


@dataclass
class RiskRouter:
    """Route each action to review depth proportional to its risk.

    risk < threshold            fast reviewer; if it objects or is unsure, the careful reviewer
                                re-reviews and its verdict is final
    threshold <= risk           careful reviewer
    critical_threshold <= risk  careful reviewer AND an independent second reviewer (a different
                                model, so their errors are less correlated); both must approve
    """

    fast: Reviewer
    careful: Reviewer
    threshold: float = 0.5
    cascade_below_confidence: float = 0.75
    critical: Reviewer | None = None
    critical_threshold: float = 0.8

    def route(self, request: ReviewRequest) -> ReviewOutcome:
        score = request.risk.score
        if self.critical is not None and score >= self.critical_threshold:
            both = [self.careful.review(request), self.critical.review(request)]
            return ReviewOutcome(both, both)
        if score >= self.threshold:
            r = self.careful.review(request)
            return ReviewOutcome([r], [r])
        first = self.fast.review(request)
        if first.grounded and first.confidence >= self.cascade_below_confidence:
            return ReviewOutcome([first], [first])
        second = self.careful.review(request)
        return ReviewOutcome([first, second], [second])

    def review(self, request: ReviewRequest) -> list[ReviewResult]:
        return self.route(request).reviews


def router_for(settings) -> RiskRouter:
    from .reviewers import reviewers_for

    fast, careful, critical = reviewers_for(settings)
    return RiskRouter(
        fast,
        careful,
        threshold=settings.risk_threshold,
        cascade_below_confidence=settings.cascade_below_confidence,
        critical=critical,
        critical_threshold=settings.critical_threshold,
    )
