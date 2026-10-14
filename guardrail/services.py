"""Application services: conversations with the guarded agent, and the human approval workflow.

These sit between the transports (HTTP API, CLI) and the core (Guardrail + agent graph), so
every entry point drives exactly the same workflow.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from .guard import Guardrail
from .ledger import ApprovalError
from .store import new_id, now


class ConversationError(Exception):
    pass


def _message_view(m: AnyMessage) -> dict:
    if isinstance(m, HumanMessage):
        return {"role": "user", "text": m.text}
    if isinstance(m, ToolMessage):
        return {"role": "tool", "name": m.name, "tool_call_id": m.tool_call_id, "status": m.status, "text": m.content}
    if isinstance(m, AIMessage):
        return {
            "role": "agent",
            "text": m.text,
            "tool_calls": [{"id": c["id"], "name": c["name"], "args": c["args"]} for c in m.tool_calls],
            "injected_faults": m.response_metadata.get("injected_faults", {}),
            "agent_error": m.response_metadata.get("agent_error"),
        }
    return {"role": m.type, "text": str(m.content)}


@dataclass
class ConversationService:
    """Runs the guarded agent graph for each conversation.

    One run per conversation at a time, across threads *and* processes: a run first claims the
    conversation row (status "running", compare-and-set in the database). A claim older than
    ``lease_seconds`` belongs to a worker that died mid-run; ``recover_incomplete`` finishes such
    runs from their last checkpoint (commit is idempotent, so nothing executes twice).
    """

    guard: Guardrail
    graph: Any  # compiled agent graph with a checkpointer
    lease_seconds: float = 300.0

    def __post_init__(self) -> None:
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()
        self.db = self.guard.db

    def _config(self, conversation_id: str) -> dict:
        return {"configurable": {"thread_id": conversation_id}, "recursion_limit": 60}

    def start(self, message: str, customer_id: str | None = None) -> dict:
        if customer_id is not None and self.guard.kb.customer(customer_id) is None:
            raise ConversationError(f"unknown customer {customer_id}")
        cid = new_id("conv")
        self.db.insert("conversations", {"id": cid, "customer_id": customer_id, "status": "running",
                                         "created_at": now(), "updated_at": now()})
        with self._lock(cid):
            return self._invoke(cid, {"messages": [HumanMessage(message)], "conversation_id": cid,
                                      "customer_id": customer_id, "pending": [], "guard_log": []})

    def send(self, conversation_id: str, message: str) -> dict:
        with self._lock(conversation_id):
            self._claim(conversation_id, allowed=("active",))
            return self._invoke(conversation_id, {"messages": [HumanMessage(message)]})

    def on_approval_decided(self, conversation_id: str) -> dict | None:
        """Resume the paused run once every approval it is waiting on has been decided. Safe to call
        from several approvers at once: exactly one of them resumes the run."""
        with self._lock(conversation_id):
            if self.guard.approvals.list(status="pending", conversation_id=conversation_id):
                return None
            if not self.graph.get_state(self._config(conversation_id)).interrupts:
                return None
            try:
                self._claim(conversation_id, allowed=("awaiting_approval",))
            except ConversationError:
                return None  # another worker is resuming it
            return self._invoke(conversation_id, Command(resume=True))

    def recover_incomplete(self, stale_after: float | None = None) -> list[str]:
        """Finish runs whose worker died mid-run. Returns the conversation ids recovered."""
        stale_after = self.lease_seconds if stale_after is None else stale_after
        recovered = []
        for row in self.db.all("SELECT id FROM conversations WHERE status = 'running'"):
            cid = row["id"]
            with self._lock(cid):
                try:
                    self._claim(cid, allowed=(), stale_after=stale_after)
                except ConversationError:
                    continue
                state = self.graph.get_state(self._config(cid))
                decided = not self.guard.approvals.list(status="pending", conversation_id=cid)
                if state.interrupts and decided:
                    self._invoke(cid, Command(resume=True))  # died while resuming after the decision
                elif state.next and not state.interrupts:
                    self._invoke(cid, None)  # continue from the last checkpoint
                else:
                    self._settle(cid)
                recovered.append(cid)
        return recovered

    def get(self, conversation_id: str) -> dict:
        row = self._row(conversation_id)
        state = self.graph.get_state(self._config(conversation_id))
        values = state.values
        return {
            **row,
            "messages": [_message_view(m) for m in values.get("messages", [])],
            "guard_log": values.get("guard_log", []),
            "pending_approvals": self.guard.approvals.list(status="pending", conversation_id=conversation_id),
        }

    def list(self, limit: int = 50) -> list[dict]:
        return self.db.all("SELECT * FROM conversations ORDER BY updated_at DESC, created_at DESC LIMIT ?", (limit,))

    # ---- internals ------------------------------------------------------------------
    def _lock(self, conversation_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks[conversation_id]

    def _row(self, conversation_id: str) -> dict:
        row = self.db.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        if row is None:
            raise ConversationError(f"conversation {conversation_id} not found")
        return row

    def _claim(self, conversation_id: str, allowed: tuple[str, ...], stale_after: float | None = None) -> None:
        stale_after = self.lease_seconds if stale_after is None else stale_after
        with self.db.transaction():
            row = self._row(conversation_id)
            status = row["status"]
            if status == "running":
                age = (datetime.now(UTC) - datetime.fromisoformat(row["updated_at"])).total_seconds()
                if age < stale_after:
                    raise ConversationError("conversation is busy with another request")
            elif status not in allowed:
                if status == "awaiting_approval":
                    raise ConversationError("conversation is waiting for a human approval decision")
                raise ConversationError(f"conversation is {status}")
            self.db.update("conversations", "id", conversation_id, {"status": "running", "updated_at": now()})

    def _invoke(self, conversation_id: str, payload: Any) -> dict:
        try:
            # "sync": each step's checkpoint is durable before the next step runs, so a crash
            # loses at most the step in flight (which commit's idempotency makes safe to redo).
            self.graph.invoke(payload, self._config(conversation_id), durability="sync")
        finally:
            self._settle(conversation_id)
        return self.get(conversation_id)

    def _settle(self, conversation_id: str) -> None:
        paused = bool(self.graph.get_state(self._config(conversation_id)).interrupts)
        self.db.update("conversations", "id", conversation_id,
                       {"status": "awaiting_approval" if paused else "active", "updated_at": now()})


@dataclass
class ApprovalService:
    guard: Guardrail
    conversations: ConversationService | None = None

    def list(self, status: str | None = "pending") -> list[dict]:
        return self.guard.approvals.list(status=status)

    def get(self, approval_id: str) -> dict:
        row = self.guard.approvals.get(approval_id)
        if row is None:
            raise ApprovalError(f"approval {approval_id} not found")
        return row

    def decide(self, approval_id: str, approve: bool, reviewer: str, note: str = "") -> dict:
        """Record a human decision, then let the waiting party carry on.

        Agent conversations resume their paused graph (which commits approved actions);
        sidecar callers (POST /v1/actions/execute) get the approved action committed here.
        """
        if not reviewer.strip():
            raise ApprovalError("a reviewer name is required")
        row = self.guard.approvals.decide(approval_id, approve, reviewer, note)
        is_agent_run = self.conversations is not None and self.guard.db.one(
            "SELECT 1 AS x FROM conversations WHERE id = ?", (row["conversation_id"],)
        )
        conversation = None
        if is_agent_run:
            conversation = self.conversations.on_approval_decided(row["conversation_id"])
        elif approve:
            self.guard.commit_approval(approval_id)
        return {"approval": self.get(approval_id), "conversation": conversation}

    def revoke(self, approval_id: str, by: str, note: str = "") -> dict:
        """Withdraw a pending or approved-but-not-yet-executed approval."""
        if not by.strip():
            raise ApprovalError("the name of whoever revokes is required")
        row = self.guard.approvals.revoke(approval_id, by, note)
        conversation = None
        if self.conversations is not None and self.guard.db.one(
            "SELECT 1 AS x FROM conversations WHERE id = ?", (row["conversation_id"],)
        ):
            conversation = self.conversations.on_approval_decided(row["conversation_id"])
        return {"approval": self.get(approval_id), "conversation": conversation}
