# Guardrail: catching an agent before it acts on a bad idea

Agents that call tools can produce actions that are **plausible but ungrounded**: a refund on an
order that doesn't exist, a policy quote that is almost right, an email to `ada@exarnple.com`.
Guardrail is a complete service that sits **between an agent's decision and the tool's
execution**. Every proposed action is verified against the company's system of record, and
against what the user actually said, before anything changes.

```
 customer / ops ──► API · CLI · dashboard ──► LangGraph agent ──proposes──► Guardrail ──► tools ──► system of record
                                                      ▲                         │
                                                      └── "BLOCKED: <reason>" ◄─┤ BLOCK     (agent self-corrects)
                                    approver ──► approval queue ◄── pause ◄─────┤ ESCALATE  (human-in-the-loop)
                                                                                └ ALLOW → re-check + execute in one DB transaction
```

**Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** (component, sequence, state and
data diagrams; threat model; trade-offs).

## What's in the box

| | |
|---|---|
| **Verification pipeline** | Schema validation → 22 deterministic grounding checks against the records → risk scoring → routing to a fast (Claude Haiku 5.5) or careful (Claude Opus 5.5) grounding reviewer. Fails closed. |
| **Agent workflow** | LangGraph state machine `agent → verify → [await approval] → execute`, with checkpoints so paused runs survive restarts. |
| **Human-in-the-loop** | Over-limit or uncertain actions pause the run; an approver approves or rejects in the dashboard, API or CLI; the run resumes. |
| **Commit safety** | Checks re-run inside the write transaction; tool failures roll back; replays never double-execute. |
| **Persistence** | SQLite system of record + audit log + approval queue + outbox + LangGraph checkpoints. |
| **Interfaces** | REST API (agent mode and sidecar mode for any other agent stack), web dashboard, `guardrail` CLI. |
| **Assurance** | 90 tests; a 52-case red-team suite with ablation; chaos testing that injects hallucinations into live agent runs. CI runs all of it. |
| **Ops** | Dockerfile + compose, health check, `.env` config, metrics endpoint, GitHub Actions. |

## Quick start

```bash
pip install -e ".[dev]"

python -m guardrail demo          # 3 scenarios: hallucinations caught · approval pause/resume · chaos run
python -m guardrail serve         # dashboard at http://127.0.0.1:8000/, API docs at /docs
```

Or with Docker: `docker compose up --build` → http://localhost:8000/

Everything runs **offline by default**, using the rule-based agent and heuristic reviewers.
To use Claude, set `ANTHROPIC_API_KEY` and either or both of these:

```bash
export GUARDRAIL_REVIEWER=claude   # Claude Haiku 5.5 (fast) / Claude Opus 5.5 (careful) grounding reviews
export GUARDRAIL_AGENT=claude      # Claude Opus 5.5 as the support agent
```

## Using it

### Dashboard (`guardrail serve`)
- **Console.** Chat as a customer, or as internal ops. Every tool call shows its decision,
  risk, review route and reasons. A paused run shows an inline **Approve / Reject** banner.
- **Approvals.** The pending queue and the decision history.
- **Audit log.** Every evaluated action with its checks, reasons and any injected fault.
- **Metrics.** Block reasons, catches by layer, review routes and cost, chaos catch rate.
- **Records.** Orders, stock, and the outbox (what the agent actually sent).

### CLI
```bash
guardrail ask "My order O-5001 arrived with a cracked lantern. Can I get a refund for the lantern?" --customer C-1001
guardrail ask "The kayak from order O-5005 leaks, it's defective. I'd like a full refund." --customer C-1002   # pauses
guardrail approvals list
guardrail approvals approve <approval-id> --reviewer-name morgan.lee --note "photos verified"   # resumes the run
guardrail chat --customer C-1001          # interactive; /approve and /reject inline
guardrail audit --decision block
guardrail metrics
guardrail chaos --rounds 3                # every agent turn corrupted; exits non-zero if any fault executes
guardrail redteam --ablation
```

### Sidecar mode: guard any agent over HTTP
```bash
curl -X POST localhost:8000/v1/actions/evaluate -H 'content-type: application/json' -d '{
  "action":  {"tool": "issue_refund", "args": {"order_id": "O-5010", "customer_id": "C-1001", "amount": 49.99, "reason": "cracked"}},
  "context": {"conversation": ["My lantern on O-5001 arrived cracked, please refund it."], "customer_id": "C-1001"}}'
# → {"decision": "block", "reasons": ["[order_exists] Order O-5010 does not exist ... (closest real order: O-5001)"], "checks": [...], ...}
```
`/v1/actions/execute` also commits if allowed, or returns an `approval_id` if escalated.

### In Python
```python
from guardrail.app import build_app
from guardrail.config import Settings

app = build_app(Settings(db_path=":memory:"))
conv = app.conversations.start("My order O-5001 arrived with a cracked lantern. Refund the lantern?", "C-1001")
conv["guard_log"]          # every decision, with reasons
```

## The scenario

The scenario is a support desk for a fictional outdoor-gear store. The system of record
(seeded from `guardrail/data/kb/`) holds customers, orders, products, staff and policies.
Policies carry machine-readable limits (`refund_window_days: 30`,
`refund_approval_limit: 500`), so checks read their limits from the source of truth.

| Tool | Class | Example checks |
|---|---|---|
| `issue_refund` | money movement, irreversible | order exists and belongs to the authenticated customer; delivered; inside the 30-day window; ≤ refundable balance; cumulative ≤ $500, otherwise escalate; amount matches line items |
| `send_email` | outbound, irreversible | recipient on file (look-alike detection); no other customer's data; every number/date grounded; "we refunded $X" matches a refund that executed |
| `answer_customer` | factual claims | cited policy exists; quote is verbatim; every number comes from a *cited* policy or the customer's records |
| `update_inventory` | state change | SKU exists; stock stays ≥ 0; > 50 units needs approval |
| `lookup_order`, `search_policies` | read-only | authorization (your own orders only); no model review needed |

## Results (offline reviewers)

**Demo, scenario 1.** A scripted hallucinating agent proposes 9 actions. 6 are blocked: a
transposed order ID, a 10× amount, the tent's price for a lantern request, a misquoted
policy, a typo-squatted email and a duplicate refund. The other 3 execute. The customer is
refunded exactly $49.99, and $679.88 of bad refunds is stopped. The agent corrects itself
after each block.

**Red-team ablation** (`guardrail redteam --ablation`, 42 attacks + 10 benign controls):

| Configuration | Attacks stopped | False positives | Review cost* |
|---|---|---|---|
| deterministic checks only | 36 / 42 | 0 / 10 | 0 |
| + fast reviewer on everything | 40 / 42 | 0 / 10 | 19 |
| **+ risk routing (default)** | **41 / 42** | **0 / 10** | **214** |
| + careful reviewer on everything | 42 / 42 | 0 / 10 | 600 |

\*Relative units: fast review = 1, careful = 40 (the ratio of Claude Haiku 5.5 to Claude Opus 5.5 list prices).

The one miss under routing is kept as a documented `known_gap` (strict xfail): a small refund
for the wrong line item scores as low risk, and the fast tier doesn't check item/amount
correspondence.

**Chaos** (`guardrail chaos --rounds 3`): every agent turn gets an injected hallucination
(transposed IDs, decimal slips, wrong customer, inflated policy numbers, fabricated citations,
stock sign flips…). **35/35 injected faults were stopped**, and because the agent recovers from
the feedback, the final records are still correct (asserted in `tests/test_faults.py`).

## Project layout

```
guardrail/
  config.py  app.py                 settings, composition root
  guard.py                          evaluate → commit / approvals
  checks.py  tools.py               deterministic checks, tool specs (schema + checks + risk)
  risk.py  reviewers.py             risk score + routing; Claude / heuristic reviewers
  knowledge_base.py  ledger.py  store.py   system of record, audit + approvals, SQLite
  services.py                       ConversationService, ApprovalService
  agent/  graph.py agents.py faults.py     LangGraph workflow, agent brains, fault injection
  api/    server.py static/index.html      REST API + dashboard
  cli.py  demo.py  redteam.py
  data/kb/*.json                    seed data
redteam/cases.json                  52 red-team cases
tests/                              unit, workflow, API, red-team, chaos
docs/ARCHITECTURE.md
Dockerfile  docker-compose.yml  Makefile  .github/workflows/ci.yml  .env.example
```

## Development

```bash
make check     # lint + tests + red-team gate + chaos gate (what CI runs)
```

To add an action class, write `check(action, ctx, kb, session) -> CheckResult | None` in
`checks.py` and list it on the tool's `ToolSpec` in `tools.py`. Then add red-team cases for it.

## Limitations

- The offline `HeuristicReviewer` and `RuleBasedAgent` are rule-based stand-ins, so the
  whole system can run and be tested without a key. The Claude reviewer's request and
  response handling is unit-tested against a fake client. The live Claude paths
  (reviewer and agent) have not been exercised against the real API in this environment.
- SQLite with a single connection suits a single node; the scaling path (Postgres) is
  described in the architecture doc.
- The Docker image and CI workflow were written but not built here, because no Docker
  daemon was available. The same install was smoke-tested in a clean virtualenv.
