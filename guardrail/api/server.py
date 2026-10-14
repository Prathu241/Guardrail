"""HTTP API and dashboard.

Two ways to use the guardrail over HTTP:

* Agent mode     POST /v1/conversations            run the built-in guarded agent end to end
* Sidecar mode   POST /v1/actions/evaluate|execute  guard tool calls from any other agent stack

Both share the approval queue (/v1/approvals) and the audit log (/v1/audit, /v1/metrics).
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..app import App, build_app
from ..ledger import ApprovalError
from ..models import AgentContext, ProposedAction
from ..services import ConversationError

STATIC = Path(__file__).parent / "static"


class StartConversation(BaseModel):
    message: str = Field(min_length=1)
    customer_id: str | None = Field(None, description="Authenticated customer; omit for internal operations")


class SendMessage(BaseModel):
    message: str = Field(min_length=1)


class ActionIn(BaseModel):
    tool: str
    args: dict[str, Any]
    rationale: str = ""


class ContextIn(BaseModel):
    conversation: list[str] = []
    customer_id: str | None = None
    conversation_id: str = "sidecar"


class GuardRequest(BaseModel):
    action: ActionIn
    context: ContextIn = ContextIn()


class Decision(BaseModel):
    reviewer: str = Field(min_length=1)
    note: str = ""


def _verdict_json(verdict) -> dict:
    return {
        **verdict.to_dict(),
        "risk_factors": verdict.risk.factors if verdict.risk else [],
        "checks": [{"check": c.check, "outcome": c.outcome.value, "message": c.message} for c in verdict.checks],
        "reviews": [asdict(r) for r in verdict.reviews],
    }


def create_app(app: App | None = None) -> FastAPI:
    app = app or build_app()
    api = FastAPI(
        title="Guardrail",
        version="0.2.0",
        description="Pre-execution verification for agent tool calls.",
    )
    api.state.app = app

    @api.get("/healthz", tags=["ops"])
    def health() -> dict:
        return {"status": "ok", "reviewer": app.settings.reviewer, "agent": app.settings.agent,
                "fault_rate": app.settings.fault_rate}

    @api.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    # ---- agent mode -------------------------------------------------------------
    @api.post("/v1/conversations", tags=["agent"], status_code=201)
    def start_conversation(body: StartConversation) -> dict:
        try:
            return app.conversations.start(body.message, body.customer_id)
        except ConversationError as e:
            raise HTTPException(400, str(e)) from e

    @api.get("/v1/conversations", tags=["agent"])
    def list_conversations(limit: int = 50) -> list[dict]:
        return app.conversations.list(limit)

    @api.get("/v1/conversations/{conversation_id}", tags=["agent"])
    def get_conversation(conversation_id: str) -> dict:
        try:
            return app.conversations.get(conversation_id)
        except ConversationError as e:
            raise HTTPException(404, str(e)) from e

    @api.post("/v1/conversations/{conversation_id}/messages", tags=["agent"])
    def send_message(conversation_id: str, body: SendMessage) -> dict:
        try:
            return app.conversations.send(conversation_id, body.message)
        except ConversationError as e:
            raise HTTPException(409 if "waiting" in str(e) else 404, str(e)) from e

    # ---- sidecar mode -----------------------------------------------------------
    def _parse(req: GuardRequest) -> tuple[ProposedAction, AgentContext]:
        return (
            ProposedAction(req.action.tool, req.action.args, req.action.rationale),
            AgentContext(req.context.conversation, req.context.customer_id, req.context.conversation_id),
        )

    @api.post("/v1/actions/evaluate", tags=["sidecar"])
    def evaluate(req: GuardRequest) -> dict:
        """Dry run: decide on an action without executing it."""
        return _verdict_json(app.guard.evaluate(*_parse(req)))

    @api.post("/v1/actions/execute", tags=["sidecar"])
    def execute(req: GuardRequest) -> dict:
        """Evaluate and, if allowed, execute. Escalations return an approval_id."""
        result = app.guard.execute(*_parse(req))
        return {"verdict": _verdict_json(result.verdict), "executed": result.executed,
                "output": result.output, "approval_id": result.approval_id}

    # ---- approvals ----------------------------------------------------------------
    @api.get("/v1/approvals", tags=["approvals"])
    def list_approvals(status: str | None = Query("pending")) -> list[dict]:
        return app.approvals.list(None if status == "all" else status)

    @api.get("/v1/approvals/{approval_id}", tags=["approvals"])
    def get_approval(approval_id: str) -> dict:
        try:
            return app.approvals.get(approval_id)
        except ApprovalError as e:
            raise HTTPException(404, str(e)) from e

    def _decide(approval_id: str, approve: bool, body: Decision) -> dict:
        try:
            return app.approvals.decide(approval_id, approve, body.reviewer, body.note)
        except ApprovalError as e:
            raise HTTPException(404 if "not found" in str(e) else 409, str(e)) from e

    @api.post("/v1/approvals/{approval_id}/approve", tags=["approvals"])
    def approve(approval_id: str, body: Decision) -> dict:
        return _decide(approval_id, True, body)

    @api.post("/v1/approvals/{approval_id}/reject", tags=["approvals"])
    def reject(approval_id: str, body: Decision) -> dict:
        return _decide(approval_id, False, body)

    # ---- observability ----------------------------------------------------------
    @api.get("/v1/audit", tags=["observability"])
    def audit(conversation_id: str | None = None, decision: str | None = None, limit: int = 200) -> list[dict]:
        return app.guard.audit.list(conversation_id, decision, limit)

    @api.get("/v1/metrics", tags=["observability"])
    def metrics() -> dict:
        return app.guard.audit.metrics()

    @api.get("/v1/outbox", tags=["observability"])
    def outbox(limit: int = 100) -> list[dict]:
        return app.db.all("SELECT * FROM outbox ORDER BY ts DESC, id DESC LIMIT ?", (limit,))

    # ---- reference data (read-only) ---------------------------------------------
    @api.get("/v1/kb", tags=["knowledge base"])
    def knowledge_base() -> dict:
        kb = app.guard.kb
        return {"as_of": kb.as_of.isoformat(), "customers": kb.all_customers(), "orders": kb.all_orders(),
                "products": kb.all_products(), "policies": kb.all_policies(), "staff": kb.all_staff()}

    return api
