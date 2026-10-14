"""Agent "brains" that plug into the guarded graph. All expose ``invoke(messages) -> AIMessage``.

* ``claude_agent``    - Claude via langchain-anthropic, tool-bound to the guarded tool schemas.
* ``RuleBasedAgent``  - an offline, deterministic support agent (refunds, policy questions,
                        warehouse notes) so the whole system runs without an API key.
* ``ScriptedModel``   - replays a fixed list of turns; used for reproducible demos and tests.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

ORDER_RE = re.compile(r"\bO-\d+\b")
SKU_RE = re.compile(r"\b[A-Z]{3}-\d{3}\b")
CUSTOMER_RE = re.compile(r"Authenticated customer: (C-\d+)")
REFUND_WORDS = ("refund", "money back", "reimburse")
WHOLE_ORDER = ("whole order", "entire order", "everything", "full refund", "all of it")
PROBLEM_WORDS = ("broken", "cracked", "chipped", "scratched", "dented", "damaged", "defective", "leak", "changed my mind",
                 "stopped working", "never arrived", "wrong", "faulty")
# question keywords -> policy title
POLICY_TOPICS = {
    "refund window": ("how long", "send back", "return", "refund window", "days do i have"),
    "shipping": ("shipping", "delivery cost", "expedited"),
    "warranty": ("warranty", "guarantee"),
}
STOCK_IN = ("received", "arrived", "restock", "delivery of")
STOCK_OUT = ("write off", "write-off", "damaged", "lost", "stolen", "scrap")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()]


def _item_requested(item_name: str, text: str) -> bool:
    """Is this product named where the customer asks for a refund or describes a problem?"""
    asking = " ".join(
        x.lower() for x in _sentences(text)
        if any(w in x.lower() for w in REFUND_WORDS) or any(w in x.lower() for w in PROBLEM_WORDS)
    )
    return re.search(rf"\b{re.escape(item_name.split()[-1].lower())}s?\b", asking) is not None


def _call(name: str, **args: Any) -> dict:
    return {"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:12]}", "type": "tool_call"}


@dataclass
class _Result:
    name: str
    ok: bool
    content: str
    args: dict

    @property
    def payload(self) -> Any:
        try:
            return json.loads(self.content)
        except ValueError:
            return None


class RuleBasedAgent:
    """Deterministic support agent. It plans from the latest customer message and the tool
    results since then, so it is stateless and safe to checkpoint/resume."""

    def invoke(self, messages: list[AnyMessage]) -> AIMessage:
        system = next((m.text for m in messages if isinstance(m, SystemMessage)), "")
        m = CUSTOMER_RE.search(system)
        customer_id = m.group(1) if m else None
        last_human = max(i for i, msg in enumerate(messages) if isinstance(msg, HumanMessage))
        text = messages[last_human].text
        call_args = {c["id"]: c["args"] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls}
        results = [
            _Result(msg.name, msg.status != "error", msg.content, call_args.get(msg.tool_call_id, {}))
            for msg in messages[last_human + 1:]
            if isinstance(msg, ToolMessage)
        ]
        return self._next(text, customer_id, results)

    # ------------------------------------------------------------------------
    def _next(self, text: str, customer_id: str | None, results: list[_Result]) -> AIMessage:
        lower = text.lower()
        done = lambda name: [r for r in results if r.name == name]  # noqa: E731
        answers = done("answer_customer")

        if customer_id is None:
            return self._operations(text, done("update_inventory"))

        orders = ORDER_RE.findall(text)
        wants_refund = any(w in lower for w in REFUND_WORDS)
        question = " ".join(
            s for s in _sentences(text) if s.endswith("?") and not any(w in s.lower() for w in REFUND_WORDS)
        )
        topic = next((t for t, words in POLICY_TOPICS.items() if any(w in question.lower() for w in words)), None)
        has_question = topic is not None
        steps_answered = 0

        if wants_refund:
            if not orders:
                # Never guess an order number: look the customer's orders up and match the item.
                listings = done("list_orders")
                if not listings:
                    return AIMessage("Let me find that order.", tool_calls=[_call("list_orders", customer_id=customer_id)])
                matches = [
                    o["id"] for o in (listings[-1].payload or []) if listings[-1].ok
                    and any(_item_requested(item, text) for item in o["items"])
                ]
                if len(matches) != 1:
                    if not answers:
                        return self._reply(customer_id, "Could you share the order number? It looks like O-1234.")
                    return AIMessage("Asked the customer for the order number.")
                orders = matches
            lookups = done("lookup_order")
            if not lookups:
                return AIMessage("Let me pull up that order.", tool_calls=[_call("lookup_order", order_id=orders[0], customer_id=customer_id)])
            lookup = lookups[-1]
            refunds = done("issue_refund")
            if not lookup.ok:
                if not answers:
                    return self._reply(customer_id, "I couldn't find that order on your account. Could you double-check the order number?")
            elif not refunds:
                return self._refund(text, customer_id, lookup.payload)
            elif len(answers) < 1:
                refund = refunds[-1]
                if not refund.ok:
                    return self._reply(customer_id, "I wasn't able to process this refund automatically, so I've "
                                                    "passed it to our support team. They will follow up with you by email.")
                # State the processing time only by quoting the policy that defines it.
                processing = [r for r in done("search_policies") if r.args.get("query") == "refund processing"]
                if not processing:
                    return AIMessage("Checking the refund processing policy.",
                                     tool_calls=[_call("search_policies", query="refund processing")])
                out = refund.payload
                msg = f"Done! I've refunded ${out['amount']:.2f} on order {out['order_id']}."
                policy = next((p for p in processing[-1].payload or [] if p["title"] == "Refund processing"), None)
                cites = []
                if policy:
                    quote = _sentences(policy["text"])[0]
                    msg += f" {quote}"
                    cites = [{"policy_id": policy["id"], "quote": quote}]
                return self._reply(customer_id, msg, cites)
            steps_answered = 1

        if has_question:
            searches = [r for r in done("search_policies") if r.args.get("query") == topic]
            if not searches:
                return AIMessage("Let me check our policy.", tool_calls=[_call("search_policies", query=topic)])
            if len(answers) <= steps_answered:
                return self._policy_answer(customer_id, topic, question, searches[-1].payload or [])

        if not wants_refund and not has_question and not answers:
            return self._reply(customer_id, "Happy to help! I can process refunds and answer questions about our "
                                            "policies. What can I do for you?")
        final = answers[-1].payload if answers and answers[-1].ok else None
        return AIMessage("Replied to the customer." if final else "I could not complete the request; a human will follow up.")

    def _refund(self, text: str, customer_id: str, order: dict) -> AIMessage:
        lower = text.lower()
        asked = [it for it in order["items"] if _item_requested(it["name"], text)]
        if asked and not any(w in lower for w in WHOLE_ORDER):
            amount = round(sum(it["unit_price"] * it["qty"] for it in asked), 2)
            what = ", ".join(it["name"] for it in asked)
        else:
            amount, what = order["refundable_remaining"], "the order"
        sentence = next((s for s in _sentences(text) if any(w in s.lower() for w in PROBLEM_WORDS)),
                        "Customer requested a refund")
        return AIMessage(
            f"Refunding {what} on {order['id']}.",
            tool_calls=[_call("issue_refund", order_id=order["id"], customer_id=customer_id,
                              amount=amount, reason=sentence.strip()[:200])],
        )

    def _policy_answer(self, customer_id: str, topic: str, question: str, policies: list[dict]) -> AIMessage:
        policy = next((p for p in policies if p["title"].lower() == topic), None)
        if policy is None:
            return self._reply(customer_id, "I'm not certain about that one, so I've asked a colleague to follow up.")
        words = set(re.findall(r"[a-z]+", question.lower()))
        quote = max(_sentences(policy["text"]), key=lambda s: len(words & set(re.findall(r"[a-z]+", s.lower()))))
        return self._reply(customer_id, f"Per our {policy['title'].lower()} policy: {quote}",
                           [{"policy_id": policy["id"], "quote": quote}])

    def _operations(self, text: str, updates: list[_Result]) -> AIMessage:
        if updates:
            return AIMessage("Stock updated." if updates[-1].ok else "Could not update stock; flagged for review.")
        sku = SKU_RE.search(text)
        qty = re.search(r"\b(\d+)\b", SKU_RE.sub("", text))
        if not sku or not qty:
            return AIMessage("Please include the SKU (e.g. LNT-200) and the quantity.")
        lower = text.lower()
        sign = -1 if any(w in lower for w in STOCK_OUT) and not any(w in lower for w in STOCK_IN) else 1
        return AIMessage(
            "Adjusting stock.",
            tool_calls=[_call("update_inventory", sku=sku.group(0), delta=sign * int(qty.group(1)), reason=text[:200])],
        )

    @staticmethod
    def _reply(customer_id: str, message: str, citations: list[dict] | None = None) -> AIMessage:
        return AIMessage(
            "Replying to the customer.",
            tool_calls=[_call("answer_customer", customer_id=customer_id, message=message, citations=citations or [])],
        )


class ScriptedModel:
    """Replays a fixed sequence of turns (a deterministic stand-in for a hallucinating LLM)."""

    def __init__(self, turns: Iterable[AIMessage]):
        self._turns = iter(turns)

    def invoke(self, messages: list[AnyMessage]) -> AIMessage:
        return next(self._turns, AIMessage("Done."))


def claude_agent(tools: list, model: str = "claude-opus-5-5", timeout: float = 120.0, max_retries: int = 2,
                 **client_kwargs: Any):
    """A real tool-calling agent backed by Claude."""
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=model, max_tokens=16000, default_request_timeout=timeout, max_retries=max_retries,
                         **client_kwargs).bind_tools(tools)
