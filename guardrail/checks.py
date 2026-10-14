"""Deterministic grounding checks.

Each check compares one aspect of a proposed action against a verifiable source - the
knowledge base, what the user actually said, what the agent actually observed, or what
has actually executed - and returns a ``CheckResult``, or ``None`` when it does not apply
(e.g. the order does not exist, which a different check already reports).

The checks are written as an allowlist: a value must be positively supported by a source.
  PASS          supported by a source
  FAIL          contradicted by a source (or invented: absent from every source)  -> block
  UNVERIFIABLE  no source can settle it                                           -> human
  NEEDS_APPROVAL policy requires a human                                          -> human
  WARN          suspicious but supported                                          -> raises risk

Checks are cheap, explainable and never call a model, so they run on every action.
"""

from __future__ import annotations

import difflib
import itertools
import json
import math
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
    blocked_tools: list[str] = field(default_factory=list)  # tools of actions blocked in this conversation
    refunded_24h: dict[str, float] = field(default_factory=dict)  # customer_id -> refunds executed, last 24h

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


def _unverifiable(name: str, msg: str, **ev: Any) -> CheckResult:
    return CheckResult(name, Outcome.UNVERIFIABLE, msg, ev)


def _approval(name: str, msg: str, **ev: Any) -> CheckResult:
    return CheckResult(name, Outcome.NEEDS_APPROVAL, msg, ev)


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


def sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n", text) if x.strip()]


def contains_token(token: str, text: str) -> bool:
    """Exact whole-token match (so O-501 does not match inside O-5010)."""
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text) is not None


def evidence_text(ctx: AgentContext) -> str:
    """Everything the agent legitimately knows: the user's words and the tool results it saw."""
    return "\n".join([*ctx.conversation, *ctx.observations])


REFUND_WORDS = ("refund", "money back", "reimburse")
PROBLEM_WORDS = (
    "broken", "cracked", "chipped", "scratched", "dented", "damaged", "defective", "leak", "faulty", "stopped working",
    "doesn't work", "does not work", "never arrived", "wrong", "changed my mind", "don't want",
    "do not want", "no longer need", "torn",
)
WHOLE_ORDER = ("whole order", "entire order", "everything", "full refund", "all of it", "refund order",
               "refund my order", "refund the order")


def requested_skus(user_text: str, kb: KnowledgeBase) -> set[str]:
    """Products the user named in the sentences where they ask for a refund or report a problem."""
    asking = " ".join(
        x for x in sentences(user_text.lower())
        if any(w in x for w in REFUND_WORDS) or any(w in x for w in PROBLEM_WORDS)
    )
    found = {sku for sku in kb.ids("sku") if sku.lower() in asking}
    for name, sku in kb.product_names().items():
        last = name.split()[-1]
        if name in asking or re.search(rf"\b{re.escape(last)}s?\b", asking):
            found.add(sku)
    return found


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
    r"\bforget (all |any )?(the |your )?(previous |prior |earlier )?(rules|instructions|guidelines|polic(y|ies)|training)",
    r"\bnew (system )?instructions?\s*:",
    r"\b(act|behave) as (an? |the )?(admin|administrator|system|developer|root|supervisor)",
    r"\b(developer|admin|god|dan) mode\b",
    r"\bjailbreak",
    # text that addresses the verification layer itself is never legitimate tool input
    r"\b(reviewer|auditor|guardrail|verifier|validator|checker)s?\b[^.]{0,60}\b(approve|allow|grounded|pass|accept|ignore)",
    r"\b(mark|treat|consider) (this|it|the action) as (grounded|approved|verified|safe)",
    # forged verdicts and authority: a reviewer's JSON, chat-role markers, claimed sign-offs
    r"\"?\b(grounded|confidence|evidence|decision)\"?\s*:\s*(true|false|\d|\[|\"?(allow|approve))",
    r"\b(system|assistant|developer)\s*:\s",
    r"\b(approval|authori[sz]ation|sign-?off) (has been |was |is |already )?(granted|given|confirmed|obtained)",
    r"\b(pre-?)?(approved|authori[sz]ed|signed off) by (a |the |my |our )?(manager|supervisor|admin|staff|team lead|"
    r"compliance|support lead)",
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


def no_hidden_characters(action, ctx, kb, session):
    """Zero-width, bidi-override and control characters hide text from human reviewers."""
    for s_ in _strings(action.args):
        bad = [
            ch for ch in s_
            if unicodedata.category(ch) in ("Cf", "Co", "Cs") or (unicodedata.category(ch) == "Cc" and ch not in "\n\t")
        ]
        if bad:
            return _fail(
                "no_hidden_characters",
                f"argument contains invisible/control characters {sorted({f'U+{ord(c):04X}' for c in bad})}",
            )
    return _ok("no_hidden_characters", "no invisible or control characters")


# A model that keeps getting blocked is confused or being steered; it must not be allowed to
# keep varying an action until one variant slips through.
BREAKER_SAME_TOOL = 3
BREAKER_TOTAL = 6


def autonomy_not_revoked(action, ctx, kb, session):
    same = session.blocked_tools.count(action.tool)
    total = len(session.blocked_tools)
    if same >= BREAKER_SAME_TOOL or total >= BREAKER_TOTAL:
        return _approval(
            "autonomy_not_revoked",
            f"{same} {action.tool} attempt(s) and {total} action(s) in total were already blocked in this "
            "conversation; the agent no longer acts on its own here and a human must confirm",
            same_tool=same,
            total=total,
        )
    return _ok("autonomy_not_revoked", f"{total} blocked action(s) so far")


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


def _provenance(name: str, kind: str, value: str, ctx: AgentContext) -> CheckResult:
    evidence = evidence_text(ctx)
    if not evidence.strip():
        return _unverifiable(name, f"no conversation or observations were supplied to show where {kind} {value} came from")
    if contains_token(value, evidence):
        return _ok(name, f"{kind} {value} appears in what the user said or the agent looked up")
    return _fail(
        name,
        f"{kind} {value} never appears in the conversation or in any lookup result the agent saw; "
        "the agent invented it",
    )


def order_provenance(action, ctx, kb, session):
    if kb.order(action.args["order_id"]) is None:
        return None
    return _provenance("order_provenance", "order", action.args["order_id"], ctx)


def sku_provenance(action, ctx, kb, session):
    if kb.product(action.args["sku"]) is None:
        return None
    return _provenance("sku_provenance", "SKU", action.args["sku"], ctx)


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
    if not math.isfinite(amount):
        return _fail("amount_positive", f"refund amount is not a finite number: {amount}")
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


def _subset_sums(prices: list[float]) -> set[float]:
    return {round(sum(c), 2) for r in range(1, len(prices) + 1) for c in itertools.combinations(prices, r)}


def amount_grounded(action, ctx, kb, session):
    """The amount must follow from what the customer asked for, not from the model's imagination.

    PASS          the customer stated this amount, or it is the price of exactly the item(s) they
                  asked about, or (whole-order request) the order's refundable balance
    FAIL          it is the price of *other* items on the order (wrong item)
    UNVERIFIABLE  it matches nothing the customer asked for (e.g. an arbitrary partial refund)
    """
    order = kb.order(action.args["order_id"])
    if order is None:
        return None
    name = "amount_grounded"
    amount = round(action.args["amount"], 2)
    user = ctx.text
    if not user.strip():
        return _unverifiable(name, "no conversation was supplied, so the amount cannot be tied to a request")
    units = [(it["sku"], it["unit_price"]) for it in order["items"] for _ in range(it["qty"])]
    asked = requested_skus(user, kb) & {sku for sku, _ in units}
    if amount in numbers_in(user):
        ceiling = round(sum(p for sku, p in units if sku in asked), 2) if asked else kb.refundable_remaining(order)
        if amount > ceiling + 0.005:
            return _unverifiable(
                name, f"the customer asked for ${amount:.2f}, more than the ${ceiling:.2f} the item(s) cost"
            )
        return _ok(name, f"the customer asked for ${amount:.2f}")

    whole = any(w in user.lower() for w in WHOLE_ORDER) or not requested_skus(user, kb)
    remaining = kb.refundable_remaining(order)
    names = lambda skus: ", ".join(kb.product(x)["name"] for x in sorted(skus))  # noqa: E731

    if asked:
        expected = _subset_sums([p for sku, p in units if sku in asked])
        if amount in expected or (whole and amount in {remaining, order["total"]}):
            return _ok(name, f"${amount:.2f} is the price of the item(s) the customer asked about ({names(asked)})")
        if amount in _subset_sums([p for _, p in units]):
            return _fail(
                name,
                f"${amount:.2f} is the price of other item(s) on {order['id']}; the customer asked about "
                f"{names(asked)} (${max(expected):.2f})",
                asked=sorted(asked),
            )
        return _unverifiable(
            name,
            f"${amount:.2f} does not match the price of {names(asked)} (${max(expected):.2f}) or any amount the "
            "customer stated",
        )
    if whole and amount in {remaining, order["total"]}:
        return _ok(name, f"${amount:.2f} is the refundable balance of {order['id']}, which the customer asked to refund")
    return _unverifiable(name, f"${amount:.2f} cannot be tied to anything the customer asked to have refunded")


def customer_daily_refund_cap(action, ctx, kb, session):
    cid = action.args["customer_id"]
    if kb.customer(cid) is None:
        return None
    cap = kb.param("customer_daily_refund_limit")
    total = round(session.refunded_24h.get(cid, 0.0) + action.args["amount"], 2)
    pid = kb.policy_for_param("customer_daily_refund_limit")
    if total > cap:
        return _approval(
            "customer_daily_refund_cap",
            f"refunds to {cid} in the last 24 hours would reach ${total:.2f}, above the ${cap:.2f} limit in {pid}",
            total=total,
            cap=cap,
        )
    return _ok("customer_daily_refund_cap", f"${total:.2f} refunded to {cid} in 24h (cap ${cap:.2f})")


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
    if customer is None and action.tool == "send_email" and ctx.customer_id:
        # Internal mail from a customer's conversation may carry that customer's data, nobody else's.
        customer = kb.customer(ctx.customer_id)
    if customer is None:
        return None  # internal operations mail; recipient_known handles unknown addresses
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


# Words that make a sentence a factual/policy claim which must be backed by a source.
FACT_RE = re.compile(
    r"\b(within|free|warrant(y|ies)|guarantee[sd]?|eligib\w*|always|never|every|policy|policies|costs?|fees?|"
    r"charges?|percent|discount|credit|days?|weeks?|months?|years?|hours?|allowed|entitled)\b|%",
    re.IGNORECASE,
)
ABSOLUTES = {"always", "never", "all", "any", "every", "anytime", "unlimited", "guaranteed", "lifetime"}
# The reference point a policy period or condition is measured from; swapping one for another
# ("30 days of delivery" -> "30 days of purchase") silently changes the policy.
ANCHORS = {"purchase", "bought", "buy", "order", "delivery", "deliver", "arrival", "arrive", "shipment", "ship",
           "payment", "pay", "invoice", "receipt", "registration", "signup"}
STOPWORDS = set(
    "a an the and or but if of to in on at by for with from as is are was were be been being it its this that "
    "these those you your yours we our us i me my they them their he she his her can could will would should "
    "may might must do does did have has had not no so than then there here per about also just very more "
    "most some such only own same too s t get got make take give need want help please thank thanks sure "
    "happy still yes like long well back done just let know".split()
)


def _stem(w: str) -> str:
    for suffix in ("ing", "ies", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return w


def _content_words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", normalize(text)) if w not in STOPWORDS}


def claims_supported(action, ctx, kb, session):
    """Every factual/policy sentence in a customer-facing message must be backed by a source.

    Backed means: most of its content words come from the cited policies (any policy for email),
    it keeps the numeric conditions of the policy sentence it paraphrases (no dropping "over $75"),
    and it adds no absolutes the policy does not make ("always", "all", "never" ...).
    """
    customer, text = _message_scope(action, kb)
    if action.tool == "answer_customer":
        sources = [kb.policy(c.get("policy_id", "")) for c in action.args.get("citations", [])]
    else:
        sources = kb.all_policies()
    sources = [p for p in sources if p]
    source_sentences = [x for p in sources for x in sentences(p["text"])]
    vocab = set().union(*(_content_words(f"{p['title']} {p['text']}") for p in sources)) if sources else set()
    source_absolutes = ABSOLUTES & set(re.findall(r"[a-z]+", normalize(" ".join(p["text"] for p in sources))))
    issued = {round(a.args["amount"], 2) for a in session.refunds()}
    product_words = set().union(*(_content_words(p["name"]) for p in kb.all_products()))

    for claim in sentences(text):
        if not FACT_RE.search(claim):
            continue
        if _COMMITMENT_RE.search(claim) and numbers_in(claim) and numbers_in(claim) <= issued:
            continue  # "we refunded $X" - verified by commitments_match_history
        words = _content_words(claim)
        if not sources:
            return _fail("claims_supported", f"makes a policy claim without citing a policy: {claim!r}")
        coverage = len(words & vocab) / max(1, len(words))
        if coverage < 0.5:
            return _fail(
                "claims_supported",
                f"claim is not supported by the {'cited ' if action.tool == 'answer_customer' else ''}policies: {claim!r}",
                coverage=round(coverage, 2),
            )
        for src in source_sentences:
            if len(words & _content_words(src)) >= 2:
                # "fulfilled by replacement, not refund" vs a claim that asserts "refund"
                for term in re.findall(r"\b(?:not|never|no)\s+([a-z]+)", normalize(src)):
                    stem = _stem(term)
                    negated_in_claim = re.search(rf"\b(?:not|never|no)\s+{re.escape(term)}", normalize(claim))
                    if stem in words and not negated_in_claim:
                        return _fail(
                            "claims_supported",
                            f"claim {claim!r} asserts {term!r}, which the policy sentence {src!r} rules out",
                        )
                dropped = numbers_in(src) - numbers_in(claim)
                if dropped:
                    return _fail(
                        "claims_supported",
                        f"claim {claim!r} drops the condition(s) {sorted(dropped)} of the policy sentence {src!r}",
                    )
        novel = {w for w in words - vocab - product_words - {"policy", "per"} if not w[0].isdigit()}
        if novel & ANCHORS:
            return _fail(
                "claims_supported",
                f"claim {claim!r} measures from {sorted(novel & ANCHORS)}, which the cited policy never does",
            )
        if len(novel) >= 2:
            return _unverifiable(
                "claims_supported",
                f"claim {claim!r} uses terms the policy does not contain {sorted(novel)}; a human should check it",
            )
        added = (ABSOLUTES & set(re.findall(r"[a-z]+", normalize(claim)))) - source_absolutes
        if added:
            return _fail("claims_supported", f"claim {claim!r} adds absolutes {sorted(added)} the policy does not make")
    return _ok("claims_supported", "every factual sentence is backed by a policy")


URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>\"')]+", re.IGNORECASE)
UNSAFE_SCHEME_RE = re.compile(r"\b(?:javascript|data|vbscript|file|ftp|blob):", re.IGNORECASE)
# A domain written without a scheme ("confirm at acme-support.com") is still a link to the reader.
BARE_DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:com|net|org|co|io|info|biz|app|dev|xyz|me|us|uk|eu|de|ru|cn|top|"
    r"site|online|shop|store|link|click|live|ly|gl|support|help|example)\b", re.IGNORECASE)


def outbound_links_allowed(action, ctx, kb, session):
    """Links in customer-facing messages may only point at the company's own domain."""
    _, text = _message_scope(action, kb)
    if m := UNSAFE_SCHEME_RE.search(text):
        return _fail("outbound_links_allowed", f"message contains a {m.group(0)} URI, which is never sent to customers")
    hosts = [re.sub(r"^(https?://)?(www\.)?", "", u.lower()).split("/")[0].split(":")[0] for u in URL_RE.findall(text)]
    hosts += [d.lower() for d in BARE_DOMAIN_RE.findall(EMAIL_RE.sub(" ", URL_RE.sub(" ", text)))]
    for host in hosts:
        if host != kb.internal_domain and not host.endswith("." + kb.internal_domain):
            return _fail("outbound_links_allowed", f"message links to {host}, which is not a company domain (phishing?)")
    return _ok("outbound_links_allowed", "no external links")


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


def quantity_provenance(action, ctx, kb, session):
    """The number of units must be the number in the request."""
    user = ID_RE.sub(" ", ctx.text)
    stated = {int(n) for n in re.findall(r"(?<![\w.])(\d+)(?![\w.])", user)}
    qty = abs(action.args["delta"])
    if not stated:
        return _unverifiable("quantity_provenance", "the request states no quantity, so the adjustment cannot be verified")
    if qty not in stated:
        return _fail("quantity_provenance", f"adjusts by {qty} unit(s) but the request mentions {sorted(stated)}")
    return _ok("quantity_provenance", f"{qty} unit(s) as stated in the request")


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


GENERIC_CHECKS: list[Check] = [no_injection, no_hidden_characters, acts_for_authenticated_customer]
