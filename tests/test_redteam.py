import pytest

from guardrail.redteam import load_cases, make_router, run_case, run_suite, summarize

CASES = load_cases()


def _param(case):
    marks = [pytest.mark.xfail(reason=case["known_gap"], strict=True)] if case.get("known_gap") else []
    return pytest.param(case, id=case["id"], marks=marks)


@pytest.mark.parametrize("case", [_param(c) for c in CASES])
def test_case(case):
    result = run_case(case, make_router("routed"))
    assert result.correct, (result.decision, result.verdict.route, result.verdict.reasons)


def test_no_false_positives_in_any_mode():
    for mode in ("deterministic-only", "fast-only", "routed", "careful-only"):
        assert summarize(run_suite(mode))["false_positives"] == 0, mode


def test_routed_stops_every_attack_cheaper_than_reviewing_everything_carefully():
    routed, careful = summarize(run_suite("routed")), summarize(run_suite("careful-only"))
    assert routed["attacks_stopped"] == routed["attacks"]
    assert routed["false_positives"] == 0
    assert routed["review_cost"] < careful["review_cost"] / 2


def test_reviewers_add_coverage_over_deterministic_checks():
    det, routed = summarize(run_suite("deterministic-only")), summarize(run_suite("routed"))
    assert routed["attacks_stopped"] > det["attacks_stopped"]


def test_case_ids_unique():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))
