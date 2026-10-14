"""External side effects: outbox delivery after commit, retries with idempotency keys, compensation
and reconciliation with the providers."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from guardrail import AgentContext, Guardrail, KnowledgeBase, ProposedAction, RiskRouter
from guardrail.api.server import create_app
from guardrail.app import build_app
from guardrail.config import Settings
from guardrail.outbox import Dispatcher, Reconciler, simulated_providers
from guardrail.redteam import AlwaysGrounded
from guardrail.store import Database

LANTERN = "My order O-5001 arrived with a cracked lantern. Can I get a refund for the lantern?"


@pytest.fixture
def env(tmp_path):
    """A guard, its providers, a dispatcher (no backoff, so retries are immediate) and a reconciler."""
    path = str(tmp_path / "g.db")
    db = Database.seeded(path)
    guard = Guardrail(KnowledgeBase(db), RiskRouter(AlwaysGrounded(), AlwaysGrounded()))
    providers = simulated_providers(path)
    return {"path": path, "db": db, "guard": guard, "providers": providers,
            "payments": providers["refund"], "messages": providers["email"],
            "dispatcher": Dispatcher(db, providers, max_attempts=3, backoff_seconds=0),
            "reconciler": Reconciler(db, providers)}


def _refund(guard, amount=49.99, conversation="conv-1"):
    action = ProposedAction("issue_refund", dict(order_id="O-5001", customer_id="C-1001", amount=amount,
                                                 reason="cracked lantern"))
    return guard.execute(action, AgentContext([LANTERN], "C-1001", conversation))


def _reply(guard, text="Thanks, we are looking into it."):
    action = ProposedAction("answer_customer", dict(customer_id="C-1001", message=text, citations=[]))
    return guard.execute(action, AgentContext(["Where is my order O-5001?"], "C-1001"))


def _outbox(db, channel=None):
    rows = db.all("SELECT * FROM outbox ORDER BY ts, rowid")
    return [r for r in rows if channel is None or r["channel"] == channel]


# ---------------------------------------------------------------------------- outbox
def test_refund_is_recorded_in_the_transaction_and_paid_after_commit(env):
    result = _refund(env["guard"])
    assert result.executed
    [intent] = _outbox(env["db"], "refund")
    assert intent["status"] == "pending" and intent["action_id"] == result.verdict.action.id
    assert intent["payload"] == {"order_id": "O-5001", "customer_id": "C-1001", "amount": 49.99}
    assert env["payments"].records() == []  # nothing left the building during the transaction

    assert env["dispatcher"].run_once()["sent"] == 1
    [paid] = env["payments"].records()
    assert paid["key"] == intent["id"] and paid["payload"]["amount"] == 49.99
    assert _outbox(env["db"], "refund")[0]["status"] == "sent"


def test_a_rolled_back_action_leaves_no_side_effect_behind(env):
    spec = env["guard"].tools["issue_refund"]
    real = spec.fn

    def breaks_after_queueing(**args):
        real(**args)
        raise RuntimeError("tool bug after queueing the payment")

    spec.fn = breaks_after_queueing
    assert not _refund(env["guard"]).executed
    assert _outbox(env["db"]) == [] and env["guard"].kb.order("O-5001")["refunded"] == 0
    env["dispatcher"].run_once()
    assert env["payments"].records() == []


def test_transient_failures_retry_with_the_same_key(env):
    _reply(env["guard"])
    env["messages"].faults.extend(["unavailable", "unavailable"])
    reports = [env["dispatcher"].run_once() for _ in range(3)]
    assert [r["retrying"] for r in reports] == [1, 1, 0] and reports[2]["sent"] == 1
    [row] = _outbox(env["db"])
    assert row["attempts"] == 3 and env["messages"].calls == [row["id"]] * 3
    assert len(env["messages"].records()) == 1


def test_lost_response_is_not_sent_twice(env):
    _refund(env["guard"])
    env["payments"].faults.append("lost_response")  # paid, but we never hear back
    assert env["dispatcher"].run_once()["retrying"] == 1
    assert env["dispatcher"].run_once()["sent"] == 1  # the retry carries the same key
    assert len(env["payments"].records()) == 1


def test_rejected_refund_is_compensated(env):
    _refund(env["guard"])
    assert env["guard"].kb.order("O-5001")["refunded"] == 49.99
    env["payments"].faults.append("reject")
    assert env["dispatcher"].run_once()["failed"] == 1
    assert _outbox(env["db"], "refund")[0]["status"] == "compensated"
    assert env["guard"].kb.order("O-5001")["refunded"] == 0  # the reservation is released
    [incident] = env["db"].all("SELECT * FROM incidents")
    assert incident["kind"] == "refund_failed" and "Tell the customer" in incident["detail"]["note"]


def test_failed_message_is_flagged_not_compensated(env):
    _reply(env["guard"])
    env["messages"].faults.extend(["unavailable"] * 3)
    for _ in range(3):
        env["dispatcher"].run_once()
    assert _outbox(env["db"])[0]["status"] == "failed"
    assert env["db"].one("SELECT kind FROM incidents")["kind"] == "message_failed"


def test_backoff_delays_the_retry(env):
    _reply(env["guard"])
    dispatcher = Dispatcher(env["db"], env["providers"], backoff_seconds=60)
    env["messages"].faults.append("unavailable")
    assert dispatcher.run_once()["retrying"] == 1
    assert dispatcher.run_once() == {"sent": 0, "retrying": 0, "failed": 0, "recovered": 0}  # not due yet
    assert _outbox(env["db"])[0]["next_attempt_at"] > datetime.now(timezone.utc).isoformat()


@pytest.mark.parametrize("provider_acted", [True, False])
def test_worker_dying_mid_delivery_is_recovered(env, provider_acted):
    _refund(env["guard"])
    [row] = _outbox(env["db"])
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    env["db"].update("outbox", "id", row["id"], {"status": "sending", "attempts": 1, "claimed_at": stale})
    if provider_acted:
        env["payments"].send(row["id"], "refund", row["payload"])
    report = env["dispatcher"].run_once()
    assert report["recovered"] == 1
    assert len(env["payments"].records()) == 1  # either confirmed, or re-sent exactly once
    assert _outbox(env["db"])[0]["status"] == "sent"


def test_concurrent_dispatchers_deliver_each_entry_once(env):
    for name in "abcdefghijklmnopqrst":
        assert _reply(env["guard"], f"Update {name}: we are looking into it.").executed
    barrier = threading.Barrier(4)

    def worker():
        d = Dispatcher(Database(env["path"]), env["providers"])
        barrier.wait()
        d.run_once()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(env["messages"].calls) == 20 == len(set(env["messages"].calls))  # each sent once, by one worker
    assert {r["status"] for r in _outbox(env["db"])} == {"sent"}


# ---------------------------------------------------------------------------- reconciliation
def test_clean_books_reconcile_with_no_findings(env):
    _refund(env["guard"])
    _reply(env["guard"])
    env["dispatcher"].run_once()
    report = env["reconciler"].run()
    assert report["findings"] == [] and report["checked"] == 2 and report["open_incidents"] == 0


def test_money_moved_outside_the_guardrail_is_flagged(env):
    env["payments"].record_external("manual-1", "refund", {"order_id": "O-5002", "customer_id": "C-1001",
                                                            "amount": 64.5})
    [finding] = env["reconciler"].run()["findings"]
    assert finding["kind"] == "unknown_provider_record"


def test_provider_record_missing_or_different_is_flagged(env):
    _refund(env["guard"])
    _reply(env["guard"])
    env["dispatcher"].run_once()
    refund, reply = _outbox(env["db"], "refund")[0], _outbox(env["db"], "chat")[0]
    env["payments"]._conn.execute("UPDATE records SET payload = ? WHERE key = ?",
                                  ('{"amount": 499.9, "customer_id": "C-1001", "order_id": "O-5001"}', refund["id"]))
    env["messages"]._conn.execute("DELETE FROM records WHERE key = ?", (reply["id"],))
    kinds = {f["kind"] for f in env["reconciler"].run()["findings"]}
    assert kinds == {"payload_mismatch", "missing_at_provider"}
    assert env["reconciler"].run()["open_incidents"] == 2  # re-running does not duplicate incidents


def test_delivery_we_gave_up_on_is_confirmed_and_reserved_again(env):
    """Every attempt's response was lost, so the dispatcher gave up and released the reservation, but
    the provider did pay. Reconciliation must believe the provider, or $49.99 looks refundable again."""
    _refund(env["guard"])
    env["payments"].faults.extend(["lost_response"] * 3)
    for _ in range(3):
        env["dispatcher"].run_once()
    assert env["guard"].kb.order("O-5001")["refunded"] == 0
    [finding] = env["reconciler"].run()["findings"]
    assert finding["kind"] == "delivered_after_failure"
    assert env["guard"].kb.order("O-5001")["refunded"] == 49.99
    assert _outbox(env["db"], "refund")[0]["status"] == "sent"


def test_unanswered_delivery_is_confirmed_from_the_provider(env):
    _refund(env["guard"])
    [row] = _outbox(env["db"])
    env["payments"].send(row["id"], "refund", row["payload"])  # the provider has it, we never marked it
    [finding] = env["reconciler"].run()["findings"]
    assert finding == {"kind": "confirmed_by_provider", "ref": row["id"], "resolved": True}
    assert _outbox(env["db"])[0]["status"] == "sent"


def test_refund_ledger_is_checked_against_the_audit_log(env):
    result = _refund(env["guard"])
    env["db"].run("DELETE FROM outbox")  # a refund decision with no payment intent
    env["db"].insert("outbox", {"id": "ref-orphan", "ts": "2026-10-01T00:00:00+00:00", "channel": "refund",
                                "recipient": "C-1001", "subject": "O-5007", "body": "", "status": "pending",
                                "payload": {"order_id": "O-5007", "customer_id": "C-1001", "amount": 64.5},
                                "action_id": "act-not-a-decision"})
    findings = {(f["kind"], f["ref"]) for f in env["reconciler"].run()["findings"]}
    assert findings == {("refund_without_intent", result.verdict.action.id),
                        ("intent_without_decision", "ref-orphan")}


# ---------------------------------------------------------------------------- service level
def test_server_worker_delivers_in_the_background(tmp_path):
    app = build_app(Settings(db_path=str(tmp_path / "g.db"), worker_interval=0.05, reconcile_interval=0.1))
    with TestClient(create_app(app)) as client:
        conv = client.post("/v1/conversations", json={"customer_id": "C-1001", "message": LANTERN}).json()
        assert any(e["tool"] == "issue_refund" and e["executed"] for e in conv["guard_log"])
        deadline = time.time() + 5
        while time.time() < deadline and client.get("/v1/outbox?status=pending").json():
            time.sleep(0.05)
        health = client.get("/healthz").json()
        assert health["worker"] == "running" and health["worker_errors"] == []
        assert health["outbox"] == {"sent": len(client.get("/v1/outbox").json())}
        assert client.post("/v1/reconcile").json()["findings"] == []


def test_operator_endpoints(tmp_path):
    app = build_app(Settings(db_path=str(tmp_path / "g.db")))
    client = TestClient(create_app(app))
    client.post("/v1/conversations", json={"customer_id": "C-1001", "message": LANTERN})
    app.providers["refund"].faults.append("reject")
    out = client.post("/v1/outbox/dispatch").json()
    assert out["failed"] == 1 and out["sent"] >= 1
    [incident] = client.get("/v1/incidents").json()
    assert incident["kind"] == "refund_failed"
    assert client.get("/v1/metrics").json()["open_incidents"] == {"refund_failed": 1}
    resolved = client.post(f"/v1/incidents/{incident['id']}/resolve",
                           json={"reviewer": "dana.ops", "note": "paid by bank transfer"}).json()
    assert resolved["status"] == "resolved" and client.get("/v1/incidents").json() == []
