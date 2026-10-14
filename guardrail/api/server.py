"""HTTP API and dashboard.

Two ways to use the guardrail over HTTP:

* Agent mode     POST /v1/conversations            run the built-in guarded agent end to end
* Sidecar mode   POST /v1/actions/evaluate|execute  guard tool calls from any other agent stack

Both share the approval queue (/v1/approvals) and the audit log (/v1/audit, /v1/metrics).
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..app import App, build_app
from ..auth import ANONYMOUS, Authenticator, Principal
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
    conversation: list[str] = Field([], description="What the user said (user turns)")
    customer_id: str | None = Field(None, description="The authenticated customer, if any")
    conversation_id: str = "sidecar"
    observations: list[str] = Field([], description="Tool results the agent actually received")


class GuardRequest(BaseModel):
    action: ActionIn
    context: ContextIn = ContextIn()


class Decision(BaseModel):
    reviewer: str = Field("", description="Who decides. Ignored when API keys are on: the key's name is used")
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
    auth = Authenticator(app.settings.api_keys)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Runs when the server starts (not for a bare TestClient): finish runs a crash interrupted,
        # then deliver and reconcile side effects in the background.
        app.conversations.recover_incomplete()
        worker = app.worker() if app.settings.worker_interval > 0 else None
        api.state.worker = worker
        if worker:
            worker.start()
        yield
        if worker:
            worker.stop()

    api = FastAPI(
        title="Guardrail",
        version="0.3.0",
        description="Pre-execution verification for agent tool calls.",
        lifespan=lifespan,
    )
    api.state.app = app
    api.state.worker = None

    @api.get("/healthz", tags=["ops"])
    def health() -> dict:
        db_ok = app.db.one("SELECT 1 AS ok")["ok"] == 1
        worker = api.state.worker
        outbox = {r["status"]: r["n"] for r in app.db.all("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")}
        return {"status": "ok" if db_ok else "degraded", "database": "ok" if db_ok else "error",
                "env": app.settings.env, "auth": "api keys" if auth.enabled else "off (development)",
                "reviewer": app.settings.reviewer, "agent": app.settings.agent, "fault_rate": app.settings.fault_rate,
                "worker": "running" if worker else "off", "worker_errors": worker.errors if worker else [],
                "outbox": outbox,
                "open_incidents": app.db.one("SELECT COUNT(*) AS n FROM incidents WHERE status = 'open'")["n"]}

    @api.get("/v1/whoami", tags=["ops"])
    def whoami(principal: Principal = Depends(auth.require("act", "read", "decide", "operate"))) -> dict:
        return {"name": principal.name, "role": principal.role, "auth": auth.enabled}

    @api.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    # ---- agent mode -------------------------------------------------------------
    @api.post("/v1/conversations", tags=["agent"], status_code=201, dependencies=[Depends(auth.require("act"))])
    def start_conversation(body: StartConversation) -> dict:
        try:
            return app.conversations.start(body.message, body.customer_id)
        except ConversationError as e:
            raise HTTPException(400, str(e)) from e

    @api.get("/v1/conversations", tags=["agent"], dependencies=[Depends(auth.require("act", "read"))])
    def list_conversations(limit: int = 50) -> list[dict]:
        return app.conversations.list(limit)

    @api.get("/v1/conversations/{conversation_id}", tags=["agent"], dependencies=[Depends(auth.require("act", "read"))])
    def get_conversation(conversation_id: str) -> dict:
        try:
            return app.conversations.get(conversation_id)
        except ConversationError as e:
            raise HTTPException(404, str(e)) from e

    @api.post("/v1/conversations/{conversation_id}/messages", tags=["agent"], dependencies=[Depends(auth.require("act"))])
    def send_message(conversation_id: str, body: SendMessage) -> dict:
        try:
            return app.conversations.send(conversation_id, body.message)
        except ConversationError as e:
            raise HTTPException(404 if "not found" in str(e) else 409, str(e)) from e

    # ---- sidecar mode -----------------------------------------------------------
    def _parse(req: GuardRequest, action_id: str | None = None) -> tuple[ProposedAction, AgentContext]:
        action = ProposedAction(req.action.tool, req.action.args, req.action.rationale)
        if action_id:
            action = ProposedAction(action.tool, action.args, action.rationale, action_id)
        return (
            action,
            AgentContext(req.context.conversation, req.context.customer_id, req.context.conversation_id,
                         req.context.observations),
        )

    @api.post("/v1/actions/evaluate", tags=["sidecar"], dependencies=[Depends(auth.require("act"))])
    def evaluate(req: GuardRequest) -> dict:
        """Dry run: decide on an action without executing it."""
        return _verdict_json(app.guard.evaluate(*_parse(req)))

    @api.post("/v1/actions/execute", tags=["sidecar"], dependencies=[Depends(auth.require("act"))])
    def execute(req: GuardRequest, response: Response,
                idempotency_key: str | None = Header(None, max_length=200)) -> dict:
        """Evaluate and, if allowed, execute. Escalations return an approval_id.

        Send an `Idempotency-Key` header to make retries safe: a repeated request with the same key
        and body returns the original response (header `Idempotent-Replay: true`) and never acts twice.
        """
        if not idempotency_key:
            return _execute(req)
        digest = hashlib.sha256(req.model_dump_json().encode()).hexdigest()
        state, row = app.guard.idempotency.begin(idempotency_key, digest)
        if state == "mismatch":
            raise HTTPException(422, "Idempotency-Key was already used with a different request")
        if state == "in_progress":
            raise HTTPException(409, f"a request with this Idempotency-Key is in progress (action {row['action_id']});"
                                     " retry later or inspect GET /v1/actions/{action_id}")
        if state == "done":
            response.headers["Idempotent-Replay"] = "true"
            return row["response"]
        try:
            out = _execute(req, row["action_id"])
        except Exception:
            app.guard.idempotency.abandon(idempotency_key)
            raise
        app.guard.idempotency.finish(idempotency_key, out)
        return out

    def _execute(req: GuardRequest, action_id: str | None = None) -> dict:
        result = app.guard.execute(*_parse(req, action_id))
        return {"verdict": _verdict_json(result.verdict), "executed": result.executed,
                "output": result.output, "approval_id": result.approval_id}

    @api.get("/v1/actions/{action_id}", tags=["sidecar"], dependencies=[Depends(auth.require("act", "read"))])
    def get_action(action_id: str) -> dict:
        """The audit record of one evaluated action: decision, reasons, whether and when it executed."""
        row = app.guard.audit.get(action_id)
        if row is None:
            raise HTTPException(404, f"action {action_id} not found")
        return row

    # ---- approvals ----------------------------------------------------------------
    @api.get("/v1/approvals", tags=["approvals"], dependencies=[Depends(auth.require("read"))])
    def list_approvals(status: str | None = Query("pending")) -> list[dict]:
        return app.approvals.list(None if status == "all" else status)

    @api.get("/v1/approvals/{approval_id}", tags=["approvals"], dependencies=[Depends(auth.require("read"))])
    def get_approval(approval_id: str) -> dict:
        try:
            return app.approvals.get(approval_id)
        except ApprovalError as e:
            raise HTTPException(404, str(e)) from e

    def who(principal: Principal, body: Decision) -> str:
        """The authenticated key's name; without keys (development) the name given in the body."""
        if principal is not ANONYMOUS:
            return principal.name
        if not body.reviewer.strip():
            raise HTTPException(422, "reviewer is required (API keys are off, so the name comes from the request)")
        return body.reviewer

    def _decide(approval_id: str, approve: bool, body: Decision, principal: Principal) -> dict:
        try:
            return app.approvals.decide(approval_id, approve, who(principal, body), body.note)
        except ApprovalError as e:
            raise HTTPException(404 if "not found" in str(e) else 409, str(e)) from e

    @api.post("/v1/approvals/{approval_id}/approve", tags=["approvals"])
    def approve(approval_id: str, body: Decision, principal: Principal = Depends(auth.require("decide"))) -> dict:
        return _decide(approval_id, True, body, principal)

    @api.post("/v1/approvals/{approval_id}/reject", tags=["approvals"])
    def reject(approval_id: str, body: Decision, principal: Principal = Depends(auth.require("decide"))) -> dict:
        return _decide(approval_id, False, body, principal)

    @api.post("/v1/approvals/{approval_id}/revoke", tags=["approvals"])
    def revoke(approval_id: str, body: Decision, principal: Principal = Depends(auth.require("decide"))) -> dict:
        """Withdraw a pending or approved approval before the action executes."""
        try:
            return app.approvals.revoke(approval_id, who(principal, body), body.note)
        except ApprovalError as e:
            raise HTTPException(404 if "not found" in str(e) else 409, str(e)) from e

    # ---- observability ----------------------------------------------------------
    @api.get("/v1/audit", tags=["observability"], dependencies=[Depends(auth.require("read"))])
    def audit(conversation_id: str | None = None, decision: str | None = None, limit: int = 200) -> list[dict]:
        return app.guard.audit.list(conversation_id, decision, limit)

    @api.get("/v1/metrics", tags=["observability"], dependencies=[Depends(auth.require("read"))])
    def metrics() -> dict:
        outbox = {r["status"]: r["n"] for r in app.db.all("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")}
        incidents = {r["kind"]: r["n"] for r in app.db.all(
            "SELECT kind, COUNT(*) AS n FROM incidents WHERE status = 'open' GROUP BY kind")}
        return {**app.guard.audit.metrics(), "outbox": outbox, "open_incidents": incidents}

    @api.get("/v1/outbox", tags=["side effects"], dependencies=[Depends(auth.require("read"))])
    def outbox(status: str | None = None, limit: int = 100) -> list[dict]:
        """Refunds, emails and chat replies queued by executed actions, and their delivery state."""
        if status:
            return app.db.all("SELECT * FROM outbox WHERE status = ? ORDER BY ts DESC, rowid DESC LIMIT ?",
                              (status, limit))
        return app.db.all("SELECT * FROM outbox ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,))

    @api.post("/v1/outbox/dispatch", tags=["side effects"], dependencies=[Depends(auth.require("operate"))])
    def dispatch() -> dict:
        """Deliver due outbox entries now (the background worker also does this on an interval)."""
        return app.dispatcher.run_once()

    @api.post("/v1/reconcile", tags=["side effects"], dependencies=[Depends(auth.require("operate"))])
    def reconcile() -> dict:
        """Compare the outbox and audit log with the providers' records; open incidents for mismatches."""
        return app.reconciler.run()

    @api.get("/v1/incidents", tags=["side effects"], dependencies=[Depends(auth.require("read"))])
    def incidents(status: str | None = Query("open")) -> list[dict]:
        if status == "all":
            return app.db.all("SELECT * FROM incidents ORDER BY id DESC")
        return app.db.all("SELECT * FROM incidents WHERE status = ? ORDER BY id DESC", (status,))

    @api.post("/v1/incidents/{incident_id}/resolve", tags=["side effects"])
    def resolve_incident(incident_id: int, body: Decision,
                         principal: Principal = Depends(auth.require("operate"))) -> dict:
        row = app.db.one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        if row is None:
            raise HTTPException(404, f"incident {incident_id} not found")
        detail = {**row["detail"], "resolved_by": who(principal, body), "resolution": body.note}
        app.db.update("incidents", "id", incident_id, {"status": "resolved", "detail": detail})
        return app.db.one("SELECT * FROM incidents WHERE id = ?", (incident_id,))

    # ---- reference data (read-only) ---------------------------------------------
    @api.get("/v1/kb", tags=["knowledge base"], dependencies=[Depends(auth.require("read"))])
    def knowledge_base() -> dict:
        kb = app.guard.kb
        return {"as_of": kb.as_of.isoformat(), "customers": kb.all_customers(), "orders": kb.all_orders(),
                "products": kb.all_products(), "policies": kb.all_policies(), "staff": kb.all_staff()}

    return api
