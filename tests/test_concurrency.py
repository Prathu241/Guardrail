"""Transaction races, duplicate execution, crashes and approval revocation.

These run against a file database with one connection per worker, the same locking a
multi-worker deployment gets, plus real separate processes and real process crashes.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from guardrail import AgentContext, Decision, Guardrail, KnowledgeBase, ProposedAction, RiskRouter
from guardrail.agent.agents import ScriptedModel
from guardrail.api.server import create_app
from guardrail.app import build_app
from guardrail.config import Settings
from guardrail.ledger import ApprovalError
from guardrail.models import Verdict
from guardrail.redteam import AlwaysGrounded
from guardrail.store import Database

ROOT = Path(__file__).resolve().parents[1]
KAYAK = "The kayak from order O-5005 leaks. Please refund $549 of it, I'll keep the pump."
BAG = "The sleeping bag from O-5003 arrived with a broken zip, it's defective. Refund the sleeping bag please."


def _guard(path) -> Guardrail:
    """A worker: its own connection to the shared database file."""
    return Guardrail(KnowledgeBase(Database.seeded(path)), RiskRouter(AlwaysGrounded(), AlwaysGrounded()))


def _tent(order: str, conversation: str) -> tuple[ProposedAction, AgentContext]:
    return (
        ProposedAction("issue_refund", dict(order_id=order, customer_id="C-1003", amount=129.99,
                                            reason="tent arrived torn")),
        AgentContext([f"The tent from order {order} arrived torn. Please refund the tent."], "C-1003", conversation),
    )


def _refund(order, customer, amount, said, conversation="conv-1", reason="defective"):
    return (ProposedAction("issue_refund", dict(order_id=order, customer_id=customer, amount=amount, reason=reason)),
            AgentContext([said], customer, conversation))


def _in_parallel(n: int, fn) -> list:
    barrier, out, errors = threading.Barrier(n), [None] * n, []

    def run(i):
        try:
            barrier.wait()
            out[i] = fn(i)
        except Exception as e:  # surfaced below; a worker must never crash
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    return out


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "guardrail.db"
    Database.seeded(path)
    return path


# ---------------------------------------------------------------------------- concurrent limits
def test_concurrent_refunds_respect_the_daily_cap(db_path):
    """8 workers race to refund single tents for one customer. Balances allow 5 tents ($649.95) but
    the 24-hour cap is $600, so exactly 4 may execute automatically, whatever the interleaving."""
    results = _in_parallel(8, lambda i: _guard(db_path).execute(*_tent(("O-5008", "O-5009")[i % 2], f"conv-{i}")))
    executed = [r for r in results if r.executed]
    assert len(executed) == 4
    kb = KnowledgeBase(Database(db_path))
    assert round(kb.order("O-5008")["refunded"] + kb.order("O-5009")["refunded"], 2) == 519.96
    late = [r for r in results if not r.executed]
    assert all(any("customer_daily_refund_cap" in x or "amount_within_refundable" in x for x in r.verdict.reasons)
               for r in late)


def test_concurrent_refunds_across_processes(db_path):
    """Same race with real OS processes (what several API workers would do)."""
    start = time.time() + 2.0
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(ROOT)!r}); sys.path.insert(0, {str(ROOT / "tests")!r})
        from test_concurrency import _guard, _tent
        i = int(sys.argv[1]); g = _guard({str(db_path)!r})
        time.sleep(max(0, {start} - time.time()))
        r = g.execute(*_tent(("O-5008", "O-5009")[i % 2], f"proc-{{i}}"))
        print("EXECUTED" if r.executed else "STOPPED")
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script, str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for i in range(6)]
    outs = [p.communicate(timeout=90) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1][-800:] for o in outs]
    assert sum(o[0].strip() == "EXECUTED" for o in outs) == 4
    kb = KnowledgeBase(Database(db_path))
    assert round(kb.order("O-5008")["refunded"] + kb.order("O-5009")["refunded"], 2) == 519.96


# ---------------------------------------------------------------------------- idempotency
def test_committing_the_same_verdict_twice_executes_once(db_path):
    guard = _guard(db_path)
    action = ProposedAction("answer_customer", dict(customer_id="C-1001", message="Thanks, we are looking into it.",
                                                    citations=[]))
    ctx = AgentContext(["Where is my order O-5001?"], "C-1001")
    verdict = guard.evaluate(action, ctx)
    first, second = guard.commit(verdict, ctx), guard.commit(verdict, ctx)
    assert first.executed and second.executed and second.output == first.output
    assert len(guard.db.all("SELECT * FROM outbox")) == 1
    row = guard.audit.get(action.id)
    assert row["executed"] == 1 and row["decision"] == "allow"  # the replay did not rewrite history


def test_racing_commits_of_one_verdict_execute_once(db_path):
    guard = _guard(db_path)
    action, ctx = _tent("O-5008", "conv-1")
    verdict = guard.evaluate(action, ctx)
    results = _in_parallel(6, lambda i: _guard(db_path).commit(verdict, ctx))
    assert all(r.executed for r in results)
    assert guard.kb.order("O-5008")["refunded"] == 129.99


def test_approved_action_runs_once_however_often_it_is_committed(db_path):
    guard = _guard(db_path)
    result = guard.execute(*_refund("O-5005", "C-1002", 549.0, KAYAK))
    guard.approvals.decide(result.approval_id, True, "morgan.lee")
    outs = _in_parallel(4, lambda i: _guard(db_path).commit_approval(result.approval_id))
    assert all(o.executed for o in outs)
    assert guard.kb.order("O-5005")["refunded"] == 549.0


def test_two_approved_duplicates_execute_once_through_the_agent(tmp_path):
    """The agent proposes the same over-limit refund twice in one turn; two approvers approve both at
    the same moment. The run resumes once; the second refund fails its commit-time re-check."""
    call = {"name": "issue_refund", "args": dict(order_id="O-5005", customer_id="C-1002", amount=549.0,
                                                 reason="kayak leaks"), "type": "tool_call"}
    turn = AIMessage("", tool_calls=[{**call, "id": "call_a"}, {**call, "id": "call_b"}])
    app = build_app(Settings(db_path=str(tmp_path / "g.db")), model=ScriptedModel([turn, AIMessage("Done.")]))
    conv = app.conversations.start(KAYAK, "C-1002")
    ids = [a["id"] for a in conv["pending_approvals"]]
    assert len(ids) == 2

    _in_parallel(2, lambda i: app.approvals.decide(ids[i], True, f"approver-{i}"))
    final = app.conversations.get(conv["id"])
    assert app.guard.kb.order("O-5005")["refunded"] == 549.0
    assert [e["executed"] for e in final["guard_log"]] == [True, False]
    assert final["status"] == "active" and final["messages"][-1]["text"] == "Done."


def test_sidecar_idempotency_key(tmp_path):
    client = TestClient(create_app(build_app(Settings(db_path=str(tmp_path / "g.db")))))
    body = {"action": {"tool": "issue_refund", "args": dict(order_id="O-5001", customer_id="C-1001", amount=49.99,
                                                            reason="cracked")},
            "context": {"conversation": ["My lantern on O-5001 arrived cracked, please refund the lantern."],
                        "customer_id": "C-1001", "conversation_id": "client-42"}}
    first = client.post("/v1/actions/execute", json=body, headers={"Idempotency-Key": "k-1"})
    again = client.post("/v1/actions/execute", json=body, headers={"Idempotency-Key": "k-1"})
    assert first.status_code == again.status_code == 200 and first.json()["executed"]
    assert again.json() == first.json() and again.headers["idempotent-replay"] == "true"
    other = client.post("/v1/actions/execute", json={**body, "action": {**body["action"], "rationale": "x"}},
                        headers={"Idempotency-Key": "k-1"})
    assert other.status_code == 422
    assert client.get("/v1/kb").json()["orders"][0]["refunded"] == 49.99
    assert client.get(f"/v1/actions/{first.json()['verdict']['action_id']}").json()["executed"] == 1


# ---------------------------------------------------------------------------- approvals
def test_approval_covers_only_what_the_approver_saw(db_path):
    """Approved for the per-order limit; by commit time a second refund has pushed the customer over
    the daily cap - a new reason for human review the approver never saw. It must not run."""
    guard = _guard(db_path)
    escalated = guard.execute(*_refund("O-5005", "C-1002", 549.0, KAYAK))
    assert {c.check for c in escalated.verdict.checks if c.outcome.value == "needs_approval"} == {
        "under_approval_limit"}
    guard.approvals.decide(escalated.approval_id, True, "morgan.lee")
    assert guard.execute(*_refund("O-5003", "C-1002", 89.99, BAG, conversation="conv-2")).executed

    result = guard.commit_approval(escalated.approval_id)
    assert not result.executed and guard.kb.order("O-5005")["refunded"] == 0
    assert any("customer_daily_refund_cap" in r for r in result.verdict.reasons)
    assert any("not covered by approval" in r for r in result.verdict.reasons)


def test_revoked_approval_never_executes(db_path):
    guard = _guard(db_path)
    result = guard.execute(*_refund("O-5005", "C-1002", 549.0, KAYAK))
    guard.approvals.decide(result.approval_id, True, "morgan.lee")
    guard.approvals.revoke(result.approval_id, "dana.ops", "customer withdrew the claim")
    with pytest.raises(ApprovalError):
        guard.commit_approval(result.approval_id)
    with pytest.raises(ApprovalError):
        guard.approvals.decide(result.approval_id, True, "morgan.lee")
    assert guard.kb.order("O-5005")["refunded"] == 0


def test_revocation_racing_execution_has_one_winner(db_path):
    for _ in range(5):
        Database(db_path).reset_state()
        guard = _guard(db_path)
        result = guard.execute(*_refund("O-5005", "C-1002", 549.0, KAYAK))
        guard.approvals.decide(result.approval_id, True, "morgan.lee")

        def act(i, aid=result.approval_id):
            g = _guard(db_path)
            try:
                return g.commit_approval(aid).executed if i == 0 else g.approvals.revoke(aid, "dana", "") and "revoked"
            except ApprovalError:
                return "refused"

        commit, revoke = _in_parallel(2, act)
        refunded = guard.kb.order("O-5005")["refunded"]
        status = guard.approvals.get(result.approval_id)["status"]
        # Either it ran and the revocation was refused, or it was revoked and never ran.
        assert (commit is True and revoke == "refused" and refunded == 549.0 and status == "approved") or \
               (commit == "refused" and revoke == "revoked" and refunded == 0 and status == "revoked")


def test_executed_approval_cannot_be_revoked(db_path):
    guard = _guard(db_path)
    result = guard.execute(*_refund("O-5005", "C-1002", 549.0, KAYAK))
    guard.approvals.decide(result.approval_id, True, "morgan.lee")
    assert guard.commit_approval(result.approval_id).executed
    with pytest.raises(ApprovalError, match="already executed"):
        guard.approvals.revoke(result.approval_id, "dana.ops")


def test_stale_approvals_expire(db_path):
    guard = _guard(db_path)
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat(timespec="seconds")
    pending = guard.execute(*_refund("O-5005", "C-1002", 549.0, KAYAK))
    guard.db.update("approvals", "id", pending.approval_id, {"created_at": old})
    with pytest.raises(ApprovalError, match="expired"):
        guard.approvals.decide(pending.approval_id, True, "morgan.lee")
    assert guard.approvals.get(pending.approval_id)["status"] == "expired"

    approved = guard.execute(*_refund("O-5005", "C-1002", 549.0, KAYAK, conversation="conv-2"))
    guard.approvals.decide(approved.approval_id, True, "morgan.lee")
    guard.db.update("approvals", "id", approved.approval_id, {"created_at": old})
    with pytest.raises(ApprovalError, match="expired"):
        guard.commit_approval(approved.approval_id)
    assert guard.kb.order("O-5005")["refunded"] == 0


def test_revoked_approval_in_an_agent_run(tmp_path):
    call = {"name": "issue_refund", "args": dict(order_id="O-5005", customer_id="C-1002", amount=549.0,
                                                 reason="kayak leaks"), "type": "tool_call"}
    turn = AIMessage("", tool_calls=[{**call, "id": "call_a"},
                                     {**call, "id": "call_b", "args": {**call["args"], "amount": 520.0}}])
    app = build_app(Settings(db_path=str(tmp_path / "g.db")), model=ScriptedModel([turn, AIMessage("Done.")]))
    conv = app.conversations.start(KAYAK + " Or $520 if that is easier.", "C-1002")
    first, second = (a["id"] for a in conv["pending_approvals"])
    app.approvals.decide(first, True, "morgan.lee")       # approved, but the run waits for the second
    app.approvals.revoke(first, "dana.ops", "duplicate claim")
    out = app.approvals.decide(second, False, "morgan.lee", "no")
    assert app.guard.kb.order("O-5005")["refunded"] == 0
    tool_texts = [m["text"] for m in out["conversation"]["messages"] if m["role"] == "tool"]
    assert tool_texts[0].startswith("REVOKED by dana.ops") and tool_texts[1].startswith("REJECTED")


# ---------------------------------------------------------------------------- crashes
def _crash(db_path, body: str) -> int:
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(ROOT)!r}); sys.path.insert(0, {str(ROOT / "tests")!r})
        from test_concurrency import _guard, _refund, _tent
        from guardrail.app import build_app
        from guardrail.config import Settings
        g = _guard({str(db_path)!r})
    """) + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=90).returncode


def test_crash_inside_the_write_leaves_nothing_behind(db_path):
    code = _crash(db_path, """
        spec = g.tools["issue_refund"]; real = spec.fn
        def dies_mid_write(**a):
            real(**a)
            os._exit(17)          # process killed after the UPDATE, before COMMIT
        spec.fn = dies_mid_write
        g.execute(*_tent("O-5008", "conv-1"))
    """)
    assert code == 17
    guard = _guard(db_path)
    assert guard.kb.order("O-5008")["refunded"] == 0
    [row] = guard.audit.list()
    assert row["executed"] == 0 and row["decision"] == "allow"
    assert guard.execute(*_tent("O-5008", "conv-1")).executed  # the retry runs exactly once
    assert guard.kb.order("O-5008")["refunded"] == 129.99


def test_crash_after_commit_is_not_redone(db_path):
    marker = str(db_path) + ".action"
    code = _crash(db_path, f"""
        action, ctx = _tent("O-5008", "conv-1")
        v = g.evaluate(action, ctx)
        open({marker!r}, "w").write(action.id)
        g.commit(v, ctx)
        os._exit(18)              # committed, but the caller never learned it
    """)
    assert code == 18
    guard = _guard(db_path)
    action, ctx = _tent("O-5008", "conv-1")
    replayed = ProposedAction(action.tool, action.args, "", Path(marker).read_text())
    result = guard.commit(Verdict(replayed, Decision.ALLOW), ctx)  # e.g. the caller retries
    assert result.executed and guard.kb.order("O-5008")["refunded"] == 129.99


def test_agent_run_killed_mid_execute_recovers_without_double_execution(tmp_path):
    db = tmp_path / "g.db"
    code = _crash(db, f"""
        from langchain_core.messages import AIMessage
        from guardrail.agent import graph as graph_mod
        from guardrail.agent.agents import ScriptedModel
        call = {{"name": "issue_refund", "id": "call_a", "type": "tool_call",
                "args": dict(order_id="O-5005", customer_id="C-1002", amount=549.0, reason="kayak leaks")}}
        app = build_app(Settings(db_path={str(db)!r}),
                        model=ScriptedModel([AIMessage("", tool_calls=[call]), AIMessage("Done.")]))
        conv = app.conversations.start({KAYAK!r}, "C-1002")
        open({str(db) + ".conv"!r}, "w").write(conv["id"])
        real = graph_mod.feedback_for_agent
        def dies(*a, **k):
            os._exit(19)          # the refund committed; the graph never checkpointed the result
        graph_mod.feedback_for_agent = dies
        app.approvals.decide(conv["pending_approvals"][0]["id"], True, "morgan.lee")
    """)
    assert code == 19
    conv_id = Path(str(db) + ".conv").read_text()
    app = build_app(Settings(db_path=str(db)), model=ScriptedModel([AIMessage("Done.")]))
    assert app.guard.kb.order("O-5005")["refunded"] == 549.0  # committed before the crash
    assert app.conversations.recover_incomplete() == []  # within the lease: maybe a live worker
    recovered = app.conversations.recover_incomplete(stale_after=0)
    assert recovered == [conv_id]
    conv = app.conversations.get(conv_id)
    assert app.guard.kb.order("O-5005")["refunded"] == 549.0  # not refunded again
    assert conv["guard_log"][-1]["executed"] and conv["messages"][-1]["text"] == "Done."
    assert app.conversations.recover_incomplete(stale_after=0) == []


def test_existing_database_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)  # the approvals/audit tables as they shipped before these columns
    conn.executescript("""
        CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, conversation_id TEXT NOT NULL,
            action_id TEXT NOT NULL UNIQUE, tool TEXT NOT NULL, args TEXT NOT NULL, rationale TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL, caught_by TEXT, risk REAL, route TEXT NOT NULL, reasons TEXT NOT NULL,
            warnings TEXT NOT NULL, checks TEXT NOT NULL, reviews TEXT NOT NULL, review_cost REAL NOT NULL DEFAULT 0,
            executed INTEGER NOT NULL DEFAULT 0, output TEXT, approval_id TEXT, injected_fault TEXT);
        CREATE TABLE approvals (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, conversation_id TEXT NOT NULL,
            action_id TEXT NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL, rationale TEXT NOT NULL,
            context TEXT NOT NULL, reasons TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', decided_by TEXT,
            note TEXT, decided_at TEXT, result TEXT);
    """)
    conn.close()
    guard = _guard(path)
    result = guard.execute(*_refund("O-5005", "C-1002", 549.0, KAYAK))
    guard.approvals.decide(result.approval_id, True, "morgan.lee")
    assert guard.commit_approval(result.approval_id).executed
    assert guard.audit.get(result.verdict.action.id)["executed_at"]
