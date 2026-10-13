"""The reference knowledge base: the system of record every action is checked against.

The agent never reads this directly when proposing an action; the guardrail does,
so a hallucinated ID, price, or policy quote has something concrete to collide with.
"""

from __future__ import annotations

import copy
import difflib
import json
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_KB_DIR = Path(__file__).parent / "data" / "kb"


def edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance: Levenshtein plus adjacent transpositions."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


class KnowledgeBase:
    def __init__(self, data: dict[str, Any]):
        self._data = data
        meta = data["meta"]
        self.as_of: date = date.fromisoformat(meta["as_of"])
        self.internal_domain: str = meta["internal_domain"]
        self.customers = {c["id"]: c for c in data["customers"]}
        self.orders = {o["id"]: o for o in data["orders"]}
        self.products = {p["sku"]: p for p in data["products"]}
        self.policies = {p["id"]: p for p in data["policies"]}
        self.staff = {s["email"].lower(): s for s in data["staff"]}

    @classmethod
    def load(cls, kb_dir: str | Path = DEFAULT_KB_DIR) -> "KnowledgeBase":
        kb_dir = Path(kb_dir)
        data = {
            name: json.loads((kb_dir / f"{name}.json").read_text())
            for name in ("meta", "customers", "orders", "products", "policies", "staff")
        }
        return cls(data)

    def clone(self) -> "KnowledgeBase":
        return KnowledgeBase(copy.deepcopy(self._data))

    # ---- lookups -------------------------------------------------------
    def order(self, order_id: str) -> dict | None:
        return self.orders.get(order_id)

    def customer(self, customer_id: str) -> dict | None:
        return self.customers.get(customer_id)

    def product(self, sku: str) -> dict | None:
        return self.products.get(sku)

    def policy(self, policy_id: str) -> dict | None:
        return self.policies.get(policy_id)

    def customer_by_email(self, email: str) -> dict | None:
        email = email.strip().lower()
        return next((c for c in self.customers.values() if c["email"].lower() == email), None)

    def orders_for(self, customer_id: str) -> list[dict]:
        return [o for o in self.orders.values() if o["customer_id"] == customer_id]

    def known_emails(self) -> set[str]:
        return {c["email"].lower() for c in self.customers.values()} | set(self.staff)

    def param(self, name: str) -> Any:
        """Look up a machine-readable policy parameter (e.g. refund_window_days)."""
        for p in self.policies.values():
            if name in p.get("params", {}):
                return p["params"][name]
        raise KeyError(name)

    def policy_for_param(self, name: str) -> str:
        return next(pid for pid, p in self.policies.items() if name in p.get("params", {}))

    @staticmethod
    def closest(value: str, candidates, n: int = 1) -> list[str]:
        """Nearest candidates by edit distance (transpositions count as one edit)."""
        limit = max(2, len(value) // 4)
        scored = sorted(
            (edit_distance(value, c), -difflib.SequenceMatcher(None, value, c).ratio(), c)
            for c in candidates
        )
        return [c for d, _, c in scored[:n] if d <= limit]

    def refundable_remaining(self, order: dict) -> float:
        return round(order["total"] - order["refunded"], 2)

    def product_names(self) -> dict[str, str]:
        return {p["name"].lower(): sku for sku, p in self.products.items()}

    # ---- mutations (performed only by tools, only after the guard allows) ----
    def record_refund(self, order_id: str, amount: float) -> None:
        self.orders[order_id]["refunded"] = round(self.orders[order_id]["refunded"] + amount, 2)

    def adjust_stock(self, sku: str, delta: int) -> int:
        self.products[sku]["stock"] += delta
        return self.products[sku]["stock"]
