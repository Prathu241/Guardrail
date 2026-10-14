"""The reference knowledge base: the system of record every action is checked against.

The agent never reads this directly when proposing an action; the guardrail does,
so a hallucinated ID, price, or policy quote has something concrete to collide with.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from .store import SEED_DIR, Database


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
    """Read/write view of the system-of-record tables in a ``Database``."""

    def __init__(self, db: Database, today: date | None = None):
        self.db = db
        self._today = today

    @classmethod
    def in_memory(cls, seed_dir: str | Path = SEED_DIR, today: date | None = None) -> KnowledgeBase:
        return cls(Database.seeded(":memory:", seed_dir), today)

    # ---- meta -------------------------------------------------------------
    def _meta(self, key: str) -> str:
        return self.db.one("SELECT value FROM meta WHERE key = ?", (key,))["value"]

    @property
    def as_of(self) -> date:
        """'Today' for policy windows. Pinned by the seed data so results are reproducible."""
        return self._today or date.fromisoformat(self._meta("as_of"))

    @property
    def internal_domain(self) -> str:
        return self._meta("internal_domain")

    # ---- lookups -------------------------------------------------------------
    def order(self, order_id: str) -> dict | None:
        return self.db.one("SELECT * FROM orders WHERE id = ?", (order_id,))

    def customer(self, customer_id: str) -> dict | None:
        return self.db.one("SELECT * FROM customers WHERE id = ?", (customer_id,))

    def product(self, sku: str) -> dict | None:
        return self.db.one("SELECT * FROM products WHERE sku = ?", (sku,))

    def policy(self, policy_id: str) -> dict | None:
        return self.db.one("SELECT * FROM policies WHERE id = ?", (policy_id,))

    def customer_by_email(self, email: str) -> dict | None:
        return self.db.one("SELECT * FROM customers WHERE lower(email) = ?", (email.strip().lower(),))

    def orders_for(self, customer_id: str) -> list[dict]:
        return self.db.all("SELECT * FROM orders WHERE customer_id = ? ORDER BY id", (customer_id,))

    def all_orders(self) -> list[dict]:
        return self.db.all("SELECT * FROM orders ORDER BY id")

    def all_customers(self) -> list[dict]:
        return self.db.all("SELECT * FROM customers ORDER BY id")

    def all_products(self) -> list[dict]:
        return self.db.all("SELECT * FROM products ORDER BY sku")

    def all_policies(self) -> list[dict]:
        return self.db.all("SELECT * FROM policies ORDER BY id")

    def all_staff(self) -> list[dict]:
        return self.db.all("SELECT * FROM staff ORDER BY email")

    def ids(self, kind: str) -> set[str]:
        table, col = {"order": ("orders", "id"), "customer": ("customers", "id"),
                      "sku": ("products", "sku"), "policy": ("policies", "id")}[kind]
        return {r[col] for r in self.db.all(f"SELECT {col} FROM {table}")}

    def is_staff(self, email: str) -> bool:
        return self.db.one("SELECT 1 AS x FROM staff WHERE email = ?", (email.strip().lower(),)) is not None

    def known_emails(self) -> set[str]:
        return {c["email"].lower() for c in self.all_customers()} | {s["email"] for s in self.all_staff()}

    def param(self, name: str):
        """Look up a machine-readable policy parameter (e.g. refund_window_days)."""
        for p in self.all_policies():
            if name in p["params"]:
                return p["params"][name]
        raise KeyError(name)

    def policy_for_param(self, name: str) -> str:
        return next(p["id"] for p in self.all_policies() if name in p["params"])

    def product_names(self) -> dict[str, str]:
        return {p["name"].lower(): p["sku"] for p in self.all_products()}

    @staticmethod
    def refundable_remaining(order: dict) -> float:
        return round(order["total"] - order["refunded"], 2)

    @staticmethod
    def closest(value: str, candidates: Iterable[str], n: int = 1) -> list[str]:
        """Nearest candidates by edit distance (transpositions count as one edit)."""
        limit = max(2, len(value) // 4)
        scored = sorted(
            (edit_distance(value, c), -difflib.SequenceMatcher(None, value, c).ratio(), c)
            for c in candidates
        )
        return [c for d, _, c in scored[:n] if d <= limit]

    # ---- mutations (performed only by tools, only inside a guarded commit) ----
    def record_refund(self, order_id: str, amount: float) -> float:
        self.db.run("UPDATE orders SET refunded = round(refunded + ?, 2) WHERE id = ?", (amount, order_id))
        return self.order(order_id)["refunded"]

    def adjust_stock(self, sku: str, delta: int) -> int:
        self.db.run("UPDATE products SET stock = stock + ? WHERE sku = ?", (delta, sku))
        return self.product(sku)["stock"]
