"""Durable guardrail state: the audit log and the human-approval queue."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any

from .models import AgentContext, ProposedAction, Verdict
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
        self.db.update("audit", "action_id", action_id, {"executed": 1, "output": output})

    def mark_failed(self, action_id: str, error: str) -> None:
        self.db.update("audit", "action_id", action_id, {"executed": 0, "output": {"error": error}})

    def overrule(self, verdict: Verdict) -> None:
        """Replace the decision when the commit-time re-check disagrees with the evaluation."""
        self.db.update(
            "audit",
            "action_id",
            verdict.action.id,
            {"decision": verdict.decision.value, "caught_by": verdict.caught_by, "reasons": verdict.reasons},
        )

    def link_approval(self, action_id: str, approval_id: str) -> None:
        self.db.update("audit", "action_id", action_id, {"approval_id": approval_id})

    def executed_actions(self, conversation_id: str) -> list[ProposedAction]:
        rows = self.db.all(
            "SELECT action_id, tool, args, rationale FROM audit WHERE conversation_id = ? AND executed = 1 ORDER BY id",
            (conversation_id,),
        )
        return [ProposedAction(r["tool"], r["args"], r["rationale"], r["action_id"]) for r in rows]

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
            c["check"] for r in rows for c in r["checks"] if c["outcome"] in ("fail", "needs_approval")
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
    """Escalated actions waiting for a human decision."""

    def __init__(self, db: Database):
        self.db = db

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
        sql = "SELECT * FROM approvals" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY created_at, id"
        return self.db.all(sql, tuple(params))

    def decide(self, approval_id: str, approve: bool, reviewer: str, note: str = "") -> dict:
        with self.db.transaction():
            row = self.get(approval_id)
            if row is None:
                raise ApprovalError(f"approval {approval_id} not found")
            if row["status"] != "pending":
                raise ApprovalError(f"approval {approval_id} is already {row['status']}")
            self.db.update(
                "approvals",
                "id",
                approval_id,
                {"status": "approved" if approve else "rejected", "decided_by": reviewer, "note": note,
                 "decided_at": now()},
            )
        return self.get(approval_id)

    def set_result(self, approval_id: str, result: Any) -> None:
        self.db.update("approvals", "id", approval_id, {"result": result})

    @staticmethod
    def action_of(row: dict) -> tuple[ProposedAction, AgentContext]:
        return ProposedAction(row["tool"], row["args"], row["rationale"], row["action_id"]), AgentContext(**row["context"])
