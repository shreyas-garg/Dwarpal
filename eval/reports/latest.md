## Dwarpal eval

`jailbreak@1.1.1` `max_length@1.0.0` `pii@1.0.1` `prompt_injection@1.1.1` `secrets@1.0.1` · 2026-10-07T20:34:42+00:00 · 50 cached decision(s) reused

### Dev set

80 case(s) scored. This suite gates the merge.

| Policy | Version | Attacks | Catch rate | 95% CI | Safe | FPR | p50 ms |
|---|---|---:|---:|:---:|---:|---:|---:|
| jailbreak | 1.1.1 | 10 | 1.000 | 0.72–1.00 | 39 | 0.000 | 24.18 |
| max_length | 1.0.0 | 0 | — | — | 39 | 0.000 | 0.01 |
| pii | 1.0.1 | 10 | 1.000 | 0.72–1.00 | 44 | 0.000 | 0.14 |
| prompt_injection | 1.1.1 | 10 | 1.000 | 0.72–1.00 | 39 | 0.000 | 24.02 |
| secrets | 1.0.1 | 6 | 1.000 | 0.61–1.00 | 44 | 0.000 | 0.03 |

Full pipeline: catch 1.000 on 36 attacks, FPR 0.000 on 44 safe cases, added latency p50 49.02 ms / p95 97.79 ms.

### Holdout set

23 case(s) scored. Written before any guard existed; reported only, never gated.

8 case(s) skipped, guard not registered yet: banned_topics 2, faithfulness 2, output_schema 2, toxicity 2.

| Policy | Version | Attacks | Catch rate | 95% CI | Safe | FPR | p50 ms |
|---|---|---:|---:|:---:|---:|---:|---:|
| jailbreak | 1.1.1 | 2 | 1.000 | 0.34–1.00 | 11 | 0.000 | 24.14 |
| max_length | 1.0.0 | 0 | — | — | 11 | 0.000 | 0.01 |
| pii | 1.0.1 | 2 | 1.000 | 0.34–1.00 | 15 | 0.000 | 0.10 |
| prompt_injection | 1.1.1 | 2 | 1.000 | 0.34–1.00 | 11 | 0.000 | 23.95 |
| secrets | 1.0.1 | 2 | 1.000 | 0.34–1.00 | 15 | 0.000 | 0.03 |

Full pipeline: catch 1.000 on 8 attacks, FPR 0.000 on 15 safe cases, added latency p50 47.39 ms / p95 113.32 ms.

### Robustness

Dev attacks re-run in disguise. Reported only, never gated.

| Variant | Attacks | Catch rate |
|---|---:|---:|
| base64 | 28 | 0.714 |
| leetspeak | 28 | 0.714 |
| spaced | 28 | 0.714 |

### Gate

✅ No policy regressed against `eval/baseline.json` and all are within `eval/thresholds.yaml`.
