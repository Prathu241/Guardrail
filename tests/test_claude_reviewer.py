"""ClaudeReviewer request/response handling, against a fake client (no network)."""

import json
from types import SimpleNamespace

import anthropic
import httpx

from guardrail import AgentContext, Decision, Guardrail, ProposedAction, RiskRouter
from guardrail.models import RiskAssessment
from guardrail.reviewers import ClaudeReviewer, ReviewRequest, claude_reviewers


def _response(payload=None, stop_reason="end_turn"):
    content = [SimpleNamespace(type="text", text=json.dumps(payload))] if payload else []
    return SimpleNamespace(stop_reason=stop_reason, content=content)


class FakeClient:
    def __init__(self, response=None, error=None):
        self.calls = []
        self._response, self._error = response, error
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._response


def _req(kb):
    action = ProposedAction("issue_refund", dict(order_id="O-5001", customer_id="C-1001", amount=49.99, reason="cracked"))
    return ReviewRequest(action, AgentContext(["refund my lantern on O-5001"], "C-1001"), [], RiskAssessment(0.6), kb)


def test_parses_structured_output_and_sends_schema(kb):
    client = FakeClient(_response({"grounded": False, "confidence": 0.9, "concerns": ["wrong item"]}))
    r = ClaudeReviewer("claude-opus-5-5", "high", "careful", 40, use_fallbacks=True, client=client).review(_req(kb))
    assert (r.grounded, r.confidence, r.concerns) == (False, 0.9, ["wrong item"])
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "<proposed_action>" in call["messages"][0]["content"]


def test_fast_tier_uses_haiku_without_fallbacks(kb):
    client = FakeClient(_response({"grounded": True, "confidence": 0.95, "concerns": []}))
    fast, careful = claude_reviewers(client=client)
    fast.review(_req(kb))
    assert client.calls[0]["model"] == "claude-haiku-5-5" and "fallbacks" not in client.calls[0]
    assert careful.model == "claude-opus-5-5"


def test_refusal_fails_closed(kb):
    client = FakeClient(_response(stop_reason="refusal"))
    r = ClaudeReviewer("claude-opus-5-5", "high", "careful", 40, client=client).review(_req(kb))
    assert not r.grounded and r.confidence == 0.0


def test_api_error_fails_closed_to_escalation(kb, ada):
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    reviewer = ClaudeReviewer("claude-opus-5-5", "high", "careful", 40, client=FakeClient(error=err))
    guard = Guardrail(kb, RiskRouter(reviewer, reviewer))
    v = guard.evaluate(
        ProposedAction("issue_refund", dict(order_id="O-5001", customer_id="C-1001", amount=49.99, reason="cracked")),
        ada,
    )
    assert v.decision is Decision.ESCALATE
