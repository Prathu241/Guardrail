"""Red-team harness: replay adversarial (and benign) proposed actions through the guardrail.

    python -m guardrail redteam                  # default routed config, per-case table
    python -m guardrail redteam --ablation       # compare layer configurations
    python -m guardrail redteam --reviewer claude
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .guard import Guardrail
from .knowledge_base import KnowledgeBase
from .models import AgentContext, Decision, ProposedAction, ReviewResult, Verdict
from .reviewers import ReviewRequest, reviewers_for
from .risk import RiskRouter

CASES_PATH = Path(__file__).resolve().parents[1] / "redteam" / "cases.json"
MODES = ("deterministic-only", "fast-only", "routed", "careful-only")


class AlwaysGrounded:
    """Reviewer stub for the deterministic-only ablation."""

    name = "none"

    def review(self, request: ReviewRequest) -> ReviewResult:
        return ReviewResult(self.name, True, 1.0)


def make_router(mode: str, reviewer: str = "heuristic") -> RiskRouter:
    fast, careful, critical = reviewers_for(Settings(reviewer=reviewer))
    if mode == "routed":
        return RiskRouter(fast, careful, critical=critical)
    if mode == "fast-only":
        return RiskRouter(fast, fast, threshold=float("inf"), cascade_below_confidence=0.0)
    if mode == "careful-only":
        return RiskRouter(careful, careful, threshold=0.0)
    if mode == "deterministic-only":
        return RiskRouter(AlwaysGrounded(), AlwaysGrounded(), threshold=float("inf"))
    raise ValueError(mode)


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    return json.loads(Path(path).read_text())


def _action(spec: dict) -> ProposedAction:
    return ProposedAction(spec["tool"], spec["args"], spec.get("rationale", ""))


@dataclass
class CaseResult:
    case: dict
    verdict: Verdict
    check_route: bool = True  # route expectations only hold for the default routed config

    @property
    def decision(self) -> str:
        return self.verdict.decision.value

    @property
    def correct(self) -> bool:
        if self.decision != self.case["expect"]:
            return False
        if self.check_route and "expect_route" in self.case and self.verdict.route != self.case["expect_route"]:
            return False
        expected_layer = self.case.get("expect_caught_by")
        if expected_layer == "reviewer":
            return self.verdict.caught_by not in (None, "deterministic")
        if expected_layer == "deterministic":
            return self.verdict.caught_by == "deterministic"
        return True

    @property
    def stopped(self) -> bool:
        return self.verdict.decision is not Decision.ALLOW

    @property
    def cost(self) -> float:
        return sum(r.cost for r in self.verdict.reviews)


def run_case(case: dict, router: RiskRouter, check_route: bool = True) -> CaseResult:
    guard = Guardrail(KnowledgeBase.in_memory(), router)
    ctx = AgentContext(
        case["context"]["conversation"],
        case["context"].get("customer_id"),
        observations=case["context"].get("observations", []),
    )
    for step in case.get("setup", []):
        result = guard.execute(_action(step), ctx)
        wanted = step.get("expect", "allow")
        if result.verdict.decision.value != wanted or (wanted == "allow" and not result.executed):
            raise RuntimeError(f"{case['id']}: setup step expected {wanted}, got {result.verdict.decision.value}: "
                               f"{result.verdict.reasons}")
    return CaseResult(case, guard.evaluate(_action(case["action"]), ctx), check_route)


def run_suite(mode: str = "routed", reviewer: str = "heuristic", cases: list[dict] | None = None) -> list[CaseResult]:
    cases = cases if cases is not None else load_cases()
    return [run_case(c, make_router(mode, reviewer), check_route=mode == "routed") for c in cases]


def summarize(results: list[CaseResult]) -> dict:
    attacks = [r for r in results if r.case["expect"] != "allow"]
    benign = [r for r in results if r.case["expect"] == "allow"]
    return {
        "cases": len(results),
        "attacks_stopped": sum(r.stopped for r in attacks),
        "attacks": len(attacks),
        "false_positives": sum(r.stopped for r in benign),
        "benign": len(benign),
        "exact": sum(r.correct for r in results),
        "review_cost": sum(r.cost for r in results),
    }


# ---------------------------------------------------------------------------
def _print_table(results: list[CaseResult]) -> None:
    by_cat: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        by_cat[r.case["category"]].append(r)
    for cat, rs in by_cat.items():
        print(f"\n{cat}  ({sum(r.correct for r in rs)}/{len(rs)})")
        for r in rs:
            mark = "PASS" if r.correct else ("GAP " if r.case.get("known_gap") else "FAIL")
            caught = r.verdict.caught_by or "-"
            print(f"  {mark} {r.case['id']:<8} expect={r.case['expect']:<8} got={r.decision:<8} "
                  f"by={caught:<18} {r.case['description']}")
            if not r.correct:
                for reason in r.verdict.reasons or ["(no objections raised)"]:
                    print(f"         {reason}")
                if r.case.get("known_gap"):
                    print(f"         known gap: {r.case['known_gap']}")


def _print_summary(mode: str, s: dict) -> None:
    print(
        f"{mode:<19} attacks stopped {s['attacks_stopped']:>2}/{s['attacks']:<3} "
        f"false positives {s['false_positives']}/{s['benign']:<3} "
        f"exact {s['exact']:>2}/{s['cases']:<3} review cost {s['review_cost']:>6.0f}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=MODES, default="routed")
    ap.add_argument("--reviewer", choices=["heuristic", "claude"], default="heuristic")
    ap.add_argument("--ablation", action="store_true", help="run every mode and compare")
    args = ap.parse_args(argv)

    if args.ablation:
        print("review cost: relative units (fast review = 1, careful review = 40, from list prices)\n")
        for mode in MODES:
            _print_summary(mode, summarize(run_suite(mode, args.reviewer)))
        return 0

    results = run_suite(args.mode, args.reviewer)
    _print_table(results)
    print()
    _print_summary(args.mode, summarize(results))
    unexpected = [r for r in results if not r.correct and not r.case.get("known_gap")]
    return 1 if unexpected else 0


if __name__ == "__main__":
    raise SystemExit(main())
