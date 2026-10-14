"""Composition root: build the whole system from Settings.

    Settings -> Database -> KnowledgeBase -> Guardrail (checks, router, reviewers, audit, approvals)
                                          -> agent model (rules | claude) [+ FaultInjector]
                                          -> agent graph (+ checkpointer)
                                          -> ConversationService, ApprovalService
                                          -> providers -> Dispatcher, Reconciler, BackgroundWorker
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from .agent import FaultInjector, RuleBasedAgent, build_agent_graph, claude_agent, tool_schemas
from .config import Settings
from .guard import Guardrail
from .outbox import BackgroundWorker, Dispatcher, Reconciler, simulated_providers
from .services import ApprovalService, ConversationService
from .store import Database


@dataclass
class App:
    settings: Settings
    db: Database
    guard: Guardrail
    conversations: ConversationService
    approvals: ApprovalService
    providers: dict[str, Any]
    dispatcher: Dispatcher
    reconciler: Reconciler

    def worker(self) -> BackgroundWorker:
        """Delivers the outbox, recovers interrupted runs and reconciles, on an interval."""
        return BackgroundWorker(self.dispatcher, self.reconciler, self.conversations.recover_incomplete,
                                self.settings.worker_interval, self.settings.reconcile_interval)


def agent_model(settings: Settings, guard: Guardrail, base: Any = None) -> Any:
    if base is None:
        if settings.agent == "claude":
            base = claude_agent(tool_schemas(guard), settings.agent_model,
                                timeout=settings.agent_timeout, max_retries=settings.agent_max_retries)
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
    providers = simulated_providers(settings.db_path)
    dispatcher = Dispatcher(db, providers, max_attempts=settings.outbox_max_attempts,
                            backoff_seconds=settings.outbox_backoff_seconds)
    return App(settings, db, guard, conversations, ApprovalService(guard, conversations), providers, dispatcher,
               Reconciler(db, providers))
