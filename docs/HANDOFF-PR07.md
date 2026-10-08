# Handoff: PR-07 (Kartik)

Read this together with `Claude.pdf` (the team plan). Between the two you have everything
needed to finish PR-07, the last PR of the project.

## 1. Who owns what (the PDF has two names swapped)

The PDF assigns PR-06 to Kartik and PR-07 to Divyanshu. The team **swapped** them:

- **PR-06 (done by Divyanshu):** output_schema + faithfulness guards, tiered pipeline.
- **PR-07 (Kartik, this one):** telemetry, dashboard, load test, final numbers.

So wherever the PDF says **"Divyanshu" for PR-07 work, read "Kartik"**. That covers the PR-07
section, the 9:45–11:45 presentation slot ("Observability & numbers"), the viva questions
listed under Divyanshu, the resume line, and the Langfuse keys secret. CONTRIBUTING.md
already shows the swap.

## 2. State of the repo when you start

- `main` has PR-01 to PR-06 merged: 9 policies, all guards done, eval gate green.
- PR-07 is the PDF's section "Divyanshu · PR-07 Telemetry, dashboard, load test, final
  numbers" (Part A telemetry, Part B dashboard, Part C load test + numbers). Build exactly that.
- Branch name from the PDF: `feat/observability-results`.

## 3. What already exists for you (don't rebuild it)

- **Trace data:** `Pipeline.run()` returns a `PipelineTrace` (`dwarpal/pipeline.py`) with
  `request_id`, `policies` (name@version), `results` (per guard: `guard`, `version`, `stage`,
  `action`, `score`, `latency_ms`, `cost_usd`, `error`, `shadow`, `cached`), `blocked_by`,
  `upstream_latency_ms`, `total_latency_ms`, `added_latency_ms` (= total − upstream) and
  `guard_cost_usd`. Your hook goes after the trace is complete.
- **Cost:** cost per request = upstream `usage` tokens × price + `trace.guard_cost_usd` (the
  judge and the schema repair already price themselves at list price). PR-07 still adds
  `config/pricing.yaml` for the upstream model.
- **Benchmark you can reuse:** `scripts/bench_pipeline.py` runs every dev case through the real
  pipeline against the in-process mock with a stand-in judge (`set_llm(...)`). Copy its
  pattern for `make bench`; `docs/tradeoffs.md` has its numbers.
- **Eval numbers:** `make eval` writes `eval/reports/latest.json`, the source for the README
  results table and the dashboard's eval panel.

## 4. Things that will bite you (read before starting)

1. **Gemini free tier = ~20 requests a day per model per Google project.** The judge and the
   schema repair use `gemini-3.5-flash-lite` (set in `policies/faithfulness.yaml` and
   `policies/output_schema.yaml`). Never load-test against real Gemini; use the mock upstream
   plus a stand-in judge, as the PDF and `bench_pipeline.py` do. The PDF's "50 real requests
   to Gemini" needs a key with quota (another Google project or billing on): plan it as one
   run, and send those requests **without** `dwarpal.context`, so the judge doesn't spend
   quota too.
2. **If the CI eval gate shows faithfulness 0.000, that's the Gemini daily quota, not your
   code.** Re-run the `gate` job the next day. The repo secret is `GEMINI_API_KEY`.
3. **There is no live URL.** Hugging Face requires PRO for CPU Spaces, so the deployment is
   built but not hosted (see README "Deploy" and `docs/decisions/0006`). Replace the PDF's
   "live URL" with a local run (`make dev`, or the Docker image on :7860) and say so in the
   README. The same applies to "deploy the dashboard as a second HF Space": ship it
   runnable locally and write down why.
4. **Shared files get small, marked additions only** (CONTRIBUTING.md rule 2):
   `pipeline.py` gets the one telemetry hook line; `app.py` gets the two admin endpoints and
   the `X-Dwarpal-Added-Latency-Ms` header. Mark them `# PR-07`.
5. **Tests must pass with no API keys**: Langfuse turns itself off without keys, and SQLite
   writes go to a temp dir in tests. `make lint test` and `make eval` are what CI runs.
6. **429s from the rate limiter never reach the pipeline**, so they have no trace. Count them
   from the response status if the dashboard should show them.
7. **Windows only:** `make` isn't installed; run the Makefile recipes directly with `uv run`.
   Set `PYTHONUTF8=1` when running `python -m eval.harness` in a Windows terminal.

## 5. Done when (from the PDF, adjusted for no live URL)

- A request to the running proxy shows up in SQLite (`data/requests.db`), in Langfuse (if
  keys are set) and in the dashboard, with correct per-guard times.
- `GET /v1/dwarpal/stats?window=1h` and `GET /v1/dwarpal/requests?limit=100` work behind
  `ADMIN_TOKEN`.
- `make bench` writes `results/benchmarks.json` + `.md` (req/s, added latency p50/p99, CPU,
  RAM per scenario, machine spec).
- **README has no placeholders left**: the results table (per-guard catch rate and FPR, dev
  and holdout, from `eval/reports/latest.json`), added latency p50/p99, throughput, cost per
  request, the dashboard link or local instructions, and the resume line with real numbers.
- A `docs/decisions/0008-*.md` note, the same shape as 0006/0007.
- PR squash-merged with CI green, then tag `v1.0.0` on `main` (Shreyas does the tag).
