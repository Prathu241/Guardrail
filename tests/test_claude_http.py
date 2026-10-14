"""Claude reviewer and Claude agent integration, through the real SDK clients over HTTP.

The SDKs (anthropic, langchain-anthropic), the composition root (build_app), the LangGraph run
and the database are all real; only the Messages API is a local scripted server. This covers
what a fake client object cannot: the SDK's own response parsing, exception types, timeouts and
retries. Live-API tests are in test_claude_live.py.
"""

from __future__ import annotations

import json
import time

import anthropic
import pytest
from fake_anthropic import FakeAnthropic, Reply, error, message, review, text, tool_use

from guardrail import AgentContext, Decision, Guardrail, KnowledgeBase, ProposedAction, RiskRouter
from guardrail.agent.graph import AGENT_UNAVAILABLE
from guardrail.app import build_app
from guardrail.config import Settings
from guardrail.reviewers import claude_reviewers

HAIKU, OPUS, SONNET = "claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5"
ADA_SAYS = "My order O-5001 arrived with a cracked lantern. Can I get a refund for the lantern?"
EVIDENCE = ["arrived with a cracked lantern"]


@pytest.fixture
def api():
    fake = FakeAnthropic()
    fake.url = fake.start()
    yield fake
    fake.stop()


def _client(api, timeout=2.0, retries=0):
    return anthropic.Anthropic(base_url=api.url, api_key="test-key", timeout=timeout, max_retries=retries)


def _guard(api, timeout=2.0, retries=0, **router):
    fast, careful, critical = claude_reviewers(client=_client(api, timeout, retries))
    return Guardrail(KnowledgeBase.in_memory(), RiskRouter(fast, careful, critical=critical, **router))


def _refund(amount=49.99, order="O-5001", reason="cracked lantern"):
    return ProposedAction("issue_refund", dict(order_id=order, customer_id="C-1001", amount=amount, reason=reason))


ADA = AgentContext([ADA_SAYS], "C-1001")


# ---------------------------------------------------------------------------- reviewer: verdicts
def test_allow_sends_the_contract_and_executes(api):
    api.script(HAIKU, review(True, 0.95, evidence=EVIDENCE))
    guard = _guard(api)
    result = guard.execute(_refund(), ADA)
    assert result.verdict.decision is Decision.ALLOW and result.executed
    assert guard.kb.order("O-5001")["refunded"] == 49.99

    [call] = api.calls(HAIKU)
    body = call["body"]
    assert call["path"] == "/v1/messages"
    assert body["output_config"]["format"]["schema"]["required"] == ["grounded", "confidence", "concerns", "evidence"]
    assert "untrusted data" in body["system"]
    assert "<proposed_action>" in body["messages"][0]["content"] and "O-5001" in body["messages"][0]["content"]


def test_block_after_both_tiers_object(api):
    api.script(HAIKU, review(False, 0.9, ["the customer never mentioned a reason"]))
    api.script(OPUS, review(False, 0.95, ["refund reason is invented"]))
    guard = _guard(api)
    result = guard.execute(_refund(reason="customer changed their mind"), ADA)
    assert result.verdict.decision is Decision.BLOCK and not result.executed
    assert result.verdict.caught_by == "claude-careful"
    [careful] = api.calls(OPUS)
    assert careful["path"] == "/v1/messages?beta=true"
    assert "server-side-fallback-2026-07-01" in careful["headers"].get("anthropic-beta", "")
    assert guard.kb.order("O-5001")["refunded"] == 0


def test_uncertain_objection_escalates_to_a_human(api):
    api.script(HAIKU, review(False, 0.9, ["unsure which item"]))
    api.script(OPUS, review(False, 0.5, ["cannot tell which item was meant"]))
    guard = _guard(api)
    result = guard.execute(_refund(), ADA)
    assert result.verdict.decision is Decision.ESCALATE and result.approval_id and not result.executed


def test_critical_actions_need_both_models(api):
    api.script(OPUS, review(True, 0.95, evidence=EVIDENCE))
    api.script(SONNET, review(False, 0.9, ["amount does not match the item"]))
    guard = _guard(api, critical_threshold=0.3)
    verdict = guard.evaluate(_refund(), ADA)
    assert verdict.decision is Decision.BLOCK and verdict.caught_by == "claude-second-opinion"
    assert len(api.calls(OPUS)) == len(api.calls(SONNET)) == 1 and not api.calls(HAIKU)


def test_approval_quoting_invented_evidence_is_not_trusted(api):
    api.script(HAIKU, review(True, 0.99, evidence=["the customer said the courier crushed it"]))
    api.script(OPUS, review(True, 0.99, evidence=["the customer said the courier crushed it"]))
    verdict = _guard(api).evaluate(_refund(), ADA)
    assert verdict.decision is Decision.ESCALATE
    assert any("does not exist in its inputs" in r for r in verdict.reasons)


# ---------------------------------------------------------------------------- reviewer: failures
MALFORMED = {
    "not json": text("Looks fine to me!"),
    "json array": text(json.dumps([True, 0.9])),
    "grounded as string": review("false", 0.9, evidence=EVIDENCE),
    "confidence NaN": text('{"grounded": true, "confidence": NaN, "concerns": [], "evidence": ["cracked lantern"]}'),
    "confidence out of range": review(True, 1.7, evidence=EVIDENCE),
    "confidence boolean": review(True, True, evidence=EVIDENCE),
    "concerns not a list": text(json.dumps({"grounded": True, "confidence": 0.9, "concerns": "none",
                                            "evidence": EVIDENCE})),
    "evidence missing": text(json.dumps({"grounded": True, "confidence": 0.9, "concerns": []})),
    "no text block": Reply(message([])),
    "truncated": text(json.dumps({"grounded": True, "confidence": 0.9, "concerns": [], "evidence": EVIDENCE}),
                      stop_reason="max_tokens"),
    "refusal": text("", stop_reason="refusal"),
    "HTTP 200 with HTML body": Reply("<html>bad gateway</html>", raw=True),
    "HTTP 500": error(500, "api_error"),
    "HTTP 529 overloaded": error(529, "overloaded_error"),
    "HTTP 429 rate limited": error(429, "rate_limit_error"),
    "HTTP 401": error(401, "authentication_error"),
}


@pytest.mark.parametrize("case", MALFORMED)
def test_bad_reviewer_output_never_allows(api, case):
    api.default = MALFORMED[case]
    guard = _guard(api)
    result = guard.execute(_refund(), ADA)
    assert result.verdict.decision is Decision.ESCALATE, result.verdict.reasons
    assert not result.executed and guard.kb.order("O-5001")["refunded"] == 0
    assert all(r.confidence == 0.0 and not r.grounded for r in result.verdict.reviews)
    assert any("failing closed" in r or "needs a human" in r for r in result.verdict.reasons)


def test_timeout_fails_closed_within_the_budget(api):
    api.default = Reply(review(True, 0.95, evidence=EVIDENCE).body, delay=3.0)
    start = time.monotonic()
    verdict = _guard(api, timeout=0.3).evaluate(_refund(), ADA)
    assert time.monotonic() - start < 2.5
    assert verdict.decision is Decision.ESCALATE
    assert any("timed out" in r for r in verdict.reasons)


def test_retries_are_bounded(api):
    api.default = error(529, "overloaded_error")
    _guard(api, retries=2).evaluate(_refund(), ADA)
    assert len(api.calls(HAIKU)) == 3 and len(api.calls(OPUS)) == 3  # 1 try + 2 retries per tier


def test_transient_error_recovers_on_retry(api):
    api.script(HAIKU, error(529, "overloaded_error"), review(True, 0.95, evidence=EVIDENCE))
    verdict = _guard(api, retries=1).evaluate(_refund(), ADA)
    assert verdict.decision is Decision.ALLOW and len(api.calls(HAIKU)) == 2


# ---------------------------------------------------------------------------- agent end to end
@pytest.fixture
def claude_app(api, monkeypatch):
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL"):
        monkeypatch.setenv(var, api.url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    return build_app(Settings(db_path=":memory:", agent="claude", reviewer="claude", agent_timeout=1.0,
                              agent_max_retries=0, reviewer_timeout=1.0, reviewer_max_retries=0))


def test_agent_hallucination_is_blocked_then_corrected(api, claude_app):
    api.script(
        "agent",
        tool_use("issue_refund", order_id="O-5010", customer_id="C-1001", amount=49.99, reason="cracked lantern"),
        tool_use("lookup_order", order_id="O-5001", customer_id="C-1001"),
        tool_use("issue_refund", order_id="O-5001", customer_id="C-1001", amount=49.99, reason="cracked lantern"),
        text("I've refunded $49.99 for the lantern."),
    )
    api.script(OPUS, review(True, 0.95, evidence=EVIDENCE))
    conv = claude_app.conversations.start(ADA_SAYS, "C-1001")

    log = [(e["tool"], e["decision"], e["executed"]) for e in conv["guard_log"]]
    assert log == [("issue_refund", "block", False), ("lookup_order", "allow", True), ("issue_refund", "allow", True)]
    assert claude_app.guard.kb.order("O-5001")["refunded"] == 49.99
    # The block reason went back to Claude as an error tool_result it can act on.
    second_turn = api.calls("agent")[1]["body"]["messages"][-1]["content"]
    [result] = [b for b in second_turn if b.get("type") == "tool_result"]
    assert result["is_error"] is True and "O-5010 does not exist" in json.dumps(result["content"])
    assert {t["name"] for t in api.calls("agent")[0]["body"]["tools"]} >= {"issue_refund", "lookup_order"}
    # The earlier block raised the corrected refund's risk, so the careful tier reviewed it; the
    # read-only lookup was not sent for model review at all.
    assert conv["guard_log"][2]["route"] == ["deterministic", "claude-careful"]
    assert len(api.calls(OPUS)) == 1 and not api.calls(HAIKU)


def test_agent_escalation_pauses_until_a_human_approves(api, claude_app):
    api.script(
        "agent",
        tool_use("issue_refund", order_id="O-5005", customer_id="C-1002", amount=649.0, reason="kayak leaks"),
        text("A manager has approved your refund of $649.00."),
    )
    conv = claude_app.conversations.start(
        "The kayak from order O-5005 leaks, it's defective. I'd like a full refund.", "C-1002")
    assert conv["status"] == "awaiting_approval" and claude_app.guard.kb.order("O-5005")["refunded"] == 0
    [pending] = conv["pending_approvals"]
    assert len(api.calls("agent")) == 1  # the run is paused, not polling the model

    out = claude_app.approvals.decide(pending["id"], True, "morgan.lee", "photos verified")
    assert out["conversation"]["status"] == "active"
    assert claude_app.guard.kb.order("O-5005")["refunded"] == 649.0
    assert len(api.calls("agent")) == 2


@pytest.mark.parametrize("failure", ["timeout", "HTTP 500", "HTTP 200 with HTML body"])
def test_agent_failure_ends_the_turn_and_changes_nothing(api, claude_app, failure):
    api.default = {
        "timeout": Reply(text("hi").body, delay=3.0),
        "HTTP 500": error(500, "api_error"),
        "HTTP 200 with HTML body": Reply("<html>bad gateway</html>", raw=True),
    }[failure]
    conv = claude_app.conversations.start(ADA_SAYS, "C-1001")
    last = conv["messages"][-1]
    assert last["text"] == AGENT_UNAVAILABLE and last["agent_error"]
    assert claude_app.guard.audit.list() == [] and claude_app.guard.kb.order("O-5001")["refunded"] == 0
    assert conv["status"] == "active"


def test_agent_inventing_tools_and_parameters_is_blocked(api, claude_app):
    api.script(
        "agent",
        tool_use("refund_everything", customer_id="C-1001"),
        tool_use("issue_refund", order_id="O-5001", customer_id="C-1001", amount=49.99, reason="cracked",
                 override_limits=True),
        text("I could not process that."),
    )
    conv = claude_app.conversations.start(ADA_SAYS, "C-1001")
    assert [(e["decision"], e["executed"]) for e in conv["guard_log"]] == [("block", False), ("block", False)]
    reasons = " ".join(r for e in conv["guard_log"] for r in e["reasons"])
    assert "refund_everything' does not exist" in reasons and "override_limits" in reasons
    assert claude_app.guard.kb.order("O-5001")["refunded"] == 0
