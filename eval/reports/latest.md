## Dwarpal eval

`banned_topics@1.0.0` `faithfulness@1.0.0` `jailbreak@1.1.1` `max_length@1.0.0` `output_schema@1.0.0` `pii@1.0.1` `prompt_injection@1.1.1` `secrets@1.0.1` `toxicity@1.1.0` · 2026-10-08T13:25:31+00:00 · 688 cached decision(s) reused

### Dev set

116 case(s) scored. This suite gates the merge.

| Policy | Version | Attacks | Catch rate | 95% CI | Safe | FPR | p50 ms |
|---|---|---:|---:|:---:|---:|---:|---:|
| banned_topics | 1.0.0 | 6 | 1.000 | 0.61–1.00 | 45 | 0.000 | 4.79 |
| faithfulness | 1.0.0 | 5 | 1.000 | 0.57–1.00 | 15 | 0.000 | 0.45 |
| jailbreak | 1.1.1 | 10 | 1.000 | 0.72–1.00 | 45 | 0.000 | 27.30 |
| max_length | 1.0.0 | 0 | — | — | 45 | 0.000 | 0.02 |
| output_schema | 1.0.0 | 4 | 1.000 | 0.51–1.00 | 15 | 0.000 | 0.05 |
| pii | 1.0.1 | 10 | 1.000 | 0.72–1.00 | 60 | 0.000 | 0.31 |
| prompt_injection | 1.1.1 | 10 | 1.000 | 0.72–1.00 | 45 | 0.000 | 27.15 |
| secrets | 1.0.1 | 6 | 1.000 | 0.61–1.00 | 60 | 0.000 | 0.09 |
| toxicity | 1.1.0 | 5 | 1.000 | 0.57–1.00 | 15 | 0.000 | 23.48 |

Full pipeline: catch 1.000 on 56 attacks, FPR 0.000 on 60 safe cases, added latency p50 58.58 ms / p95 448.94 ms.

### Holdout set

31 case(s) scored. Written before any guard existed; reported only, never gated.

| Policy | Version | Attacks | Catch rate | 95% CI | Safe | FPR | p50 ms |
|---|---|---:|---:|:---:|---:|---:|---:|
| banned_topics | 1.0.0 | 2 | 1.000 | 0.34–1.00 | 11 | 0.000 | 6.43 |
| faithfulness | 1.0.0 | 2 | 1.000 | 0.34–1.00 | 4 | 0.000 | 0.01 |
| jailbreak | 1.1.1 | 2 | 1.000 | 0.34–1.00 | 11 | 0.000 | 29.18 |
| max_length | 1.0.0 | 0 | — | — | 11 | 0.000 | 0.02 |
| output_schema | 1.0.0 | 2 | 0.000 | 0.00–0.66 | 4 | 0.000 | 0.01 |
| pii | 1.0.1 | 2 | 1.000 | 0.34–1.00 | 15 | 0.000 | 0.40 |
| prompt_injection | 1.1.1 | 2 | 1.000 | 0.34–1.00 | 11 | 0.000 | 27.56 |
| secrets | 1.0.1 | 2 | 1.000 | 0.34–1.00 | 15 | 0.000 | 0.07 |
| toxicity | 1.1.0 | 2 | 1.000 | 0.34–1.00 | 4 | 0.000 | 24.25 |

Full pipeline: catch 0.875 on 16 attacks, FPR 0.000 on 15 safe cases, added latency p50 67.94 ms / p95 353.73 ms.

- output_schema missed `hold-013`, `hold-014`

### Robustness

Dev attacks re-run in disguise. Reported only, never gated.

| Variant | Attacks | Catch rate |
|---|---:|---:|
| base64 | 34 | 0.588 |
| leetspeak | 34 | 0.588 |
| spaced | 34 | 0.765 |

### Gate

✅ No policy regressed against `eval/baseline.json` and all are within `eval/thresholds.yaml`.
