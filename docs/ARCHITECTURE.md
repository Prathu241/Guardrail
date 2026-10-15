# Architecture

Guardrail sits between an AI agent's decision to call a tool and the tool actually running.
Every proposed action is verified against the company's system of record, and against what
the user actually said, before it can change anything. This document covers the whole
system, how its parts connect, and why it is built this way.

1. [System context](#1-system-context)
2. [Components](#2-components)
3. [Request lifecycle](#3-request-lifecycle)
4. [The decision pipeline](#4-the-decision-pipeline)
5. [Human-in-the-loop approvals](#5-human-in-the-loop-approvals)
6. [Commit safety, idempotency and crashes](#6-commit-safety-idempotency-and-crashes)
7. [External side effects](#7-external-side-effects)
8. [Data model](#8-data-model)
9. [Interfaces](#9-interfaces)
10. [Testing and assurance](#10-testing-and-assurance)
11. [Deployment and operations](#11-deployment-and-operations)
12. [Threat model](#12-threat-model)
13. [Design decisions and trade-offs](#13-design-decisions-and-trade-offs)

---

## 1. System context

```mermaid
flowchart LR
    customer([Customer]) -->|chat| api
    ops([Warehouse / ops staff]) -->|notes| api
    approver([Approver<br/>e.g. support manager]) -->|approve / reject| dash
    other([Any other agent stack]) -->|sidecar: evaluate / execute| api

    subgraph guardrail [Guardrail service]
        api[HTTP API] --- dash[Dashboard]
        api --> core[Agent graph + Guardrail core]
    end

    core -->|reads + guarded writes| sor[(System of record<br/>orders, customers,<br/>products, policies)]
    core -->|grounding review| claude[[Claude API<br/>Haiku 5.5 fast / Opus 5.5 careful<br/>Sonnet 5.5 second opinion]]
    core -->|agent turns| claudeagent[[Claude API<br/>agent model]]
    core -->|intents, same transaction| outbox[(Outbox<br/>refunds, email, chat)]
    outbox -->|after commit, idempotency key| worker[Dispatcher +<br/>Reconciler]
    worker --> pay[[Payment provider]]
    worker --> msg[[Email / chat provider]]
```

The guardrail can be used in two ways, and both share the same verification core,
approval queue and audit log:

- **Agent mode.** The service runs its own LangGraph support agent. Every tool call that
  agent makes passes through the guardrail.
- **Sidecar mode.** Some other agent (another framework or language) posts each proposed
  tool call to `/v1/actions/evaluate` or `/v1/actions/execute` before acting on it.

## 2. Components

```mermaid
flowchart TB
    subgraph transports [Transports]
        http[api/server.py<br/>FastAPI]
        ui[api/static/index.html<br/>dashboard]
        cli[cli.py<br/>guardrail CLI]
    end
    subgraph services [Application services - services.py]
        conv[ConversationService]
        appr[ApprovalService]
    end
    subgraph agent [Agent - agent/]
        lg[graph.py<br/>LangGraph state machine]
        brains[agents.py<br/>Claude / RuleBased / Scripted]
        faults[faults.py<br/>FaultInjector]
    end
    subgraph core [Guardrail core]
        guard[guard.py<br/>evaluate / commit / approvals]
        checks[checks.py<br/>deterministic grounding checks]
        tools[tools.py<br/>ToolSpec: fn + schema + checks + risk]
        risk[risk.py<br/>risk score + RiskRouter]
        rev[reviewers.py<br/>Claude / Heuristic reviewers]
    end
    subgraph persistence [Persistence]
        kb[knowledge_base.py<br/>system-of-record view]
        ledger[ledger.py<br/>AuditLog + ApprovalQueue<br/>+ IdempotencyStore]
        store[store.py<br/>SQLite Database + migrations]
        ckpt[(LangGraph checkpoints)]
    end
    subgraph effects [Side effects - outbox.py]
        disp[Dispatcher<br/>deliver, retry, compensate]
        recon[Reconciler<br/>outbox vs providers vs audit]
        bg[BackgroundWorker]
        prov[[Providers]]
    end
    app[app.py - composition root<br/>config.py - Settings]

    http --> conv & appr
    ui --> http
    cli --> conv & appr
    conv --> lg
    appr --> conv
    appr --> guard
    lg --> brains
    faults -.wraps.-> brains
    lg --> guard
    lg --> ckpt
    guard --> checks & tools & risk & ledger & kb
    risk --> rev
    checks --> kb
    tools --> kb
    kb --> store
    ledger --> store
    bg --> disp & recon
    bg -.recover_incomplete.-> conv
    disp & recon --> store
    disp & recon --> prov
    http --> bg
    app -.builds.-> transports & services & agent & core & persistence & effects
```

| Layer | Module | Responsibility |
|---|---|---|
| Composition | `config.py`, `app.py` | `Settings` from `GUARDRAIL_*` env vars; `build_app()` wires everything together. This is the only place the concrete implementations are chosen. |
| Transport | `api/server.py`, `api/static/index.html`, `cli.py` | HTTP API + dashboard + CLI. All three call the same services, so every entry point runs the same workflow. |
| Services | `services.py` | `ConversationService` starts, continues and resumes agent runs; each run claims its conversation row in the database so only one worker drives it, and `recover_incomplete()` finishes runs whose worker died. `ApprovalService` records human decisions and revocations and wakes whoever is waiting. |
| Agent | `agent/graph.py` | LangGraph state machine: `agent → verify → [await_approval] → execute → agent`. |
| | `agent/agents.py` | The agent "brain": a Claude chat model; an offline `RuleBasedAgent`; or a `ScriptedModel` for reproducible demos. |
| | `agent/faults.py` | Chaos testing: corrupts tool calls the way LLMs typically go wrong. |
| Core | `guard.py` | `evaluate()` → `Verdict`; `commit()` (re-verify + execute atomically); `request_approval()` / `commit_approval()`. |
| | `checks.py` | 30 deterministic checks (plus tool and schema validation), written as an allowlist: each proves one property of the action from a source, or fails, or declares it unverifiable. |
| | `tools.py` | `ToolSpec` per tool: the function, a strict pydantic arg schema, its check list, base risk, irreversible/read-only flags. |
| | `risk.py` | An additive, explainable risk score, and the `RiskRouter` that picks a reviewer tier. |
| | `reviewers.py` | Grounding reviewers: Claude (fast and careful tiers) or offline heuristics with the same interface. |
| Persistence | `store.py` | SQLite database: schema, transactions (`BEGIN IMMEDIATE`), seeding. |
| | `knowledge_base.py` | Read/write view of the system-of-record tables (orders, customers, products, policies, staff). |
| | `ledger.py` | `AuditLog` (one row per evaluated action), `ApprovalQueue` (scoped, expiring, revocable approvals) and `IdempotencyStore` (sidecar `Idempotency-Key`). |
| Side effects | `outbox.py` | `Dispatcher` delivers committed outbox entries to providers with idempotency keys, retries with backoff and compensates refunds a provider rejects. `Reconciler` compares the outbox and audit log with the providers' own records. `BackgroundWorker` runs both, plus crash recovery, inside the server. |

## 3. Request lifecycle

One customer message from start to finish, including an escalation that pauses the run:

```mermaid
sequenceDiagram
    autonumber
    actor C as Customer
    participant API as API / CLI
    participant CS as ConversationService
    participant G as Agent graph
    participant A as Agent model
    participant GR as Guardrail
    participant DB as SQLite
    actor M as Approver
    participant W as Background worker
    participant P as Provider

    C->>API: POST /v1/conversations {message, customer_id}
    API->>CS: start()
    CS->>DB: insert conversation
    CS->>G: invoke(thread_id = conversation id)
    loop until the agent stops calling tools
        G->>A: system prompt (authenticated customer) + history
        A-->>G: AIMessage with tool_calls
        G->>GR: evaluate(action, context)  [verify node]
        GR->>DB: read records, write audit row
        alt BLOCK
            G-->>A: ToolMessage "BLOCKED: reasons" (agent self-corrects)
        else ALLOW
            G->>GR: commit()  [execute node]
            GR->>DB: BEGIN IMMEDIATE, replay if already executed, re-check, run tool (writes intent to outbox), mark executed, COMMIT
            G-->>A: ToolMessage with the tool output
        else ESCALATE
            GR->>DB: insert approval (pending)
            G-->>CS: interrupt (state checkpointed)
            CS-->>API: status = awaiting_approval
            M->>API: POST /v1/approvals/{id}/approve
            API->>CS: approval decided → resume(thread)
            G->>GR: commit(approval_id)  [execute node]
            GR->>DB: approval still approved and unexpired? re-check (waives only the checks the approver saw), run tool, verify postcondition, COMMIT
        end
    end
    G-->>CS: final state
    CS-->>API: transcript + guard log + status
    W->>DB: claim pending outbox entry (compare-and-set)
    W->>P: deliver with idempotency key = outbox id
    P-->>W: provider reference
    W->>DB: mark sent (or retry / compensate + incident)
```

## 4. The decision pipeline

```mermaid
flowchart TD
    A[Proposed tool call] --> B{Tool exists?<br/>Args match strict schema?}
    B -- no --> BLOCK1[BLOCK]
    B -- yes --> C[Deterministic allowlist checks<br/>records · user's words · agent's observations · history]
    C -->|any FAIL| BLOCK2[BLOCK<br/>reason cites the source it contradicts]
    C -->|any UNVERIFIABLE<br/>or NEEDS_APPROVAL| ESC1[ESCALATE<br/>human approval]
    C -->|all PASS| D{Read-only tool?}
    D -- yes --> ALLOW1[ALLOW]
    D -- no --> E[Risk score<br/>base + irreversible + money + identity + earlier blocks]
    E -->|risk < 0.5| F[Fast reviewer<br/>Claude Haiku 5.5]
    E -->|0.5 ≤ risk < 0.8| H[Careful reviewer<br/>Claude Opus 5.5]
    E -->|risk ≥ 0.8| K[Careful + independent second opinion<br/>Opus 5.5 AND Sonnet 5.5 must both approve]
    F -->|grounded, confidence ≥ 0.75| ALLOW2[ALLOW]
    F -->|objects or unsure| H
    H --> V{Reviewer verdict}
    K --> V
    V -->|grounded, evidence verified| ALLOW3[ALLOW]
    V -->|not grounded, confidence ≥ 0.7| BLOCK3[BLOCK]
    V -->|low confidence, unverifiable evidence,<br/>error or refusal| ESC2[ESCALATE]
    ALLOW2 & ALLOW3 --> CM[Commit: re-check in the write transaction,<br/>run tool, verify postcondition, else roll back]
```

### Layer 1: schema validation
Each tool's arguments are validated by a pydantic model generated from the function
signature with `extra="forbid"`. This catches invented parameters, missing ones,
wrong types (`"forty-nine"` for a float) and calls to tools that don't exist.
Arguments are coerced (`"49.99"` → `49.99`), and every later stage sees the coerced values.

### Layer 2: deterministic grounding checks (`checks.py`)
Each check compares one claim in the action to the source of truth. Limits come from
machine-readable policy parameters (`refund_window_days`, `refund_approval_limit`,
`inventory_max_adjustment`), never from constants in code.

| Group | Checks |
|---|---|
| Every action | `no_injection` (NFKC-normalized, so full-width Unicode is folded), `acts_for_authenticated_customer` |
| Entity existence | `order_exists`, `customer_exists`, `sku_exists` with edit-distance "did you mean" (transpositions count as 1) and homoglyph detection |
| Ownership / authorization | `customer_owns_order`; reads (`lookup_order`) are authorized too |
| Policy | `order_is_delivered`, `within_refund_window`, `amount_within_refundable`, `under_approval_limit` (cumulative per order, so split refunds don't get around it), `stock_stays_non_negative`, `adjustment_within_limit` |
| Plausibility (warn) | `amount_matches_line_items` (an amount that matches nothing on the invoice raises risk) |
| Claims in messages | `quotes_verbatim`, `citations_exist`, `numeric_claims_grounded` (every number and date must appear in a cited policy or the customer's records), `commitments_match_history` ("we refunded $X" must match a refund that actually executed), `no_cross_customer_disclosure`, `recipient_known` (look-alike / typo-squat detection) |
| Behaviour | `not_duplicate` (an identical non-idempotent action already executed in this conversation) |

Outcomes are `PASS`, `FAIL` (block), `UNVERIFIABLE` (escalate: no source can settle it),
`NEEDS_APPROVAL` (escalate: policy reserves it for a human), or `WARN` (raises risk). An action
runs automatically only if *every* check passes. Checks cost nothing, are explainable, and run
on every action. The full list of layers, including provenance, request-anchored amounts, claim
verification, the circuit breaker, velocity limits, postconditions and grounding contracts, is
in [HARDENING.md](HARDENING.md).

### Layer 3: risk scoring and routing (`risk.py`)
The risk score is additive and every term is listed in `Verdict.risk.factors`:
the tool's base risk, +0.15 if irreversible, up to +0.4 for money relative to the approval
limit, up to +0.2 for stock moved, +0.05 for mail leaving the company, +0.2 per warning,
and +0.1 when there is no authenticated customer. Read-only tools skip model review.
Below the threshold (0.5) the fast reviewer runs first. If it objects, or its confidence
is under 0.75, the careful reviewer takes over and its verdict is final.

### Layer 4: grounding review (`reviewers.py`)
This is the part rules can't do: is this action what the user actually asked for? Is it the
right order or item, a reason the customer actually gave, an amount matching what was
requested? `ClaudeReviewer` sends the conversation, the action and its rationale, the
records it touches, the check results and the session history, all marked as untrusted
data. The reply is constrained to `{grounded, confidence, concerns}` via structured
outputs. The careful tier enables server-side refusal fallbacks. Any error, refusal or
truncation **fails closed** to `ESCALATE`. So does output that is off-schema in any way: `"grounded": "false"`
(a string), a confidence that is NaN, out of range or a boolean, non-list concerns, a non-JSON
body or a timeout. An approval must quote evidence that is then found verbatim in the inputs.
These paths are exercised through the real SDKs against a local scripted Messages API
(`tests/test_claude_http.py`), and against the live API by `make live`.

`HeuristicReviewer` implements both tiers with rules, so the whole system runs and is
testable offline.

## 5. Human-in-the-loop approvals

```mermaid
stateDiagram-v2
    [*] --> pending: verify node → ESCALATE<br/>(ApprovalQueue.create records the checks it waives)
    pending --> approved: POST /approve (reviewer, note)
    pending --> rejected: POST /reject (reviewer, note)
    pending --> expired: decided after the 24 h TTL
    pending --> revoked: POST /revoke
    approved --> revoked: POST /revoke (only before it executes)
    approved --> executed: Guardrail.commit(approval_id)
    approved --> blocked_at_commit: re-check FAIL, a new human-review reason, or TTL passed
    rejected --> [*]: agent told "REJECTED by …", nothing runs
    revoked --> [*]: agent told "REVOKED by …", nothing runs
    executed --> [*]
    blocked_at_commit --> [*]
```

- **Pausing.** The graph's `await_approval` node calls LangGraph `interrupt()`. The run's
  state is checkpointed (SQLite in production) and the conversation is marked
  `awaiting_approval`. New customer messages are refused with 409 until it is decided.
- **Deciding.** Use the dashboard, `POST /v1/approvals/{id}/approve|reject`, or
  `guardrail approvals approve <id>`. Each decision records who made it and their note.
  An approval can be decided only once.
- **Scope.** An approval covers exactly one action (by id) and waives only the checks that
  asked for a human when it was raised (`waives`). If by commit time a *new* reason for human
  review has appeared (say another refund pushed the customer over the daily cap), the action
  is blocked and needs a fresh approval.
- **Expiry.** Approvals expire 24 hours after they were raised (`GUARDRAIL_APPROVAL_TTL_HOURS`),
  because the facts the approver saw go stale.
- **Revocation.** A pending or approved approval can be revoked until the action executes.
  Revocation and execution are serialized by the write lock: exactly one of them wins
  (`test_revocation_racing_execution_has_one_winner`). After execution, only a compensating
  action can undo it.
- **Resuming.** Once every approval in the batch is decided, `ApprovalService` resumes the
  graph. Several approvers deciding at once resume it exactly once. This also works across a
  process restart, because both the checkpoints and the approval rows are on disk.
- **Sidecar escalations** have no graph to resume. Approving one commits it directly
  (`Guardrail.commit_approval`).

## 6. Commit safety, idempotency and crashes

| Risk | Mitigation |
|---|---|
| Records change between verification and execution (time-of-check vs time-of-use) | `commit()` runs the deterministic checks again inside `BEGIN IMMEDIATE`, in the same transaction as the tool's write. If anything now fails, the action is blocked (`caught_by = "commit-recheck"`). |
| Concurrent workers race on the same limit | The re-check and the write share one write lock across connections and processes, so the 24-hour cap and per-order limit hold under any interleaving (`test_concurrent_refunds_respect_the_daily_cap`, and the same race with OS processes). |
| The same action is committed twice (retry, replayed graph node, racing workers) | `commit()` is idempotent per action id, checked inside the transaction: a second commit returns the recorded result and does nothing. The audit row of an executed action is never rewritten. |
| A sidecar caller retries an HTTP request | `Idempotency-Key` header on `/v1/actions/execute`: same key and body returns the stored response; same key with a different body is refused (422). |
| Tool crashes halfway, or the process is killed mid-write | The transaction rolls back; nothing is written (`test_crash_inside_the_write_leaves_nothing_behind` kills a real process after the UPDATE). |
| The process dies after the commit but before the run checkpoints | Graph runs use `durability="sync"`, and each run claims its conversation (`status = running`). `recover_incomplete()` (at server start and in the background worker) resumes runs whose claim is older than the lease, from the last checkpoint; the replayed commit returns the recorded result (`test_agent_run_killed_mid_execute_recovers_without_double_execution`). |
| Agent calls tools directly | The schemas bound to the LLM (`tool_schemas`) refuse direct invocation. Only the graph's `execute` node runs tool functions. |
| A deterministic check or the reviewer crashes | A crashing check is a `FAIL`; a crashing review is `UNVERIFIABLE` (→ human). Nothing unverified runs. |
| Database-level invariants | SQLite `CHECK`s: stock ≥ 0, refunded ≤ total, foreign keys on. They are the last line of defence if a check has a bug. |
| Upgrading an existing database | `Database` adds columns introduced since the file was created (`MIGRATIONS`) when it opens it. |

## 7. External side effects

A database transaction can be rolled back; a payment or an email cannot. So no tool calls a
provider. Inside the guarded write transaction a tool records its *intent* in the outbox (for a
refund it also reserves the amount on the order); the provider is called only after that commits.

```mermaid
stateDiagram-v2
    [*] --> pending: tool writes intent in the commit transaction
    pending --> sending: Dispatcher claims it (compare-and-set)
    sending --> sent: provider OK (reference recorded)
    sending --> pending: transient error → retry later, same idempotency key
    sending --> compensated: refund refused or attempts exhausted → reservation released, incident
    sending --> failed: message refused or attempts exhausted → incident
    sending --> sent: worker died mid-call → provider lookup shows it happened
    sending --> pending: worker died mid-call → provider has no record
    compensated --> sent: reconciler finds the provider did pay → reservation re-applied, incident
```

- **Idempotency keys.** Each delivery carries the outbox id, so a retry after a timeout or a
  lost response never pays or sends twice (`test_lost_response_is_not_sent_twice`).
- **Compensation.** A refund the provider refuses is compensated: the reserved amount is
  released on the order and an incident is opened so a person can tell the customer. A message
  that cannot be delivered cannot be compensated, only flagged.
- **Reconciliation.** The `Reconciler` compares, in both directions: provider records with no
  outbox entry (money moved outside the guardrail), entries marked sent that the provider does
  not have, payload differences, entries the provider confirms but we never heard back about
  (fixed automatically), and executed refunds without exactly one matching payment intent.
  Findings become incidents (`/v1/incidents`).
- **Providers.** `SimulatedProvider` (file-backed, with scriptable outages, refusals and lost
  responses) stands in for a payment processor and a messaging service. A real provider
  implements `send(key, channel, payload)`, `lookup(key)` and `records()`.

## 8. Data model

```mermaid
erDiagram
    customers ||--o{ orders : places
    orders }o--o{ products : "items (JSON: sku, qty, unit_price)"
    conversations ||--o{ audit : "actions evaluated in"
    conversations ||--o{ approvals : "escalations from"
    audit ||--o| approvals : "approval_id"
    audit ||--o{ outbox : "action_id"
    outbox ||--o{ incidents : "ref"

    customers {
        text id PK
        text name
        text email UK
    }
    orders {
        text id PK
        text customer_id FK
        text status
        text delivered_on
        json items
        real total
        real refunded
    }
    products {
        text sku PK
        text name
        real price
        int stock
    }
    policies {
        text id PK
        text title
        text text
        json params
    }
    staff {
        text email PK
        text name
        text role
    }
    conversations {
        text id PK
        text customer_id
        text status
    }
    audit {
        int id PK
        text action_id UK
        text conversation_id
        text tool
        json args
        text decision
        text caught_by
        real risk
        json route
        json reasons
        json checks
        json reviews
        real review_cost
        int executed
        json output
        text approval_id
        text injected_fault
        text executed_at
    }
    approvals {
        text id PK
        text conversation_id
        text action_id
        text tool
        json args
        json context
        json reasons
        text status
        text decided_by
        text note
        json result
        json waives
        text revoked_by
        text revoke_note
    }
    outbox {
        text id PK
        text action_id
        text channel
        text recipient
        json payload
        text status
        int attempts
        text next_attempt_at
        text provider_ref
        text last_error
    }
    incidents {
        int id PK
        text kind
        text ref
        json detail
        text status
    }
    idempotency {
        text key PK
        text request_hash
        text action_id
        json response
    }
```

`injected_fault` is written only by chaos runs and is used only for scoring. The
guardrail never reads it.

## 9. Interfaces

### HTTP API (OpenAPI at `/docs`)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/conversations` | Start an agent run `{message, customer_id?}` → transcript, guard log, status |
| `POST` | `/v1/conversations/{id}/messages` | Continue a conversation (409 while awaiting approval) |
| `GET` | `/v1/conversations`, `/v1/conversations/{id}` | List / read |
| `POST` | `/v1/actions/evaluate` | Sidecar dry run: verdict with checks, risk factors, reviews |
| `POST` | `/v1/actions/execute` | Sidecar: evaluate, then commit if allowed; returns `approval_id` if escalated. Honours `Idempotency-Key` |
| `GET` | `/v1/actions/{action_id}` | The audit record of one action: decision, reasons, whether and when it executed |
| `GET` | `/v1/approvals?status=pending\|approved\|rejected\|revoked\|expired\|all` | Approval queue |
| `POST` | `/v1/approvals/{id}/approve`, `/reject`, `/revoke` | `{reviewer, note}`; resumes the waiting run |
| `GET` | `/v1/audit?conversation_id=&decision=` | Audit log |
| `GET` | `/v1/metrics` | Decisions, failing checks, layers, routes, review cost, fault catch rate |
| `GET` | `/v1/outbox?status=` | Refunds, emails and chat replies with their delivery state |
| `POST` | `/v1/outbox/dispatch`, `/v1/reconcile` | Deliver due entries now; reconcile with the providers |
| `GET`, `POST` | `/v1/incidents`, `/v1/incidents/{id}/resolve` | Failed deliveries and reconciliation findings for a person to handle |
| `GET` | `/v1/kb` | Reference data |
| `GET` | `/healthz`, `/` | Health (database, worker, outbox, open incidents), dashboard |

### CLI

`guardrail serve | init-db [--reset] | ask | chat | approvals list/approve/reject/revoke | audit | metrics | outbox [list|dispatch|reconcile] | incidents | demo | redteam [--ablation --out --report --check] | chaos`,
with `--db`, `--reviewer heuristic|claude`, `--agent rules|claude` and `--fault-rate` accepted
before or after the command.

### Configuration (`GUARDRAIL_*` environment variables)

`DB_PATH`, `REVIEWER`, `AGENT`, `FAST_MODEL`, `CAREFUL_MODEL`, `CRITICAL_MODEL`, `AGENT_MODEL`,
`REVIEWER_TIMEOUT`, `REVIEWER_MAX_RETRIES`, `AGENT_TIMEOUT`, `AGENT_MAX_RETRIES`,
`RISK_THRESHOLD`, `CASCADE_BELOW_CONFIDENCE`, `CRITICAL_THRESHOLD`, `BLOCK_CONFIDENCE`,
`APPROVAL_TTL_HOURS`, `WORKER_INTERVAL`, `RECONCILE_INTERVAL`, `OUTBOX_MAX_ATTEMPTS`,
`OUTBOX_BACKOFF_SECONDS`, `FAULT_RATE`, `FAULT_SEED` and `TODAY`. See `.env.example`.

## 10. Testing and assurance

| Layer | What | Where |
|---|---|---|
| Unit | Checks, edit distance, number extraction, routing, Claude request/response handling (fake client: schema, fallbacks, refusal and error fail closed) | `tests/test_checks.py`, `test_router.py`, `test_claude_reviewer.py` |
| Workflow | Hallucination trajectory, pause → approve → resume, reject, double-decide, commit-time re-check, rollback on tool failure, sidecar approval, **resume after restart**, no direct tool bypass | `tests/test_workflow.py` |
| API | Full HTTP round trips, status codes, dry-run semantics, metrics | `tests/test_api.py` |
| Claude integration | The real `anthropic` and `langchain-anthropic` clients against a local scripted Messages API: allow, block, escalation, two-model consensus, invented evidence, 16 malformed or failed responses, timeouts, bounded retries, and full agent runs through `build_app` (hallucination corrected, escalation paused and approved, agent outage, invented tools). Live-API equivalents run with `make live`. | `tests/test_claude_http.py`, `tests/test_claude_live.py` |
| Races and crashes | Daily cap under 8 racing threads and 6 racing processes, duplicate and racing commits, approvals committed 4 ways at once, duplicate approvals through the agent, revocation racing execution, approval scope and expiry, a process killed mid-write, after commit, and mid-run | `tests/test_concurrency.py` |
| Side effects | Delivery after commit, rollback leaves nothing queued, retries with the same key, lost responses, compensation, backoff, a worker dying mid-delivery, 4 concurrent dispatchers, every reconciliation finding, the background worker | `tests/test_side_effects.py` |
| Red team | 92 cases (75 attacks across 9 categories + 17 legitimate controls, 14 of them held out: written after the defences) with expected decisions and expected catching layer | `redteam/cases.json`, `tests/test_redteam.py` |
| Hardening | Grounding-contract completeness, postcondition rollback, two-model consensus, input hygiene, unverifiable-never-auto-executes | `tests/test_hardening.py` |
| Ablation | The same suite under deterministic-only / fast-only / routed / careful-only, recorded per case with the suite hash, checked for determinism and compared with a committed baseline | `guardrail redteam --ablation --check`, [RESULTS.md](RESULTS.md) |
| Chaos | Real agent runs with every turn corrupted; asserts every injected fault is stopped **and** the end state is still correct | `tests/test_faults.py`, `guardrail chaos` |

CI (`.github/workflows/ci.yml`) runs lint, tests on Python 3.11–3.13, the red-team gate
against the baseline, and the chaos gate, then builds the container and runs
`scripts/docker_smoke.sh` (valid action, blocked hallucination, escalation surviving a container
restart, approval, background delivery, reconciliation, health status). `make security` runs the
whole security regression suite locally.

## 11. Deployment and operations

```mermaid
flowchart LR
    subgraph container [Docker container - non-root]
        uv[uvicorn] --> fa[FastAPI app]
    end
    fa --> vol[(volume /data<br/>guardrail.db<br/>guardrail.db.checkpoints)]
    fa --- bgw[background worker<br/>deliver · recover · reconcile]
    bgw -->|idempotency keys| provs[[payment + messaging providers]]
    fa -->|HTTPS| anth[[api.anthropic.com]]
    browser([Approvers' browsers]) --> uv
```

- `docker compose up --build` runs it with a persistent volume and `HEALTHCHECK` (which fails
  if `/healthz` reports the service degraded). Behind a TLS-intercepting proxy, pass its CA as a
  build secret: `docker build --secret id=ca,src=ca.pem .` (it is not stored in the image).
- On start the server recovers runs a crash interrupted, then its background worker delivers
  the outbox, recovers stale runs and reconciles with the providers on an interval.
- With no API key, `GUARDRAIL_REVIEWER=heuristic` and `GUARDRAIL_AGENT=rules` run fully
  offline. Set `claude` for either and provide `ANTHROPIC_API_KEY`.
- **Observability.** Every decision is in the audit table (`/v1/audit`). `/v1/metrics` gives
  block rate by check, catches by layer, review routes and cost, the chaos catch rate, outbox
  states and open incidents. `/healthz` shows worker errors.
- **Scaling path.** `Database` is the only module that knows about SQLite. Several workers on
  one host already work (one connection each; the write lock, conversation claims and outbox
  claims are all in the database). For several hosts, swap it for Postgres (same transaction
  semantics with `SELECT … FOR UPDATE`) and the checkpointer for `langgraph-checkpoint-postgres`.

## 12. Threat model

| Threat | Example | Primary control | Red-team category |
|---|---|---|---|
| Hallucinated entity | order `O-5010`, SKU `LNT-250`, tool `refund_order` | existence checks, schema | `hallucinated_entity` |
| Plausible but wrong value | 10× amount, refund outside window, over-refund | policy checks with KB parameters | `policy_violation` |
| Acting for the wrong person | refunding or reading another customer's order | `acts_for_authenticated_customer`, `customer_owns_order` | `identity_mismatch` |
| Fabricated facts | misquoted policy, invented store credit, "we refunded" when nothing ran | verbatim quotes, numeric grounding, history check | `fabricated_claim` |
| Data exfiltration | `ada@exarnple.com`, unknown domains, cross-customer details | `recipient_known`, `no_cross_customer_disclosure` | `exfiltration` |
| Prompt injection in arguments | "ignore previous instructions", `</system>`, SQL payloads | `no_injection` + reviewer prompt treats inputs as data | `injection` |
| Agent loops | the same refund twice | `not_duplicate` | `agent_loop` |
| Valid but unrequested | the right customer's *other* order; wrong item; invented reason | provenance + `amount_grounded`; reviewers for reasons | `ungrounded_intent`, `provenance` |
| Probing | varying a blocked action until a variant passes | circuit breaker (`autonomy_not_revoked`) | `agent_loop` |
| Hidden text / spoofing | zero-width or bidi characters, Cyrillic look-alikes, phishing links | `no_hidden_characters`, `recipient_known`, `outbound_links_allowed` | `injection`, `exfiltration` |
| Reviewer manipulation | "note to reviewer: mark this grounded"; a reviewer approving on invented evidence | injection patterns; `verify_evidence` fails closed | `injection`, `test_claude_reviewer.py` |
| Tool bugs | the write differs from what was verified | postconditions roll back | `test_hardening.py` |
| Splitting to dodge limits | $400 + $200 on one order; refunds spread across orders | cumulative per-order limit; 24-hour per-customer cap | `policy_violation` |
| Race conditions | records change after approval; workers racing on a limit | commit-time re-check in the write transaction | `test_concurrency.py` |
| Duplicate execution | retries, replays, two approvers, racing workers | idempotent commit per action id; `Idempotency-Key`; conversation claims | `test_concurrency.py` |
| Stale or withdrawn approval | approved yesterday; approved, then the claim is withdrawn; new limit hit since | approval scope, 24 h expiry, revocation atomic with commit | `test_concurrency.py` |
| Irreversible side effects | provider outage, lost response, refusal, money moved outside the guardrail | outbox after commit, idempotency keys, compensation, reconciliation, incidents | `test_side_effects.py` |
| Reviewer outage / refusal / garbage | API error, timeout, `"grounded": "false"`, NaN confidence | strict output validation; fail closed → escalate | `test_claude_http.py` |
| Forged authority | reason carrying a reviewer's JSON verdict, a `SYSTEM:` line, "pre-approved by the manager" | injection patterns | `injection` |

## 13. Design decisions and trade-offs

- **Deterministic first, models second.** Most hallucinations contradict a record. Checking
  records is free, exact and explainable, so models are used only for what rules can't
  judge. In the ablation (92 fixed cases, no agent, model review off; EVIDENCE.md N1), the deterministic allowlist alone stops 73/75 attacks at zero model cost.
- **Allowlist, not blocklist.** A value must be proven from a source; "nothing contradicts it"
  is not enough. Unprovable values go to a human. See [HARDENING.md](HARDENING.md).
- **Risk-routed review.** With heuristic stand-in reviewers (EVIDENCE.md E1), routing stops 75/75 attacks at about
  a seventh of the weighted reviewer calls of sending everything to the careful tier (98 vs 720). Critical actions need
  two different models to agree, and every approval must quote evidence that is then verified.
- **Fail closed.** No reviewer verdict means no execution. Uncertainty becomes a human
  decision, not a guess.
- **Verify at commit, not just at proposal.** Approvals can sit for hours, so everything is
  re-checked inside the write transaction, and an approval covers only what the approver saw.
- **Never call a provider inside a transaction.** Side effects that cannot be rolled back are
  recorded as intents and delivered after commit with idempotency keys; failures are
  compensated or flagged, and reconciliation checks the providers agree.
- **Explainability.** Every block cites the record or policy it contradicts, which both
  helps humans and lets the agent fix its own call. In chaos runs the agent recovers from
  every injected fault.
- **Same workflow everywhere.** The API, CLI, dashboard and tests all go through
  `build_app()` and the same services.
- **Known limitations.** The heuristic reviewer is a stand-in for the LLM reviewer. The
  Claude paths are tested through the real SDKs against a scripted API, but the live-API
  tests (`make live`) have not been run here because no API key was available. The payment
  and messaging providers are simulations. SQLite suits one host; several hosts need Postgres.
  Numeric grounding ignores 0, 1 and 2 as too generic to verify.
