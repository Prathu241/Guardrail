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

## How the strength is measured

| Instrument | Result |
|---|---|
| Red-team suite: 78 cases (65 attacks in 9 categories + 13 legitimate controls) | **65/65 attacks stopped, 0/13 false positives** |
| Deterministic layer alone | 63/65 at zero model cost; the other 2 (invented reason, reversed stock direction) need the reviewer |
| Chaos runs: realistic hallucinations injected into live agent runs | **35/35 stopped**, and the agent recovers so the final records are still correct |
| Held-out probing: new attacks written *after* the defences, to avoid testing only what was designed for | first round found 4 holes (a "forget your rules" injection, a customer asking for more than an item's price, a phishing link, "purchase" swapped for "delivery"); all fixed and added to the suite |
| Tests | 131, including contract completeness, postcondition rollback, consensus, fail-closed reviewer paths, and restart survival |

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
  evidence it is given.
- **Live Claude paths** (agent, reviewers, second opinion) are unit-tested against a fake
  client. They must be exercised with a real key and the red-team suite before production.
