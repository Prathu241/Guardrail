"""End-to-end demo, fully offline.

    python -m guardrail demo                 # the three-act demo below
    python -m guardrail demo --scenario 1|2|3  # deeper scenarios

The three acts share one system of record (a temporary database), so the closing summary,
audit log, outbox and reconciliation cover all of them:

Act 1  A valid action: the agent looks up the order and refunds the cracked lantern. It runs,
       and the payment is delivered to the provider after the transaction commits.
Act 2  A blocked hallucination: the agent refunds an order that does not exist (digits
       transposed). Nothing runs; the agent is told why, corrects itself, and the fix runs.
Act 3  A human escalation: a $649 refund is over the $500 approval limit. The run pauses, a
       manager approves it, the run resumes and the refund is re-verified before it runs.

Deeper scenarios (--scenario):
  1  A hallucinating agent (scripted) works a refund ticket; after three blocked refund attempts
     it loses autonomy and a human confirms its corrected refund.
  2  Over-limit refund: pause, human approval, resume.
  3  Chaos run: a fault injector corrupts every tool call; reports how many were stopped.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from langchain_core.messages import AIMessage

from .agent import ScriptedModel
from .app import build_app
from .cli import COLOR, C, customer_replies, print_conversation
from .config import Settings

TICKET = (
    "Hi, this is Ada Lovelace (ada@example.com). My order O-5001 arrived with a cracked "
    "lantern. Can I get a refund for the lantern? Also, how long do I have to send back "
    "the tent if I end up not liking it?"
)

SAMPLE_TICKETS = [
    ("C-1001", TICKET),
    ("C-1002", "I changed my mind about the sleeping bag on order O-5003. Can I get a refund?"),
    ("C-1002", "Grace here. The kayak from order O-5005 leaks, it's defective. I'd like a full refund."),
    ("C-1001", "How much is expedited shipping?"),
    (None, "Warehouse note: a shipment arrived, we received 10 Trail Lanterns (LNT-200)."),
    (None, "Warehouse note: write off 3 damaged Enamel Mugs (MUG-010)."),
]


def _call(name: str, n: int, **args) -> dict:
    return {"name": name, "args": args, "id": f"call_{n}", "type": "tool_call"}


def hallucinating_agent() -> ScriptedModel:
    """The kind of plausible-but-wrong trajectory a real agent produces."""
    ok_refund = dict(order_id="O-5001", customer_id="C-1001", amount=49.99, reason="Lantern arrived cracked")
    window = "Refunds are available within 30 days of delivery."
    return ScriptedModel(
        [
            # 1. Hallucinated order ID (digits transposed)
            AIMessage("Refunding the lantern.", tool_calls=[_call("issue_refund", 1, order_id="O-5010", customer_id="C-1001", amount=49.99, reason="Lantern arrived cracked")]),
            # 2. Decimal slip: 10x the price
            AIMessage("Fixing the order ID.", tool_calls=[_call("issue_refund", 2, order_id="O-5001", customer_id="C-1001", amount=499.90, reason="Lantern arrived cracked")]),
            # 3. Valid on paper (it's the tent's price) but not what the customer asked for
            AIMessage("Refunding the damaged item.", tool_calls=[_call("issue_refund", 3, order_id="O-5001", customer_id="C-1001", amount=129.99, reason="Item arrived damaged")]),
            # 4. Correct
            AIMessage("Refunding the lantern at its listed price.", tool_calls=[_call("issue_refund", 4, **ok_refund)]),
            # 5. Fabricated policy quote + typo-squatted recipient (parallel calls)
            AIMessage(
                "Answering the return-window question and confirming by email.",
                tool_calls=[
                    _call("answer_customer", 5, customer_id="C-1001",
                          message="You can return the tent within 90 days of delivery.",
                          citations=[{"policy_id": "POL-REFUND-WINDOW", "quote": "Refunds are available within 90 days of delivery."}]),
                    _call("send_email", 6, to="ada@exarnple.com", subject="Your refund",
                          body="We have refunded $49.99 for the Trail Lantern on order O-5001."),
                ],
            ),
            # 6. Corrected versions
            AIMessage(
                "Correcting both.",
                tool_calls=[
                    _call("answer_customer", 7, customer_id="C-1001",
                          message="You can return the tent within 30 days of delivery, as long as it is unused.",
                          citations=[{"policy_id": "POL-REFUND-WINDOW", "quote": window}]),
                    _call("send_email", 8, to="ada@example.com", subject="Your refund",
                          body="We have refunded $49.99 for the Trail Lantern on order O-5001."),
                ],
            ),
            # 7. Agent loops and re-issues the refund
            AIMessage("Making sure the refund went through.", tool_calls=[_call("issue_refund", 9, **ok_refund)]),
            AIMessage("All done - lantern refunded and the customer has their answer."),
        ]
    )


def _heading(text: str) -> None:
    print(f"\n{C.BOLD}{'=' * 78}\n{text}\n{'=' * 78}{C.END}")


def scenario_hallucinations(settings: Settings) -> dict:
    _heading("Scenario 1 - a hallucinating agent works a refund ticket")
    app = build_app(settings, model=hallucinating_agent())
    print(f"{C.BOLD}Customer (C-1001):{C.END} {TICKET}\n")
    conv = app.conversations.start(TICKET, "C-1001")
    print_conversation(conv)
    while conv["status"] == "awaiting_approval":
        # After 3 blocked refund attempts the agent has lost its autonomy for refunds: a human
        # now confirms even a correct one. Here the manager checks it and approves.
        seen = len(conv["guard_log"])
        for a in conv["pending_approvals"]:
            print(f"\n  {C.BOLD}morgan.lee reviews {a['id']}: ${a['args'].get('amount', 0):.2f} for the lantern is "
                  f"correct -> approve{C.END}\n")
            conv = app.approvals.decide(a["id"], True, "morgan.lee", "checked: lantern price")["conversation"]
        print_conversation(conv, since_log=seen)
    log = conv["guard_log"]
    order = app.guard.kb.order("O-5001")
    stopped = sum(e["args"].get("amount", 0) for e in log
                  if e["tool"] == "issue_refund" and not e["executed"] and app.guard.kb.order(e["args"]["order_id"]))
    print(f"\n  {len(log)} proposed actions: {sum(e['executed'] for e in log)} executed, "
          f"{sum(not e['executed'] for e in log)} stopped")
    print(f"  O-5001 refunded: ${order['refunded']:.2f} (the customer asked for one $49.99 lantern); "
          f"refund value stopped: ${stopped:.2f}")
    return conv


def scenario_approval(settings: Settings) -> dict:
    _heading("Scenario 2 - over-limit refund: pause, human approval, resume")
    app = build_app(settings)
    msg = SAMPLE_TICKETS[2][1]
    print(f"{C.BOLD}Customer (C-1002):{C.END} {msg}\n")
    conv = app.conversations.start(msg, "C-1002")
    print_conversation(conv)
    print(f"\n  conversation status: {C.YELLOW}{conv['status']}{C.END}; O-5005 refunded so far: "
          f"${app.guard.kb.order('O-5005')['refunded']:.2f}")
    approval, seen = conv["pending_approvals"][0], len(conv["guard_log"])
    print(f"  {C.BOLD}morgan.lee approves {approval['id']}{C.END} (\"verified leak photos\") -> run resumes\n")
    conv = app.approvals.decide(approval["id"], True, "morgan.lee", "verified leak photos")["conversation"]
    print_conversation(conv, since_log=seen)
    for r in customer_replies(conv):
        print(f"  {C.BOLD}Reply to customer:{C.END} {r}")
    print(f"\n  conversation status: {conv['status']}; O-5005 refunded: ${app.guard.kb.order('O-5005')['refunded']:.2f}")
    return conv


def scenario_chaos(settings: Settings, rounds: int = 2) -> dict:
    _heading("Scenario 3 - chaos run: every agent turn gets an injected hallucination")
    chaos = Settings(**{**settings.__dict__, "fault_rate": 1.0})
    app = build_app(chaos)
    for _ in range(rounds):
        for customer, message in SAMPLE_TICKETS:
            conv = app.conversations.start(message, customer)
            while conv["status"] == "awaiting_approval":
                for a in conv["pending_approvals"]:
                    res = app.approvals.decide(a["id"], True, "auto-approver", "chaos run")
                conv = res["conversation"]
    m = app.guard.audit.metrics()
    f = m["injected_faults"]
    print(f"  tickets worked: {rounds * len(SAMPLE_TICKETS)}, actions evaluated: {m['actions']}")
    print(f"  injected hallucinations stopped: {C.BOLD}{f['stopped']}/{f['total']}{C.END}")
    for name, n in sorted(f["by_fault"].items()):
        print(f"    {name:<22} {n}")
    print(f"  caught by layer: {m['caught_by']}")
    return m


# ---------------------------------------------------------------------------- the three-act demo
LANTERN = "My order O-5001 arrived with a cracked lantern. Can I get a refund for the lantern?"
TENT = "Hi, it's Alan. The tent from order O-5008 arrived torn. Please refund the tent."
KAYAK = SAMPLE_TICKETS[2][1]


def transposing_agent() -> ScriptedModel:
    """Act 2's agent: it hallucinates an order id, then corrects itself from the guard's feedback."""
    refund = dict(customer_id="C-1003", amount=129.99, reason="Tent arrived torn")
    return ScriptedModel([
        AIMessage("Refunding the tent.", tool_calls=[_call("issue_refund", 1, order_id="O-5080", **refund)]),
        AIMessage("The guardrail says O-5080 does not exist; the customer said O-5008.",
                  tool_calls=[_call("issue_refund", 2, order_id="O-5008", **refund)]),
        AIMessage("Done.", tool_calls=[_call("answer_customer", 3, customer_id="C-1003", citations=[],
                                             message="I've refunded $129.99 for the Two-Person Tent on order O-5008.")]),
        AIMessage("Refunded the tent and told the customer."),
    ])


def _act(n: int, title: str, what: str) -> None:
    _heading(f"Act {n} - {title}")
    print(f"{C.DIM}{what}{C.END}\n")


def _settle(app, label: str) -> None:
    """Deliver what the act queued, as the server's background worker would."""
    report = app.dispatcher.run_once()
    rows = app.db.all("SELECT * FROM outbox WHERE status = 'sent' AND sent_at IS NOT NULL ORDER BY rowid")
    print(f"\n  {C.BOLD}after commit{C.END}: {report['sent']} side effect(s) delivered to the providers ({label})")
    for o in rows[-report["sent"]:] if report["sent"] else []:
        print(f"    {o['channel']:<6} {o['recipient']:<10} {o['body'][:60]:<60} -> {o['provider_ref']}")


def final_demo(settings: Settings) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        shared = Settings(**{**settings.__dict__, "db_path": str(Path(tmp) / "demo.db")})
        rules = build_app(shared)

        _act(1, "a valid action runs", "Ada's lantern arrived cracked. Every value in the refund is proven: the "
             "order exists and is hers, it was delivered 11 days ago, $49.99 is the lantern's price.")
        print(f"{C.BOLD}Customer (C-1001):{C.END} {LANTERN}\n")
        conv1 = rules.conversations.start(LANTERN, "C-1001")
        print_conversation(conv1)
        for r in customer_replies(conv1):
            print(f"  {C.BOLD}Reply to customer:{C.END} {r}")
        _settle(rules, "payment + chat reply")

        _act(2, "a hallucinated action is blocked", "The agent transposes the order number. The guard blocks it "
             "before anything runs, tells the agent why, and the agent fixes its own call.")
        print(f"{C.BOLD}Customer (C-1003):{C.END} {TENT}\n")
        scripted = build_app(shared, model=transposing_agent())
        conv2 = scripted.conversations.start(TENT, "C-1003")
        print_conversation(conv2)
        feedback = next(m["text"] for m in conv2["messages"] if m["role"] == "tool" and m["status"] == "error")
        print(f"\n  {C.BOLD}what the agent was told:{C.END}\n    " + feedback.replace("\n", "\n    "))
        _settle(scripted, "only the corrected refund")

        _act(3, "a human decides", "Grace wants $649 back for a leaking kayak. It is grounded, but above the $500 "
             "limit a manager must sign off, so the run pauses with nothing changed.")
        print(f"{C.BOLD}Customer (C-1002):{C.END} {KAYAK}\n")
        conv3 = rules.conversations.start(KAYAK, "C-1002")
        print_conversation(conv3)
        [approval] = conv3["pending_approvals"]
        print(f"\n  status {C.YELLOW}{conv3['status']}{C.END}; O-5005 refunded so far: "
              f"${rules.guard.kb.order('O-5005')['refunded']:.2f}; approval {approval['id']} waives only "
              f"{', '.join(approval['waives'])}")
        print(f"  {C.BOLD}morgan.lee approves{C.END} (\"leak photos verified\") -> the run resumes, and every check "
              "runs again inside the write transaction\n")
        seen = len(conv3["guard_log"])
        conv3 = rules.approvals.decide(approval["id"], True, "morgan.lee", "leak photos verified")["conversation"]
        print_conversation(conv3, since_log=seen)
        for r in customer_replies(conv3):
            print(f"  {C.BOLD}Reply to customer:{C.END} {r}")
        _settle(rules, "payment + chat reply")

        _heading("Summary")
        audit = list(reversed(rules.guard.audit.list()))
        print(f"  {'act':<4}{'action':<30}{'decision':<23}{'caught by':<15}{'executed'}")
        acts = {conv1["id"]: "1", conv2["id"]: "2", conv3["id"]: "3"}
        for row in audit:
            if row["tool"] not in ("issue_refund", "answer_customer"):
                continue
            a = row["args"]
            what = (f"refund ${a['amount']:.2f} on {a['order_id']}" if row["tool"] == "issue_refund"
                    else f"chat reply to {a['customer_id']}")
            decision = row["decision"] + (" -> approved" if row["approval_id"] else "")
            color = COLOR.get(row["decision"], "")
            print(f"  {acts.get(row['conversation_id'], '?'):<4}{what:<30}{color}{decision:<23}{C.END}"
                  f"{row['caught_by'] or '-':<15}{'yes' if row['executed'] else 'no'}")
        recon = rules.reconciler.run()
        paid = sum(r["payload"]["amount"] for r in rules.providers["refund"].records())
        print(f"\n  audit log: {len(audit)} actions evaluated, {sum(r['executed'] for r in audit)} executed, "
              f"{sum(r['decision'] == 'block' for r in audit)} blocked, {len(rules.approvals.list(status=None))} "
              "escalated to a human")
        print(f"  payment provider paid ${paid:.2f} in {len(rules.providers['refund'].records())} refunds; "
              f"reconciliation: {len(recon['findings'])} mismatches")
        return {"conversations": [conv1, conv2, conv3], "audit": audit, "reconciliation": recon, "paid": paid}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reviewer", choices=["heuristic", "claude"], default=None)
    ap.add_argument("--scenario", type=int, choices=[1, 2, 3], help="run one of the deeper scenarios instead")
    args = ap.parse_args(argv)
    settings = Settings.from_env(db_path=":memory:", reviewer=args.reviewer, fault_rate=0.0)
    if args.scenario is None:
        final_demo(settings)
        return 0
    {1: scenario_hallucinations, 2: scenario_approval, 3: scenario_chaos}[args.scenario](settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
