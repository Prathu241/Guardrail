from guardrail import Decision, Outcome, ProposedAction
from guardrail.checks import normalize, numbers_in
from guardrail.knowledge_base import KnowledgeBase, edit_distance


def test_edit_distance_counts_transposition_as_one():
    assert edit_distance("O-5010", "O-5001") == 1
    assert edit_distance("O-5010", "O-5006") == 2


def test_closest_suggests_the_transposed_id(guard, ada):
    v = guard.evaluate(
        ProposedAction("issue_refund", dict(order_id="O-5010", customer_id="C-1001", amount=49.99, reason="cracked")),
        ada,
    )
    assert "closest real order: O-5001" in v.reasons[0]


def test_numbers_ignore_ids_and_dates():
    assert numbers_in("Order O-5001 (SKU LNT-200) delivered 2026-09-20 cost $49.99") == {49.99}
    assert numbers_in("1,250.00 and 30 days, 2 items") == {1250.0, 30.0}


def test_normalize_folds_fullwidth_and_whitespace():
    assert normalize("Ｉｇｎｏｒｅ   THIS") == "ignore this"


def test_checks_run_on_coerced_args(guard, ada):
    v = guard.evaluate(
        ProposedAction("issue_refund", dict(order_id="O-5001", customer_id="C-1001", amount="49.99", reason="cracked")),
        ada,
    )
    assert v.decision is Decision.ALLOW
    assert v.action.args["amount"] == 49.99


def test_blocked_action_does_not_mutate_kb(guard, ada, kb):
    before = kb.order("O-5001")["refunded"]
    r = guard.execute(
        ProposedAction("issue_refund", dict(order_id="O-5001", customer_id="C-1001", amount=499.90, reason="cracked")),
        ada,
    )
    assert not r.executed
    assert kb.order("O-5001")["refunded"] == before


def test_allowed_action_executes_and_is_recorded(guard, ada, kb):
    action = ProposedAction("issue_refund", dict(order_id="O-5001", customer_id="C-1001", amount=49.99, reason="cracked"))
    r = guard.execute(action, ada)
    assert r.executed
    assert kb.order("O-5001")["refunded"] == 49.99
    assert guard.session(ada.conversation_id).refunds()[0].args["amount"] == 49.99
    row = guard.audit.get(r.verdict.action.id)
    assert row["decision"] == "allow" and row["executed"] == 1


def test_warning_raises_risk(guard, ada):
    v = guard.evaluate(
        ProposedAction("issue_refund", dict(order_id="O-5001", customer_id="C-1001", amount=50.00, reason="cracked lantern")),
        ada,
    )
    assert any(c.outcome is Outcome.WARN for c in v.checks)
    assert v.risk.score >= 0.5
    assert v.route[-1] == "heuristic-careful"


def test_in_memory_knowledge_bases_are_independent():
    a, b = KnowledgeBase.in_memory(), KnowledgeBase.in_memory()
    a.record_refund("O-5001", 10)
    assert b.order("O-5001")["refunded"] == 0


def test_acts_only_for_authenticated_customer(guard, ada):
    v = guard.evaluate(
        ProposedAction("lookup_order", dict(order_id="O-5003", customer_id="C-1002")), ada
    )
    assert v.decision is Decision.BLOCK
    assert any("authenticated as C-1001" in r for r in v.reasons)


def test_read_only_tools_skip_model_review(guard, ada):
    v = guard.evaluate(ProposedAction("lookup_order", dict(order_id="O-5001", customer_id="C-1001")), ada)
    assert v.decision is Decision.ALLOW and v.route == ["deterministic"]
