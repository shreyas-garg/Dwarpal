.PHONY: install dev mock dev-mock lint format test eval bench

install:
	uv sync --all-extras

dev:
	uv run uvicorn dwarpal.app:app --reload --port 8000

# Fake LLM on :9000, no API key needed.
mock:
	uv run uvicorn dwarpal.testing.mock_upstream:app --port 9000

# Proxy pointed at the mock.
dev-mock:
	UPSTREAM_BASE_URL=http://localhost:9000/v1/ uv run uvicorn dwarpal.app:app --reload --port 8000

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

test:
	uv run pytest

# Score the red-team sets; exits non-zero when a policy regresses. See eval/README.md.
eval:
	uv run python -m eval.harness

# Filled in by PR-07 (load test + benchmarks).
bench:
	@echo "benchmarks arrive in PR-07"
