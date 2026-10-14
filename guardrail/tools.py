"""The agent's tools and the guardrail contract attached to each one.

A ``ToolSpec`` pairs the side-effecting function with:
  * a strict argument schema derived from its signature: unknown parameters, missing ones,
    NaN/Infinity, booleans posing as numbers and oversized strings are all rejected
  * the deterministic checks that must pass before it may run
  * a grounding contract: for every parameter, the checks that verify it. The contract is
    validated when the tool is registered, so a parameter can never go unverified
  * a postcondition verified after execution, inside the same transaction
  * risk metadata used by the router
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, BeforeValidator, ConfigDict, create_model

from . import checks as c
from .knowledge_base import KnowledgeBase
from .store import new_id, now

# The grounding contract may name these in addition to a tool's own checks.
GENERIC_CHECK_NAMES = {f.__name__ for f in c.GENERIC_CHECKS} | {"args_match_schema", "model_review"}


class Citation(TypedDict):
    policy_id: str
    quote: str


class ContractError(Exception):
    """A tool was registered with an incomplete or inconsistent grounding contract."""


class PostconditionError(Exception):
    """The tool's effect on the system of record was not what the action specified."""


def _no_bool(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("booleans are not accepted as numbers")
    return value


@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    checks: list[c.Check]
    grounding: dict[str, tuple[str, ...]]
    base_risk: float
    irreversible: bool = False
    read_only: bool = False  # read-only tools skip model review (nothing to undo)
    # snapshot(kb, args) -> state before; postcondition(kb, args, before, output) -> error or None
    snapshot: Callable[[KnowledgeBase, dict], Any] | None = None
    postcondition: Callable[[KnowledgeBase, dict, Any, Any], str | None] | None = None
    schema: type[BaseModel] = field(init=False)

    def __post_init__(self) -> None:
        hints = typing.get_type_hints(self.fn)
        params = inspect.signature(self.fn).parameters
        fields = {}
        for name, p in params.items():
            ann = hints[name]
            if ann in (int, float):
                ann = Annotated[ann, BeforeValidator(_no_bool)]
            fields[name] = (ann, ... if p.default is inspect.Parameter.empty else p.default)
        self.schema = create_model(
            f"{self.name}_args",
            __config__=ConfigDict(extra="forbid", strict=False, allow_inf_nan=False, str_max_length=4000),
            **fields,
        )
        self._validate_contract(set(params))

    def _validate_contract(self, params: set[str]) -> None:
        if set(self.grounding) != params:
            missing, extra = params - set(self.grounding), set(self.grounding) - params
            raise ContractError(f"{self.name}: grounding contract missing {sorted(missing)}, unknown {sorted(extra)}")
        known = {f.__name__ for f in self.checks} | GENERIC_CHECK_NAMES
        for param, verifiers in self.grounding.items():
            if not verifiers:
                raise ContractError(f"{self.name}.{param} has no verifier")
            unknown = set(verifiers) - known
            if unknown:
                raise ContractError(f"{self.name}.{param} names checks the tool does not run: {sorted(unknown)}")
            if "model_review" in verifiers and self.read_only:
                raise ContractError(f"{self.name}.{param} relies on model review, which read-only tools skip")
        if not self.read_only and self.postcondition is None:
            raise ContractError(f"{self.name} changes state but declares no postcondition")

    @property
    def description(self) -> str:
        return inspect.getdoc(self.fn) or self.name


# The action being committed. Guardrail.commit sets it so a tool's outbox entries are linked to the
# decision that authorized them (the reconciler checks that link).
CURRENT_ACTION: ContextVar[str | None] = ContextVar("current_action", default=None)


def _outbox(kb: KnowledgeBase, channel: str, recipient: str, subject: str, body: str, payload: dict) -> str:
    """Record an external side effect. It is delivered only after this transaction commits."""
    outbox_id = new_id("ref" if channel == "refund" else "msg")
    kb.db.insert(
        "outbox",
        {"id": outbox_id, "ts": now(), "channel": channel, "recipient": recipient, "subject": subject, "body": body,
         "action_id": CURRENT_ACTION.get(), "payload": payload, "status": "pending"},
    )
    return outbox_id


def _outbox_written(kb: KnowledgeBase, args: dict, before: Any, output: Any) -> str | None:
    row = kb.db.one("SELECT * FROM outbox WHERE id = ?", (output["outbox_id"],))
    expected = args.get("body", args.get("message"))
    if row is None or row["body"] != expected or row["status"] != "pending":
        return "the message stored in the outbox is not the message that was verified"
    return None


def make_tools(kb: KnowledgeBase) -> dict[str, ToolSpec]:
    """Build the tool set bound to a knowledge base (the system the tools mutate)."""

    def issue_refund(order_id: str, customer_id: str, amount: float, reason: str) -> dict:
        """Refund `amount` USD on an order to the customer who placed it."""
        # Reserve the amount on the order and queue the payment; the provider is paid after commit.
        kb.record_refund(order_id, amount)
        outbox_id = _outbox(kb, "refund", customer_id, order_id, f"Refund ${amount:.2f} on {order_id}: {reason}",
                            {"order_id": order_id, "customer_id": customer_id, "amount": amount})
        return {"status": "refunded", "order_id": order_id, "amount": amount, "outbox_id": outbox_id,
                "settlement": "queued for the payment provider"}

    def send_email(to: str, subject: str, body: str) -> dict:
        """Send an email to a customer or staff member."""
        outbox_id = _outbox(kb, "email", to, subject, body, {"to": to, "subject": subject, "body": body})
        return {"status": "queued", "outbox_id": outbox_id, "to": to}

    def answer_customer(customer_id: str, message: str, citations: list[Citation]) -> dict:
        """Reply to the customer in chat. Any policy statement must cite the policy_id and quote
        its text verbatim (use search_policies to get the exact wording)."""
        outbox_id = _outbox(kb, "chat", customer_id, "", message, {"customer_id": customer_id, "message": message})
        return {"status": "queued", "outbox_id": outbox_id}

    def update_inventory(sku: str, delta: int, reason: str) -> dict:
        """Adjust on-hand stock for a SKU by `delta` units (negative to remove)."""
        return {"status": "updated", "sku": sku, "stock": kb.adjust_stock(sku, delta)}

    def lookup_order(order_id: str, customer_id: str) -> dict:
        """Read an order (status, delivery date, line items with names and prices, amount refunded)."""
        order = kb.order(order_id)
        items = [{**it, "name": kb.product(it["sku"])["name"]} for it in order["items"]]
        return {**order, "items": items, "refundable_remaining": kb.refundable_remaining(order)}

    def list_orders(customer_id: str) -> list[dict]:
        """List the customer's orders with their item names, to find an order the customer did not number."""
        return [
            {"id": o["id"], "status": o["status"], "delivered_on": o["delivered_on"],
             "items": [kb.product(it["sku"])["name"] for it in o["items"]]}
            for o in kb.orders_for(customer_id)
        ]

    def search_policies(query: str) -> list[dict]:
        """Find company policies relevant to a question. Returns id, title and exact text."""
        words = {w for w in query.lower().split() if len(w) > 3}
        policies = kb.all_policies()
        hits = [p for p in policies if words & set(f"{p['title']} {p['text']}".lower().replace(".", " ").split())]
        return [{"id": p["id"], "title": p["title"], "text": p["text"]} for p in (hits or policies)]

    # ---- postconditions: the effect must be exactly what was verified -------------
    def refund_snapshot(kb_: KnowledgeBase, args: dict) -> float:
        return kb_.order(args["order_id"])["refunded"]

    def refund_post(kb_: KnowledgeBase, args: dict, before: float, output: Any) -> str | None:
        order = kb_.order(args["order_id"])
        if abs(order["refunded"] - round(before + args["amount"], 2)) > 0.005:
            return f"refunded total moved from {before} to {order['refunded']}, expected +{args['amount']}"
        if order["refunded"] > order["total"] + 0.005:
            return "refunded total exceeds the order total"
        intent = kb_.db.one("SELECT * FROM outbox WHERE id = ?", (output["outbox_id"],))
        if intent is None or intent["payload"] != {"order_id": args["order_id"], "customer_id": args["customer_id"],
                                                   "amount": args["amount"]}:
            return "the queued payment does not match the verified refund"
        return None

    def stock_snapshot(kb_: KnowledgeBase, args: dict) -> int:
        return kb_.product(args["sku"])["stock"]

    def stock_post(kb_: KnowledgeBase, args: dict, before: int, output: Any) -> str | None:
        stock = kb_.product(args["sku"])["stock"]
        if stock != before + args["delta"] or stock < 0:
            return f"stock moved from {before} to {stock}, expected {before + args['delta']}"
        return None

    order_checks = [c.order_exists, c.customer_exists, c.customer_owns_order, c.order_provenance]
    specs = [
        ToolSpec(
            "issue_refund",
            issue_refund,
            [
                c.autonomy_not_revoked,
                c.not_duplicate,
                *order_checks,
                c.order_is_delivered,
                c.amount_positive,
                c.amount_within_refundable,
                c.amount_grounded,
                c.within_refund_window,
                c.under_approval_limit,
                c.customer_daily_refund_cap,
            ],
            grounding={
                "order_id": ("order_exists", "customer_owns_order", "order_provenance", "order_is_delivered",
                             "within_refund_window"),
                "customer_id": ("customer_exists", "acts_for_authenticated_customer", "customer_owns_order"),
                "amount": ("amount_positive", "amount_within_refundable", "amount_grounded", "under_approval_limit",
                           "customer_daily_refund_cap"),
                "reason": ("no_injection", "no_hidden_characters", "model_review"),
            },
            base_risk=0.25,
            irreversible=True,
            snapshot=refund_snapshot,
            postcondition=refund_post,
        ),
        ToolSpec(
            "send_email",
            send_email,
            [
                c.autonomy_not_revoked,
                c.not_duplicate,
                c.recipient_known,
                c.no_cross_customer_disclosure,
                c.numeric_claims_grounded,
                c.commitments_match_history,
                c.claims_supported,
                c.outbound_links_allowed,
            ],
            grounding={
                "to": ("recipient_known", "acts_for_authenticated_customer"),
                "subject": ("no_injection", "no_hidden_characters", "numeric_claims_grounded",
                            "no_cross_customer_disclosure"),
                "body": ("no_injection", "no_hidden_characters", "numeric_claims_grounded",
                         "commitments_match_history", "claims_supported", "no_cross_customer_disclosure",
                         "outbound_links_allowed", "model_review"),
            },
            base_risk=0.15,
            irreversible=True,
            postcondition=_outbox_written,
        ),
        ToolSpec(
            "answer_customer",
            answer_customer,
            [
                c.autonomy_not_revoked,
                c.customer_exists,
                c.citations_exist,
                c.quotes_verbatim,
                c.no_cross_customer_disclosure,
                c.numeric_claims_grounded,
                c.commitments_match_history,
                c.claims_supported,
                c.outbound_links_allowed,
            ],
            grounding={
                "customer_id": ("customer_exists", "acts_for_authenticated_customer"),
                "message": ("no_injection", "no_hidden_characters", "numeric_claims_grounded",
                            "commitments_match_history", "claims_supported", "no_cross_customer_disclosure",
                            "outbound_links_allowed", "model_review"),
                "citations": ("citations_exist", "quotes_verbatim"),
            },
            base_risk=0.05,
            postcondition=_outbox_written,
        ),
        ToolSpec(
            "update_inventory",
            update_inventory,
            [
                c.autonomy_not_revoked,
                c.not_duplicate,
                c.sku_exists,
                c.sku_provenance,
                c.delta_nonzero,
                c.quantity_provenance,
                c.stock_stays_non_negative,
                c.adjustment_within_limit,
            ],
            grounding={
                "sku": ("sku_exists", "sku_provenance"),
                "delta": ("delta_nonzero", "quantity_provenance", "stock_stays_non_negative",
                          "adjustment_within_limit", "model_review"),
                "reason": ("no_injection", "no_hidden_characters"),
            },
            base_risk=0.1,
            snapshot=stock_snapshot,
            postcondition=stock_post,
        ),
        ToolSpec(
            "lookup_order",
            lookup_order,
            order_checks,
            grounding={
                "order_id": ("order_exists", "customer_owns_order", "order_provenance"),
                "customer_id": ("customer_exists", "acts_for_authenticated_customer", "customer_owns_order"),
            },
            base_risk=0.0,
            read_only=True,
        ),
        ToolSpec(
            "list_orders",
            list_orders,
            [c.customer_exists],
            grounding={"customer_id": ("customer_exists", "acts_for_authenticated_customer")},
            base_risk=0.0,
            read_only=True,
        ),
        ToolSpec(
            "search_policies",
            search_policies,
            [],
            grounding={"query": ("no_injection", "no_hidden_characters")},
            base_risk=0.0,
            read_only=True,
        ),
    ]
    return {s.name: s for s in specs}
