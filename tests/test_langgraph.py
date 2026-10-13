from langchain_core.messages import HumanMessage, ToolMessage

from guardrail.demo import TICKET, hallucinating_agent
from guardrail.integrations.langgraph import build_guarded_agent, langchain_tools


def test_demo_trajectory(guard):
    app = build_guarded_agent(hallucinating_agent(), guard)
    final = app.invoke({"messages": [HumanMessage(TICKET)], "customer_id": "C-1001", "guard_log": []})
    decisions = [(e["tool"], e["decision"]) for e in final["guard_log"]]
    assert decisions == [
        ("issue_refund", "block"),     # hallucinated order id
        ("issue_refund", "block"),     # 10x amount
        ("issue_refund", "block"),     # tent price for a lantern request (careful reviewer)
        ("issue_refund", "allow"),
        ("answer_customer", "block"),  # fabricated quote
        ("send_email", "block"),       # typo-squatted domain
        ("answer_customer", "allow"),
        ("send_email", "allow"),
        ("issue_refund", "block"),     # duplicate
    ]
    # Only the correct refund reached the system of record.
    assert guard.kb.order("O-5001")["refunded"] == 49.99

    tool_msgs = [m for m in final["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 9
    blocked = [m for m in tool_msgs if m.status == "error"]
    assert len(blocked) == 6 and all("NOT performed" in m.content for m in blocked)


def test_bound_tools_cannot_bypass_guard(guard):
    tools = {t.name: t for t in langchain_tools(guard)}
    assert set(tools) == set(guard.tools)
    try:
        tools["issue_refund"].func(order_id="O-5001", customer_id="C-1001", amount=1.0, reason="x")
    except RuntimeError:
        pass
    else:
        raise AssertionError("direct tool call should be refused")
    assert guard.kb.order("O-5001")["refunded"] == 0
