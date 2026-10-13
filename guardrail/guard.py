"""The guardrail: verify -> score -> route -> decide -> (maybe) execute."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from .checks import GENERIC_CHECKS, Session
from .knowledge_base import KnowledgeBase
from .models import (
    AgentContext,
    CheckResult,
    Decision,
    ExecutionResult,
    Outcome,
    ProposedAction,
    Verdict,
)
from .reviewers import ReviewRequest, default_reviewers
from .risk import RiskRouter, assess_risk
from .tools import ToolSpec, make_tools

BLOCK_CONFIDENCE = 0.7


class Guardrail:
    def __init__(
        self,
        kb: KnowledgeBase,
        tools: dict[str, ToolSpec] | None = None,
        router: RiskRouter | None = None,
    ):
        self.kb = kb
        self.tools = tools if tools is not None else make_tools(kb)
        if router is None:
            fast, careful = default_reviewers()
            router = RiskRouter(fast, careful)
        self.router = router
        self.session = Session()
        self.audit_log: list[Verdict] = []

    @classmethod
    def default(cls, reviewer: str | None = None, kb_dir: str | Path | None = None) -> "Guardrail":
        kb = KnowledgeBase.load(kb_dir) if kb_dir else KnowledgeBase.load()
        fast, careful = default_reviewers(reviewer)
        return cls(kb, router=RiskRouter(fast, careful))

    # ------------------------------------------------------------------
    def evaluate(self, action: ProposedAction, ctx: AgentContext) -> Verdict:
        verdict = self._evaluate(action, ctx)
        self.audit_log.append(verdict)
        return verdict

    def _evaluate(self, action: ProposedAction, ctx: AgentContext) -> Verdict:
        spec = self.tools.get(action.tool)
        if spec is None:
            hint = KnowledgeBase.closest(action.tool, self.tools)
            msg = f"tool '{action.tool}' does not exist" + (f" (did you mean '{hint[0]}'?)" if hint else "")
            return self._blocked(action, [CheckResult("tool_exists", Outcome.FAIL, msg)])

        try:
            parsed = spec.schema.model_validate(action.args)
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            return self._blocked(
                action, [CheckResult("args_match_schema", Outcome.FAIL, f"invalid arguments: {problems}")]
            )
        # Run checks against the coerced arguments (e.g. "49.99" -> 49.99).
        action = ProposedAction(action.tool, parsed.model_dump(), action.rationale, action.id)

        results = [
            r
            for check in (*GENERIC_CHECKS, *spec.checks)
            if (r := check(action, ctx, self.kb, self.session)) is not None
        ]
        if any(r.outcome is Outcome.FAIL for r in results):
            return self._blocked(action, results)

        risk = assess_risk(spec, action, ctx, results, self.kb)
        if any(r.outcome is Outcome.NEEDS_APPROVAL for r in results):
            return Verdict(action, Decision.ESCALATE, results, risk, caught_by="deterministic")

        reviews = self.router.review(
            ReviewRequest(action, ctx, results, risk, self.kb, self.session)
        )
        final = reviews[-1]
        if final.grounded:
            return Verdict(action, Decision.ALLOW, results, risk, reviews)
        decision = Decision.BLOCK if final.confidence >= BLOCK_CONFIDENCE else Decision.ESCALATE
        return Verdict(action, decision, results, risk, reviews, caught_by=final.reviewer)

    @staticmethod
    def _blocked(action: ProposedAction, results: list[CheckResult]) -> Verdict:
        return Verdict(action, Decision.BLOCK, results, caught_by="deterministic")

    # ------------------------------------------------------------------
    def execute(self, action: ProposedAction, ctx: AgentContext) -> ExecutionResult:
        """Evaluate, and run the tool only if the guardrail allows it."""
        verdict = self.evaluate(action, ctx)
        if verdict.decision is not Decision.ALLOW:
            return ExecutionResult(verdict, executed=False)
        output = self.tools[action.tool].fn(**verdict.action.args)
        self.session.executed.append(verdict.action)
        return ExecutionResult(verdict, executed=True, output=output)

    def export_audit_log(self, path: str | Path) -> None:
        Path(path).write_text("\n".join(json.dumps(v.to_dict(), default=str) for v in self.audit_log) + "\n")


def feedback_for_agent(result: ExecutionResult) -> str:
    """Tool-result text returned to the agent so it can correct itself."""
    v = result.verdict
    if result.executed:
        return json.dumps(result.output, default=str)
    head = "BLOCKED by guardrail" if v.decision is Decision.BLOCK else "ESCALATED to a human for approval; not executed"
    lines = [f"{head}. The action was NOT performed."] + [f"- {r}" for r in v.reasons]
    if v.decision is Decision.BLOCK:
        lines.append("Re-check the facts against the conversation and records before retrying.")
    return "\n".join(lines)
