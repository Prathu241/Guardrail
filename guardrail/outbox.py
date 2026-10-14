"""External side effects: transactional outbox, delivery with idempotency keys, compensation and
reconciliation with the providers.

A database transaction can be rolled back; a payment sent by a provider or an email that left
the building cannot. So no tool talks to a provider. Inside the guarded write transaction a tool
records its *intent* in the outbox (and, for refunds, reserves the amount on the order). Only
after that transaction commits does the dispatcher deliver it:

    pending --claim--> sending --provider ok--> sent
       ^                  |--transient error--> pending (retry with backoff, same idempotency key)
       |                  '--permanent error / attempts exhausted--> failed  (+ compensation, incident)
       '--- stale "sending" (worker died mid-call): ask the provider by key -> sent | pending

* Every delivery carries the outbox id as its idempotency key, so a retry after a timeout or a
  lost response can never send twice (the provider returns the original result).
* A refund the provider rejects is compensated: the reservation is released and an incident is
  opened so a person can tell the customer. A failed message cannot be compensated, only flagged.
* The reconciler compares the outbox with each provider's own records and the audit log, and
  opens incidents for anything that does not match, in either direction (including money that
  moved without a guardrail decision).
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .store import Database, now

PAYMENT_CHANNELS = ("refund",)
MESSAGE_CHANNELS = ("email", "chat")


class ProviderError(Exception):
    pass


class TransientProviderError(ProviderError):
    """Timeout, outage, rate limit, lost response: safe to retry with the same idempotency key."""


class PermanentProviderError(ProviderError):
    """The provider refused (closed account, invalid recipient): retrying will not help."""


class Provider(Protocol):
    name: str

    def send(self, key: str, channel: str, payload: dict) -> str:
        """Perform the side effect once per ``key``; return the provider's reference."""

    def lookup(self, key: str) -> dict | None: ...

    def records(self) -> list[dict]: ...


class SimulatedProvider:
    """A stand-in payment or messaging provider with its own durable ledger (a separate database,
    because it is not ours to roll back) and scriptable faults for tests and drills.

    faults: "unavailable" (fails before acting), "reject" (permanent refusal),
            "lost_response" (acts, then the response is lost: the caller sees a timeout)
    """

    def __init__(self, name: str, path: str = ":memory:"):
        self.name = name
        self.faults: deque[str] = deque()
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, ref TEXT NOT NULL, "
                           "channel TEXT NOT NULL, payload TEXT NOT NULL, ts TEXT NOT NULL)")
        self._refs = itertools.count(1 + self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])

    def send(self, key: str, channel: str, payload: dict) -> str:
        with self._lock:
            self.calls.append(key)
            fault = self.faults.popleft() if self.faults else None
            if fault == "unavailable":
                raise TransientProviderError(f"{self.name} unavailable")
            if fault == "reject":
                raise PermanentProviderError(f"{self.name} rejected the request")
            existing = self.lookup(key)
            if existing:  # idempotent: the same key never acts twice
                ref = existing["ref"]
            else:
                ref = f"{self.name}_{next(self._refs):06d}"
                self._conn.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)",
                                   (key, ref, channel, json.dumps(payload, sort_keys=True), now()))
            if fault == "lost_response":
                raise TransientProviderError(f"{self.name} timed out (the request may have succeeded)")
            return ref

    def lookup(self, key: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM records WHERE key = ?", (key,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def records(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM records ORDER BY ts, rowid").fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def clear(self) -> None:
        """Development reset only: forget every simulated record."""
        with self._lock:
            self._conn.execute("DELETE FROM records")

    def record_external(self, key: str, channel: str, payload: dict) -> None:
        """Test hook: a transaction made directly at the provider, bypassing the guardrail."""
        self._conn.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)",
                           (key, f"{self.name}_ext", channel, json.dumps(payload, sort_keys=True), now()))


def simulated_providers(db_path: str) -> dict[str, SimulatedProvider]:
    """channel -> provider. File-backed next to the main database, so they survive restarts."""
    base = None if db_path == ":memory:" else db_path
    payments = SimulatedProvider("payments", f"{base}.payments" if base else ":memory:")
    messages = SimulatedProvider("messages", f"{base}.messages" if base else ":memory:")
    return {**{c: payments for c in PAYMENT_CHANNELS}, **{c: messages for c in MESSAGE_CHANNELS}}


def open_incident(db: Database, kind: str, ref: str, detail: dict) -> bool:
    """Record something a person must look at. One incident per (kind, ref)."""
    return db.run(
        "INSERT OR IGNORE INTO incidents (ts, kind, ref, detail, status) VALUES (?, ?, ?, ?, 'open')",
        (now(), kind, ref, json.dumps(detail, default=str)),
    ) == 1


def _later(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


@dataclass
class Dispatcher:
    """Deliver committed outbox entries. Safe to run in several workers at once: each entry is
    claimed with a compare-and-set before its provider is called."""

    db: Database
    providers: dict[str, Provider]
    max_attempts: int = 5
    backoff_seconds: float = 2.0
    lease_seconds: float = 60.0
    clock: Callable[[], str] = now

    def run_once(self, limit: int = 100) -> dict[str, int]:
        report = {"sent": 0, "retrying": 0, "failed": 0, "recovered": self._recover_stale()}
        rows = self.db.all(
            "SELECT * FROM outbox WHERE status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY ts, rowid LIMIT ?", (self.clock(), limit))
        for row in rows:
            if self._claim(row["id"]):
                report[self._deliver({**row, "attempts": row["attempts"] + 1})] += 1
        return report

    def _claim(self, outbox_id: str) -> bool:
        return self.db.run(
            "UPDATE outbox SET status = 'sending', attempts = attempts + 1, claimed_at = ? "
            "WHERE id = ? AND status = 'pending'", (self.clock(), outbox_id)) == 1

    def _deliver(self, row: dict) -> str:
        provider = self.providers[row["channel"]]
        try:
            ref = provider.send(row["id"], row["channel"], row["payload"])
        except PermanentProviderError as e:
            self.fail(row, str(e))
            return "failed"
        except Exception as e:  # anything else may be transient: retry with the same key
            if row["attempts"] >= self.max_attempts:
                self.fail(row, f"gave up after {row['attempts']} attempts: {e}")
                return "failed"
            self.db.update("outbox", "id", row["id"], {
                "status": "pending", "last_error": f"{type(e).__name__}: {e}",
                "next_attempt_at": _later(self.backoff_seconds * 2 ** (row["attempts"] - 1))})
            return "retrying"
        self.db.update("outbox", "id", row["id"],
                       {"status": "sent", "provider_ref": ref, "sent_at": self.clock(), "last_error": None})
        return "sent"

    def fail(self, row: dict, error: str) -> None:
        """Permanent failure. Refunds are compensated (reservation released); messages are flagged."""
        with self.db.transaction():
            if row["channel"] in PAYMENT_CHANNELS:
                p = row["payload"]
                self.db.run("UPDATE orders SET refunded = round(refunded - ?, 2) WHERE id = ?",
                            (p["amount"], p["order_id"]))
                status, kind = "compensated", "refund_failed"
                note = (f"refund of ${p['amount']:.2f} on {p['order_id']} failed at the provider; the amount was "
                        "released on the order. Tell the customer and decide how to pay them.")
            else:
                status, kind = "failed", "message_failed"
                note = f"{row['channel']} to {row['recipient']} could not be delivered; follow up manually."
            self.db.update("outbox", "id", row["id"], {"status": status, "last_error": error})
            open_incident(self.db, kind, row["id"], {"error": error, "action_id": row["action_id"], "note": note})

    def _recover_stale(self) -> int:
        """Entries left "sending" by a worker that died mid-call: the provider knows if it acted."""
        cutoff = (datetime.now(UTC) - timedelta(seconds=self.lease_seconds)).isoformat(timespec="seconds")
        recovered = 0
        for row in self.db.all("SELECT * FROM outbox WHERE status = 'sending' AND claimed_at < ?", (cutoff,)):
            record = self.providers[row["channel"]].lookup(row["id"])
            changes = ({"status": "sent", "provider_ref": record["ref"], "sent_at": self.clock()} if record
                       else {"status": "pending", "last_error": "worker died mid-delivery; retrying"})
            recovered += self.db.run(
                f"UPDATE outbox SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ? AND status = 'sending'",
                (*changes.values(), row["id"]))
        return recovered


@dataclass
class Reconciler:
    """Compare what we believe happened with what the providers say happened."""

    db: Database
    providers: dict[str, Provider]
    findings: list[dict] = field(default_factory=list)

    def run(self) -> dict:
        self.findings = []
        by_provider: dict[int, tuple[Provider, list[str]]] = {}
        for channel, provider in self.providers.items():
            by_provider.setdefault(id(provider), (provider, []))[1].append(channel)
        checked = 0
        for provider, channels in by_provider.values():
            marks = ",".join("?" * len(channels))
            local = {r["id"]: r for r in self.db.all(f"SELECT * FROM outbox WHERE channel IN ({marks})",
                                                     tuple(channels))}
            remote = {r["key"]: r for r in provider.records()}
            checked += len(local)
            for key, rec in remote.items():
                row = local.get(key)
                if row is None:
                    self._finding("unknown_provider_record", key, provider=provider.name, record=rec)
                elif rec["payload"] != row["payload"]:
                    self._finding("payload_mismatch", key, ours=row["payload"], theirs=rec["payload"])
                elif row["status"] in ("pending", "sending"):
                    # It happened; we never heard back. Record it as sent.
                    self.db.update("outbox", "id", key,
                                   {"status": "sent", "provider_ref": rec["ref"], "sent_at": now()})
                    self.findings.append({"kind": "confirmed_by_provider", "ref": key, "resolved": True})
                elif row["status"] in ("failed", "compensated"):
                    # We gave up (e.g. every response was lost) but the provider did act. Believe the
                    # provider: re-apply the refund reservation, or the same money looks refundable again.
                    with self.db.transaction():
                        if row["status"] == "compensated":
                            p = row["payload"]
                            self.db.run("UPDATE orders SET refunded = round(refunded + ?, 2) WHERE id = ?",
                                        (p["amount"], p["order_id"]))
                        self.db.update("outbox", "id", key,
                                       {"status": "sent", "provider_ref": rec["ref"], "sent_at": now()})
                        self._finding("delivered_after_failure", key, was=row["status"], record=rec)
            for key, row in local.items():
                if row["status"] == "sent" and key not in remote:
                    self._finding("missing_at_provider", key, provider=provider.name)
        self._check_audit_links()
        return {"checked": checked, "findings": self.findings,
                "open_incidents": self.db.one("SELECT COUNT(*) AS n FROM incidents WHERE status = 'open'")["n"]}

    def _check_audit_links(self) -> None:
        """Every executed refund has exactly one matching refund intent, and vice versa."""
        refunds = self.db.all("SELECT action_id, args FROM audit WHERE tool = 'issue_refund' AND executed = 1")
        intents = {r["action_id"]: r for r in self.db.all("SELECT * FROM outbox WHERE channel = 'refund'")}
        for a in refunds:
            intent = intents.pop(a["action_id"], None)
            if intent is None:
                self._finding("refund_without_intent", a["action_id"], args=a["args"])
            elif round(intent["payload"]["amount"], 2) != round(a["args"]["amount"], 2):
                self._finding("intent_amount_mismatch", a["action_id"], audit=a["args"], intent=intent["payload"])
        for action_id, intent in intents.items():
            self._finding("intent_without_decision", intent["id"], action_id=action_id, intent=intent["payload"])

    def _finding(self, kind: str, ref: str, **detail: Any) -> None:
        self.findings.append({"kind": kind, "ref": ref, **detail})
        open_incident(self.db, kind, ref, detail)


class BackgroundWorker:
    """Runs delivery, recovery of interrupted agent runs and reconciliation on an interval."""

    def __init__(self, dispatcher: Dispatcher, reconciler: Reconciler, recover: Callable[[], Any],
                 interval: float, reconcile_every: float):
        self.dispatcher, self.reconciler, self.recover = dispatcher, reconciler, recover
        self.interval, self.reconcile_every = interval, reconcile_every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="guardrail-worker", daemon=True)
        self.errors: list[str] = []

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(10)

    def _loop(self) -> None:
        last_reconcile = 0.0
        while not self._stop.wait(self.interval):
            for job in (self.dispatcher.run_once, self.recover):
                self._safely(job)
            elapsed = datetime.now(UTC).timestamp()
            if self.reconcile_every and elapsed - last_reconcile >= self.reconcile_every:
                self._safely(self.reconciler.run)
                last_reconcile = elapsed

    def _safely(self, job: Callable[[], Any]) -> None:
        try:
            job()
        except Exception as e:  # keep the worker alive; surface the error in /healthz
            self.errors = [*self.errors[-9:], f"{now()} {getattr(job, '__name__', job)}: {type(e).__name__}: {e}"]
