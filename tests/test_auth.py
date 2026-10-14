"""API keys, roles, identity of approvers, and the production start-up checks."""

import pytest
from fastapi.testclient import TestClient

from guardrail.api.server import create_app
from guardrail.app import build_app
from guardrail.auth import ConfigError, parse_keys
from guardrail.config import Settings

KEYS = ",".join([
    "support-bot:agent:" + "a" * 32,
    "morgan.lee:approver:" + "b" * 32,
    "dana.ops:operator:" + "c" * 32,
    "root:admin:" + "d" * 32,
])
AGENT, APPROVER, OPERATOR, ADMIN = ({"Authorization": f"Bearer {c * 32}"} for c in "abcd")
KAYAK = {"customer_id": "C-1002", "message": "The kayak from order O-5005 leaks, it's defective. I'd like a full refund."}


@pytest.fixture
def client():
    return TestClient(create_app(build_app(Settings(db_path=":memory:", api_keys=KEYS))))


def test_requests_without_a_valid_key_are_refused(client):
    assert client.get("/v1/kb").status_code == 401
    assert client.get("/v1/kb", headers={"Authorization": "Bearer " + "z" * 32}).status_code == 401
    assert client.get("/v1/kb", headers={"X-API-Key": "c" * 32}).status_code == 200
    assert client.get("/healthz").json()["auth"] == "api keys"  # health stays open for probes


def test_roles_limit_what_each_key_can_do(client):
    conv = client.post("/v1/conversations", json=KAYAK, headers=AGENT)
    assert conv.status_code == 201
    approval = conv.json()["pending_approvals"][0]["id"]
    assert client.post(f"/v1/approvals/{approval}/approve", json={}, headers=AGENT).status_code == 403
    assert client.post(f"/v1/approvals/{approval}/approve", json={}, headers=OPERATOR).status_code == 403
    assert client.post("/v1/conversations", json=KAYAK, headers=APPROVER).status_code == 403
    assert client.post("/v1/outbox/dispatch", headers=APPROVER).status_code == 403
    assert client.post("/v1/outbox/dispatch", headers=OPERATOR).status_code == 200
    assert client.get("/v1/audit", headers=AGENT).status_code == 403
    assert client.get("/v1/whoami", headers=ADMIN).json() == {"name": "root", "role": "admin", "auth": True}


def test_the_approver_is_whoever_holds_the_key(client):
    conv = client.post("/v1/conversations", json=KAYAK, headers=AGENT).json()
    approval = conv["pending_approvals"][0]["id"]
    out = client.post(f"/v1/approvals/{approval}/approve", json={"reviewer": "the CEO", "note": "ok"},
                      headers=APPROVER).json()
    assert out["approval"]["decided_by"] == "morgan.lee"  # the claimed name in the body is ignored
    assert out["conversation"]["status"] == "active"


@pytest.mark.parametrize("spec, problem", [
    ("bot:agent", "name:role:secret"),
    ("bot:superuser:" + "x" * 32, "unknown role"),
    ("bot:agent:short", "shorter than"),
    ("bot:agent:" + "x" * 32 + ",bot:admin:" + "y" * 32, "unique"),
])
def test_bad_key_lists_are_rejected_at_start_up(spec, problem):
    with pytest.raises(ConfigError, match=problem):
        parse_keys(spec)


def test_production_refuses_unsafe_configurations(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError) as e:
        build_app(Settings(env="production", db_path=":memory:"))
    for problem in ("GUARDRAIL_API_KEYS is empty", "must be 'claude'", "':memory:'"):
        assert problem in str(e.value)
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY is not set"):
        build_app(Settings(env="production", db_path=":memory:", reviewer="claude", api_keys=KEYS))


def test_production_starts_when_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    app = build_app(Settings(env="production", db_path=str(tmp_path / "g.db"), api_keys=KEYS, reviewer="claude"))
    assert app.settings.env == "production"
    assert "api_keys" not in repr(app.settings) and "a" * 32 not in repr(app.settings)
