## Dwarpal eval

`banned_topics@1.0.0` `jailbreak@1.1.1` `max_length@1.0.0` `pii@1.0.1` `prompt_injection@1.1.1` `secrets@1.0.1` `toxicity@1.0.0` · 2026-10-08T06:34:01+00:00 · 656 cached decision(s) reused

### Dev set

99 case(s) scored. This suite gates the merge.

| Policy | Version | Attacks | Catch rate | 95% CI | Safe | FPR | p50 ms |
|---|---|---:|---:|:---:|---:|---:|---:|
| banned_topics | 1.0.0 | 6 | 1.000 | 0.61–1.00 | 45 | 0.000 | 1.72 |
| jailbreak | 1.1.1 | 10 | 1.000 | 0.72–1.00 | 45 | 0.000 | 9.95 |
| max_length | 1.0.0 | 0 | — | — | 45 | 0.000 | 0.00 |
| pii | 1.0.1 | 10 | 1.000 | 0.72–1.00 | 52 | 0.000 | 0.08 |
| prompt_injection | 1.1.1 | 10 | 1.000 | 0.72–1.00 | 45 | 0.000 | 9.28 |
| secrets | 1.0.1 | 6 | 1.000 | 0.61–1.00 | 52 | 0.000 | 0.01 |
| toxicity | 1.0.0 | 5 | 1.000 | 0.57–1.00 | 7 | 0.000 | 9.39 |

Full pipeline: catch 1.000 on 47 attacks, FPR 0.000 on 52 safe cases, added latency p50 29.07 ms / p95 54.45 ms.

### Holdout set

27 case(s) scored. Written before any guard existed; reported only, never gated.

4 case(s) skipped, guard not registered yet: faithfulness 2, output_schema 2.

| Policy | Version | Attacks | Catch rate | 95% CI | Safe | FPR | p50 ms |
|---|---|---:|---:|:---:|---:|---:|---:|
| banned_topics | 1.0.0 | 2 | 1.000 | 0.34–1.00 | 11 | 0.000 | 1.74 |
| jailbreak | 1.1.1 | 2 | 1.000 | 0.34–1.00 | 11 | 0.000 | 9.62 |
| max_length | 1.0.0 | 0 | — | — | 11 | 0.000 | 0.00 |
| pii | 1.0.1 | 2 | 1.000 | 0.34–1.00 | 15 | 0.000 | 0.08 |
| prompt_injection | 1.1.1 | 2 | 1.000 | 0.34–1.00 | 11 | 0.000 | 9.64 |
| secrets | 1.0.1 | 2 | 1.000 | 0.34–1.00 | 15 | 0.000 | 0.01 |
| toxicity | 1.0.0 | 2 | 1.000 | 0.34–1.00 | 4 | 0.000 | 9.24 |

Full pipeline: catch 1.000 on 12 attacks, FPR 0.000 on 15 safe cases, added latency p50 29.81 ms / p95 50.95 ms.

### Robustness

Dev attacks re-run in disguise. Reported only, never gated.

| Variant | Attacks | Catch rate |
|---|---:|---:|
| base64 | 34 | 0.588 |
| leetspeak | 34 | 0.588 |
| spaced | 34 | 0.588 |

### Gate

✅ No policy regressed against `eval/baseline.json` and all are within `eval/thresholds.yaml`.
