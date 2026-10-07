## Dwarpal eval

`jailbreak@1.1.0` `max_length@1.0.0` `pii@1.0.0` `prompt_injection@1.1.0` `secrets@1.0.0` · 2026-10-07T19:40:14+00:00 · 492 cached decision(s) reused

### Dev set

80 case(s) scored. This suite gates the merge.

| Policy | Version | Attacks | Catch rate | 95% CI | Safe | FPR | p50 ms |
|---|---|---:|---:|:---:|---:|---:|---:|
| jailbreak | 1.1.0 | 10 | 1.000 | 0.72–1.00 | 39 | 0.000 | 28.18 |
| max_length | 1.0.0 | 0 | — | — | 39 | 0.000 | 0.01 |
| pii | 1.0.0 | 10 | 1.000 | 0.72–1.00 | 44 | 0.000 | 0.06 |
| prompt_injection | 1.1.0 | 10 | 1.000 | 0.72–1.00 | 39 | 0.000 | 25.64 |
| secrets | 1.0.0 | 6 | 1.000 | 0.61–1.00 | 44 | 0.000 | 0.06 |

Full pipeline: catch 1.000 on 36 attacks, FPR 0.000 on 44 safe cases, added latency p50 49.99 ms / p95 108.14 ms.

### Holdout set

23 case(s) scored. Written before any guard existed; reported only, never gated.

8 case(s) skipped, guard not registered yet: banned_topics 2, faithfulness 2, output_schema 2, toxicity 2.

| Policy | Version | Attacks | Catch rate | 95% CI | Safe | FPR | p50 ms |
|---|---|---:|---:|:---:|---:|---:|---:|
| jailbreak | 1.1.0 | 2 | 1.000 | 0.34–1.00 | 11 | 0.000 | 30.71 |
| max_length | 1.0.0 | 0 | — | — | 11 | 0.000 | 0.01 |
| pii | 1.0.0 | 2 | 1.000 | 0.34–1.00 | 15 | 0.000 | 0.05 |
| prompt_injection | 1.1.0 | 2 | 1.000 | 0.34–1.00 | 11 | 0.000 | 31.35 |
| secrets | 1.0.0 | 2 | 1.000 | 0.34–1.00 | 15 | 0.000 | 0.05 |

Full pipeline: catch 1.000 on 8 attacks, FPR 0.000 on 15 safe cases, added latency p50 68.59 ms / p95 149.52 ms.

### Robustness

Dev attacks re-run in disguise. Reported only, never gated.

| Variant | Attacks | Catch rate |
|---|---:|---:|
| base64 | 28 | 0.714 |
| leetspeak | 28 | 0.750 |
| spaced | 28 | 0.714 |

### Gate

✅ No policy regressed against `eval/baseline.json` and all are within `eval/thresholds.yaml`.
