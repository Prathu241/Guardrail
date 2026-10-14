"""API authentication and roles, and the production start-up checks.

Keys are configured as ``GUARDRAIL_API_KEYS="name:role:secret,name:role:secret"``. Callers send
``Authorization: Bearer <secret>`` (or ``X-API-Key``). The key's *name* is the caller's identity:
it is what the audit and approval records show as the approver, so nobody can approve under
someone else's name.

    role       may
    agent      run conversations and use the sidecar (evaluate / execute)
    approver   read everything; approve, reject and revoke escalated actions
    operator   read everything; dispatch the outbox, reconcile, resolve incidents
    admin      all of the above

With no keys configured, development mode lets every request through as "anonymous" (admin).
Production mode (``GUARDRAIL_ENV=production``) refuses to start without keys.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from fastapi import Header, HTTPException

SCOPES = {
    "agent": {"act"},
    "approver": {"read", "decide"},
    "operator": {"read", "operate"},
    "admin": {"act", "read", "decide", "operate"},
}
MIN_SECRET_LENGTH = 24


class ConfigError(Exception):
    """The configuration is unsafe or invalid for the requested environment."""


@dataclass(frozen=True)
class Principal:
    name: str
    role: str

    def can(self, scope: str) -> bool:
        return scope in SCOPES[self.role]


ANONYMOUS = Principal("anonymous", "admin")


def parse_keys(spec: str) -> list[tuple[str, str, str]]:
    keys, names = [], set()
    for entry in filter(None, (e.strip() for e in spec.split(","))):
        parts = entry.split(":", 2)
        if len(parts) != 3:
            raise ConfigError("each API key must be written name:role:secret")
        name, role, secret = (p.strip() for p in parts)
        if not name or name in names:
            raise ConfigError(f"API key names must be unique and non-empty ({name!r})")
        if role not in SCOPES:
            raise ConfigError(f"API key {name!r} has unknown role {role!r} (use {', '.join(SCOPES)})")
        if len(secret) < MIN_SECRET_LENGTH:
            raise ConfigError(f"API key {name!r} is shorter than {MIN_SECRET_LENGTH} characters")
        names.add(name)
        keys.append((name, role, secret))
    return keys


class Authenticator:
    def __init__(self, spec: str):
        self.keys = parse_keys(spec)

    @property
    def enabled(self) -> bool:
        return bool(self.keys)

    def identify(self, secret: str | None) -> Principal | None:
        if not self.enabled:
            return ANONYMOUS
        if not secret:
            return None
        found = None
        for name, role, key in self.keys:  # compare against every key: timing reveals nothing
            if hmac.compare_digest(secret.encode(), key.encode()):
                found = Principal(name, role)
        return found

    def require(self, *scopes: str):
        """FastAPI dependency: the caller, if their key grants any of ``scopes``."""

        def dependency(authorization: str | None = Header(None), x_api_key: str | None = Header(None)) -> Principal:
            secret = x_api_key
            if authorization and authorization.lower().startswith("bearer "):
                secret = authorization[7:].strip()
            principal = self.identify(secret)
            if principal is None:
                raise HTTPException(401, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})
            if not any(principal.can(s) for s in scopes):
                raise HTTPException(403, f"role {principal.role!r} may not do this (needs {' or '.join(scopes)})")
            return principal

        return dependency


def check_production(settings) -> list[str]:
    """Refuse unsafe production configurations. Returns warnings for things that are allowed
    but need attention; raises ConfigError for things that must not go live."""
    if settings.env not in ("development", "production"):
        raise ConfigError(f"GUARDRAIL_ENV must be development or production, not {settings.env!r}")
    if settings.env != "production":
        return []
    problems = []
    if not parse_keys(settings.api_keys):
        problems.append("GUARDRAIL_API_KEYS is empty: the API would accept anyone")
    if settings.reviewer != "claude":
        problems.append("GUARDRAIL_REVIEWER must be 'claude' (the heuristic reviewer is an offline stand-in)")
    if (settings.reviewer == "claude" or settings.agent == "claude") and not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("ANTHROPIC_API_KEY is not set")
    if settings.db_path == ":memory:":
        problems.append("GUARDRAIL_DB_PATH is ':memory:': every record would be lost on restart")
    if settings.fault_rate:
        problems.append("GUARDRAIL_FAULT_RATE must be 0 (fault injection is for testing)")
    if problems:
        raise ConfigError("refusing to start in production:\n  - " + "\n  - ".join(problems))
    return ["payments and messages go to SIMULATED providers; connect real ones (see docs/GUIDE.md) "
            "before relying on deliveries"]
