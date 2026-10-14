"""The guardrail: validate -> verify -> score -> route -> decide, then commit atomically.

Lifecycle of one proposed action

    evaluate()   schema + deterministic checks + risk + model review  -> Verdict (audited)
       ALLOW     -> commit(): re-run the deterministic checks and execute the tool inside one
                    database transaction, so nothing that changed since evaluation slips through
       ESCALATE  -> request_approval(): park it in the approval queue; a human decides later and
                    commit(approved=True) runs it (still re-checked; only NEEDS_APPROVAL is waived)
       BLOCK     -> nothing runs; the reasons go back to the agent
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from .checks import GENERIC_CHECKS, Session
from .config import Settings
from .knowledge_base import KnowledgeBase
from .ledger import ApprovalError, ApprovalQueue, AuditLog
from .models import (
    AgentContext,
    CheckResult,
    Decision,
    ExecutionResult,
    Outcome,
    ProposedAction,
    Verdict,
)
from .reviewers import ReviewRequest, reviewers_for
from .risk import RiskRouter, assess_risk
from .store import Database
from .tools import ToolSpec, make_tools


class Guardrail:
    def __init__(
        self,
        kb: KnowledgeBase,
        router: RiskRouter | None = None,
        tools: dict[str, ToolSpec] | None = None,
        block_confidence: float = 0.7,
    ):
        self.kb = kb
        self.db = kb.db
        self.tools = tools if tools is not None else make_tools(kb)
        self.router = router or RiskRouter(*reviewers_for(Settings()))
        self.block_confidence = block_confidence
        self.audit = AuditLog(self.db)
        self.approvals = ApprovalQueue(self.db)

    @classmethod
    def from_settings(cls, settings: Settings, db: Database | None = None) -> Guardrail:
        db = db or Database.seeded(settings.db_path)
        router = RiskRouter(
            *reviewers_for(settings),
            threshold=settings.risk_threshold,
            cascade_below_confidence=settings.cascade_below_confidence,
        )
        return cls(KnowledgeBase(db, settings.today_date), router, block_confidence=settings.block_confidence)

    @classmethod
    def in_memory(cls, router: RiskRouter | None = None) -> Guardrail:
        return cls(KnowledgeBase.in_memory(), router or RiskRouter(*reviewers_for(Settings())))

    def session(self, conversation_id: str) -> Session:
        return Session(self.audit.executed_actions(conversation_id))

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

    def _checks(self, spec: ToolSpec, action: ProposedAction, ctx: AgentContext) -> list[CheckResult]:
        session = self.session(ctx.conversation_id)
        return [
            r for check in (*GENERIC_CHECKS, *spec.checks) if (r := check(action, ctx, self.kb, session)) is not None
        ]

    def _evaluate(self, action: ProposedAction, ctx: AgentContext) -> Verdict:
        spec, action, invalid = self._validate(action)
        if invalid:
            return Verdict(action, Decision.BLOCK, [invalid], caught_by="deterministic")

        results = self._checks(spec, action, ctx)
        if any(r.outcome is Outcome.FAIL for r in results):
            return Verdict(action, Decision.BLOCK, results, caught_by="deterministic")

        risk = assess_risk(spec, action, ctx, results, self.kb)
        if any(r.outcome is Outcome.NEEDS_APPROVAL for r in results):
            return Verdict(action, Decision.ESCALATE, results, risk, caught_by="deterministic")
        if spec.read_only:
            return Verdict(action, Decision.ALLOW, results, risk)

        request = ReviewRequest(action, ctx, results, risk, self.kb, self.session(ctx.conversation_id))
        reviews = self.router.review(request)
        final = reviews[-1]
        if final.grounded:
            return Verdict(action, Decision.ALLOW, results, risk, reviews)
        decision = Decision.BLOCK if final.confidence >= self.block_confidence else Decision.ESCALATE
        return Verdict(action, decision, results, risk, reviews, caught_by=final.reviewer)

    # ------------------------------------------------------------------ commit
    def commit(self, verdict: Verdict, ctx: AgentContext, approved: bool = False) -> ExecutionResult:
        """Execute an allowed (or human-approved) action, re-verifying inside the write transaction."""
        if verdict.decision is Decision.BLOCK or (verdict.decision is Decision.ESCALATE and not approved):
            raise ValueError(f"cannot commit a {verdict.decision.value} verdict without approval")
        spec, action = self.tools[verdict.action.tool], verdict.action
        try:
            with self.db.transaction():
                results = self._checks(spec, action, ctx)
                stale = [r for r in results if r.outcome is Outcome.FAIL]
                stale += [] if approved else [r for r in results if r.outcome is Outcome.NEEDS_APPROVAL]
                if stale:
                    overruled = Verdict(action, Decision.BLOCK, results, verdict.risk, verdict.reviews,
                                        caught_by="commit-recheck")
                    self.audit.overrule(overruled)
                    return ExecutionResult(overruled, executed=False)
                output = spec.fn(**action.args)
                self.audit.mark_executed(action.id, output)
        except Exception as e:  # the transaction rolled back; nothing was written
            self.audit.mark_failed(action.id, f"{type(e).__name__}: {e}")
            return ExecutionResult(verdict, executed=False, output={"error": str(e)})
        return ExecutionResult(verdict, executed=True, output=output)

    def request_approval(self, verdict: Verdict, ctx: AgentContext) -> str:
        approval_id = self.approvals.create(verdict, ctx)
        self.audit.link_approval(verdict.action.id, approval_id)
        return approval_id

    def commit_approval(self, approval_id: str) -> ExecutionResult:
        """Run an action a human approved (sidecar mode; the agent graph does this itself)."""
        row = self.approvals.get(approval_id)
        if row is None or row["status"] != "approved":
            raise ApprovalError(f"approval {approval_id} is not approved")
        action, ctx = ApprovalQueue.action_of(row)
        verdict = Verdict(action, Decision.ESCALATE)
        result = self.commit(verdict, ctx, approved=True)
        self.approvals.set_result(approval_id, {"executed": result.executed, "output": result.output,
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
