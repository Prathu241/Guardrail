"""The agent's tools and the guardrail metadata attached to each one.

A ``ToolSpec`` pairs the side-effecting function with:
  * an argument schema derived from its signature (rejects hallucinated or missing params)
  * the deterministic checks that must pass before it may run
  * risk metadata used by the router
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, TypedDict

from pydantic import BaseModel, ConfigDict, create_model

from . import checks as c
from .knowledge_base import KnowledgeBase


class Citation(TypedDict):
    policy_id: str
    quote: str


@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    checks: list[c.Check]
    base_risk: float
    irreversible: bool = False
    idempotent: bool = False
    schema: type[BaseModel] = field(init=False)

    def __post_init__(self) -> None:
        fields = {
            name: (p.annotation, ... if p.default is inspect.Parameter.empty else p.default)
            for name, p in inspect.signature(self.fn).parameters.items()
        }
        self.schema = create_model(
            f"{self.name}_args",
            __config__=ConfigDict(extra="forbid", strict=False),
            **fields,
        )

    @property
    def description(self) -> str:
        return inspect.getdoc(self.fn) or self.name


def make_tools(kb: KnowledgeBase) -> dict[str, ToolSpec]:
    """Build the tool set bound to a knowledge base (the system the tools mutate)."""

    def issue_refund(order_id: str, customer_id: str, amount: float, reason: str) -> dict:
        """Refund `amount` USD on an order to the customer who placed it."""
        kb.record_refund(order_id, amount)
        return {"status": "refunded", "order_id": order_id, "amount": amount}

    def send_email(to: str, subject: str, body: str) -> dict:
        """Send an email to a customer or staff member."""
        return {"status": "sent", "to": to, "subject": subject}

    def answer_customer(customer_id: str, message: str, citations: list[Citation]) -> dict:
        """Reply to the customer in chat. Policy statements must cite policy_id + verbatim quote."""
        return {"status": "replied", "customer_id": customer_id}

    def update_inventory(sku: str, delta: int, reason: str) -> dict:
        """Adjust on-hand stock for a SKU by `delta` units (negative to remove)."""
        return {"status": "updated", "sku": sku, "stock": kb.adjust_stock(sku, delta)}

    specs = [
        ToolSpec(
            "issue_refund",
            issue_refund,
            [
                c.not_duplicate,
                c.order_exists,
                c.customer_exists,
                c.customer_owns_order,
                c.order_is_delivered,
                c.amount_positive,
                c.amount_within_refundable,
                c.within_refund_window,
                c.under_approval_limit,
                c.amount_matches_line_items,
            ],
            base_risk=0.25,
            irreversible=True,
        ),
        ToolSpec(
            "send_email",
            send_email,
            [
                c.not_duplicate,
                c.recipient_known,
                c.no_cross_customer_disclosure,
                c.numeric_claims_grounded,
                c.commitments_match_history,
            ],
            base_risk=0.15,
            irreversible=True,
        ),
        ToolSpec(
            "answer_customer",
            answer_customer,
            [
                c.customer_exists,
                c.citations_exist,
                c.quotes_verbatim,
                c.no_cross_customer_disclosure,
                c.numeric_claims_grounded,
                c.commitments_match_history,
            ],
            base_risk=0.05,
            idempotent=True,
        ),
        ToolSpec(
            "update_inventory",
            update_inventory,
            [
                c.not_duplicate,
                c.sku_exists,
                c.delta_nonzero,
                c.stock_stays_non_negative,
                c.adjustment_within_limit,
            ],
            base_risk=0.1,
        ),
    ]
    return {s.name: s for s in specs}
