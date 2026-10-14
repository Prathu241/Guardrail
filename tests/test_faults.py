from guardrail.agent.faults import (
    decimal_slip,
    inflated_number,
    sign_flip,
    transposed_order_id,
    typosquat_recipient,
    wrong_customer,
)
from guardrail.app import build_app
from guardrail.config import Settings
from guardrail.demo import SAMPLE_TICKETS


def test_fault_functions():
    assert transposed_order_id({"order_id": "O-5001"})["order_id"] == "O-5010"
    assert transposed_order_id({"order_id": "O-5011"}) is None
    assert decimal_slip({"amount": 49.99})["amount"] == 499.9
    assert wrong_customer({"customer_id": "C-1001"})["customer_id"] == "C-1002"
    assert typosquat_recipient({"to": "ada@example.com"})["to"] == "ada@exarnple.com"
    assert sign_flip({"delta": 10})["delta"] == -10
    out = inflated_number({"message": "within 30 days", "citations": [{"policy_id": "P", "quote": "30 days"}]})
    assert out["message"] == "within 90 days" and out["citations"][0]["quote"] == "90 days"


def test_every_injected_fault_is_stopped_and_agent_recovers():
    app = build_app(Settings(db_path=":memory:", fault_rate=1.0, fault_seed=11))
    for customer, message in SAMPLE_TICKETS:
        conv = app.conversations.start(message, customer)
        while conv["status"] == "awaiting_approval":
            for a in conv["pending_approvals"]:
                res = app.approvals.decide(a["id"], True, "auto")
            conv = res["conversation"]
    faults = app.guard.audit.metrics()["injected_faults"]
    assert faults["total"] >= len(SAMPLE_TICKETS)
    assert faults["stopped"] == faults["total"]
    # ... and the corrected actions still produced the right end state
    kb = app.guard.kb
    assert kb.order("O-5001")["refunded"] == 49.99
    assert kb.order("O-5003")["refunded"] == 109.97
    assert kb.order("O-5005")["refunded"] == 649.0
    assert kb.product("LNT-200")["stock"] == 44 and kb.product("MUG-010")["stock"] == 137
