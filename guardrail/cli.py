"""Command-line entry point: ``guardrail <command>`` (or ``python -m guardrail``)."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .app import App, build_app
from .config import Settings


class C:
    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    RED, GREEN, YELLOW, PURPLE, DIM, BOLD, END = (
        ("\033[31m", "\033[32m", "\033[33m", "\033[35m", "\033[2m", "\033[1m", "\033[0m") if on else ("",) * 7
    )


COLOR = {"allow": C.GREEN, "block": C.RED, "escalate": C.YELLOW}


def print_guard_entry(e: dict) -> None:
    color = COLOR.get(e["decision"], "")
    args = json.dumps(e["args"], ensure_ascii=False)
    args = args if len(args) <= 110 else args[:107] + "..."
    status = "executed" if e.get("executed") else "not executed"
    fault = f" {C.PURPLE}[injected fault: {e['injected_fault']}]{C.END}" if e.get("injected_fault") else ""
    print(f"  {C.BOLD}{e['tool']}{C.END} {C.DIM}{args}{C.END}{fault}")
    risk = f"risk {e['risk']:.2f}" if e.get("risk") is not None else "risk n/a"
    print(f"    {color}{C.BOLD}{e['decision'].upper()}{C.END} ({status})  {C.DIM}{risk} | {' -> '.join(e['route'])}{C.END}")
    if e.get("approval"):
        a = e["approval"]
        print(f"    {C.YELLOW}approval {a['id']}: {a['status']} by {a['decided_by']}{C.END}" + (f" - {a['note']}" if a.get("note") else ""))
    if not e.get("executed"):
        for r in e["reasons"]:
            print(f"    {color}x{C.END} {r}")


def print_conversation(conv: dict, since_log: int = 0) -> None:
    for e in conv["guard_log"][since_log:]:
        print_guard_entry(e)
    if conv["status"] == "awaiting_approval":
        for a in conv["pending_approvals"]:
            print(f"  {C.YELLOW}{C.BOLD}PAUSED{C.END}{C.YELLOW}: {a['tool']} needs approval ({a['id']}){C.END}")
            for r in a["reasons"]:
                print(f"    {C.YELLOW}!{C.END} {r}")


def customer_replies(conv: dict) -> list[str]:
    calls = {c["id"]: c for m in conv["messages"] if m["role"] == "agent" for c in m["tool_calls"]}
    return [
        calls[m["tool_call_id"]]["args"]["message"]
        for m in conv["messages"]
        if m["role"] == "tool" and m["name"] == "answer_customer" and m["status"] == "success"
    ]


# ---------------------------------------------------------------------------- commands
def cmd_serve(app: App, args) -> int:
    import uvicorn

    from .api.server import create_app

    print(f"Dashboard: http://{args.host}:{args.port}/   API docs: http://{args.host}:{args.port}/docs")
    uvicorn.run(create_app(app), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_init_db(app: App, args) -> int:
    if args.reset:
        app.db.reset_state()
        print(f"reset {app.settings.db_path}: reseeded records, cleared audit/approvals/conversations")
    else:
        print(f"{app.settings.db_path} ready ({len(app.guard.kb.all_orders())} orders)")
    return 0


def cmd_ask(app: App, args) -> int:
    conv = app.conversations.start(args.message, args.customer)
    print(f"{C.DIM}conversation {conv['id']}{C.END}")
    print_conversation(conv)
    for r in customer_replies(conv):
        print(f"{C.BOLD}Reply to customer:{C.END} {r}")
    return 0


def cmd_chat(app: App, args) -> int:
    print(f"Chatting as {args.customer or 'internal operations'}. Commands: /approve [note], /reject [note], /quit")
    conv, seen, replied = None, 0, 0
    while True:
        try:
            line = input(f"{C.BOLD}you>{C.END} ").strip()
        except EOFError:
            return 0
        if not line:
            continue
        if line == "/quit":
            return 0
        if line.startswith(("/approve", "/reject")):
            if not conv or not conv["pending_approvals"]:
                print("nothing is waiting for approval")
                continue
            note = line.split(" ", 1)[1] if " " in line else ""
            for a in conv["pending_approvals"]:
                res = app.approvals.decide(a["id"], line.startswith("/approve"), args.reviewer_name, note)
            conv = res["conversation"] or app.conversations.get(conv["id"])
        elif conv is None:
            conv = app.conversations.start(line, args.customer)
        else:
            try:
                conv = app.conversations.send(conv["id"], line)
            except Exception as e:  # noqa: BLE001
                print(f"{C.RED}{e}{C.END}")
                continue
        print_conversation(conv, seen)
        seen = len(conv["guard_log"])
        replies = customer_replies(conv)
        for r in replies[replied:]:
            print(f"{C.BOLD}agent>{C.END} {r}")
        replied = len(replies)


def cmd_approvals(app: App, args) -> int:
    if args.action == "list":
        rows = app.approvals.list(None if args.status == "all" else args.status)
        for a in rows:
            print(f"{a['id']}  {a['status']:<9} {a['tool']} {json.dumps(a['args'])}  ({a['conversation_id']})")
            for r in a["reasons"]:
                print(f"    {r}")
        if not rows:
            print("no approvals")
        return 0
    if args.action == "revoke":
        res = app.approvals.revoke(args.id, args.reviewer_name, args.note)
        print(f"{res['approval']['id']}: revoked by {res['approval']['revoked_by']}")
    else:
        res = app.approvals.decide(args.id, args.action == "approve", args.reviewer_name, args.note)
        print(f"{res['approval']['id']}: {res['approval']['status']} by {res['approval']['decided_by']}")
    if res["conversation"]:
        print_conversation(res["conversation"])
    return 0


def cmd_audit(app: App, args) -> int:
    for r in reversed(app.guard.audit.list(args.conversation, args.decision, args.limit)):
        e = {**r, "executed": bool(r["executed"]), "approval": None}
        print(f"{C.DIM}{r['ts']} {r['conversation_id']}{C.END}")
        print_guard_entry(e)
    return 0


def cmd_metrics(app: App, args) -> int:
    print(json.dumps(app.guard.audit.metrics(), indent=2))
    return 0


def cmd_chaos(app: App, args) -> int:
    from .demo import SAMPLE_TICKETS

    for _ in range(args.rounds):
        for customer, message in SAMPLE_TICKETS:
            conv = app.conversations.start(message, customer)
            while conv["status"] == "awaiting_approval":  # auto-decide so the run completes
                for a in conv["pending_approvals"]:
                    res = app.approvals.decide(a["id"], True, "chaos-auto-approver", "auto-approved by chaos run")
                conv = res["conversation"]
    f = app.guard.audit.metrics()["injected_faults"]
    print(json.dumps(f, indent=2))
    return 0 if f["stopped"] == f["total"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="guardrail", description="Pre-execution guardrails for agent tool calls.")
    ap.add_argument("--db", help="SQLite path (default: $GUARDRAIL_DB_PATH or guardrail.db; ':memory:' for scratch)")
    ap.add_argument("--reviewer", choices=["heuristic", "claude"])
    ap.add_argument("--agent", choices=["rules", "claude"])
    ap.add_argument("--fault-rate", type=float, help="probability of injecting a hallucination per agent turn")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the HTTP API and dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p = sub.add_parser("init-db", help="create/seed the database")
    p.add_argument("--reset", action="store_true", help="reseed records and clear all guardrail state")
    p = sub.add_parser("ask", help="send one message to the guarded agent")
    p.add_argument("message")
    p.add_argument("--customer", help="authenticated customer id (omit for internal operations)")
    p = sub.add_parser("chat", help="interactive conversation with the guarded agent")
    p.add_argument("--customer")
    p.add_argument("--reviewer-name", dest="reviewer_name", default="cli-reviewer")
    p = sub.add_parser("approvals", help="list, decide or revoke escalated actions")
    p.add_argument("action", choices=["list", "approve", "reject", "revoke"])
    p.add_argument("id", nargs="?")
    p.add_argument("--status", default="pending", help="pending | approved | rejected | revoked | expired | all")
    p.add_argument("--reviewer-name", dest="reviewer_name", default="cli-reviewer")
    p.add_argument("--note", default="")
    p = sub.add_parser("audit", help="print the audit log")
    p.add_argument("--conversation")
    p.add_argument("--decision", choices=["allow", "block", "escalate"])
    p.add_argument("--limit", type=int, default=50)
    sub.add_parser("metrics", help="print aggregate metrics")
    p = sub.add_parser("chaos", help="run sample tickets with fault injection and report the catch rate")
    p.add_argument("--rounds", type=int, default=3)
    sub.add_parser("demo", help="scripted end-to-end demo (in-memory, offline)")
    p = sub.add_parser("redteam", help="run the red-team suite")
    p.add_argument("--mode", default="routed")
    p.add_argument("--ablation", action="store_true")

    args, rest = ap.parse_known_args(argv)
    if args.command == "demo":
        from .demo import main as demo_main

        return demo_main(rest)
    if args.command == "redteam":
        from .redteam import main as redteam_main

        return redteam_main(["--mode", args.mode] + (["--ablation"] if args.ablation else [])
                            + (["--reviewer", args.reviewer] if args.reviewer else []) + rest)
    if args.command == "approvals" and args.action != "list" and not args.id:
        ap.error(f"approvals {args.action} needs an approval id")
    if args.command == "chaos":  # a throwaway database, every turn corrupted unless told otherwise
        args.db = args.db or ":memory:"
        args.fault_rate = 1.0 if args.fault_rate is None else args.fault_rate

    settings = Settings.from_env(db_path=args.db, reviewer=args.reviewer, agent=args.agent, fault_rate=args.fault_rate)
    app = build_app(settings)
    commands = {
        "serve": cmd_serve, "init-db": cmd_init_db, "ask": cmd_ask, "chat": cmd_chat, "approvals": cmd_approvals,
        "audit": cmd_audit, "metrics": cmd_metrics, "chaos": cmd_chaos,
    }
    return commands[args.command](app, args)


if __name__ == "__main__":
    raise SystemExit(main())
