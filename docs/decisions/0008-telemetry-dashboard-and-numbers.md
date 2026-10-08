# 0008: Telemetry, dashboard, load test and the final numbers

**Status:** accepted · **Owner:** Kartik · **PR:** 07 (owner swapped with PR-06)

## Decision

**Request log.** Every request the proxy finishes becomes one SQLite row in `data/requests.db`
(`REQUEST_DB`): request id, time, active policies and versions, each guard's action, score,
latency, cost and shadow / cached / error flags, upstream time and tokens, upstream and guard
cost, total latency, **added latency = total − upstream**, and `blocked_by`. The pipeline hands
the finished trace to `dwarpal/telemetry` (`await telemetry.record(trace, ctx)`, one marked line
at each of `Pipeline.run`'s two exits). That builds the row and queues it; a background task
writes queued rows in batches, off the event loop, so logging never slows a request. A
telemetry error is logged, never raised.

**No raw prompts** (`LOG_PROMPTS=false`). The row keeps a SHA-256 of the user text as it was
forwarded upstream, i.e. after redaction. `LOG_PROMPTS=true` stores that redacted text instead.
Guard reasons are left out because some quote the text they matched.

**Cost.** `config/pricing.yaml` lists USD per 1M input / output tokens per model, with the source
link and the date checked (Gemini 2.5 Flash: $0.30 / $2.50, 2026-10-08). Upstream cost = the
upstream's `usage` × list price. Gemini bills thinking tokens as output, and its OpenAI layer may
count them only in `total_tokens`, so output = max(completion, total − prompt). Cost per request =
upstream cost + what the guards' own model calls cost (judge, schema repair; they price
themselves, from PR-06). Free-tier calls are priced at list price too.

**Langfuse.** One trace per request, sent over OpenTelemetry to Langfuse's OTLP endpoint. It holds:

- a root span;
- one `guardrail` observation per guard that ran (shadow ones too), carrying its action, score
  and policy version;
- one `generation` for the upstream call, with model, tokens and cost.

Every span is tagged with the active policy versions. Langfuse is off unless both
`LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set, so tests and CI need no keys.

**Endpoints**, both behind `ADMIN_TOKEN`, sent in an `X-Admin-Token` header (403 while it is
unset, 401 on a wrong token):

- `GET /v1/dwarpal/stats?window=1h`: requests, block rate, added / total / upstream latency
  p50/p95/p99, per guard runs, blocks, redactions, errors and latency p50/p99, cost per request,
  shadow-vs-enforce disagreements and the active policies.
- `GET /v1/dwarpal/requests?limit=100`: the latest rows.

Every response also carries an `X-Dwarpal-Added-Latency-Ms` header.

**Dashboard.** `dashboard/app.py` (Streamlit, `make dashboard`) reads the two endpoints and
`eval/reports/latest.json`. It shows:

- headline tiles: requests, block rate, added latency p50 / p99, cost per request;
- latency p50/p99 and block rate per guard;
- added latency per request over time;
- the cost split (upstream model vs guard LLM calls);
- active policy versions;
- shadow-vs-enforce disagreements;
- the latest eval tables;
- one request at a time, with each guard's decision and time.

**Load test.** `loadtest/locustfile.py` against the mock upstream fixed at 300 ms, driven by
`make bench` (`loadtest/bench.py`). There are four scenarios, each at 1, 10 and 50 concurrent
users for 30 s, against a fresh proxy that has only that scenario's policies:

- no guards;
- cheap only;
- all but faithfulness;
- all, where each request sends the FAQ as context so the judge checks every reply.

Where the numbers come from:

- requests/second: Locust;
- added latency and cost: the proxy's own request log;
- CPU and peak RAM: psutil on the proxy process.

The machine spec goes into `results/benchmarks.json` and `.md`.

`loadtest/real_run.py` sends the 50 real requests to Gemini, with no context so the judge spends
no quota.

## Why

- **SQLite with a background writer.** No server to run, one file, in the standard library, and
  WAL mode lets the dashboard read while the proxy writes. Batching keeps up with the load test
  (150+ requests a second) without a commit on the request path.
- **A hash, not the prompt.** The PII guard removes personal data on the way in; a log that kept
  the raw prompt would store exactly what it removed. The hash still tells you whether two
  requests sent the same text.
- **OpenTelemetry, not the Langfuse SDK.** The hook runs once the request is finished, so the
  spans need explicit start and end times. The v4 SDK's public API starts a span "now" (only its
  end time can be set), and the old `/api/public/ingestion` API, which took explicit times, stops
  accepting traces on Langfuse Cloud on 16 Nov 2026. OTLP is Langfuse's supported path and takes
  explicit times; the attribute names are the ones the SDK itself sends.
- **An admin token separate from `API_KEYS`.** A client key lets an app call the model. The log
  shows every request's metadata, which is a different permission.
- **Load test against the mock**, as the plan asked: it measures Dwarpal's overhead rather than
  Gemini's latency, it is repeatable, and it costs nothing. Real Gemini on the free tier allows
  about 20 requests a day per model.
- **Safe traffic only in the load test.** An attack is cut short by the first block (no
  upstream call, fewer guards), so a mix with attacks would make Dwarpal look faster than it is
  for real users.
- **Plain chat for "all but faithfulness", the FAQ as context for "all".** Faithfulness runs only
  when a request sends context, so on plain chat requests "all" and "all but faithfulness" are
  the same thing. Sending context in "all" shows what a grounded request costs.
- **Dashboard run locally, not on a second Space.** Free CPU Spaces need a PRO plan
  ([0006](0006-content-guards-and-deployment.md)), and the dashboard needs the proxy's admin API,
  so it would need hosting too. `make dashboard` serves it on :8501.

## Rejected

| Option | Why not |
|---|---|
| Prometheus + Grafana | Two more services to run for one process; per-request, per-guard decisions are not metrics |
| Postgres or a hosted database | Needs a server; SQLite is enough for one process |
| Writing the row inside the request | A SQLite commit on every request's critical path |
| Langfuse Python SDK | Its public API cannot set a span's start time, and the trace is complete when the hook runs |
| Langfuse `/api/public/ingestion` | Deprecated; stops taking traces on Langfuse Cloud on 16 Nov 2026 |
| Logging raw prompts by default | The log would keep the PII the guards removed |
| Load-testing against Gemini | Measures Gemini, not Dwarpal; the free tier allows ~20 requests a day |
| A second Hugging Face Space for the dashboard | Needs PRO (0006); runs locally instead |
| Logging 429s | Refused by the rate limits before the pipeline, so they have no trace; counting them would need more code in `app.py` than the plan's marked additions |

## Evidence

`tests/test_telemetry.py` (12 tests, no keys) checks:

- a request through the proxy lands in the log with the same per-guard times as its trace, and
  the header matches the logged added latency;
- the prompt text appears nowhere in the database files;
- cost = usage × price + the guards' own costs;
- shadow disagreements are counted;
- the admin endpoints refuse a missing or wrong token;
- the Langfuse exporter emits one span per guard plus a `generation`, with exact durations.

By hand, against a running proxy:

- injection and banned-topic requests showed up as blocked;
- the PII request showed up as redacted;
- the dashboard showed all of them;
- `requests.db` held no trace of the PII text.

`make bench` on an Apple M3 laptop (8 cores, 16 GB), mock upstream at 300 ms, 30 s per run
([results/benchmarks.md](../../results/benchmarks.md)):

| Scenario | Users | Req/s | Added p50 | Added p99 | Proxy CPU | Safe requests refused |
|---|---:|---:|---:|---:|---:|---:|
| no guards | 1 / 10 / 50 | 3.3 / 32.2 / 149.4 | 0.0 ms | ≤ 0.1 ms | ≤ 0.2 cores | 0 |
| cheap only | 1 / 10 / 50 | 3.2 / 32.0 / 151.8 | 0.7 / 4.2 / 9.8 ms | 4.0 / 12.8 / 26.9 ms | ≤ 0.25 cores | 0 |
| all but faithfulness | 1 | 2.7 | 60 ms | 123 ms | 0.8 cores | 0 |
| | 10 | 20.5 | 155 ms | 496 ms | 5.9 cores | 0 |
| | 50 | 26.9 | 1,577 ms | 2,533 ms | 7.7 cores | 533 / 844 |
| all (FAQ as context) | 1 | 0.4 | 2,260 ms | 2,284 ms | 3.7 cores | 0 |
| | 10 | 3.3 | 3,006 ms | 3,062 ms | 7.8 cores | 100 / 100 |
| | 50 | 16.3 | 3,015 ms | 3,093 ms | 7.8 cores | 504 / 504 |

What the numbers say:

- **What dominates p99.** On plain chat it is the two injection classifiers: 30 ms p50 and
  84 ms p99 each, side by side in the `model` tier. Toxicity on the reply follows at 56 ms p99.
  The cheap tier costs under 1 ms; at 50 users it still adds only 10 ms, which is event-loop
  queueing. With context, the same two classifiers scanning the context dominate at 1.9 s each,
  not the judge (0.3 s here, 1.7 s for real).
- **The CPU is the limit.** About 20 req/s with every guard on 8 cores. Under overload a guard
  that overruns its `timeout_ms` is decided by `on_error`. At 50 users `pii` (1 s budget) timed
  out on 531 requests, and failing closed refused 63% of safe traffic. That is the designed
  failure mode (refuse rather than pass unchecked personal data), but a deployment must be sized
  so it never runs there.
- **Scanning context is the expensive check.** The injection guards score every context
  document in 512-token windows, and again as a de-disguised copy whenever normalising changes
  the text, which it does for the FAQ's markdown. That is four classifier passes for a 2 KB
  document: about 0.9 s per guard alone and 1.9 s with both running. From 10 users they time
  out and every grounded request is refused.
- **Cost per request** at list price, with the mock's token counts: $0.00006 for a plain
  request, $0.00038 when the judge checks the reply. Dwarpal's own cost is only its model calls:
  $0 on a plain request, and about $0.0004–0.0005 per judged reply against real Gemini
  ([0007](0007-output-guards-and-pipeline.md)).

**Real run.** `loadtest/real_run.py` (50 requests to Gemini through the proxy, no context) is
built and tested against the mock, but it was not run for this note: it needs a Gemini key with
quota, since the free tier allows about 20 requests a day per model. Its output,
`results/real_run.md`, gives true end-to-end latency and cost per request with real token counts.

## Consequences and known limits

- **One process.** The queue is in memory, so rows still queued when the process is killed hard
  are lost (a normal shutdown writes them first). Several workers could share the SQLite file in
  WAL mode, but each would keep its own queue.
- **Not every request is logged.** 429s from the rate limits and upstream errors never produce a
  trace, so the log and the dashboard don't include them.
- **Langfuse timeline.** The pipeline measures durations, not start times, so span starts are
  reconstructed. Durations are exact; guards that ran at the same time appear one after another.
- **Machine-specific numbers.** The load generator, the mock and the proxy share one laptop.
  Compare scenarios with each other, not with other machines.
- **The judge stand-in is fast.** It answers in 300 ms; the real judge's p50 is 1.7 s
  ([0007](0007-output-guards-and-pipeline.md)).
- **Mock token counts.** The mock counts four characters as a token and answers in one sentence,
  so the load-test costs show the arithmetic, not a real bill; the real run has real counts.
- **Overload refuses safe traffic.** This follows from the timeouts and `fail_closed` above.
  Nothing in the proxy sheds load before the guards saturate; only the per-IP limit of the
  deployed image comes close.

## What we would do with more time

These are for the guard owners; PR-07 only measures.

- **Cache the context scan per document.** The FAQ is the same on every request, but the
  decision cache keys on the whole request, so a new question rescans the same document.
- **Skip the de-disguised copy** when normalising only changes whitespace or markdown.
- **Move the CPU models** to a process pool, a second worker or a GPU, so the `model` tier
  really runs in parallel.
- **Shed load with a 429** before the guards saturate, instead of letting timeouts fail closed.

## Viva questions (PR-07)

- *Why load-test against a mock upstream, and what does that number not tell you?* It isolates
  Dwarpal's own overhead, repeatably and for free. It says nothing about end-to-end latency
  (Gemini's own seconds), Gemini's rate limits and errors under load, real token counts, or the
  network.
- *Which guard dominates p99?* The two injection classifiers: 84 ms p99 on plain chat, and
  1.9 s when they scan a context document. The judge adds 1.7 s, but only when context is sent.
- *How is cost per request computed on a free tier?* Usage tokens × list price
  (`config/pricing.yaml`, thinking tokens as output), plus what the judge and the schema repair
  report for their own calls. The free tier bills $0, so list price is what makes the number
  mean something.
- *Why not log raw prompts, and what does that cost you when debugging?* The PII guard removes
  personal data; logging it anyway would undo that. The hash still matches identical requests.
  The cost: a false positive can't be read back from the log. You need the user's text, or
  `LOG_PROMPTS=true` for a while, which logs the redacted text.
