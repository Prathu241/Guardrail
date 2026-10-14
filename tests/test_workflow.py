"""End-to-end workflow: agent graph, human approvals, commit-time re-verification, durability."""

import pytest

from guardrail import Decision
from guardrail.agent import tool_schemas
from guardrail.app import build_app
from guardrail.config import Settings
from guardrail.demo import TICKET, hallucinating_agent
from guardrail.ledger import ApprovalError
from guardrail.models import AgentContext, ProposedAction
from guardrail.services import ConversationError


def refund(order_id="O-5001", customer_id="C-1001", amount=49.99, reason="Lantern arrived cracked"):
    return ProposedAction("issue_refund", dict(order_id=order_id, customer_id=customer_id, amount=amount, reason=reason))


KAYAK = "Grace here. The kayak from order O-5005 leaks, it's defective. I'd like a full refund."


def test_hallucinating_agent_trajectory():
    app = build_app(Settings(db_path=":memory:"), model=hallucinating_agent())
    conv = app.conversations.start(TICKET, "C-1001")
    assert [(e["tool"], e["decision"]) for e in conv["guard_log"]] == [
        ("issue_refund", "block"),  # hallucinated order id
        ("issue_refund", "block"),  # 10x amount
        ("issue_refund", "block"),  # the tent's price for a lantern request
    ]
    # Three blocked refunds: the agent loses autonomy, even its correct 4th attempt waits for a human.
    assert conv["status"] == "awaiting_approval"
    (approval,) = conv["pending_approvals"]
    assert "autonomy_not_revoked" in approval["reasons"][0]
    assert app.guard.kb.order("O-5001")["refunded"] == 0

    conv = app.approvals.decide(approval["id"], True, "morgan.lee", "checked")["conversation"]
    assert [(e["tool"], e["decision"], e["executed"]) for e in conv["guard_log"][3:]] == [
        ("issue_refund", "escalate", True),    # human-confirmed
        ("answer_customer", "block", False),   # fabricated quote
        ("send_email", "block", False),        # typo-squatted domain
        ("answer_customer", "allow", True),
        ("send_email", "allow", True),
        ("issue_refund", "block", False),      # duplicate
    ]
    assert app.guard.kb.order("O-5001")["refunded"] == 49.99
    blocked = [m for m in conv["messages"] if m["role"] == "tool" and m["status"] == "error"]
    assert len(blocked) == 6 and all("NOT performed" in m["text"] for m in blocked)
    outbox = app.db.all("SELECT * FROM outbox")
    assert {o["recipient"] for o in outbox} == {"C-1001", "ada@example.com"}


def test_agent_finds_an_unnumbered_order_by_looking_it_up(app):
    conv = app.conversations.start("My lantern arrived cracked. Can I get a refund for it?", "C-1001")
    assert [e["tool"] for e in conv["guard_log"]][:3] == ["list_orders", "lookup_order", "issue_refund"]
    refund = next(e for e in conv["guard_log"] if e["tool"] == "issue_refund")
    assert refund["executed"] and refund["args"]["order_id"] == "O-5001"  # provenance: seen in list_orders


def test_rule_agent_resolves_ticket(app):
    conv = app.conversations.start(TICKET, "C-1001")
    assert conv["status"] == "active"
    assert all(e["executed"] for e in conv["guard_log"])
    assert app.guard.kb.order("O-5001")["refunded"] == 49.99
    replies = [o["body"] for o in app.db.all("SELECT body FROM outbox ORDER BY ts, rowid")]
    assert any("$49.99" in r and "5 to 10 business days" in r for r in replies)
    assert any("30 days" in r for r in replies)


def test_escalation_pauses_then_approval_resumes_and_commits(app):
    conv = app.conversations.start(KAYAK, "C-1002")
    assert conv["status"] == "awaiting_approval"
    (approval,) = conv["pending_approvals"]
    assert approval["tool"] == "issue_refund" and "manager approval" in approval["reasons"][0]
    assert app.guard.kb.order("O-5005")["refunded"] == 0  # nothing ran while paused

    with pytest.raises(ConversationError):
        app.conversations.send(conv["id"], "hello?")  # can't talk over a pending approval

    out = app.approvals.decide(approval["id"], True, "morgan.lee", "photos verified")
    conv = out["conversation"]
    assert conv["status"] == "active"
    assert app.guard.kb.order("O-5005")["refunded"] == 649.0
    entry = next(e for e in conv["guard_log"] if e["tool"] == "issue_refund")
    assert entry["executed"] and entry["approval"]["decided_by"] == "morgan.lee"
    assert app.guard.approvals.get(approval["id"])["result"]["executed"] is True


def test_rejection_resumes_without_executing(app):
    conv = app.conversations.start(KAYAK, "C-1002")
    approval = conv["pending_approvals"][0]
    conv = app.approvals.decide(approval["id"], False, "morgan.lee", "no photos")["conversation"]
    assert app.guard.kb.order("O-5005")["refunded"] == 0
    rejected = next(m for m in conv["messages"] if m["role"] == "tool" and m["name"] == "issue_refund")
    assert rejected["text"].startswith("REJECTED by morgan.lee: no photos")
    reply = app.db.one("SELECT body FROM outbox WHERE recipient = 'C-1002'")["body"]
    assert "passed it to our support team" in reply


def test_approval_cannot_be_decided_twice(app):
    conv = app.conversations.start(KAYAK, "C-1002")
    aid = conv["pending_approvals"][0]["id"]
    app.approvals.decide(aid, True, "morgan.lee")
    with pytest.raises(ApprovalError):
        app.approvals.decide(aid, False, "someone.else")


def test_commit_rechecks_against_current_records(guard):
    """Allowed at evaluation, but the records change before commit (another refund lands)."""
    ada = AgentContext(["Everything in order O-5001 arrived damaged. I want a full refund."], "C-1001")
    verdict = guard.evaluate(refund(amount=189.97, reason="all items arrived damaged"), ada)
    assert verdict.decision is Decision.ALLOW
    guard.kb.record_refund("O-5001", 49.99)  # concurrent change
    result = guard.commit(verdict, ada)
    assert not result.executed
    assert result.verdict.caught_by == "commit-recheck"
    assert guard.kb.order("O-5001")["refunded"] == 49.99
    assert guard.audit.get(verdict.action.id)["decision"] == "block"


def test_tool_failure_rolls_back(guard, ada):
    def boom(**_):
        guard.kb.record_refund("O-5001", 49.99)
        raise RuntimeError("payment gateway down")

    guard.tools["issue_refund"].fn = boom
    result = guard.execute(refund(), ada)
    assert not result.executed and "payment gateway down" in result.output["error"]
    assert guard.kb.order("O-5001")["refunded"] == 0  # the partial write was rolled back


def test_sidecar_escalation_is_committed_on_approval(app):
    ctx = AgentContext(["The kayak on O-5005 leaks, I want a full refund."], "C-1002", "sidecar-1")
    result = app.guard.execute(refund("O-5005", "C-1002", 649.00, "kayak leaks, defective"), ctx)
    assert result.verdict.decision is Decision.ESCALATE and result.approval_id
    app.approvals.decide(result.approval_id, True, "morgan.lee")
    assert app.guard.kb.order("O-5005")["refunded"] == 649.0


def test_paused_run_survives_restart(tmp_path):
    settings = Settings(db_path=str(tmp_path / "g.db"))
    first = build_app(settings)
    conv = first.conversations.start(KAYAK, "C-1002")
    aid = conv["pending_approvals"][0]["id"]

    second = build_app(settings)  # a new process: same database and checkpoints
    assert second.conversations.get(conv["id"])["status"] == "awaiting_approval"
    resumed = second.approvals.decide(aid, True, "morgan.lee")["conversation"]
    assert resumed["status"] == "active"
    assert second.guard.kb.order("O-5005")["refunded"] == 649.0


def test_bound_tool_schemas_cannot_bypass_guard(app):
    tools = {t.name: t for t in tool_schemas(app.guard)}
    assert set(tools) == set(app.guard.tools)
    with pytest.raises(RuntimeError):
        tools["issue_refund"].func(order_id="O-5001", customer_id="C-1001", amount=1.0, reason="x")
    assert app.guard.kb.order("O-5001")["refunded"] == 0
