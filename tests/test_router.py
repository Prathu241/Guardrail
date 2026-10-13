from guardrail import AgentContext, ProposedAction, RiskRouter
from guardrail.models import ReviewResult, RiskAssessment
from guardrail.reviewers import ReviewRequest


class Stub:
    def __init__(self, name, grounded=True, confidence=0.95):
        self.name, self.grounded, self.confidence, self.calls = name, grounded, confidence, 0

    def review(self, req):
        self.calls += 1
        return ReviewResult(self.name, self.grounded, self.confidence, [], 1)


def _req(kb, score):
    return ReviewRequest(ProposedAction("x", {}), AgentContext(), [], RiskAssessment(score), kb)


def test_high_risk_skips_fast_tier(kb):
    fast, careful = Stub("fast"), Stub("careful")
    out = RiskRouter(fast, careful).review(_req(kb, 0.8))
    assert [r.reviewer for r in out] == ["careful"] and fast.calls == 0


def test_low_risk_confident_fast_stops_there(kb):
    fast, careful = Stub("fast"), Stub("careful")
    out = RiskRouter(fast, careful).review(_req(kb, 0.2))
    assert [r.reviewer for r in out] == ["fast"] and careful.calls == 0


def test_low_risk_unsure_fast_cascades(kb):
    fast, careful = Stub("fast", confidence=0.6), Stub("careful")
    out = RiskRouter(fast, careful).review(_req(kb, 0.2))
    assert [r.reviewer for r in out] == ["fast", "careful"]


def test_low_risk_objection_cascades(kb):
    fast, careful = Stub("fast", grounded=False), Stub("careful")
    out = RiskRouter(fast, careful).review(_req(kb, 0.2))
    assert [r.reviewer for r in out] == ["fast", "careful"]
