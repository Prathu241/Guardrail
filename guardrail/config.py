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
    critical_model: str = "claude-sonnet-5-5"  # independent second opinion; "" disables it
    agent_model: str = "claude-opus-5-5"
    # Claude call limits (seconds / retries). A reviewer that times out fails closed (-> human).
    reviewer_timeout: float = 30.0
    reviewer_max_retries: int = 2
    agent_timeout: float = 120.0
    agent_max_retries: int = 2
    # Routing and decision thresholds
    risk_threshold: float = 0.5
    cascade_below_confidence: float = 0.75
    critical_threshold: float = 0.8
    block_confidence: float = 0.7
    # A human approval is valid for this long after the action was escalated
    approval_ttl_hours: float = 24.0
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
