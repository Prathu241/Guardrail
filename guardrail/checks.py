"""Deterministic grounding checks.

Each check compares one aspect of a proposed action against the knowledge base
(or the session's record of what has actually executed) and returns a
``CheckResult``, or ``None`` when it does not apply (e.g. the order does not
exist, which a different check already reports).

Checks are cheap, explainable and never call a model, so they run on every action.
"""

from __future__ import annotations

import difflib
import itertools
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .knowledge_base import KnowledgeBase
from .models import AgentContext, CheckResult, Outcome, ProposedAction


@dataclass
class Session:
    """What has *actually* happened in this agent run (only allowed actions land here)."""

    executed: list[ProposedAction] = field(default_factory=list)

    def executed_keys(self) -> set[str]:
        return {action_key(a) for a in self.executed}

    def refunds(self) -> list[ProposedAction]:
        return [a for a in self.executed if a.tool == "issue_refund"]


Check = Callable[[ProposedAction, AgentContext, KnowledgeBase, Session], "CheckResult | None"]


def action_key(action: ProposedAction) -> str:
    return action.tool + json.dumps(action.args, sort_keys=True, default=str)


def _ok(name: str, msg: str, **ev: Any) -> CheckResult:
    return CheckResult(name, Outcome.PASS, msg, ev)


def _fail(name: str, msg: str, **ev: Any) -> CheckResult:
    return CheckResult(name, Outcome.FAIL, msg, ev)


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------
ID_RE = re.compile(r"\b[A-Z]{1,4}(?:-[A-Z]+)*-\d+\b")
ORDER_ID_RE = re.compile(r"\bO-\d+\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
NUMBER_RE = re.compile(r"(?<![\w.])\$?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)")
# Numbers this small carry too little information to ground ("1 item", "2 days").
TRIVIAL_NUMBERS = {0.0, 1.0, 2.0}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def numbers_in(text: str) -> set[float]:
    """Numeric claims in free text, ignoring the digits inside record IDs and ISO dates."""
    text = DATE_RE.sub(" ", ID_RE.sub(" ", text))
    out = set()
    for m in NUMBER_RE.finditer(text):
        value = float(m.group(1).replace(",", ""))
        if value not in TRIVIAL_NUMBERS:
            out.add(round(value, 2))
    return out


def _has_lookalike_chars(value: str) -> bool:
    return any(ord(ch) > 127 for ch in value)


def _id_check(name: str, kind: str, value: str, table: set[str]) -> CheckResult:
    if value in table:
        return _ok(name, f"{kind} {value} exists")
    if _has_lookalike_chars(value):
        return _fail(
            name,
            f"{kind} {value!r} contains non-ASCII look-alike characters",
            codepoints=[f"U+{ord(c):04X}" for c in value if ord(c) > 127],
        )
    suggestion = KnowledgeBase.closest(value.upper(), table)
    hint = f" (closest real {kind.lower()}: {suggestion[0]})" if suggestion else ""
    return _fail(name, f"{kind} {value} does not exist in the knowledge base{hint}", closest=suggestion)


# --------------------------------------------------------------------------
# generic checks (every tool)
# --------------------------------------------------------------------------
INJECTION_PATTERNS = [
    r"ignore (all |any )?(the |your )?(previous|prior|above|earlier) (instructions|rules|messages)",
    r"disregard (all |any )?(the |your )?(previous|prior|above|earlier|safety)",
    r"\byou are now\b",
    r"\bsystem prompt\b",
    r"</?\s*(system|assistant|tool|instructions?)\s*>",
    r"\b(bypass|skip|disable|override) (the )?(guardrails?|approvals?|checks?|polic(y|ies)|verification)",
    r"'\s*or\s+'?1'?\s*=\s*'?1",
    r";\s*(drop|delete|truncate|alter|update|insert)\s+",
    r"\bunion\s+select\b",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in INJECTION_PATTERNS), re.IGNORECASE)


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def no_injection(action, ctx, kb, session):
    for s in _strings(action.args):
        m = _INJECTION_RE.search(normalize(s))
        if m:
            return _fail(
                "no_injection",
                f"argument contains an instruction/injection payload: {m.group(0)!r}",
                match=m.group(0),
            )
    return _ok("no_injection", "no injection payloads in arguments")


def acts_for_authenticated_customer(action, ctx, kb, session):
    """The agent may only act for the customer this conversation is authenticated as."""
    if ctx.customer_id is None:
        return None
    if "customer_id" in action.args and action.args["customer_id"] != ctx.customer_id:
        return _fail(
            "acts_for_authenticated_customer",
            f"action is for {action.args['customer_id']} but the conversation is authenticated as {ctx.customer_id}",
        )
    if action.tool == "send_email" and not kb.is_staff(action.args["to"]):
        recipient = kb.customer_by_email(action.args["to"])
        if recipient and recipient["id"] != ctx.customer_id:
            return _fail(
                "acts_for_authenticated_customer",
                f"email goes to {recipient['id']} but the conversation is authenticated as {ctx.customer_id}",
            )
    return _ok("acts_for_authenticated_customer", "acts for the authenticated customer")


def not_duplicate(action, ctx, kb, session):
    if action_key(action) in session.executed_keys():
        return _fail("not_duplicate", "an identical action already executed in this session (agent loop?)")
    return _ok("not_duplicate", "action has not executed before")


# --------------------------------------------------------------------------
# issue_refund
# --------------------------------------------------------------------------
def order_exists(action, ctx, kb, session):
    return _id_check("order_exists", "Order", action.args["order_id"], kb.ids("order"))


def customer_exists(action, ctx, kb, session):
    return _id_check("customer_exists", "Customer", action.args["customer_id"], kb.ids("customer"))


def customer_owns_order(action, ctx, kb, session):
    order = kb.order(action.args["order_id"])
    cid = action.args["customer_id"]
    if order is None or kb.customer(cid) is None:
        return None
    if order["customer_id"] != cid:
        return _fail(
            "customer_owns_order",
            f"order {order['id']} belongs to {order['customer_id']}, not {cid}",
            owner=order["customer_id"],
        )
    return _ok("customer_owns_order", f"{cid} owns {order['id']}")


def order_is_delivered(action, ctx, kb, session):
    order = kb.order(action.args["order_id"])
    if order is None:
        return None
    if order["status"] != "delivered":
        return _fail(
            "order_is_delivered",
            f"order {order['id']} is '{order['status']}', refunds apply only to delivered orders",
            status=order["status"],
        )
    return _ok("order_is_delivered", "order was delivered")


def amount_positive(action, ctx, kb, session):
    amount = action.args["amount"]
    if amount <= 0:
        return _fail("amount_positive", f"refund amount must be positive, got {amount}")
    if round(amount, 2) != amount:
        return _fail("amount_positive", f"refund amount has sub-cent precision: {amount}")
    return _ok("amount_positive", "amount is a positive currency value")


def amount_within_refundable(action, ctx, kb, session):
    order = kb.order(action.args["order_id"])
    if order is None:
        return None
    remaining = kb.refundable_remaining(order)
    ev = {"total": order["total"], "already_refunded": order["refunded"], "remaining": remaining}
    if action.args["amount"] > remaining + 1e-9:
        return _fail(
            "amount_within_refundable",
            f"refund ${action.args['amount']:.2f} exceeds the ${remaining:.2f} still refundable on "
            f"{order['id']} (total ${order['total']:.2f}, already refunded ${order['refunded']:.2f})",
            **ev,
        )
    return _ok("amount_within_refundable", f"${remaining:.2f} still refundable", **ev)


def within_refund_window(action, ctx, kb, session):
    order = kb.order(action.args["order_id"])
    if order is None or not order.get("delivered_on"):
        return None
    window = kb.param("refund_window_days")
    age = (kb.as_of - date.fromisoformat(order["delivered_on"])).days
    pid = kb.policy_for_param("refund_window_days")
    if age > window:
        return _fail(
            "within_refund_window",
            f"{order['id']} was delivered {age} days ago; {pid} allows {window} days",
            age_days=age,
            window_days=window,
            policy=pid,
        )
    return _ok("within_refund_window", f"delivered {age} days ago (limit {window})", age_days=age)


def under_approval_limit(action, ctx, kb, session):
    order = kb.order(action.args["order_id"])
    if order is None:
        return None
    limit = kb.param("refund_approval_limit")
    cumulative = round(order["refunded"] + action.args["amount"], 2)
    pid = kb.policy_for_param("refund_approval_limit")
    if cumulative > limit:
        return CheckResult(
            "under_approval_limit",
            Outcome.NEEDS_APPROVAL,
            f"cumulative refunds on {order['id']} would reach ${cumulative:.2f}, above the "
            f"${limit:.2f} limit in {pid}; manager approval required",
            {"cumulative": cumulative, "limit": limit, "policy": pid},
        )
    return _ok("under_approval_limit", f"cumulative ${cumulative:.2f} within ${limit:.2f}")


def amount_matches_line_items(action, ctx, kb, session):
    """A hallucinated amount usually matches nothing on the invoice."""
    order = kb.order(action.args["order_id"])
    if order is None:
        return None
    units = [it["unit_price"] for it in order["items"] for _ in range(it["qty"])]
    sums = {
        round(sum(combo), 2)
        for r in range(1, len(units) + 1)
        for combo in itertools.combinations(units, r)
    }
    sums.add(kb.refundable_remaining(order))
    if round(action.args["amount"], 2) in sums:
        return _ok("amount_matches_line_items", "amount equals a combination of line items")
    return CheckResult(
        "amount_matches_line_items",
        Outcome.WARN,
        f"${action.args['amount']:.2f} does not match any combination of line items on {order['id']}",
        {"line_items": order["items"]},
    )


# --------------------------------------------------------------------------
# outbound messages (send_email, answer_customer)
# --------------------------------------------------------------------------
def recipient_known(action, ctx, kb, session):
    to = action.args["to"].strip().lower()
    known = kb.known_emails()
    if to in known:
        return _ok("recipient_known", f"{to} is on file")
    lookalike = KnowledgeBase.closest(to, known)
    domain = to.rsplit("@", 1)[-1]
    known_domains = {e.rsplit("@", 1)[-1] for e in known}
    domain_lookalike = KnowledgeBase.closest(domain, known_domains)
    if lookalike or (domain_lookalike and domain not in known_domains):
        target = lookalike[0] if lookalike else domain_lookalike[0]
        return _fail(
            "recipient_known",
            f"{to} is a look-alike of known address/domain {target} (possible typo or exfiltration)",
            lookalike_of=target,
        )
    return _fail("recipient_known", f"{to} is not a customer or staff address on file")


def _message_scope(action: ProposedAction, kb: KnowledgeBase) -> tuple[dict | None, str]:
    """Return (customer the message is going to, full message text)."""
    if action.tool == "send_email":
        customer = kb.customer_by_email(action.args["to"])
        text = f"{action.args.get('subject', '')}\n{action.args['body']}"
    else:
        customer = kb.customer(action.args["customer_id"])
        text = action.args["message"]
    return customer, text


def no_cross_customer_disclosure(action, ctx, kb, session):
    customer, text = _message_scope(action, kb)
    if action.tool == "send_email" and customer is None:
        return None  # internal mail; recipient_known handles unknown addresses
    if customer is None:
        return None
    own = {o["id"] for o in kb.orders_for(customer["id"])}
    leaked_orders = sorted(set(ORDER_ID_RE.findall(text)) - own)
    others = [c for c in kb.all_customers() if c["id"] != customer["id"]]
    lowered = text.lower()
    leaked_people = [
        c["id"] for c in others if c["email"].lower() in lowered or c["name"].lower() in lowered
    ]
    if leaked_orders or leaked_people:
        return _fail(
            "no_cross_customer_disclosure",
            f"message to {customer['id']} references other customers' data "
            f"(orders {leaked_orders or '-'}, customers {leaked_people or '-'}); see POL-PRIVACY",
            orders=leaked_orders,
            customers=leaked_people,
        )
    return _ok("no_cross_customer_disclosure", "only the recipient's own records are referenced")


def _order_facts(order: dict, kb: KnowledgeBase) -> tuple[set[float], set[str]]:
    """Values a message may legitimately state about an order (not, e.g., digits of its dates)."""
    nums = {order["total"], order["refunded"], kb.refundable_remaining(order)}
    for it in order["items"]:
        nums |= {it["unit_price"], it["qty"], round(it["unit_price"] * it["qty"], 2)}
    dates = {order["delivered_on"]} if order.get("delivered_on") else set()
    return {round(float(n), 2) for n in nums}, dates


def _grounding_facts(action: ProposedAction, kb: KnowledgeBase, customer: dict | None, session: Session):
    if action.tool == "send_email" and kb.is_staff(action.args["to"]):
        orders = kb.all_orders()  # internal mail may discuss any record
    elif customer is not None:
        orders = kb.orders_for(customer["id"])
    else:
        orders = []
    nums: set[float] = {round(a.args["amount"], 2) for a in session.refunds()}
    dates: set[str] = set()
    for o in orders:
        n, d = _order_facts(o, kb)
        nums |= n
        dates |= d
    if action.tool == "answer_customer":
        # A policy fact only counts as grounded if the agent actually cites that policy.
        policies = [kb.policy(c.get("policy_id", "")) for c in action.args.get("citations", [])]
    else:
        policies = kb.all_policies()
    for p in filter(None, policies):
        nums |= numbers_in(p["text"])
    return nums, dates


def numeric_claims_grounded(action, ctx, kb, session):
    customer, text = _message_scope(action, kb)
    claimed, claimed_dates = numbers_in(text), set(DATE_RE.findall(text))
    if not claimed and not claimed_dates:
        return _ok("numeric_claims_grounded", "message makes no numeric claims")
    nums, dates = _grounding_facts(action, kb, customer, session)
    unsupported = sorted(claimed - nums) + sorted(claimed_dates - dates)
    if unsupported:
        scope = "the cited policies or" if action.tool == "answer_customer" else "policies or"
        return _fail(
            "numeric_claims_grounded",
            f"values {unsupported} are not supported by {scope} the customer's records",
            unsupported=unsupported,
        )
    return _ok("numeric_claims_grounded", "every number and date found in sources")


_COMMITMENT_RE = re.compile(
    r"\b(refunded|have (issued|processed|refunded)|has been (issued|processed|refunded|credited)|"
    r"credited|reimbursed)\b",
    re.IGNORECASE,
)


def commitments_match_history(action, ctx, kb, session):
    """'We've refunded $X' must correspond to a refund that actually happened."""
    customer, text = _message_scope(action, kb)
    sentences = re.split(r"(?<=[.!?])\s+|\n", text)
    issued = {round(a.args["amount"], 2) for a in session.refunds()}
    if customer is not None:
        issued |= {round(o["refunded"], 2) for o in kb.orders_for(customer["id"]) if o["refunded"]}
    for s in sentences:
        if not _COMMITMENT_RE.search(s):
            continue
        for amount in numbers_in(s):
            if amount not in issued:
                return _fail(
                    "commitments_match_history",
                    f"message says a ${amount:.2f} refund was issued, but no such refund executed",
                    claimed=amount,
                    issued=sorted(issued),
                )
    return _ok("commitments_match_history", "claims about completed actions match history")


def citations_exist(action, ctx, kb, session):
    cites = action.args.get("citations", [])
    missing = [c.get("policy_id") for c in cites if kb.policy(c.get("policy_id", "")) is None]
    if missing:
        hints = {m: KnowledgeBase.closest(str(m), kb.ids("policy")) for m in missing}
        return _fail("citations_exist", f"cited policies do not exist: {missing}", closest=hints)
    return _ok("citations_exist", f"{len(cites)} citation(s) resolve to real policies")


def quotes_verbatim(action, ctx, kb, session):
    for c in action.args.get("citations", []):
        policy = kb.policy(c.get("policy_id", ""))
        if policy is None:
            continue
        quote = normalize(c.get("quote", ""))
        if quote and quote not in normalize(policy["text"]):
            actual = max(
                re.split(r"(?<=\.)\s+", policy["text"]),
                key=lambda s: _similarity(normalize(s), quote),
            )
            return _fail(
                "quotes_verbatim",
                f"quote attributed to {policy['id']} is not in the policy. "
                f"Quoted: {c['quote']!r}; the policy actually says: {actual!r}",
                policy=policy["id"],
                actual=actual,
            )
    return _ok("quotes_verbatim", "every quote appears verbatim in its cited policy")


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


# --------------------------------------------------------------------------
# update_inventory
# --------------------------------------------------------------------------
def sku_exists(action, ctx, kb, session):
    return _id_check("sku_exists", "SKU", action.args["sku"], kb.ids("sku"))


def stock_stays_non_negative(action, ctx, kb, session):
    product = kb.product(action.args["sku"])
    if product is None:
        return None
    new = product["stock"] + action.args["delta"]
    if new < 0:
        return _fail(
            "stock_stays_non_negative",
            f"{product['sku']} has {product['stock']} in stock; delta {action.args['delta']} would make it {new}",
            stock=product["stock"],
        )
    return _ok("stock_stays_non_negative", f"stock {product['stock']} -> {new}")


def delta_nonzero(action, ctx, kb, session):
    if action.args["delta"] == 0:
        return _fail("delta_nonzero", "a zero adjustment is a no-op")
    return _ok("delta_nonzero", "delta is non-zero")


def adjustment_within_limit(action, ctx, kb, session):
    limit = kb.param("inventory_max_adjustment")
    if abs(action.args["delta"]) > limit:
        return CheckResult(
            "adjustment_within_limit",
            Outcome.NEEDS_APPROVAL,
            f"|delta| {abs(action.args['delta'])} exceeds {limit}; {kb.policy_for_param('inventory_max_adjustment')} "
            "requires warehouse manager approval",
            {"limit": limit},
        )
    return _ok("adjustment_within_limit", f"|delta| within {limit}")


GENERIC_CHECKS: list[Check] = [no_injection, acts_for_authenticated_customer]
