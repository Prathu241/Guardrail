"""The committed evidence register must match what the code produces today."""

import json
from pathlib import Path

from guardrail.evidence import OUT_JSON, build


def test_committed_evidence_matches_a_fresh_run():
    committed = {c["id"]: c for c in json.loads(Path(OUT_JSON).read_text())["claims"]}
    fresh = build(run_tests=False, history=False)
    assert fresh["live_models_used"] is False
    for c in fresh["claims"]:
        old = committed[c["id"]]
        assert (c["cases"], c["agent"], c["reviewers"]) == (old["cases"], old["agent"], old["reviewers"]), c["id"]
        result = json.loads(json.dumps(c["result"]))
        stale = {k: v for k, v in old["result"].items() if k in result and result[k] != v}
        assert not stale, f"{c['id']}: docs/EVIDENCE.md is stale ({stale}); run `make evidence`"
