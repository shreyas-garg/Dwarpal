# Trade-offs: how guards are scheduled

PR-06 experiment: one-after-another vs tiered vs all-parallel, measured with
`scripts/bench_pipeline.py`.

**Setup.** Every dev-set case (116: 56 attacks, 60 safe) through the real pipeline with all nine
policies, 3 rounds per strategy, decision cache off. Upstream is the in-process mock, so the
numbers are Dwarpal's own overhead. The LLM guards get a stand-in judge that waits 1.7 s (the
p50 measured against `gemini-3.5-flash-lite` with `scripts/check_judge.py`) and reports typical
token counts, so the run needs no API key. Machine: Windows 11 laptop, 4 cores, Python 3.11.

| Strategy | Added p50 ms | Added p99 ms | Judge calls / request | Guard cost / request (USD) | Decisions that differ from sequential |
|---|---:|---:|---:|---:|---:|
| sequential | 117.4 | 2008 | 0.121 | 0.000062 | 0 |
| **tiered** (default) | **81.6** | 1925 | 0.121 | 0.000062 | 0 |
| parallel | 84.5 | 1903 | 0.121 | 0.000062 | 0 |

Raw output: `results/pipeline_strategies.json`.

## What this shows

- **Speed must not change results, and it does not.** All three strategies block, redact and
  forward exactly the same text on every case.
- **Tiered is about 30% faster at p50 than one-after-another.** The four CPU models (injection,
  jailbreak, banned topics, toxicity) overlap instead of queueing.
- **All-parallel buys nothing over tiered here,** and on an earlier run on the same laptop it was
  the slowest of the three (123 ms p50 vs 76 ms tiered): with every guard started at once, the
  ONNX models compete for the same cores. It also starts the judge before the cheap guards have
  had a chance to block.
- **p99 is the judge.** Any request that sends context waits ~1.7 s for it, whatever the
  strategy. That is the cost of the faithfulness check, and the reason it runs last and only
  when context is supplied.

## The judge cost that tiering saves

On the dev set no reply that carries context is also blocked by a cheaper output guard, so all
three strategies make the same number of judge calls (0.121 per request). The saving appears
when replies carry context *and* get blocked early, e.g. a leaked key with the FAQ attached:
tiered blocks it in the `cheap` tier (secrets) and never calls the judge, while all-parallel
starts the judge alongside, and one-after-another reaches `faithfulness.yaml` before
`secrets.yaml` in file order. `tests/test_pipeline_tiers.py` checks that a block in an earlier
tier skips the `llm` tier. `scripts/bench_pipeline.py --faq-context` measures it on the dev set
(sends the FAQ with every reply); it was not run for this note.

## What we would do with more time or budget

- Sample the judge (e.g. 1 in N replies) or run it only on replies with numbers, dates or plan
  names, since those are where unfaithful answers did damage in our cases.
- Run the CPU models in a process pool or on a GPU so the `model` tier truly runs in parallel.
- A paid Gemini tier: the free tier's per-project daily quota is the real limit on how often the
  judge can run, not latency.
