"""The guardrail: validate -> verify -> score -> route -> decide, then commit atomically.

Lifecycle of one proposed action

    evaluate()   schema + deterministic checks + risk + model review  -> Verdict (audited)
       ALLOW     -> commit(): re-run the deterministic checks and execute the tool inside one
                    database transaction, so nothing that changed since evaluation slips through;
                    then verify the tool's postcondition and roll back if the effect is wrong
       ESCALATE  -> request_approval(): park it in the approval queue; a human decides later and
                    commit(approval_id=...) runs it (still re-checked; only the checks that asked the
                    approver - their UNVERIFIABLE / NEEDS_APPROVAL outcomes - are waived, and only
                    while the approval is approved, unexpired and unrevoked)
       BLOCK     -> nothing runs; the reasons go back to the agent
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from .checks import GENERIC_CHECKS, Session
from .config import Settings
from .knowledge_base import KnowledgeBase
from .ledger import ApprovalError, ApprovalQueue, AuditLog, IdempotencyStore
from .models import (
    HUMAN_OUTCOMES,
    AgentContext,
    CheckResult,
    Decision,
    ExecutionResult,
    Outcome,
    ProposedAction,
    Verdict,
)
from .reviewers import ReviewRequest
from .risk import RiskRouter, assess_risk, router_for
from .store import Database
from .tools import PostconditionError, ToolSpec, make_tools


class Guardrail:
    def __init__(
        self,
        kb: KnowledgeBase,
        router: RiskRouter | None = None,
        tools: dict[str, ToolSpec] | None = None,
        block_confidence: float = 0.7,
        approval_ttl_hours: float = 24.0,
    ):
        self.kb = kb
        self.db = kb.db
        self.tools = tools if tools is not None else make_tools(kb)
        self.router = router or router_for(Settings())
        self.block_confidence = block_confidence
        self.audit = AuditLog(self.db)
        self.approvals = ApprovalQueue(self.db, approval_ttl_hours)
        self.idempotency = IdempotencyStore(self.db)

    @classmethod
    def from_settings(cls, settings: Settings, db: Database | None = None) -> Guardrail:
        db = db or Database.seeded(settings.db_path)
        router = router_for(settings)
        return cls(KnowledgeBase(db, settings.today_date), router, block_confidence=settings.block_confidence,
                   approval_ttl_hours=settings.approval_ttl_hours)

    @classmethod
    def in_memory(cls, router: RiskRouter | None = None) -> Guardrail:
        return cls(KnowledgeBase.in_memory(), router or router_for(Settings()))

    def session(self, conversation_id: str) -> Session:
        """What has actually happened, read from the audit log (never from the agent's claims)."""
        return Session(
            executed=self.audit.executed_actions(conversation_id),
            blocked_tools=self.audit.blocked_tools(conversation_id),
            refunded_24h=self.audit.refunded_by_customer(hours=24),
        )

    # ------------------------------------------------------------------ evaluate
    def evaluate(self, action: ProposedAction, ctx: AgentContext, injected_fault: str | None = None) -> Verdict:
        """Decide on an action without executing it. Every verdict is written to the audit log."""
        verdict = self._evaluate(action, ctx)
        self.audit.record(verdict, ctx, injected_fault)
        return verdict

    def _validate(self, action: ProposedAction) -> tuple[ToolSpec | None, ProposedAction, CheckResult | None]:
        spec = self.tools.get(action.tool)
        if spec is None:
            hint = KnowledgeBase.closest(action.tool, self.tools)
            msg = f"tool '{action.tool}' does not exist" + (f" (did you mean '{hint[0]}'?)" if hint else "")
            return None, action, CheckResult("tool_exists", Outcome.FAIL, msg)
        try:
            parsed = spec.schema.model_validate(action.args)
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            return spec, action, CheckResult("args_match_schema", Outcome.FAIL, f"invalid arguments: {problems}")
        # Checks see the coerced arguments (e.g. "49.99" -> 49.99).
        return spec, ProposedAction(action.tool, parsed.model_dump(), action.rationale, action.id), None

    def _checks(self, spec: ToolSpec, action: ProposedAction, ctx: AgentContext,
                session: Session | None = None) -> list[CheckResult]:
        session = session or self.session(ctx.conversation_id)
        results = []
        for check in (*GENERIC_CHECKS, *spec.checks):
            try:
                r = check(action, ctx, self.kb, session)
            except Exception as e:  # a check that cannot run has verified nothing
                r = CheckResult(check.__name__, Outcome.FAIL, f"check crashed ({type(e).__name__}: {e}); blocking")
            if r is not None:
                results.append(r)
        return results

    def _evaluate(self, action: ProposedAction, ctx: AgentContext) -> Verdict:
        spec, action, invalid = self._validate(action)
        if invalid:
            return Verdict(action, Decision.BLOCK, [invalid], caught_by="deterministic")

        session = self.session(ctx.conversation_id)
        results = self._checks(spec, action, ctx, session)
        if any(r.outcome is Outcome.FAIL for r in results):
            return Verdict(action, Decision.BLOCK, results, caught_by="deterministic")

        risk = assess_risk(spec, action, ctx, results, self.kb, session)
        # Anything not positively verified (or that policy reserves for humans) never auto-executes.
        if any(r.outcome in HUMAN_OUTCOMES for r in results):
            return Verdict(action, Decision.ESCALATE, results, risk, caught_by="deterministic")
        if spec.read_only:
            return Verdict(action, Decision.ALLOW, results, risk)

        try:
            outcome = self.router.route(ReviewRequest(action, ctx, results, risk, self.kb, session))
        except Exception as e:  # a review that cannot complete is not a review: a human decides
            failed = CheckResult("model_review", Outcome.UNVERIFIABLE,
                                 f"model review could not complete ({type(e).__name__}); needs a human")
            return Verdict(action, Decision.ESCALATE, [*results, failed], risk, caught_by="review-error")
        if outcome.grounded:
            return Verdict(action, Decision.ALLOW, results, risk, outcome.reviews)
        decision = Decision.BLOCK if outcome.confidence >= self.block_confidence else Decision.ESCALATE
        return Verdict(action, decision, results, risk, outcome.reviews, caught_by=outcome.decided_by)

    # ------------------------------------------------------------------ commit
    def commit(self, verdict: Verdict, ctx: AgentContext, approval_id: str | None = None) -> ExecutionResult:
        """Execute an allowed (or human-approved) action, re-verifying inside the write transaction.

        Idempotent per action id: committing an action that already executed returns the recorded
        result and does nothing, however many callers race to commit it.
        Raises ApprovalError if ``approval_id`` does not (or no longer) authorize the action.
        """
        if verdict.decision is Decision.BLOCK or (verdict.decision is Decision.ESCALATE and approval_id is None):
            raise ValueError(f"cannot commit a {verdict.decision.value} verdict without approval")
        spec, action = self.tools[verdict.action.tool], verdict.action
        if self.audit.get(action.id) is None:
            raise ValueError(f"action {action.id} was never evaluated")
        try:
            with self.db.transaction():
                prior = self.audit.get(action.id)
                if prior["executed"]:  # a retry, a replayed graph node, or a racing worker
                    return ExecutionResult(verdict, executed=True, output=prior["output"])
                waived: set[str] = set()
                if approval_id is not None:
                    waived = set(self.approvals.authorizes(approval_id, action.id)["waives"])
                results = self._checks(spec, action, ctx)
                stale = [r for r in results if r.outcome is Outcome.FAIL]
                # A human approval waives only the checks that asked the approver, nothing new since.
                unapproved = [r for r in results if r.outcome in HUMAN_OUTCOMES and r.check not in waived]
                if unapproved and approval_id is not None:
                    names = ", ".join(sorted({r.check for r in unapproved}))
                    stale.append(CheckResult("approval_scope", Outcome.FAIL,
                                             f"{names} not covered by approval {approval_id}; a new approval is needed"))
                stale += unapproved
                if stale:
                    overruled = Verdict(action, Decision.BLOCK, results + [r for r in stale if r not in results],
                                        verdict.risk, verdict.reviews, caught_by="commit-recheck")
                    self.audit.overrule(overruled)
                    return ExecutionResult(overruled, executed=False)
                before = spec.snapshot(self.kb, action.args) if spec.snapshot else None
                output = spec.fn(**action.args)
                if spec.postcondition and (problem := spec.postcondition(self.kb, action.args, before, output)):
                    raise PostconditionError(problem)  # rolls the write back
                self.audit.mark_executed(action.id, output)
                if approval_id is not None:
                    self.approvals.set_result(approval_id, {"executed": True, "output": output})
        except ApprovalError:
            raise
        except Exception as e:  # the transaction rolled back; nothing was written
            self.audit.mark_failed(action.id, f"{type(e).__name__}: {e}")
            return ExecutionResult(verdict, executed=False, output={"error": f"{type(e).__name__}: {e}"})
        return ExecutionResult(verdict, executed=True, output=output)

    def request_approval(self, verdict: Verdict, ctx: AgentContext) -> str:
        approval_id = self.approvals.create(verdict, ctx)
        self.audit.link_approval(verdict.action.id, approval_id)
        return approval_id

    def commit_approval(self, approval_id: str) -> ExecutionResult:
        """Run an action a human approved (sidecar mode; the agent graph does this itself)."""
        row = self.approvals.get(approval_id)
        if row is None:
            raise ApprovalError(f"approval {approval_id} not found")
        action, ctx = ApprovalQueue.action_of(row)
        result = self.commit(Verdict(action, Decision.ESCALATE), ctx, approval_id)
        if not result.executed:
            self.approvals.set_result(approval_id, {"executed": False, "output": result.output,
                                                    "reasons": result.verdict.reasons})
        return result

    # ------------------------------------------------------------------ one-shot
    def execute(self, action: ProposedAction, ctx: AgentContext) -> ExecutionResult:
        """Evaluate and, if allowed, commit. Escalations are queued for a human."""
        verdict = self.evaluate(action, ctx)
        if verdict.decision is Decision.ALLOW:
            return self.commit(verdict, ctx)
        if verdict.decision is Decision.ESCALATE:
            return ExecutionResult(verdict, executed=False, approval_id=self.request_approval(verdict, ctx))
        return ExecutionResult(verdict, executed=False)


def feedback_for_agent(result: ExecutionResult, recorded_reasons: list[str] | None = None) -> str:
    """Tool-result text returned to the agent so it can correct itself."""
    v = result.verdict
    if result.executed:
        return json.dumps(result.output, default=str)
    if isinstance(result.output, dict) and "error" in result.output:
        return f"The tool failed and nothing was changed: {result.output['error']}"
    if v.decision is Decision.ESCALATE:
        head = "ESCALATED to a human for approval; not executed"
    elif v.caught_by == "commit-recheck":
        head = "BLOCKED at commit: the records changed since this action was checked"
    else:
        head = "BLOCKED by guardrail"
    reasons = v.reasons or recorded_reasons or []
    lines = [f"{head}. The action was NOT performed."] + [f"- {r}" for r in reasons]
    if v.decision is Decision.BLOCK:
        lines.append("Re-check the facts against the conversation and records before retrying.")
    return "\n".join(lines)
