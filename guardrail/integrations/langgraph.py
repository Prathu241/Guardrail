"""LangGraph wiring: a tool node that routes every tool call through the guardrail.

    agent --(tool_calls)--> guarded_tools --(ToolMessages)--> agent
      \\--(no tool_calls)--> END

Blocked or escalated calls never reach the tool. The agent instead receives a
ToolMessage explaining what failed verification, so it can correct itself.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Iterable

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, MessagesState, StateGraph

from ..guard import Guardrail, feedback_for_agent
from ..models import AgentContext, ProposedAction

SYSTEM_PROMPT = """\
You are a customer-support agent for Acme Outfitters. Use the tools to resolve the
customer's request. Every tool call is verified against company records before it runs;
if a call is blocked, read the reason, fix the call, and do not repeat it unchanged."""


class GuardedState(MessagesState):
    customer_id: str | None
    guard_log: Annotated[list[dict], operator.add]


def langchain_tools(guard: Guardrail) -> list[StructuredTool]:
    """Tool schemas for ``model.bind_tools``. They cannot be invoked directly - only via the guard."""

    def _direct_call_refused(**_: Any) -> str:
        raise RuntimeError("guarded tools must be executed through the guarded_tools node")

    return [
        StructuredTool(
            name=spec.name,
            description=spec.description,
            args_schema=spec.schema,
            func=_direct_call_refused,
        )
        for spec in guard.tools.values()
    ]


def guarded_tool_node(guard: Guardrail):
    def guarded_tools(state: GuardedState) -> dict:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        ctx = AgentContext(
            conversation=[m.text for m in state["messages"] if isinstance(m, HumanMessage)],
            customer_id=state.get("customer_id"),
        )
        messages, log = [], []
        for call in last.tool_calls:
            action = ProposedAction(call["name"], call["args"], rationale=last.text)
            result = guard.execute(action, ctx)
            messages.append(
                ToolMessage(
                    content=feedback_for_agent(result),
                    tool_call_id=call["id"],
                    name=call["name"],
                    status="success" if result.executed else "error",
                )
            )
            log.append(result.verdict.to_dict() | {"executed": result.executed})
        return {"messages": messages, "guard_log": log}

    return guarded_tools


def build_guarded_agent(model: Any, guard: Guardrail, system_prompt: str = SYSTEM_PROMPT):
    """``model`` is anything with ``.invoke(messages) -> AIMessage``, already bound to tools."""

    def agent(state: GuardedState) -> dict:
        return {"messages": [model.invoke([SystemMessage(system_prompt), *state["messages"]])]}

    def route(state: GuardedState) -> str:
        last = state["messages"][-1]
        return "guarded_tools" if isinstance(last, AIMessage) and last.tool_calls else END

    graph = StateGraph(GuardedState)
    graph.add_node("agent", agent)
    graph.add_node("guarded_tools", guarded_tool_node(guard))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["guarded_tools", END])
    graph.add_edge("guarded_tools", "agent")
    return graph.compile()


class ScriptedModel:
    """A stand-in chat model that replays a fixed sequence of turns.

    Used to reproduce a hallucinating agent deterministically (no API key needed).
    """

    def __init__(self, turns: Iterable[AIMessage]):
        self._turns = iter(turns)

    def invoke(self, messages: list[AnyMessage]) -> AIMessage:
        return next(self._turns, AIMessage("Done."))


def claude_agent_model(guard: Guardrail, model: str = "claude-opus-5-5"):
    """A real tool-calling agent backed by Claude via langchain-anthropic."""
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=model, max_tokens=16000).bind_tools(langchain_tools(guard))
