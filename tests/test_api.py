import pytest
from fastapi.testclient import TestClient

from guardrail.api.server import create_app
from guardrail.app import build_app
from guardrail.config import Settings


@pytest.fixture
def client():
    return TestClient(create_app(build_app(Settings(db_path=":memory:"))))


def test_health_and_dashboard(client):
    assert client.get("/healthz").json()["status"] == "ok"
    page = client.get("/")
    assert page.status_code == 200 and "Guardrail Console" in page.text


def test_conversation_with_approval_round_trip(client):
    r = client.post("/v1/conversations", json={"customer_id": "C-1002",
                                               "message": "The kayak from order O-5005 leaks. I'd like a full refund."})
    assert r.status_code == 201
    conv = r.json()
    assert conv["status"] == "awaiting_approval"
    assert client.post(f"/v1/conversations/{conv['id']}/messages", json={"message": "hi"}).status_code == 409

    pending = client.get("/v1/approvals").json()
    assert [a["id"] for a in pending] == [conv["pending_approvals"][0]["id"]]
    assert client.post(f"/v1/approvals/{pending[0]['id']}/approve", json={"reviewer": ""}).status_code == 422
    out = client.post(f"/v1/approvals/{pending[0]['id']}/approve", json={"reviewer": "morgan.lee"}).json()
    assert out["conversation"]["status"] == "active"
    assert client.post(f"/v1/approvals/{pending[0]['id']}/reject", json={"reviewer": "x"}).status_code == 409

    orders = {o["id"]: o for o in client.get("/v1/kb").json()["orders"]}
    assert orders["O-5005"]["refunded"] == 649.0
    assert client.get("/v1/conversations").json()[0]["id"] == conv["id"]
    assert any(o["recipient"] == "C-1002" for o in client.get("/v1/outbox").json())


def test_sidecar_evaluate_is_a_dry_run(client):
    body = {"action": {"tool": "issue_refund",
                       "args": {"order_id": "O-5010", "customer_id": "C-1001", "amount": 49.99, "reason": "cracked"}},
            "context": {"conversation": ["My lantern on O-5001 arrived cracked, please refund it."], "customer_id": "C-1001"}}
    v = client.post("/v1/actions/evaluate", json=body).json()
    assert v["decision"] == "block" and "O-5001" in v["reasons"][0]
    assert any(c["check"] == "order_exists" and c["outcome"] == "fail" for c in v["checks"])

    body["action"]["args"]["order_id"] = "O-5001"
    out = client.post("/v1/actions/execute", json=body).json()
    assert out["executed"] and out["output"]["status"] == "refunded"
    audit = client.get("/v1/audit", params={"conversation_id": "sidecar"}).json()
    assert [a["decision"] for a in audit] == ["allow", "block"]
    m = client.get("/v1/metrics").json()
    assert m["actions"] == 2 and m["executed"] == 1 and m["failing_checks"]["order_exists"] == 1


def test_unknown_resources(client):
    assert client.get("/v1/conversations/nope").status_code == 404
    assert client.post("/v1/approvals/nope/approve", json={"reviewer": "x"}).status_code == 404
    assert client.post("/v1/conversations", json={"customer_id": "C-9999", "message": "hi"}).status_code == 400
