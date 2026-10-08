.PHONY: install dev mock dev-mock lint format test eval bench dashboard

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

# PR-07: Dwarpal's overhead under load, against the mock upstream (300 ms), per scenario and
# concurrency. Writes results/benchmarks.json and .md; takes about 10 minutes. loadtest/bench.py
bench:
	uv run --all-extras --group loadtest python loadtest/bench.py

# PR-07: dashboard on http://localhost:8501, reading the proxy on :8000 (set ADMIN_TOKEN in .env).
dashboard:
	uv run --group dashboard streamlit run dashboard/app.py --server.headless true --browser.gatherUsageStats false
