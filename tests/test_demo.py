from guardrail.config import Settings
from guardrail.demo import final_demo


def test_final_demo_shows_one_valid_one_blocked_one_escalated(capsys):
    out = final_demo(Settings(db_path=":memory:"))
    refunds = [(r["args"]["order_id"], r["decision"], bool(r["executed"]), bool(r["approval_id"]))
               for r in out["audit"] if r["tool"] == "issue_refund"]
    assert refunds == [
        ("O-5001", "allow", True, False),      # act 1: valid
        ("O-5080", "block", False, False),     # act 2: hallucinated order
        ("O-5008", "allow", True, False),      #        the agent's correction
        ("O-5005", "escalate", True, True),    # act 3: over the limit, approved by a human
    ]
    assert out["paid"] == 828.98 and out["reconciliation"]["findings"] == []
    printed = capsys.readouterr().out
    assert "closest real order: O-5008" in printed and "ESCALATE -> APPROVED" in printed
