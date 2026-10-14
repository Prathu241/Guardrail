#!/usr/bin/env bash
# End-to-end smoke test of the container image (run by CI and `make docker-smoke`).
#   IMAGE=guardrail:test PORT=8000 scripts/docker_smoke.sh
# Exercises: health check, database initialization, a valid action, a blocked hallucination, an
# escalation that survives a container restart, approval, background delivery and reconciliation.
set -euo pipefail

IMAGE=${IMAGE:-guardrail:test}
PORT=${PORT:-8000}
NAME=guardrail-smoke-$$
VOLUME=guardrail-smoke-data-$$
URL=http://127.0.0.1:$PORT

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
fail() { echo "FAIL: $*" >&2; docker logs "$NAME" 2>&1 | tail -30 >&2; exit 1; }
# json <expression on `d`> : evaluate a Python expression against the JSON on stdin
json() { python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"; }
post() { curl -sf -X POST "$URL$1" -H 'content-type: application/json' -d "$2"; }
wait_healthy() {
  for _ in $(seq 1 60); do curl -sf "$URL/healthz" >/dev/null 2>&1 && return 0; sleep 1; done
  fail "service did not become healthy"
}

step "start $IMAGE"
docker run -d --name "$NAME" -p "$PORT:8000" -v "$VOLUME:/data" --health-interval 2s --health-start-period 5s \
  -e GUARDRAIL_WORKER_INTERVAL=0.5 "$IMAGE" >/dev/null
wait_healthy
curl -sf "$URL/healthz" | json 'd'
[ "$(curl -sf "$URL/healthz" | json 'd["worker"]')" = running ] || fail "background worker not running"

step "database initialization"
docker exec "$NAME" guardrail init-db | grep -q "ready (9 orders)" || fail "init-db"
docker exec "$NAME" sh -c 'ls /data' | grep -q guardrail.db || fail "database not on the volume"

step "valid action: the lantern refund executes"
conv=$(post /v1/conversations '{"customer_id":"C-1001","message":"My order O-5001 arrived with a cracked lantern. Can I get a refund for the lantern?"}')
echo "$conv" | json '[(e["tool"], e["decision"], e["executed"]) for e in d["guard_log"]]'
[ "$(echo "$conv" | json 'any(e["tool"] == "issue_refund" and e["executed"] for e in d["guard_log"])')" = True ] \
  || fail "refund did not execute"

step "hallucination: a refund on an order that does not exist is blocked"
verdict=$(post /v1/actions/evaluate '{"action":{"tool":"issue_refund","args":{"order_id":"O-5010","customer_id":"C-1001","amount":49.99,"reason":"cracked"}},"context":{"conversation":["My lantern on O-5001 arrived cracked, please refund it."],"customer_id":"C-1001"}}')
echo "$verdict" | json 'd["decision"], d["reasons"][0]'
[ "$(echo "$verdict" | json 'd["decision"]')" = block ] || fail "hallucinated order was not blocked"

step "escalation: an over-limit refund waits for a human"
kayak=$(post /v1/conversations '{"customer_id":"C-1002","message":"The kayak from order O-5005 leaks, it is defective. I would like a full refund."}')
cid=$(echo "$kayak" | json 'd["id"]')
aid=$(echo "$kayak" | json 'd["pending_approvals"][0]["id"]')
[ "$(echo "$kayak" | json 'd["status"]')" = awaiting_approval ] || fail "kayak refund did not pause"
echo "paused on $aid"

step "restart: the paused run and its approval survive"
docker restart "$NAME" >/dev/null
wait_healthy
[ "$(curl -sf "$URL/v1/conversations/$cid" | json 'd["status"]')" = awaiting_approval ] || fail "paused run lost"
[ "$(curl -sf "$URL/v1/approvals/$aid" | json 'd["status"]')" = pending ] || fail "approval lost"

step "approve: the run resumes and the refund executes"
out=$(post "/v1/approvals/$aid/approve" '{"reviewer":"smoke.test","note":"photos verified"}')
[ "$(echo "$out" | json 'd["conversation"]["status"]')" = active ] || fail "run did not resume"
[ "$(curl -sf "$URL/v1/kb" | json '[o for o in d["orders"] if o["id"] == "O-5005"][0]["refunded"]')" = 649.0 ] \
  || fail "approved refund not recorded"

step "side effects: the worker delivers the outbox and the books reconcile"
for _ in $(seq 1 30); do
  [ "$(curl -sf "$URL/v1/outbox?status=pending" | json 'len(d)')" = 0 ] && break; sleep 0.5
done
curl -sf "$URL/v1/outbox" | json 'sorted({(o["channel"], o["status"]) for o in d})'
[ "$(curl -sf "$URL/v1/outbox" | json 'all(o["status"] == "sent" for o in d)')" = True ] || fail "outbox not delivered"
[ "$(post /v1/reconcile '{}' | json 'len(d["findings"])')" = 0 ] || fail "reconciliation found mismatches"

step "docker HEALTHCHECK reports healthy"
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy ] && break; sleep 1
done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy ] || fail "container health is not healthy"

step "OK"
