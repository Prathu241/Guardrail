"""Composition root: build the whole system from Settings.

    Settings -> Database -> KnowledgeBase -> Guardrail (checks, router, reviewers, audit, approvals)
                                          -> agent model (rules | claude) [+ FaultInjector]
                                          -> agent graph (+ checkpointer)
                                          -> ConversationService, ApprovalService
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from .agent import FaultInjector, RuleBasedAgent, build_agent_graph, claude_agent, tool_schemas
from .config import Settings
from .guard import Guardrail
from .services import ApprovalService, ConversationService
from .store import Database


@dataclass
class App:
    settings: Settings
    db: Database
    guard: Guardrail
    conversations: ConversationService
    approvals: ApprovalService


def agent_model(settings: Settings, guard: Guardrail, base: Any = None) -> Any:
    if base is None:
        if settings.agent == "claude":
            base = claude_agent(tool_schemas(guard), settings.agent_model)
        elif settings.agent == "rules":
            base = RuleBasedAgent()
        else:
            raise ValueError(f"unknown agent {settings.agent!r} (expected 'rules' or 'claude')")
    return FaultInjector(base, settings.fault_rate, settings.fault_seed) if settings.fault_rate > 0 else base


def _checkpointer(settings: Settings):
    if settings.db_path == ":memory:":
        return InMemorySaver()
    from langgraph.checkpoint.sqlite import SqliteSaver

    # Paused (awaiting-approval) runs survive restarts: graph state lives next to the main DB.
    conn = sqlite3.connect(f"{settings.db_path}.checkpoints", check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def build_app(settings: Settings | None = None, model: Any = None) -> App:
    settings = settings or Settings.from_env()
    db = Database.seeded(settings.db_path)
    guard = Guardrail.from_settings(settings, db)
    graph = build_agent_graph(agent_model(settings, guard, model), guard, _checkpointer(settings))
    conversations = ConversationService(guard, graph)
    return App(settings, db, guard, conversations, ApprovalService(guard, conversations))
