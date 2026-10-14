"""Durable guardrail state: the audit log, the human-approval queue and sidecar idempotency keys."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import HUMAN_OUTCOMES, AgentContext, ProposedAction, Verdict
from .store import Database, new_id, now


class AuditLog:
    """One row per evaluated action, updated when (and if) it executes."""

    def __init__(self, db: Database):
        self.db = db

    def record(self, verdict: Verdict, ctx: AgentContext, injected_fault: str | None = None) -> None:
        a = verdict.action
        self.db.insert(
            "audit",
            {
                "ts": now(),
                "conversation_id": ctx.conversation_id,
                "action_id": a.id,
                "tool": a.tool,
                "args": a.args,
                "rationale": a.rationale,
                "decision": verdict.decision.value,
                "caught_by": verdict.caught_by,
                "risk": round(verdict.risk.score, 3) if verdict.risk else None,
                "route": verdict.route,
                "reasons": verdict.reasons,
                "warnings": verdict.warnings,
                "checks": [
                    {"check": c.check, "outcome": c.outcome.value, "message": c.message} for c in verdict.checks
                ],
                "reviews": [asdict(r) for r in verdict.reviews],
                "review_cost": verdict.review_cost,
                "injected_fault": injected_fault,
            },
        )

    def mark_executed(self, action_id: str, output: Any) -> None:
        self.db.update("audit", "action_id", action_id, {"executed": 1, "output": output, "executed_at": now()})

    def mark_failed(self, action_id: str, error: str) -> None:
        self.db.update("audit", "action_id", action_id, {"executed": 0, "output": {"error": error}})

    def overrule(self, verdict: Verdict) -> None:
        """Replace the decision when the commit-time re-check disagrees with the evaluation.
        An executed action's record is history and is never rewritten."""
        self.db.run(
            "UPDATE audit SET decision = ?, caught_by = ?, reasons = ? WHERE action_id = ? AND executed = 0",
            (verdict.decision.value, verdict.caught_by, json.dumps(verdict.reasons), verdict.action.id),
        )

    def link_approval(self, action_id: str, approval_id: str) -> None:
        self.db.update("audit", "action_id", action_id, {"approval_id": approval_id})

    def executed_actions(self, conversation_id: str) -> list[ProposedAction]:
        rows = self.db.all(
            "SELECT action_id, tool, args, rationale FROM audit WHERE conversation_id = ? AND executed = 1 ORDER BY id",
            (conversation_id,),
        )
        return [ProposedAction(r["tool"], r["args"], r["rationale"], r["action_id"]) for r in rows]

    def blocked_tools(self, conversation_id: str) -> list[str]:
        rows = self.db.all(
            "SELECT tool FROM audit WHERE conversation_id = ? AND decision = 'block' ORDER BY id", (conversation_id,)
        )
        return [r["tool"] for r in rows]

    def refunded_by_customer(self, hours: int = 24) -> dict[str, float]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = self.db.all(
            "SELECT json_extract(args, '$.customer_id') AS cid, SUM(json_extract(args, '$.amount')) AS total "
            "FROM audit WHERE tool = 'issue_refund' AND executed = 1 AND COALESCE(executed_at, ts) >= ? GROUP BY cid",
            (since,),
        )
        return {r["cid"]: round(r["total"], 2) for r in rows}

    def get(self, action_id: str) -> dict | None:
        return self.db.one("SELECT * FROM audit WHERE action_id = ?", (action_id,))

    def list(self, conversation_id: str | None = None, decision: str | None = None, limit: int = 200) -> list[dict]:
        where, params = [], []
        if conversation_id:
            where.append("conversation_id = ?")
            params.append(conversation_id)
        if decision:
            where.append("decision = ?")
            params.append(decision)
        sql = "SELECT * FROM audit" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY id DESC LIMIT ?"
        return self.db.all(sql, (*params, limit))

    def metrics(self) -> dict:
        rows = self.db.all("SELECT * FROM audit")
        decisions = Counter(r["decision"] for r in rows)
        caught_by = Counter(r["caught_by"] for r in rows if r["caught_by"])
        failing_checks = Counter(
            c["check"] for r in rows for c in r["checks"] if c["outcome"] in ("fail", "needs_approval", "unverifiable")
        )
        routes = Counter(" -> ".join(r["route"]) for r in rows)
        faults = [r for r in rows if r["injected_fault"]]
        return {
            "actions": len(rows),
            "executed": sum(r["executed"] for r in rows),
            "decisions": dict(decisions),
            "caught_by": dict(caught_by),
            "failing_checks": dict(failing_checks.most_common()),
            "routes": dict(routes.most_common()),
            "review_cost": round(sum(r["review_cost"] for r in rows), 1),
            "injected_faults": {
                "total": len(faults),
                "stopped": sum(r["decision"] != "allow" for r in faults),
                "by_fault": dict(Counter(r["injected_fault"] for r in faults)),
            },
        }


class ApprovalError(Exception):
    pass


class ApprovalQueue:
    """Escalated actions waiting for a human decision.

    status   pending -> approved | rejected | expired;  pending | approved -> revoked
    An approval covers exactly the action it was raised for and only the checks that asked for a
    human at the time (``waives``); it expires ``ttl_hours`` after it was raised, because the facts
    the approver saw go stale. Whether it executed is recorded in the audit log, not here.
    """

    def __init__(self, db: Database, ttl_hours: float = 24.0):
        self.db = db
        self.ttl = timedelta(hours=ttl_hours)

    def create(self, verdict: Verdict, ctx: AgentContext) -> str:
        approval_id = new_id("apr")
        a = verdict.action
        self.db.insert(
            "approvals",
            {
                "id": approval_id,
                "created_at": now(),
                "conversation_id": ctx.conversation_id,
                "action_id": a.id,
                "tool": a.tool,
                "args": a.args,
                "rationale": a.rationale,
                "context": asdict(ctx),
                "reasons": verdict.reasons,
                "waives": sorted({c.check for c in verdict.checks if c.outcome in HUMAN_OUTCOMES}),
            },
        )
        return approval_id

    def get(self, approval_id: str) -> dict | None:
        return self.db.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))

    def list(self, status: str | None = None, conversation_id: str | None = None) -> list[dict]:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if conversation_id:
            where.append("conversation_id = ?")
            params.append(conversation_id)
        sql = "SELECT * FROM approvals" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY created_at, rowid"
        return self.db.all(sql, tuple(params))

    def is_expired(self, row: dict) -> bool:
        return datetime.fromisoformat(row["created_at"]) + self.ttl < datetime.now(timezone.utc)

    def executed(self, row: dict) -> bool:
        audit = self.db.one("SELECT executed FROM audit WHERE action_id = ?", (row["action_id"],))
        return bool(audit and audit["executed"])

    def decide(self, approval_id: str, approve: bool, reviewer: str, note: str = "") -> dict:
        with self.db.transaction():
            row = self._require(approval_id)
            if row["status"] != "pending":
                raise ApprovalError(f"approval {approval_id} is already {row['status']}")
            expired = self.is_expired(row)
            status = "expired" if expired else "approved" if approve else "rejected"
            self.db.update("approvals", "id", approval_id,
                           {"status": status, "decided_by": reviewer, "note": note, "decided_at": now()})
        if expired:
            raise ApprovalError(f"approval {approval_id} expired: it was raised more than {self.ttl} ago and the "
                                "facts may have changed; the action must be proposed again")
        return self.get(approval_id)

    def revoke(self, approval_id: str, by: str, note: str = "") -> dict:
        """Withdraw a pending or approved (not yet executed) approval. Atomic with execution:
        exactly one of revoke() and the commit that would run the action can succeed."""
        with self.db.transaction():
            row = self._require(approval_id)
            if self.executed(row):
                raise ApprovalError(f"approval {approval_id} has already executed; revoking is no longer possible "
                                    "(use a compensating action)")
            if row["status"] not in ("pending", "approved"):
                raise ApprovalError(f"approval {approval_id} is already {row['status']}")
            self.db.update("approvals", "id", approval_id,
                           {"status": "revoked", "revoked_by": by, "revoked_at": now(), "revoke_note": note})
        return self.get(approval_id)

    def authorizes(self, approval_id: str, action_id: str) -> dict:
        """The approval row, if it currently authorizes running ``action_id``; otherwise raise.
        Call inside the commit transaction so revocation and expiry cannot race the write."""
        row = self._require(approval_id)
        if row["action_id"] != action_id:
            raise ApprovalError(f"approval {approval_id} is for a different action")
        if row["status"] != "approved":
            raise ApprovalError(f"approval {approval_id} is {row['status']}, not approved")
        if self.is_expired(row):
            raise ApprovalError(f"approval {approval_id} expired before the action ran; propose it again")
        return row

    def set_result(self, approval_id: str, result: Any) -> None:
        self.db.update("approvals", "id", approval_id, {"result": result})

    def _require(self, approval_id: str) -> dict:
        row = self.get(approval_id)
        if row is None:
            raise ApprovalError(f"approval {approval_id} not found")
        return row

    @staticmethod
    def action_of(row: dict) -> tuple[ProposedAction, AgentContext]:
        return ProposedAction(row["tool"], row["args"], row["rationale"], row["action_id"]), AgentContext(**row["context"])


class IdempotencyStore:
    """Idempotency-Key handling for sidecar callers that retry requests.

    The first request with a key reserves it with a fresh action id; a retry with the same key and
    body gets the stored response; the same key with a different body is refused.
    """

    def __init__(self, db: Database):
        self.db = db

    def begin(self, key: str, request_hash: str) -> tuple[str, dict]:
        """("new" | "done" | "in_progress" | "mismatch", row)."""
        with self.db.transaction():
            row = self.db.one("SELECT * FROM idempotency WHERE key = ?", (key,))
            if row is None:
                row = {"key": key, "request_hash": request_hash, "action_id": ProposedAction("", {}).id,
                       "status": "in_progress", "response": None, "created_at": now()}
                self.db.insert("idempotency", row)
                return "new", row
        if row["request_hash"] != request_hash:
            return "mismatch", row
        return ("done" if row["status"] == "done" else "in_progress"), row

    def finish(self, key: str, response: dict) -> None:
        self.db.update("idempotency", "key", key, {"status": "done", "response": response})

    def abandon(self, key: str) -> None:
        """The request failed before anything was decided; let a retry start over."""
        self.db.run("DELETE FROM idempotency WHERE key = ? AND status = 'in_progress'", (key,))
