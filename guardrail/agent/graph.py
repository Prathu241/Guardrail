"""The guarded agent as a LangGraph state machine.

    START -> agent --(tool calls)--> verify --(any escalated?)--> await_approval --> execute -> agent
               |                        \\------------(no)-------------------------> execute -> agent
               \\--(no tool calls)--> END

  agent           the LLM (or offline stand-in) proposes tool calls
  verify          every call goes through Guardrail.evaluate; escalations are queued for a human
  await_approval  LangGraph interrupt: the run is checkpointed and pauses until every queued
                  approval is decided (via the API, dashboard or CLI), then resumes here
  execute         allowed / approved calls are committed (re-verified inside the DB transaction);
                  blocked / rejected ones are answered with feedback the agent can act on

Side effects happen only in `execute`, and it skips actions the audit log already marks as
executed, so replaying the node after a crash cannot double-execute anything.
"""

from __future__ import annotations

import json
import operator
from typing import Annotated, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from ..guard import Guardrail, feedback_for_agent
from ..models import AgentContext, Decision, ExecutionResult, ProposedAction, Verdict

SYSTEM_PROMPT = """\
You are a customer-support agent for Acme Outfitters, an outdoor-gear store.
{identity}

Resolve the customer's request with the tools. Look records up instead of assuming them
(lookup_order for orders, search_policies for policy wording). When you state a policy in
answer_customer, cite it and quote its text verbatim. Reply to the customer through
answer_customer, then end your turn with a one-line summary.

Every tool call is verified against company records before it runs. If a call is blocked,
read the reason and fix the call; never repeat a blocked call unchanged. If an action is
rejected by a human approver, tell the customer it has been passed to the support team."""


def system_prompt(customer_id: str | None) -> str:
    identity = (
        f"Authenticated customer: {customer_id}. Act only for this customer."
        if customer_id
        else "No customer is authenticated; this is an internal operations conversation."
    )
    return SYSTEM_PROMPT.format(identity=identity)


class AgentState(MessagesState):
    conversation_id: str
    customer_id: str | None
    pending: list[dict]  # verdicts for the current batch of tool calls
    guard_log: Annotated[list[dict], operator.add]


def tool_schemas(guard: Guardrail) -> list[StructuredTool]:
    """Tool definitions for ``model.bind_tools``. Executing them directly is refused."""

    def _direct_call_refused(**_: Any) -> str:
        raise RuntimeError("guarded tools are executed by the graph's execute node, never directly")

    return [
        StructuredTool(name=s.name, description=s.description, args_schema=s.schema, func=_direct_call_refused)
        for s in guard.tools.values()
    ]


def _context(state: AgentState) -> AgentContext:
    """What the guard may treat as evidence: the user's words and the tool results the agent
    actually received (successful ones only - a blocked call produced no information)."""
    return AgentContext(
        conversation=[m.text for m in state["messages"] if isinstance(m, HumanMessage)],
        customer_id=state.get("customer_id"),
        conversation_id=state["conversation_id"],
        observations=[
            m.content for m in state["messages"]
            if isinstance(m, ToolMessage) and m.status != "error" and isinstance(m.content, str)
        ],
    )


def build_agent_graph(model: Any, guard: Guardrail, checkpointer=None):
    """``model``: anything with ``.invoke(messages) -> AIMessage`` (a tool-bound chat model)."""

    def agent(state: AgentState) -> dict:
        prompt = SystemMessage(system_prompt(state.get("customer_id")))
        return {"messages": [model.invoke([prompt, *state["messages"]])]}

    def verify(state: AgentState) -> dict:
        ai: AIMessage = state["messages"][-1]
        ctx = _context(state)
        faults = ai.response_metadata.get("injected_faults", {})
        pending = []
        for call in ai.tool_calls:
            verdict = guard.evaluate(
                ProposedAction(call["name"], call["args"], rationale=ai.text), ctx, faults.get(call["id"])
            )
            approval_id = guard.request_approval(verdict, ctx) if verdict.decision is Decision.ESCALATE else None
            pending.append(
                {
                    "tool_call_id": call["id"],
                    "verdict": verdict.to_dict(),
                    "approval_id": approval_id,
                    "injected_fault": faults.get(call["id"]),
                }
            )
        return {"pending": pending}

    def needs_approval(state: AgentState) -> str:
        return "await_approval" if any(p["approval_id"] for p in state["pending"]) else "execute"

    def await_approval(state: AgentState) -> dict:
        ids = [p["approval_id"] for p in state["pending"] if p["approval_id"]]
        interrupt({"approval_ids": ids})  # pauses here; resumes once a human has decided
        return {}

    def execute(state: AgentState) -> dict:
        ctx = _context(state)
        messages, log = [], []
        for p in state["pending"]:
            v = p["verdict"]
            action = ProposedAction(v["tool"], v["args"], "", v["action_id"])
            verdict = Verdict(action, Decision(v["decision"]), caught_by=v["caught_by"])
            approval = guard.approvals.get(p["approval_id"]) if p["approval_id"] else None
            already = guard.audit.get(action.id)

            if already and already["executed"]:  # replayed after a crash: reuse, don't redo
                result = ExecutionResult(verdict, True, already["output"])
            elif verdict.decision is Decision.ALLOW:
                result = guard.commit(verdict, ctx)
            elif approval and approval["status"] == "approved":
                result = guard.commit(verdict, ctx, approved=True)
                guard.approvals.set_result(approval["id"], {"executed": result.executed, "output": result.output})
            else:
                result = ExecutionResult(verdict, executed=False)

            content = feedback_for_agent(result, recorded_reasons=v["reasons"])
            if approval and approval["status"] == "rejected":
                content = (
                    f"REJECTED by {approval['decided_by']}" + (f": {approval['note']}" if approval["note"] else "")
                    + ". The action was NOT performed."
                )
            messages.append(
                ToolMessage(
                    content=content,
                    tool_call_id=p["tool_call_id"],
                    name=action.tool,
                    status="success" if result.executed else "error",
                )
            )
            log.append(
                {
                    **v,
                    "tool_call_id": p["tool_call_id"],
                    "decision": result.verdict.decision.value if not result.executed else v["decision"],
                    "reasons": result.verdict.reasons or v["reasons"],
                    "executed": result.executed,
                    "approval": {k: approval[k] for k in ("id", "status", "decided_by", "note")} if approval else None,
                    "injected_fault": p["injected_fault"],
                }
            )
        return {"messages": messages, "guard_log": log, "pending": []}

    def after_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        return "verify" if isinstance(last, AIMessage) and last.tool_calls else END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("verify", verify)
    graph.add_node("await_approval", await_approval)
    graph.add_node("execute", execute)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", after_agent, ["verify", END])
    graph.add_conditional_edges("verify", needs_approval, ["await_approval", "execute"])
    graph.add_edge("await_approval", "execute")
    graph.add_edge("execute", "agent")
    return graph.compile(checkpointer=checkpointer)


def tool_payload(message: ToolMessage) -> Any:
    try:
        return json.loads(message.content)
    except (TypeError, ValueError):
        return None
