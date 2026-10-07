## Dwarpal eval

`max_length@1.0.0` · 2026-10-07T12:38:04+00:00 · 0 cached decision(s) reused

### Dev set

18 case(s) scored. This suite gates the merge.

| Policy | Version | Attacks | Catch rate | 95% CI | Safe | FPR | p50 ms |
|---|---|---:|---:|:---:|---:|---:|---:|
| max_length | 1.0.0 | 0 | — | — | 18 | 0.000 | 0.00 |

Full pipeline: catch — on 0 attacks, FPR 0.000 on 18 safe cases, added latency p50 0.01 ms / p95 0.01 ms.

### Holdout set

15 case(s) scored. Written before any guard existed; reported only, never gated.

16 case(s) skipped, guard not registered yet: banned_topics 2, faithfulness 2, jailbreak 2, output_schema 2, pii 2, prompt_injection 2, secrets 2, toxicity 2.

| Policy | Version | Attacks | Catch rate | 95% CI | Safe | FPR | p50 ms |
|---|---|---:|---:|:---:|---:|---:|---:|
| max_length | 1.0.0 | 0 | — | — | 11 | 0.000 | 0.00 |

Full pipeline: catch — on 0 attacks, FPR 0.000 on 15 safe cases, added latency p50 0.01 ms / p95 0.01 ms.

### Robustness

Dev attacks re-run in disguise. Reported only, never gated.

| Variant | Attacks | Catch rate |
|---|---:|---:|
| base64 | 0 | — |
| leetspeak | 0 | — |
| spaced | 0 | — |

### Gate

✅ No policy regressed against `eval/baseline.json` and all are within `eval/thresholds.yaml`.
