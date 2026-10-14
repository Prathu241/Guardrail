.PHONY: install test lint redteam demo chaos serve docker check live

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

redteam:
	python -m guardrail redteam --ablation
	python -m guardrail redteam

demo:
	python -m guardrail demo

chaos:
	python -m guardrail chaos --rounds 3

serve:
	python -m guardrail serve

docker:
	docker compose up --build

# Everything CI runs
check: lint test redteam chaos
