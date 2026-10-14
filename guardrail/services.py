"""Application services: conversations with the guarded agent, and the human approval workflow.

These sit between the transports (HTTP API, CLI) and the core (Guardrail + agent graph), so
every entry point drives exactly the same workflow.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
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
        }
    return {"role": m.type, "text": str(m.content)}


@dataclass
class ConversationService:
    guard: Guardrail
    graph: Any  # compiled agent graph with a checkpointer

    def __post_init__(self) -> None:
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.db = self.guard.db

    def _config(self, conversation_id: str) -> dict:
        return {"configurable": {"thread_id": conversation_id}, "recursion_limit": 60}

    def start(self, message: str, customer_id: str | None = None) -> dict:
        if customer_id is not None and self.guard.kb.customer(customer_id) is None:
            raise ConversationError(f"unknown customer {customer_id}")
        cid = new_id("conv")
        self.db.insert("conversations", {"id": cid, "customer_id": customer_id, "status": "active",
                                         "created_at": now(), "updated_at": now()})
        return self._run(cid, {"messages": [HumanMessage(message)], "conversation_id": cid,
                               "customer_id": customer_id, "pending": [], "guard_log": []})

    def send(self, conversation_id: str, message: str) -> dict:
        row = self._row(conversation_id)
        if row["status"] == "awaiting_approval":
            raise ConversationError("conversation is waiting for a human approval decision")
        return self._run(conversation_id, {"messages": [HumanMessage(message)]})

    def resume(self, conversation_id: str) -> dict:
        return self._run(conversation_id, Command(resume=True))

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

    def on_approval_decided(self, conversation_id: str) -> dict | None:
        """Resume the paused run once every approval it is waiting on has been decided."""
        if self.guard.approvals.list(status="pending", conversation_id=conversation_id):
            return None
        if not self.graph.get_state(self._config(conversation_id)).interrupts:
            return None
        return self.resume(conversation_id)

    def _row(self, conversation_id: str) -> dict:
        row = self.db.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        if row is None:
            raise ConversationError(f"conversation {conversation_id} not found")
        return row

    def _run(self, conversation_id: str, payload: Any) -> dict:
        with self._locks[conversation_id]:
            self.graph.invoke(payload, self._config(conversation_id))
            paused = bool(self.graph.get_state(self._config(conversation_id)).interrupts)
            self.db.update("conversations", "id", conversation_id,
                           {"status": "awaiting_approval" if paused else "active", "updated_at": now()})
        return self.get(conversation_id)


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
