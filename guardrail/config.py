"""Runtime configuration, read from GUARDRAIL_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from datetime import date


@dataclass(frozen=True)
class Settings:
    db_path: str = "guardrail.db"
    # Which implementation backs each LLM role: "heuristic"/"rules" run offline, "claude" calls the API.
    reviewer: str = "heuristic"
    agent: str = "rules"
    fast_model: str = "claude-haiku-5-5"
    careful_model: str = "claude-opus-5-5"
    agent_model: str = "claude-opus-5-5"
    # Routing and decision thresholds
    risk_threshold: float = 0.5
    cascade_below_confidence: float = 0.75
    block_confidence: float = 0.7
    # Chaos testing: probability that the agent's tool call is corrupted before the guard sees it
    fault_rate: float = 0.0
    fault_seed: int = 7
    # Pin "today" for policy windows (ISO date). Empty = the knowledge base's as_of date.
    today: str = ""

    @classmethod
    def from_env(cls, **overrides) -> Settings:
        values = {}
        for f in fields(cls):
            raw = os.environ.get(f"GUARDRAIL_{f.name.upper()}")
            if raw is not None:
                values[f.name] = type(f.default)(raw) if not isinstance(f.default, str) else raw
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    @property
    def today_date(self) -> date | None:
        return date.fromisoformat(self.today) if self.today else None
