# Hardening: how strong is the guardrail, and why

## The guarantee, stated precisely

No software can promise that an AI model never produces a wrong idea. What this system
guarantees is narrower, and enforceable:

> **No action the system cannot verify is ever executed automatically.**
> Every proposed action is either *proven* consistent with the sources (the system of record,
> the user's own words, and the tool results the agent actually received) and then runs, or it
> is **blocked**, or it is **handed to a human**. If anything is in doubt, including a reviewer
> outage, a refusal, malformed output, or evidence that can't be found, the result is never
> "allow".

Everything below is a mechanism that enforces this guarantee, plus a test that proves it.

## From blocklist to allowlist

The first version looked for *known kinds of mistakes*. An adversarial probe showed what that
misses: a `True` passed as a stock quantity, invisible characters, a policy claim with no
numbers, a claim that quietly dropped "over $75", and a refund at another item's price. All of
them were **allowed**, because nothing *contradicted* them.

The guard now works the other way round. Every check returns one of:

| Outcome | Meaning | Result |
|---|---|---|
| `PASS` | positively supported by a source | may proceed |
| `FAIL` | contradicted by a source, or absent from every source (invented) | **block** |
| `UNVERIFIABLE` | no source can settle it | **human** |
| `NEEDS_APPROVAL` | policy reserves it for a human | **human** |

An action runs automatically only when every check passes *and* the risk-routed reviewer
agrees. A human approval waives only `UNVERIFIABLE` / `NEEDS_APPROVAL`; a `FAIL` can never be
approved away. Every check runs again inside the write transaction.

## The defence layers

| # | Layer | What it stops | Where |
|---|---|---|---|
| 1 | **Strict schemas** | invented tools and parameters, wrong types, NaN/Infinity, booleans posing as numbers, oversized strings | `tools.py` (`ToolSpec`) |
| 2 | **Input hygiene** | prompt or SQL injection (NFKC-normalized), text addressed to the reviewer, zero-width and bidi-override characters, homoglyph IDs and emails | `no_injection`, `no_hidden_characters`, `_id_check`, `recipient_known` |
| 3 | **Existence & authorization** | hallucinated IDs, acting for or reading another customer | `*_exists`, `customer_owns_order`, `acts_for_authenticated_customer` |
| 4 | **Provenance** | values the agent made up: every order and SKU must appear in what the user said or in a tool result the agent actually received | `order_provenance`, `sku_provenance`, `quantity_provenance` |
| 5 | **Request-anchored amounts** | the wrong item's price, arbitrary amounts, a customer asking for more than the item cost | `amount_grounded` |
| 6 | **Policy limits** | refund window, refundable balance, per-order approval limit, 24-hour per-customer cap (stops splitting across orders), stock floor | policy checks, `customer_daily_refund_cap` |
| 7 | **Claim verification** for customer-facing text | fabricated quotes and citations, invented numbers and dates, uncited policy claims, dropped conditions, added absolutes, reversed meaning ("not refund"), swapped reference points ("purchase" vs "delivery"), unfamiliar terms (→ human), refunds claimed but never issued, external links, cross-customer data | `quotes_verbatim`, `numeric_claims_grounded`, `claims_supported`, `commitments_match_history`, `outbound_links_allowed`, `no_cross_customer_disclosure` |
| 8 | **Circuit breaker** | probing: an agent varying a blocked action until one variant slips through. After 3 blocked attempts of a tool (or 6 in total) the agent loses autonomy for that conversation. | `autonomy_not_revoked` |
| 9 | **Risk-routed model review** | intent errors rules can't judge (invented reasons, wrong direction). Fast tier → careful tier → for critical risk, **two different models must both approve** | `risk.py`, `reviewers.py` |
| 10 | **Verify the verifier** | a reviewer approving on evidence it made up: approvals must quote evidence that is found verbatim in their inputs, otherwise fail closed | `verify_evidence` |
| 11 | **Commit-time re-verification** | records changing between check and write (time-of-check vs time-of-use), stale approvals | `Guardrail.commit` |
| 12 | **Postconditions** | tool bugs: the effect must equal what was verified (refund moved by exactly the amount, stock by exactly delta, message stored is the message checked), otherwise the transaction rolls back | `ToolSpec.postcondition` |
| 13 | **Database invariants** | the last line of defence: `CHECK (stock >= 0)`, `CHECK (refunded <= total)`, foreign keys | `store.py` |
| 14 | **Grounding contracts** | coverage gaps: every parameter of every tool must name the checks that verify it; the app refuses to start otherwise | `ToolSpec._validate_contract` |
| 15 | **Strict reviewer output** | a reviewer "approving" through malformed output: `"grounded": "false"` (a string), NaN, out-of-range or boolean confidence, non-list fields, non-JSON bodies, timeouts and SDK errors all fail closed. Five of these used to become approvals | `parse_review`, `ClaudeReviewer.review` |
| 16 | **Idempotent execution** | duplicate side effects from retries, replayed graph nodes, racing workers or two approvers: an action executes at most once, checked inside the write transaction; sidecar `Idempotency-Key` | `Guardrail.commit`, `IdempotencyStore` |
| 17 | **Approvals that can't go stale** | an approval reused for something the approver never saw: it waives only the checks that asked for it, expires after 24 h, and can be revoked until the action runs (atomic with execution) | `ApprovalQueue.authorizes`, `revoke` |
| 18 | **Outbox, compensation, reconciliation** | irreversible effects done wrong: nothing calls a provider inside a transaction; intents are delivered after commit with idempotency keys, refused refunds are compensated, and the providers' own records are reconciled with the outbox and audit log | `outbox.py` |
| 19 | **Crash recovery** | half-finished runs: conversation claims in the database, synchronous checkpoints, and recovery that resumes a dead worker's run without re-executing | `ConversationService.recover_incomplete` |
| 20 | **Access control** | anyone reaching the API approving refunds, or approving under someone else's name: API keys with roles, the decider recorded from the key, and production mode refusing to start without keys | `auth.py` |

## How the strength is measured

| Instrument | Result |
|---|---|
| Red-team suite: 92 cases (75 attacks in 9 categories + 17 legitimate controls), all four configurations, recorded in [RESULTS.md](RESULTS.md) | **75/75 attacks stopped, 0/17 false positives** (routed, fast-only and careful-only); identical decisions on repeated runs; CI fails on any regression against the committed baseline |
| Deterministic layer alone | 73/75 at zero model cost; the other 2 (invented reason, reversed stock direction) need the reviewer |
| Chaos runs: realistic hallucinations injected into live agent runs | **35/35 stopped**, and the agent recovers so the final records are still correct |
| Held-out probing: new attacks written *after* the defences, to avoid testing only what was designed for | round 1 found 4 holes (a "forget your rules" injection, a customer asking for more than an item's price, a phishing link, "purchase" swapped for "delivery"). Round 2 found 6 more that the previous code **allowed**, reviewer included: a reason carrying a forged reviewer verdict in JSON, a forged `SYSTEM:` line, a claimed manager sign-off, a `javascript:` URI, a look-alike domain written without `https://`, and another customer's data emailed to staff from a customer's conversation. All fixed and in the suite, with 4 more variants and 4 legitimate controls for false positives |
| Claude failure modes, through the real SDKs against a scripted API | 16 malformed or failed responses, timeouts and bounded retries all fail closed; full agent runs through `build_app` |
| Races and crashes | daily cap under 8 racing threads and 6 racing processes; duplicate and racing commits execute once; revocation racing execution has one winner; real process kills mid-write, after commit, and mid-run |
| Side effects | lost responses never pay twice; refused refunds are compensated; reconciliation detects money moved outside the guardrail, missing and altered provider records, and broken audit links |
| Tests | 228 (plus 11 live-API tests that need a key), on Python 3.11, 3.12 and 3.13, locally from a fresh clone and on GitHub Actions |

A perfect score on attacks the builder wrote is necessary, not sufficient. Keep adding held-out
attacks, ideally written by someone else, and run the suite with the Claude reviewers.

## Other approaches considered

| Approach | Status | Why |
|---|---|---|
| Allowlist / three-valued verification | **implemented** | the single biggest strength gain |
| Provenance (lightweight taint tracking) | **implemented** | makes "invented but real-looking" values detectable |
| Claim verification (coverage, conditions, absolutes, negation, anchors) | **implemented** | deterministic and explainable; the LLM reviewer adds full entailment on top |
| Consensus of two different model families for critical actions | **implemented** (Claude mode) | different models make less correlated errors. Offline there is no second model, so that tier falls back to the careful reviewer |
| Verifier evidence checking | **implemented** | stops a reviewer from approving on made-up evidence |
| Postconditions + transactional re-checks | **implemented** | turns "verified then executed" into one atomic step |
| **Capability handles** (lookups return opaque references; mutating tools accept only those) | recommended next step | makes inventing an ID impossible at the interface rather than detected after the fact. Changes the tool API the agent sees |
| Policy-as-code engine (OPA / Cedar) | optional | lets non-engineers own the rules. The current checks are already isolated, parameterized from the database, and covered by contracts |
| Dedicated entailment (NLI) classifier for claims | optional | cheaper than an LLM call per message; the Claude reviewer covers this today |
| Self-consistency sampling of one reviewer | not chosen | costs N calls for less independence than a second model family |
| Two-person approval above a higher limit | easy extension | add a second approval step for amounts above a policy parameter |
| Shadow mode + continuous red-teaming in production | operational | run new rules alongside the old ones and compare before enforcing; feed real near-misses back into `redteam/cases.json` |

## Residual risks (known, documented)

- **Rule-based language understanding.** Product matching and keyword lists can be fooled by
  unusual phrasing. The design fails toward humans: an unrecognized item name makes the amount
  `UNVERIFIABLE` (→ approval), not allowed.
- **Subtle meaning changes** that use the policy's own words in a different sense need the
  LLM reviewer's entailment check. The heuristic reviewer used offline does not do this.
- **Item-level refund history** is not modeled (refunds are tracked per order), so refunding
  the same item twice is limited only by the order's refundable balance.
- **Sidecar callers** must pass honest `observations`. The guard can only verify against the
  evidence it is given. They should also pass their own `conversation_id`: the default
  (`"sidecar"`) is shared, so the circuit breaker and duplicate check would pool callers.
- **Compensated refunds still count** toward the 24-hour cap (the cap reads executed
  decisions). This errs on the safe side but can make a customer wait for a human sooner.
- **Messages can't be unsent.** A message that fails permanently is flagged as an incident;
  one that was delivered with wrong content can only be followed by a correction.
- **Live Claude paths** (agent, reviewers, second opinion) are tested through the real SDKs
  against a scripted API, including every failure mode above. `make live` runs them against
  the real API with the red-team suite; that has not been done yet because no key was
  available. Do it before production.
