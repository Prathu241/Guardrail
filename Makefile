.PHONY: install test lint redteam security baseline demo chaos serve docker docker-smoke check live

install:
	pip install -e ".[dev]"

lint:
	ruff check guardrail tests

test:
	python -m pytest -q

# Real Claude reviewers and agent (needs ANTHROPIC_API_KEY), then the red-team suite on Claude reviewers
live:
	python -m pytest -m live -v
	python -m guardrail redteam --reviewer claude

# Red-team suite: every configuration, recorded, and compared with the committed baseline
redteam:
	python -m guardrail redteam --ablation --out redteam/results/latest.json --report redteam/results/latest.md \
		--check redteam/results/baseline.json
	python -m guardrail redteam

# Re-record the baseline after deliberately changing the suite or the defences
baseline:
	python -m guardrail redteam --ablation --out redteam/results/baseline.json --report docs/RESULTS.md

# The complete security regression suite: adversarial unit tests, races and crashes, side effects,
# Claude failure handling, red-team (all configurations) and chaos runs
security:
	python -m pytest -q tests/test_hardening.py tests/test_checks.py tests/test_redteam.py tests/test_concurrency.py \
		tests/test_side_effects.py tests/test_claude_http.py tests/test_faults.py
	$(MAKE) redteam
	python -m guardrail chaos --rounds 3 > redteam/results/latest.chaos.json

demo:
	python -m guardrail demo

chaos:
	python -m guardrail chaos --rounds 3

serve:
	python -m guardrail serve

docker:
	docker compose up --build

# Build the image and run the end-to-end container smoke test (what CI's docker job runs)
docker-smoke:
	docker build -t guardrail:test .
	IMAGE=guardrail:test scripts/docker_smoke.sh

# Everything CI runs
check: lint test redteam chaos
