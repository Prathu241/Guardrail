# Guardrail: the complete guide

This is the one document to read first. It covers what Guardrail is, whether it is ready for
you, how to put it in front of your agent, how to deploy and run it, and how to adapt it to a
different business. Deeper material lives in [ARCHITECTURE.md](ARCHITECTURE.md) (diagrams,
data model, design decisions), [HARDENING.md](HARDENING.md) (every defence layer and how
strength is measured) and [RESULTS.md](RESULTS.md) (recorded security results).

1. [What it is](#1-what-it-is)
2. [Is it ready for production?](#2-is-it-ready-for-production)
3. [How it works](#3-how-it-works)
4. [Try it in five minutes](#4-try-it-in-five-minutes)
5. [Putting it in front of your agent](#5-putting-it-in-front-of-your-agent)
6. [API reference](#6-api-reference)
7. [API keys and roles](#7-api-keys-and-roles)
8. [Configuration reference](#8-configuration-reference)
9. [Deploying to production](#9-deploying-to-production)
10. [Running it: the operations runbook](#10-running-it-the-operations-runbook)
11. [Adapting it to your own business](#11-adapting-it-to-your-own-business)
12. [Security model and known limits](#12-security-model-and-known-limits)
13. [Production checklist](#13-production-checklist)

---

## 1. What it is

AI agents that call tools sometimes propose actions that look right but are not grounded in
anything: a refund on an order that does not exist, a policy quote that is almost right, an
email to `ada@exarnple.com`, a refund for the wrong item. Guardrail is a service that sits
**between the agent's decision and the tool running**. Every proposed action is checked
against the company's system of record, the customer's own words and the tool results the
agent actually received. Only then can it change anything.

**The guarantee.** No action the system cannot verify is ever executed automatically. Each
proposed action ends in exactly one of three ways:

| Outcome | When | What happens |
|---|---|---|
| **Allow** | every value is proven from a source and the risk-routed model review agrees | it runs inside one database transaction that re-checks everything first |
| **Block** | something contradicts a source, or was invented | nothing runs; the reason goes back to the agent so it can fix its call |
| **Escalate** | something cannot be proven, or policy reserves it for a person | the run pauses until a person approves, rejects or revokes it |

Doubt never becomes "allow": a reviewer outage, a timeout, malformed model output or a crash
in a check all lead to block or escalate.

**What ships in the box.** A complete service for a fictional outdoor-gear support desk
(refunds, emails, chat replies, stock changes): the verification pipeline, a LangGraph support
agent, human approvals, delivery of payments and messages after commit, reconciliation, a REST
API, a web dashboard, a CLI, Docker images, and a test suite of 229 tests (plus 11 live-API tests that
run when an Anthropic key is set) and a 92-case red-team suite.

## 2. Is it ready for production?

**Short answer: it is ready to run as a hardened pilot for this support-desk domain. It is not
yet something a large AI company can drop in front of arbitrary agents.** Here is what that
means in practice.

**Ready and verified:**

- The verification pipeline and its guarantee, measured with **simulated** reviewers and agents
  (no live model yet; every figure's configuration is in [EVIDENCE.md](EVIDENCE.md)): 75/75
  attacks stopped and 0/17 legitimate actions wrongly stopped on 92 fixed red-team cases replayed
  without an agent (E1), including 10 held-out attacks written after the defences (E2); 35/35
  injected faults blocked in chaos runs of the rule-based stand-in agent (E3).
- Correctness under concurrency and crashes: limits hold with racing threads and processes;
  every action executes at most once; crashed runs recover without repeating anything.
- Side effects that can't be rolled back: delivered after commit with idempotency keys,
  retried, compensated, and reconciled against the providers.
- Deployment: API keys with roles, a production mode that refuses unsafe settings, HTTPS
  through Caddy, a non-root read-only container, health checks, backups, schema migrations.
  The production stack was started and exercised end to end.
- CI on GitHub: lint, tests on Python 3.11 to 3.13, the security gate, chaos, Docker build and
  container smoke test, all green.

**Must be done before real customers and real money:**

1. **Run it with the real Claude models.** The live-API tests (`make live`) and the red-team
   suite with Claude reviewers (`guardrail redteam --reviewer claude`) have not been run, because
   no API key was available while building it. The failure handling of the Claude paths is
   tested through the real SDKs, but their judgement on real cases is not.
2. **Connect your real systems.** Payments and messages go to *simulated* providers, and the
   system of record is the bundled SQLite seed data. You implement the provider interface for
   your payment processor and messaging service, and point the knowledge base at your real
   records (section 11).
3. **Fit the checks to your business.** The 30 checks encode this store's policies (refund
   window, approval limit, daily cap). Another business needs its own tools, checks and
   red-team cases. This is engineering work, typically days per tool, not configuration.
4. **Scale beyond one host.** SQLite is right for one server (several workers on it are fine).
   Several servers need the Postgres path described in section 11.
5. **Your own security review**, including single sign-on for approvers if you need it (the
   built-in auth is API keys).

**For large AI platforms specifically.** The *pattern* is general: a sidecar any agent stack
can call before acting, with an allowlist of proofs, risk-routed model review, human
escalation and transactional commit. The *checks* are specific to a domain, as they must be: a
guardrail can only prove an action against records it understands. Use it as the reference
implementation for each high-stakes tool you expose to an agent, not as a universal filter.

## 3. How it works

```
agent proposes a tool call
   │
   ▼
1 schema         strict types; unknown tools/params, NaN, booleans-as-numbers rejected
2 30 checks      allowlist: each proves one value from a source → PASS / FAIL / UNVERIFIABLE / NEEDS_APPROVAL
3 risk score     additive and explained: money, irreversibility, identity, earlier blocks
4 model review   fast (Haiku) → careful (Opus) → for critical risk two models must both agree;
                 approvals must quote evidence that is then found verbatim
   │
   ├─ BLOCK     → reason back to the agent
   ├─ ESCALATE  → approval queue → person approves / rejects / revokes → run resumes
   └─ ALLOW     → one write transaction: already executed? approval still valid? re-run every
                  check, run the tool (it records payments/messages as outbox intents), verify
                  the postcondition, mark executed → COMMIT
                     │
                     ▼ after commit
                  dispatcher → providers (idempotency key) → retries, compensation, incidents
                  reconciler → compares providers, outbox and audit log
```

The checks fall into groups: existence and ownership of every ID; **provenance** (every ID,
amount and quantity must appear in what the customer said or in a lookup the agent actually
made); policy limits read from the policy records; claim verification for anything said to a
customer (verbatim quotes, grounded numbers, no dropped conditions, no external links, no other
customers' data); input hygiene (injection, hidden characters, look-alike IDs and emails); and
brakes (a circuit breaker after repeated blocks, a 24-hour per-customer refund cap). The full
list, with the attack each one stops, is in [HARDENING.md](HARDENING.md).

## 4. Try it in five minutes

Requires Python 3.11 or newer.

```bash
git clone https://github.com/Prathu241/Guardrail.git && cd Guardrail
pip install -e ".[dev]"
python -m guardrail demo        # a valid action runs · a hallucination is blocked · a person decides
python -m guardrail serve       # dashboard http://127.0.0.1:8000/   API docs http://127.0.0.1:8000/docs
```

Or with Docker: `docker compose up --build`, then open http://localhost:8000/.

Everything runs offline by default (rule-based agent, heuristic reviewers, simulated
providers). In development mode no API key is needed.

## 5. Putting it in front of your agent

There are two ways to use it. Both share the same checks, approval queue and audit log.

**Agent mode.** Guardrail runs its own LangGraph support agent (Claude or the offline
rule-based one). You send customer messages to `POST /v1/conversations`; every tool call the
agent makes is verified. Use this if you are building on the bundled agent.

**Sidecar mode.** Your own agent (any framework, any language) asks Guardrail before every
tool call and only acts on an "allow". This is how to put it in front of an existing agent.

```python
import uuid, httpx

GUARD = httpx.Client(base_url="https://guardrail.example.com",
                     headers={"Authorization": "Bearer <agent key>"}, timeout=60)

def guarded_call(tool, args, *, conversation_id, customer_id, user_messages, tool_results):
    """Ask Guardrail to verify and execute one tool call. Never call the tool yourself."""
    response = GUARD.post(
        "/v1/actions/execute",
        headers={"Idempotency-Key": str(uuid.uuid4())},   # reuse the same key when retrying
        json={
            "action": {"tool": tool, "args": args},
            "context": {
                "conversation": user_messages,          # what the user said, verbatim
                "customer_id": customer_id,             # the authenticated customer
                "conversation_id": conversation_id,     # one per conversation, never shared
                "observations": tool_results,           # results of lookups the agent really made
            },
        },
    ).json()
    decision = response["verdict"]["decision"]
    if decision == "allow":
        return {"ok": True, "result": response["output"]}
    if decision == "escalate":
        return {"ok": False, "pending_approval": response["approval_id"],
                "tell_agent": "A person must approve this; tell the customer it has been passed on."}
    return {"ok": False, "tell_agent": "BLOCKED: " + "; ".join(response["verdict"]["reasons"])}
```

Rules for sidecar callers:

- **Send the reasons back to your agent** as the tool result. Agents fix their own calls from
  them (in the chaos runs the agent recovers from every injected fault).
- **Pass honest context.** Guardrail can only verify against the evidence you give it:
  `conversation` must be the user's real words and `observations` the real tool results.
- **Use one `conversation_id` per conversation.** The duplicate check and the circuit breaker
  work per conversation; the default `"sidecar"` would pool all callers together.
- **Retry with the same `Idempotency-Key`.** A retried request returns the original response
  and never acts twice.
- **Escalations.** When a person approves, Guardrail runs the action itself. Poll
  `GET /v1/approvals/{approval_id}`; `status` becomes `approved`, `rejected`, `revoked` or
  `expired`, and `result.executed` tells you whether it ran.
- Use `POST /v1/actions/evaluate` for a dry run that decides without executing.

## 6. API reference

Interactive documentation (OpenAPI) is served at `/docs`. Every route except `/healthz` and
the dashboard needs an API key when keys are configured (section 7).

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/healthz` | open | status, database, worker, outbox counts, open incidents |
| GET | `/v1/whoami` | any | the key's name and role |
| POST | `/v1/conversations` | agent | start an agent-mode conversation `{message, customer_id?}` |
| GET | `/v1/conversations`, `/v1/conversations/{id}` | agent, read | list / read with transcript and guard log |
| POST | `/v1/conversations/{id}/messages` | agent | continue (409 while waiting for approval) |
| POST | `/v1/actions/evaluate` | agent | sidecar dry run: verdict, checks, risk factors, reviews |
| POST | `/v1/actions/execute` | agent | sidecar: evaluate and execute if allowed; honours `Idempotency-Key` |
| GET | `/v1/actions/{action_id}` | agent, read | the audit record of one action |
| GET | `/v1/approvals?status=` | read | `pending` (default), `approved`, `rejected`, `revoked`, `expired`, `all` |
| GET | `/v1/approvals/{id}` | read | one approval, with what it waives and its result |
| POST | `/v1/approvals/{id}/approve`, `/reject`, `/revoke` | decide | `{note}`; the decider is the key's name |
| GET | `/v1/audit?conversation_id=&decision=` | read | the audit log |
| GET | `/v1/metrics` | read | decisions, failing checks, layers, review cost, outbox, incidents |
| GET | `/v1/outbox?status=` | read | payments and messages with their delivery state |
| POST | `/v1/outbox/dispatch` | operate | deliver due entries now |
| POST | `/v1/reconcile` | operate | compare with the providers; opens incidents |
| GET | `/v1/incidents?status=` | read | `open` (default), `resolved`, `all` |
| POST | `/v1/incidents/{id}/resolve` | operate | `{note}`: how it was handled |
| GET | `/v1/kb` | read | the reference data |

Errors: 401 missing or invalid key, 403 role not allowed, 404 not found, 409 conflict (already
decided, busy, waiting for approval), 422 invalid request.

## 7. API keys and roles

Keys are configured in `GUARDRAIL_API_KEYS` as a comma-separated list of `name:role:secret`.
Callers send `Authorization: Bearer <secret>` (or `X-API-Key: <secret>`). The key's **name**
is the caller's identity: it is recorded as the approver of every decision made with it, so
nobody can approve under someone else's name.

| Role | Scope | Give it to |
|---|---|---|
| `agent` | conversations, sidecar evaluate/execute | the agent service |
| `approver` | read everything; approve, reject, revoke | each person who signs off (one key per person) |
| `operator` | read everything; dispatch, reconcile, resolve incidents | on-call / operations |
| `admin` | everything | break-glass only |

- Secrets must be at least 24 characters. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- An agent key cannot approve its own escalations, by design.
- **Rotation.** Add the new key, restart, switch the client, remove the old key, restart.
- **The dashboard** asks for a key (top right) when keys are on; it keeps it for the browser
  session only. Use an approver or admin key there.
- With no keys configured, development mode lets every request through as `anonymous`.
  Production mode refuses to start without keys.

## 8. Configuration reference

All settings are environment variables (or a `.env` file read by Docker Compose).

| Variable | Default | Meaning |
|---|---|---|
| `GUARDRAIL_ENV` | `development` | `production` refuses unsafe settings (section 9) |
| `GUARDRAIL_API_KEYS` | empty | `name:role:secret,...` (section 7) |
| `GUARDRAIL_DB_PATH` | `guardrail.db` | the SQLite file; checkpoints and provider ledgers sit next to it |
| `GUARDRAIL_REVIEWER` | `heuristic` | `claude` for model review (required in production) |
| `GUARDRAIL_AGENT` | `rules` | `claude` to run Claude as the bundled support agent |
| `ANTHROPIC_API_KEY` | | needed when reviewer or agent is `claude` |
| `GUARDRAIL_FAST_MODEL` / `CAREFUL_MODEL` / `CRITICAL_MODEL` | Haiku 5.5 / Opus 5.5 / Sonnet 5.5 | reviewer tiers; an empty critical model disables two-model consensus |
| `GUARDRAIL_AGENT_MODEL` | Opus 5.5 | the bundled agent's model |
| `GUARDRAIL_REVIEWER_TIMEOUT`, `_MAX_RETRIES` | 30 s, 2 | a reviewer that times out fails closed (escalates) |
| `GUARDRAIL_AGENT_TIMEOUT`, `_MAX_RETRIES` | 120 s, 2 | an agent failure ends the turn with nothing changed |
| `GUARDRAIL_RISK_THRESHOLD` | 0.5 | risk at which the careful reviewer takes over |
| `GUARDRAIL_CRITICAL_THRESHOLD` | 0.8 | risk at which two models must agree |
| `GUARDRAIL_CASCADE_BELOW_CONFIDENCE` | 0.75 | fast-tier confidence below which the careful tier re-reviews |
| `GUARDRAIL_BLOCK_CONFIDENCE` | 0.7 | reviewer confidence needed to block rather than escalate |
| `GUARDRAIL_APPROVAL_TTL_HOURS` | 24 | approvals expire after this |
| `GUARDRAIL_WORKER_INTERVAL` | 2 s | background delivery and recovery; 0 turns the worker off |
| `GUARDRAIL_RECONCILE_INTERVAL` | 60 s | how often to reconcile with the providers |
| `GUARDRAIL_OUTBOX_MAX_ATTEMPTS`, `_BACKOFF_SECONDS` | 5, 2 s | delivery retries (backoff doubles each attempt) |
| `GUARDRAIL_FAULT_RATE`, `_FAULT_SEED` | 0, 7 | chaos testing only; must be 0 in production |
| `GUARDRAIL_TODAY` | empty | pin "today" for policy windows (testing) |

Business limits (refund window, approval limit, daily cap, maximum stock adjustment) are not
settings: they live in the policy records (`guardrail/data/kb/policies.json` → `params`), so the
checks read them from the same source the agent quotes.

## 9. Deploying to production

The supported deployment is Docker Compose on one Linux host: Guardrail behind Caddy, which
obtains and renews HTTPS certificates automatically.

**You need:** a Linux server with Docker and the Compose plugin, a DNS name pointing at it,
ports 80 and 443 open, and an Anthropic API key.

**Step 1: get the code onto the server.**
```bash
git clone https://github.com/Prathu241/Guardrail.git && cd Guardrail
```

**Step 2: configure it.**
```bash
cp deploy/.env.production.example deploy/.env.production
chmod 600 deploy/.env.production
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # once per API key
```
Edit `deploy/.env.production`: set `DOMAIN`, the `GUARDRAIL_API_KEYS` (one approver key per
person), `ANTHROPIC_API_KEY`, and `GUARDRAIL_AGENT` (`claude` for the bundled agent; for
sidecar-only use it does not matter). The file is in `.gitignore`; never commit it.

**Step 3: start it.**
```bash
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production up -d --build
```
The Guardrail container starts first; Caddy starts once Guardrail's health check passes. In
production mode Guardrail **refuses to start** if keys are missing, the reviewer is not Claude,
the Anthropic key is missing, the database is in memory or fault injection is on. It prints
exactly what to fix (`docker compose ... logs guardrail`).

**Step 4: verify it.**
```bash
curl https://$DOMAIN/healthz          # "env":"production","auth":"api keys","worker":"running"
curl -o /dev/null -w '%{http_code}\n' https://$DOMAIN/v1/kb                                   # 401
curl -H "Authorization: Bearer <agent key>" -X POST https://$DOMAIN/v1/actions/evaluate \
  -H 'content-type: application/json' \
  -d '{"action":{"tool":"issue_refund","args":{"order_id":"O-5010","customer_id":"C-1001","amount":49.99,"reason":"cracked"}},
       "context":{"conversation":["My lantern on O-5001 arrived cracked, please refund it."],"customer_id":"C-1001","conversation_id":"smoke-1"}}'
# → "decision":"block", "Order O-5010 does not exist ... (closest real order: O-5001)"
```
Then open `https://$DOMAIN/` and enter an approver key at the top right.

**What the production stack does for you:** HTTPS with HSTS and an HTTP→HTTPS redirect;
security headers; a 1 MB request limit; the app reachable only through the proxy; a non-root,
read-only container with all Linux capabilities dropped; data on a named volume; automatic
restarts; a health check that fails when the service is degraded.

**What this was tested with:** the production stack was started from these files with
`DOMAIN=localhost` and exercised over HTTPS: 401 without a key, 403 when the agent key tried to
approve, the approver recorded from the key not the request body, an unreachable reviewer
failing closed to a person, an approved refund delivered, and a backup taken while serving.

**Without Docker.** Install with `pip install .` into a virtualenv, run
`guardrail serve --host 127.0.0.1 --port 8000` under systemd with the same environment
variables, and put any TLS reverse proxy (nginx, Caddy) in front. Run one process per database.

**On Kubernetes.** Run a single replica (a StatefulSet with one persistent volume for `/data`),
the same environment variables from a Secret, `/healthz` as the readiness and liveness probe,
and your ingress for TLS. Do not scale replicas on SQLite; see section 11 for multi-host.

**Upgrading.**
```bash
git pull
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production exec guardrail guardrail backup /data/backups
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production up -d --build
```
New database columns are added automatically when the new version opens the database. Runs
interrupted by the restart are resumed from their checkpoints on start-up.

## 10. Running it: the operations runbook

**Daily work: approvals.** Escalations appear in the dashboard's Approvals tab (or
`GET /v1/approvals`). Each shows the action, the exact reasons, and which checks the approval
covers. Approving re-checks everything before it runs; if something new has come up since (for
example the customer hit the daily cap with another refund), it is blocked and needs a fresh
approval. Approvals expire after 24 hours. An approved action that has not run yet can be
revoked.

**Incidents.** `GET /v1/incidents` (CLI: `guardrail incidents`; open counts are also in `/healthz` and `/v1/metrics`). Resolve
each with a note saying what was done (`POST /v1/incidents/{id}/resolve`).

| Kind | Meaning | What to do |
|---|---|---|
| `refund_failed` | the payment provider refused; the reserved amount was released | tell the customer; pay another way or re-propose |
| `message_failed` | an email or chat reply could not be delivered | contact the customer another way |
| `delivered_after_failure` | we gave up, but the provider did act; the reservation was re-applied | check the customer was not told the refund failed |
| `unknown_provider_record` | money or a message at the provider with no Guardrail decision | **treat as a security event**: who acted outside the guardrail? |
| `missing_at_provider` | we marked it sent; the provider has no record | check with the provider; re-send if needed |
| `payload_mismatch` | the provider's record differs from what we verified | investigate; correct with the provider |
| `refund_without_intent`, `intent_without_decision` | the audit log and the payment queue disagree | investigate the tool change or manual edit that caused it |

**Monitoring.** Poll `/healthz` and alert when `status` is not `ok`, `worker` is not `running`,
`worker_errors` is non-empty, `outbox.pending` keeps growing, or `open_incidents` is above
zero. `/v1/metrics` gives block rates by check and catches by layer: a sudden change in either
usually means the agent, a model or the data changed.

**Backups and restore.**
```bash
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production exec guardrail guardrail backup /data/backups
docker cp "$(docker compose -f deploy/docker-compose.prod.yml ps -q guardrail)":/data/backups ./backups   # copy off the host
```
`guardrail backup` copies every database file (records and audit, conversation checkpoints,
provider ledgers) consistently while the service runs. To restore: stop the service, copy the
files from one backup folder into the volume's `/data`, and start it. Schedule backups daily
(for example with cron on the host) and keep copies off the machine.

**Security regression.** Before changing checks, models or prompts, run `make security`. It runs
the adversarial tests, all four red-team configurations against the committed baseline, and
the chaos run. CI runs the same gate on every push. When a change is deliberate, re-record the
baseline with `make baseline` and review the diff of `docs/RESULTS.md`.

## 11. Adapting it to your own business

The parts that are specific to the bundled store, and what to do with each:

| Part | Where | What you do |
|---|---|---|
| Tools | `guardrail/tools.py` | declare each of your agent's tools as a `ToolSpec`: the function, its checks, a grounding contract (which checks verify each parameter), base risk, and a postcondition for any tool that changes state |
| Checks | `guardrail/checks.py` | write `check(action, ctx, kb, session) -> CheckResult` for each property that must be proven; return `PASS` only on positive proof, `FAIL` on contradiction, `UNVERIFIABLE` when no source can settle it |
| System of record | `guardrail/knowledge_base.py`, `store.py` | point the knowledge base at your real records (read replicas are fine for checks; writes must go through the guarded transaction) |
| Policies | `guardrail/data/kb/policies.json` | your policy texts and their machine-readable limits |
| Providers | `guardrail/outbox.py` (`Provider`) | implement `send(key, channel, payload) -> reference` (idempotent per key), `lookup(key)` and `records()` for your payment processor and messaging service, and register them in `simulated_providers`' place in `app.py` |
| Red-team cases | `redteam/cases.json` | attacks and legitimate controls for every new tool, ideally written by someone other than the check's author |

The app refuses to start if a tool parameter has no named verifier, so coverage gaps show up
at start-up, not in production.

**Going multi-host.** `store.py` is the only module that knows about SQLite. Replace it with a
Postgres implementation of the same interface (transactions become `SELECT … FOR UPDATE` on
the affected rows) and swap the LangGraph checkpointer for `langgraph-checkpoint-postgres`. The
conversation claims, outbox claims and idempotency keys are all database rows, so they work
across hosts once the database is shared.

## 12. Security model and known limits

What it defends against, each with tests: hallucinated IDs and tools; plausible but wrong
values; acting for the wrong customer; fabricated policy claims; data leaving to the wrong
place; prompt injection in arguments, including text aimed at the reviewer and forged verdicts
or sign-offs; agent loops and probing; splitting refunds to dodge limits; races; duplicate
execution; stale or withdrawn approvals; provider failures; reviewer outages and garbage
output; approving under someone else's name. The full threat model is in
[ARCHITECTURE.md §12](ARCHITECTURE.md#12-threat-model).

Known limits:

- **Rule-based language understanding.** Product matching and keyword lists can be fooled by
  unusual phrasing. The design fails toward people: an unrecognised item makes the amount
  unverifiable, which escalates.
- **Meaning that changes while reusing the policy's own words** needs the Claude reviewer's
  entailment check; the offline heuristic reviewer does not do it.
- **Sidecar evidence is only as honest as the caller.** If your agent's wrapper sends fabricated
  `observations`, provenance checks are weakened.
- **Messages cannot be unsent.** A wrong message that was delivered can only be corrected.
- **Compensated refunds still count toward the daily cap.** This is conservative.
- **Approver authentication is per-person API keys**, not single sign-on. Put SSO in front (an
  authenticating proxy) if your organisation requires it.
- **Not yet verified:** live Claude judgement (`make live`), real providers, multi-host.

## 13. Production checklist

| | Item | Status |
|---|---|---|
| ✅ | Allowlist verification, provenance, claim checks, risk-routed review, fail-closed paths | built and tested |
| ✅ | Idempotent execution, race safety, crash recovery | built and tested (threads, processes, real kills) |
| ✅ | Outbox delivery, compensation, reconciliation, incidents | built and tested (simulated providers) |
| ✅ | API keys with roles; approver identity from the key | built and tested |
| ✅ | Production mode refuses unsafe settings | built and tested |
| ✅ | HTTPS, security headers, non-root read-only container, health checks, backups, migrations | built; production stack exercised |
| ✅ | CI: tests on 3 Python versions, security gate vs baseline, chaos, container smoke test | green on GitHub |
| ☐ | Run `make live` and `guardrail redteam --reviewer claude` with your API key; review every disagreement | **you** |
| ☐ | Implement real payment and messaging providers; test refusals and timeouts against their sandboxes | **you** |
| ☐ | Point the knowledge base at your real records | **you** |
| ☐ | Write checks and red-team cases for every tool your agent has | **you** |
| ☐ | One approver key per person; store secrets in a secret manager; rotate on staff changes | **you** |
| ☐ | Monitoring and alerts on `/healthz`; daily off-host backups; a tested restore | **you** |
| ☐ | Shadow mode first: send real traffic to `/v1/actions/evaluate` (decides, never executes) and compare its decisions with what your team would do | **you** |
| ☐ | An independent security review and held-out red-teaming by someone else | **you** |
