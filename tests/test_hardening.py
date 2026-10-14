"""Structural guarantees: complete grounding contracts, postconditions, consensus, input hygiene."""

import math

import pytest

from guardrail import AgentContext, Decision, ProposedAction, RiskRouter
from guardrail.models import ReviewResult, RiskAssessment
from guardrail.reviewers import ReviewRequest
from guardrail.tools import ContractError, ToolSpec, make_tools


def test_every_tool_parameter_is_covered_by_the_grounding_contract(kb):
    for spec in make_tools(kb).values():
        params = set(spec.schema.model_fields)
        assert set(spec.grounding) == params, spec.name
        assert all(spec.grounding[p] for p in params), spec.name
        if not spec.read_only:
            assert spec.postcondition is not None, spec.name


def test_incomplete_contract_is_rejected_at_registration():
    def tool(order_id: str, amount: float) -> dict:
        return {}

    with pytest.raises(ContractError, match="missing"):
        ToolSpec("t", tool, [], grounding={"order_id": ("no_injection",)}, base_risk=0.1, read_only=True)
    with pytest.raises(ContractError, match="does not run"):
        ToolSpec("t", tool, [], grounding={"order_id": ("order_exists",), "amount": ("no_injection",)},
                 base_risk=0.1, read_only=True)
    with pytest.raises(ContractError, match="postcondition"):
        ToolSpec("t", tool, [], grounding={"order_id": ("no_injection",), "amount": ("no_injection",)}, base_risk=0.1)


def test_postcondition_failure_rolls_back(guard, ada):
    real = guard.tools["issue_refund"].fn

    def buggy_refund(**args):  # a bug that refunds twice the verified amount
        return real(**{**args, "amount": args["amount"] * 2})

    guard.tools["issue_refund"].fn = buggy_refund
    r = guard.execute(ProposedAction("issue_refund", dict(order_id="O-5001", customer_id="C-1001",
                                                          amount=49.99, reason="Lantern arrived cracked")), ada)
    assert not r.executed and "PostconditionError" in r.output["error"]
    assert guard.kb.order("O-5001")["refunded"] == 0
    assert guard.audit.get(r.verdict.action.id)["executed"] == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "x" * 5000])
def test_input_hygiene(guard, ada, bad):
    args = dict(order_id="O-5001", customer_id="C-1001", amount=49.99, reason="Lantern arrived cracked")
    key = "reason" if isinstance(bad, str) else "amount"
    v = guard.evaluate(ProposedAction("issue_refund", {**args, key: bad}), ada)
    assert v.decision is Decision.BLOCK and v.checks[0].check == "args_match_schema"


class Stub:
    def __init__(self, name, grounded=True, confidence=0.95):
        self.name, self.grounded, self.confidence = name, grounded, confidence

    def review(self, req):
        return ReviewResult(self.name, self.grounded, self.confidence)


def _req(kb, score):
    return ReviewRequest(ProposedAction("x", {}), AgentContext(), [], RiskAssessment(score), kb)


def test_critical_actions_need_two_independent_approvals(kb):
    agree = RiskRouter(Stub("fast"), Stub("careful"), critical=Stub("second")).route(_req(kb, 0.9))
    assert agree.grounded and [r.reviewer for r in agree.reviews] == ["careful", "second"]

    dissent = RiskRouter(Stub("fast"), Stub("careful"), critical=Stub("second", grounded=False, confidence=0.8))
    out = dissent.route(_req(kb, 0.9))
    assert not out.grounded and out.decided_by == "second" and math.isclose(out.confidence, 0.8)

    below = RiskRouter(Stub("fast"), Stub("careful"), critical=Stub("second", grounded=False)).route(_req(kb, 0.6))
    assert below.grounded and [r.reviewer for r in below.reviews] == ["careful"]


def test_unverifiable_is_never_auto_executed_but_a_human_can_approve_it(app):
    ctx = AgentContext(["My lantern on O-5001 arrived cracked, please refund it."], "C-1001", "sidecar-h")
    r = app.guard.execute(ProposedAction("issue_refund", dict(order_id="O-5001", customer_id="C-1001",
                                                              amount=30.00, reason="Lantern arrived cracked")), ctx)
    assert r.verdict.decision is Decision.ESCALATE and not r.executed and r.approval_id
    assert app.guard.kb.order("O-5001")["refunded"] == 0
    app.approvals.decide(r.approval_id, True, "morgan.lee", "agreed a $30 goodwill refund by phone")
    assert app.guard.kb.order("O-5001")["refunded"] == 30.0


def test_malformed_agent_tool_call_is_blocked_on_the_record():
    from langchain_core.messages import AIMessage

    from guardrail.agent.agents import ScriptedModel
    from guardrail.app import build_app
    from guardrail.config import Settings

    bad = AIMessage("", invalid_tool_calls=[{"type": "invalid_tool_call", "id": "call_bad", "name": "issue_refund",
                                             "args": '{"order_id": "O-50', "error": "truncated JSON"}])
    app = build_app(Settings(db_path=":memory:"), model=ScriptedModel([bad]))
    conv = app.conversations.start("Refund my lantern from O-5001, it arrived cracked.", "C-1001")
    [entry] = conv["guard_log"]
    assert entry["decision"] == "block" and not entry["executed"]
    assert any("__malformed_arguments__" in r for r in entry["reasons"])
    tool_msgs = [m for m in conv["messages"] if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "call_bad"
