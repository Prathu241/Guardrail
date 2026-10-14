"""Persistence: one SQLite database holding the system of record and the guardrail's own state.

Tables
  system of record   meta, customers, orders, products, policies, staff
  side effects       outbox (emails and chat replies the tools sent)
  guardrail state    conversations, audit, approvals

SQLite keeps the project dependency-free and runnable anywhere; every query goes through
this module, so swapping in Postgres means reimplementing ``Database`` only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SEED_DIR = Path(__file__).parent / "data" / "kb"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta      (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS customers (id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS staff     (email TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products  (sku TEXT PRIMARY KEY, name TEXT NOT NULL, price REAL NOT NULL,
                                      stock INTEGER NOT NULL CHECK (stock >= 0));
CREATE TABLE IF NOT EXISTS orders    (id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
                                      status TEXT NOT NULL, delivered_on TEXT, items TEXT NOT NULL,
                                      total REAL NOT NULL, refunded REAL NOT NULL DEFAULT 0,
                                      CHECK (refunded <= total + 0.001));
CREATE TABLE IF NOT EXISTS policies  (id TEXT PRIMARY KEY, title TEXT NOT NULL, text TEXT NOT NULL,
                                      params TEXT NOT NULL DEFAULT '{}');

CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY, ts TEXT NOT NULL, channel TEXT NOT NULL, recipient TEXT NOT NULL,
    subject TEXT NOT NULL, body TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, customer_id TEXT, status TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    action_id TEXT NOT NULL UNIQUE,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL,
    caught_by TEXT,
    risk REAL,
    route TEXT NOT NULL,
    reasons TEXT NOT NULL,
    warnings TEXT NOT NULL,
    checks TEXT NOT NULL,
    reviews TEXT NOT NULL,
    review_cost REAL NOT NULL DEFAULT 0,
    executed INTEGER NOT NULL DEFAULT 0,
    output TEXT,
    approval_id TEXT,
    injected_fault TEXT);
CREATE INDEX IF NOT EXISTS audit_conversation ON audit(conversation_id);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    rationale TEXT NOT NULL,
    context TEXT NOT NULL,
    reasons TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT,
    note TEXT,
    decided_at TEXT,
    result TEXT);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status);
"""

JSON_COLUMNS = {
    "items", "params", "args", "route", "reasons", "warnings", "checks", "reviews", "output", "context", "result",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for k in JSON_COLUMNS & out.keys():
        if out[k] is not None:
            out[k] = json.loads(out[k])
    return out


class Database:
    """Thread-safe wrapper over a single SQLite connection.

    ``transaction()`` takes the write lock (BEGIN IMMEDIATE) so that a guardrail
    re-check and the tool's write happen atomically.
    """

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._depth = 0
        self._conn.executescript(SCHEMA)

    @classmethod
    def seeded(cls, path: str | Path = ":memory:", seed_dir: str | Path = SEED_DIR) -> Database:
        db = cls(path)
        if not db.is_seeded():
            db.seed(seed_dir)
        return db

    # ---- primitives ---------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[Database]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self._conn.execute("COMMIT")

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [_row(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            return _row(self._conn.execute(sql, params).fetchone())

    def run(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def insert(self, table: str, row: dict[str, Any]) -> None:
        cols = list(row)
        values = tuple(json.dumps(v, default=str) if k in JSON_COLUMNS and v is not None else v for k, v in row.items())
        self.run(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", values)

    def update(self, table: str, key: str, key_value: Any, changes: dict[str, Any]) -> None:
        sets = ", ".join(f"{k} = ?" for k in changes)
        values = tuple(json.dumps(v, default=str) if k in JSON_COLUMNS and v is not None else v for k, v in changes.items())
        self.run(f"UPDATE {table} SET {sets} WHERE {key} = ?", (*values, key_value))

    # ---- seeding --------------------------------------------------------------
    def is_seeded(self) -> bool:
        return self.one("SELECT 1 AS x FROM meta LIMIT 1") is not None

    def seed(self, seed_dir: str | Path = SEED_DIR) -> None:
        seed_dir = Path(seed_dir)
        load = lambda name: json.loads((seed_dir / f"{name}.json").read_text())  # noqa: E731
        with self.transaction():
            for table in ("orders", "policies", "products", "staff", "customers", "meta"):
                self.run(f"DELETE FROM {table}")
            for k, v in load("meta").items():
                self.insert("meta", {"key": k, "value": str(v)})
            for c in load("customers"):
                self.insert("customers", c)
            for s in load("staff"):
                self.insert("staff", {**s, "email": s["email"].lower()})
            for p in load("products"):
                self.insert("products", p)
            for o in load("orders"):
                self.insert("orders", o)
            for p in load("policies"):
                self.insert("policies", {**p, "params": p.get("params", {})})

    def reset_state(self) -> None:
        """Forget conversations, audit and approvals; reseed the system of record."""
        with self.transaction():
            for table in ("audit", "approvals", "conversations", "outbox"):
                self.run(f"DELETE FROM {table}")
            self.seed()
