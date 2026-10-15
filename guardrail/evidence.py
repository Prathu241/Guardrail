"""Evidence register: re-run every headline result and record how it was produced.

    python -m guardrail.evidence                      # writes docs/evidence/evidence.json and docs/EVIDENCE.md
    python -m guardrail.evidence --no-tests --no-history   # quick: skip the pytest run and the git-history replay

Each claim records its result, the exact configuration, how many cases it rests on, and whether
the agent and the reviewers were live models or simulated stand-ins, so a reader can audit it and
re-run it. Nothing here calls a live model; claims that need one are listed as not yet run.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from . import __version__
from .app import build_app
from .config import Settings
from .demo import SAMPLE_TICKETS, final_demo, scenario_hallucinations
from .redteam import MODES, load_cases, make_router, record, run_case

ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "docs" / "evidence" / "evidence.json"
OUT_MD = ROOT / "docs" / "EVIDENCE.md"
PRE_HELD_OUT_COMMIT = "582c4ad"  # the code before round-2 held-out attacks were written

NO_AGENT = "none: each case replays one fixed, pre-written proposed action"
REVIEWERS = {
    "deterministic-only": "none: model review switched off (stub approves everything)",
    "fast-only": "simulated: offline heuristic fast tier on every action",
    "routed": "simulated: offline heuristic fast and careful tiers, routed by risk",
    "careful-only": "simulated: offline heuristic careful tier on every action",
}
COST_NOTE = ("review cost = number of simulated reviewer calls weighted by list-price ratio (fast 1, careful 40); "
             "it is not measured API spend")


def claim(cid, area, statement, result, configuration, cases, agent, reviewers, reproduce, **extra):
    return {"id": cid, "area": area, "claim": statement, "result": result, "configuration": configuration,
            "cases": cases, "agent": agent, "reviewers": reviewers, "reproduce": reproduce, **extra}


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------------------- red team
def redteam_claims() -> tuple[list[dict], dict]:
    rec = record("heuristic", repeats=2)
    cases = {c["id"]: c for c in load_cases()}
    attacks = [c for c in cases.values() if c["expect"] != "allow"]
    suite = rec["suite"]
    base_cfg = {"suite": f"redteam/cases.json (sha256 {suite['sha256'][:12]})",
                "attacks": suite["attacks"], "legitimate_controls": suite["benign"],
                "runs_per_configuration": 2, "deterministic": None}
    out = []
    for mode in MODES:
        c = rec["configurations"][mode]
        out.append(claim(
            f"E1-{mode}", "evaluation",
            f"Ablation, {mode}: attacks stopped and legitimate actions wrongly stopped",
            {"attacks_stopped": f"{c['attacks_stopped']}/{c['attacks']}",
             "false_positives": f"{c['false_positives']}/{c['benign']}",
             "review_cost": c["review_cost"], "missed": c["missed"],
             "identical_across_runs": c["deterministic"]},
            {**base_cfg, "deterministic": c["deterministic"], "configuration": mode,
             "stopped_means": "BLOCK or ESCALATE (not executed automatically)", "cost": COST_NOTE},
            suite["cases"], NO_AGENT, REVIEWERS[mode],
            "python -m guardrail redteam --ablation"))

    # Novelty mechanisms, isolated with model review switched off.
    det = {r.case["id"]: r for r in _run("deterministic-only")}
    def stopped_by_checks(ids):
        return f"{sum(det[i].stopped for i in ids)}/{len(ids)}"
    escalate_ids = [c["id"] for c in attacks if c["expect"] == "escalate"]
    escalated = [i for i in escalate_ids if det[i].decision == "escalate"]
    provenance = [c["id"] for c in attacks if c["category"] == "provenance"]
    intent = [c["id"] for c in attacks if c["category"] == "ungrounded_intent"]
    reviewer_targeted = [c["id"] for c in attacks if c["category"] == "injection"]
    nocfg = {**base_cfg, "configuration": "deterministic-only (checks alone)"}
    out += [
        claim("N1", "novelty", "Deterministic allowlist first; models only for what rules can't judge",
              {"stopped_by_checks_alone": f"{sum(det[c['id']].stopped for c in attacks)}/{len(attacks)}",
               "left_for_reviewers": [i for i in det if not det[i].stopped and cases[i]["expect"] != "allow"]},
              nocfg, len(attacks), NO_AGENT, REVIEWERS["deterministic-only"],
              "python -m guardrail redteam --mode deterministic-only"),
        claim("N2", "novelty", "Unprovable or policy-reserved actions go to a person instead of executing",
              {"escalated_by_checks_alone": f"{len(escalated)}/{len(escalate_ids)}",
               "legitimate_controls_stopped": f"{sum(det[i].stopped for i in det if cases[i]['expect'] == 'allow')}"
                                              f"/{suite['benign']}"},
              nocfg, len(escalate_ids) + suite["benign"], NO_AGENT, REVIEWERS["deterministic-only"],
              "python -m guardrail redteam --mode deterministic-only", case_ids=escalate_ids),
        claim("N3", "novelty", "Provenance catches real but unrequested records",
              {"stopped_by_checks_alone": stopped_by_checks(provenance)},
              nocfg, len(provenance), NO_AGENT, REVIEWERS["deterministic-only"],
              "python -m guardrail redteam --mode deterministic-only", case_ids=provenance),
        claim("N4", "novelty", "Injection, including text aimed at the reviewer, is stopped before any model sees it",
              {"stopped_by_checks_alone": stopped_by_checks(reviewer_targeted)},
              nocfg, len(reviewer_targeted), NO_AGENT, REVIEWERS["deterministic-only"],
              "python -m guardrail redteam --mode deterministic-only", case_ids=reviewer_targeted),
        claim("N5", "novelty", "Intent errors (wrong item, invented reason) need the reviewer layer",
              {"stopped_by_checks_alone": stopped_by_checks(intent),
               "stopped_with_routed_review": f"{sum(rec['configurations']['routed']['decisions'][i] != 'allow' for i in intent)}"
                                             f"/{len(intent)}"},
              {**base_cfg, "configuration": "deterministic-only vs routed"}, len(intent), NO_AGENT,
              "simulated: offline heuristic reviewers (routed); none (deterministic-only)",
              "python -m guardrail redteam --ablation", case_ids=intent),
    ]
    return out, rec


def _run(mode: str):
    return [run_case(c, make_router(mode), check_route=mode == "routed") for c in load_cases()]


def held_out_history() -> dict | None:
    """Replay the round-2 held-out cases against the code from before they were written."""
    held = [c for c in load_cases() if c.get("held_out") == "round-2"]
    if not git("rev-parse", "--verify", PRE_HELD_OUT_COMMIT):
        return None
    script = ("import json, sys; sys.path.insert(0, sys.argv[1]); from guardrail.redteam import make_router, run_case;"
              "cases = json.load(open(sys.argv[2]));"
              "print(json.dumps({c['id']: run_case(c, make_router('routed'), False).decision for c in cases}))")
    with tempfile.TemporaryDirectory() as tmp:
        tree, case_file = Path(tmp) / "tree", Path(tmp) / "cases.json"
        case_file.write_text(json.dumps(held))
        try:
            subprocess.run(["git", "worktree", "add", "--detach", str(tree), PRE_HELD_OUT_COMMIT], cwd=ROOT,
                           check=True, capture_output=True, timeout=60)
            out = subprocess.run([sys.executable, "-I", "-c", script, str(tree), str(case_file)], cwd=tmp,
                                 capture_output=True, text=True, timeout=300, check=True).stdout
            return json.loads(out.strip().splitlines()[-1])
        except (subprocess.SubprocessError, ValueError, IndexError):
            return None
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT, capture_output=True)


def held_out_claim(rec: dict, before: dict | None) -> dict:
    held = [c for c in load_cases() if c.get("held_out") == "round-2"]
    attacks = [c["id"] for c in held if c["expect"] != "allow"]
    controls = [c["id"] for c in held if c["expect"] == "allow"]
    now = rec["configurations"]["routed"]["decisions"]
    result = {"attacks_stopped_now": f"{sum(now[i] != 'allow' for i in attacks)}/{len(attacks)}",
              "controls_stopped_now": f"{sum(now[i] != 'allow' for i in controls)}/{len(controls)}"}
    if before is not None:
        result["attacks_allowed_by_earlier_code"] = sorted(i for i in attacks if before.get(i) == "allow")
        result["earlier_code"] = PRE_HELD_OUT_COMMIT
    return claim("E2", "evaluation", "Held-out attacks (written after the defences) are stopped",
                 result, {"configuration": "routed", "selection": "cases tagged held_out=round-2",
                          "written_by": "the same author as the defences (not independent)"},
                 len(held), NO_AGENT, REVIEWERS["routed"],
                 "python -m guardrail.evidence (replays the earlier commit in a git worktree)",
                 case_ids=attacks + controls)


# ---------------------------------------------------------------------------- agent runs
def chaos_claim(seed: int = 7, rounds: int = 3) -> dict:
    app = build_app(Settings(db_path=":memory:", fault_rate=1.0, fault_seed=seed))
    conversations = 0
    for _ in range(rounds):
        for customer, message in SAMPLE_TICKETS:
            conv = app.conversations.start(message, customer)
            conversations += 1
            while conv["status"] == "awaiting_approval":
                for a in conv["pending_approvals"]:
                    res = app.approvals.decide(a["id"], True, "chaos-auto-approver", "chaos run")
                conv = res["conversation"]
    rows = app.guard.audit.list(limit=100_000)
    faulted = [r for r in rows if r["injected_fault"]]
    return claim(
        "E3", "evaluation", "Hallucinations injected into agent runs are stopped (chaos run, stand-in agent)",
        {"faults_injected": len(faulted), "blocked": sum(r["decision"] == "block" for r in faulted),
         "escalated": sum(r["decision"] == "escalate" for r in faulted),
         "executed": sum(bool(r["executed"]) for r in faulted),
         "by_fault": dict(Counter(r["injected_fault"] for r in faulted))},
        {"tickets": len(SAMPLE_TICKETS), "rounds": rounds, "conversations": conversations,
         "actions_evaluated": len(rows), "fault_rate": 1.0, "fault_seed": seed,
         "approvals": "every escalation auto-approved, so an escalated fault would have executed"},
        len(faulted), "simulated: offline rule-based agent, one tool call per turn corrupted by the fault injector",
        "simulated: offline heuristic reviewers, routed by risk",
        f"python -m guardrail chaos --rounds {rounds}")


def scripted_claims() -> list[dict]:
    with contextlib.redirect_stdout(io.StringIO()):
        conv = scenario_hallucinations(Settings(db_path=":memory:"))
        demo = final_demo(Settings(db_path=":memory:"))
    log = conv["guard_log"]
    bad = [e for e in log if not e["executed"]]
    refunds = [r for r in demo["audit"] if r["tool"] == "issue_refund"]
    return [
        claim("E4", "evaluation", "A scripted hallucinating agent: every bad action is stopped",
              {"proposed": len(log), "stopped": len(bad), "executed": len(log) - len(bad),
               "executed_only_after_human_approval": sum(1 for e in log if e["executed"] and e.get("approval"))},
              {"scenario": "guardrail demo --scenario 1", "human_approval": "scripted: the demo approves it"},
              len(log), "simulated: scripted agent replaying a fixed 9-step trajectory",
              "simulated: offline heuristic reviewers, routed by risk", "python -m guardrail demo --scenario 1"),
        claim("E5", "evaluation", "Three-act demo: valid action, blocked hallucination, human escalation",
              {"refunds": [(r["args"]["order_id"], r["decision"], bool(r["executed"])) for r in refunds],
               "paid": demo["paid"], "reconciliation_mismatches": len(demo["reconciliation"]["findings"])},
              {"acts": 3, "approver": "scripted", "providers": "simulated payment and messaging providers"},
              len(demo["audit"]), "simulated: offline rule-based agent (acts 1, 3) and a scripted agent (act 2)",
              "simulated: offline heuristic reviewers, routed by risk", "python -m guardrail demo"),
    ]


# ---------------------------------------------------------------------------- automated tests
TEST_GROUPS = [
    ("T1", "novelty", "Reviewer output is verified: invented evidence, 16 malformed or failed replies, timeouts "
                      "and retries all fail closed",
     ["test_claude_http.py", "test_claude_reviewer.py"],
     "simulated: scripted agent turns served by a local fake Messages API",
     "simulated: replies scripted by a local fake Messages API, parsed by the real anthropic SDK"),
    ("T2", "novelty", "Verification is atomic with execution: limits hold under races, commits are idempotent, "
                      "crashes recover without repeating anything",
     ["test_concurrency.py"], "none, or a scripted agent; threads and real OS processes",
     "none: review stubbed to approve everything, to isolate the transaction layer"),
    ("T3", "novelty", "Side effects are delivered after commit, retried, compensated and reconciled",
     ["test_side_effects.py"], "none, or the offline rule-based agent",
     "none (stub) or simulated: offline heuristic reviewers"),
    ("T4", "novelty", "Grounding contracts, postconditions and fail-closed paths",
     ["test_hardening.py"], "none, or a scripted agent", "simulated: stub reviewers with fixed verdicts"),
]


def test_claims() -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "junit.xml"
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}"],
                       cwd=ROOT, capture_output=True, timeout=1800)
        tree = ET.parse(report)
    per_file: dict[str, Counter] = {}
    for case in tree.iter("testcase"):
        name = case.get("classname", "").split(".")[-1] + ".py"
        status = ("skipped" if case.find("skipped") is not None
                  else "failed" if case.find("failure") is not None or case.find("error") is not None else "passed")
        per_file.setdefault(name, Counter())[status] += 1
    total = sum(per_file.values(), Counter())
    out = []
    for cid, area, statement, files, agent, reviewers in TEST_GROUPS:
        counts = sum((per_file.get(f, Counter()) for f in files), Counter())
        out.append(claim(cid, area, statement, dict(counts), {"test_files": [f"tests/{f}" for f in files]},
                         sum(counts.values()), agent, reviewers,
                         "python -m pytest -q " + " ".join(f"tests/{f}" for f in files)))
    out.append(claim("T5", "evaluation", "Automated test suite", dict(total),
                     {"live_tests": "skipped unless ANTHROPIC_API_KEY is set"}, sum(total.values()),
                     "mixed (see T1–T4)", "mixed (see T1–T4)", "python -m pytest -q"))
    return out


NOT_RUN = claim(
    "L1", "evaluation", "Behaviour with live Claude reviewers and a live Claude agent",
    {"status": "NOT RUN: no API key was available; every result above uses simulated reviewers"},
    {"planned": "make live, then python -m guardrail redteam --reviewer claude --ablation"},
    0, "live (planned)", "live (planned)", "make live")


# ---------------------------------------------------------------------------- report
def build(run_tests: bool = True, history: bool = True) -> dict:
    claims, rec = redteam_claims()
    claims.insert(len(MODES), held_out_claim(rec, held_out_history() if history else None))
    claims += [chaos_claim(), *scripted_claims()]
    if run_tests:
        claims += test_claims()
    claims.append(NOT_RUN)
    return {
        "environment": {"guardrail": __version__, "git_commit": git("describe", "--always", "--dirty"),
                        "python": platform.python_version(), "platform": platform.platform()},
        "suite": rec["suite"],
        "live_models_used": False,
        "claims": claims,
    }


def _fmt(value) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{k.replace('_', ' ')}: {_fmt(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(_fmt(v) for v in value) if value else "none"
    return str(value)


def markdown(ev: dict) -> str:
    env = ev["environment"]
    lines = [
        "# Evidence register",
        "",
        "Every headline number in the abstract, deck and docs, with the configuration that produced it, how",
        "many cases it rests on, and whether the agent and the reviewers were live models or simulated stand-ins.",
        "Generated by `python -m guardrail.evidence`; do not edit by hand.",
        "",
        f"Commit `{env['git_commit']}` · guardrail {env['guardrail']} · Python {env['python']} · "
        f"red-team suite sha256 `{ev['suite']['sha256'][:12]}`.",
        "",
        "**No live model has been used for any result below.** \"Simulated\" means an offline stand-in: the",
        "heuristic reviewers are rule-based approximations of the Claude reviewers, the rule-based agent is a",
        "deterministic stand-in for the Claude agent, and scripted agents replay fixed turns. Live results (L1)",
        "have not been run.",
        "",
        "| ID | Claim | Result | Cases | Agent | Reviewers |",
        "|---|---|---|---|---|---|",
    ]
    for c in ev["claims"]:
        lines.append(f"| {c['id']} | {c['claim']} | {_fmt(c['result'])} | {c['cases']} | {c['agent']} | "
                     f"{c['reviewers']} |")
    lines += ["", "## Configuration and how to reproduce each claim", ""]
    for c in ev["claims"]:
        lines.append(f"**{c['id']}** ({c['area']}): {_fmt(c['configuration'])}. Reproduce: `{c['reproduce']}`"
                     + (f". Cases: {', '.join(c['case_ids'])}" if c.get("case_ids") else "") + ".")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-tests", action="store_true", help="skip the pytest run (T1–T5)")
    ap.add_argument("--no-history", action="store_true", help="skip replaying held-out cases on the earlier commit")
    ap.add_argument("--json", type=Path, default=OUT_JSON)
    ap.add_argument("--markdown", type=Path, default=OUT_MD)
    args = ap.parse_args(argv)
    ev = build(run_tests=not args.no_tests, history=not args.no_history)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(ev, indent=2, ensure_ascii=False) + "\n")
    args.markdown.write_text(markdown(ev))
    for c in ev["claims"]:
        print(f"{c['id']:<22} {_fmt(c['result'])[:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
