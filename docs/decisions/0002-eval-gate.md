# 0002: Gate every merge on a per-guard red-team score

**Status:** accepted · **Owner:** Harshit Sachan · **PR:** 02

## Decision

Every enabled policy is scored on its own against a hand-written red-team set. `make eval`
reports catch rate (with a Wilson 95% interval), false-positive rate and p50 latency per policy,
then the full pipeline end to end. CI fails the build when any policy, on the dev suite, drops
below its recorded number in `eval/baseline.json` or crosses a floor in `eval/thresholds.yaml`.
A second suite, `eval/datasets/holdout.jsonl`, was written in this PR before any guard existed
and is reported but never gated.

Scoring details and the full set of rules are in [eval/README.md](../../eval/README.md).

## Alternatives considered

| Option | Why not |
|---|---|
| One end-to-end score for the whole pipeline | A regression tells you the system got worse but not which guard did it, and a strict guard can mask a broken one. We report end-to-end *as well*, but gate per policy. |
| A percentage tolerance (e.g. "no more than 5% worse") | With ~10 attacks per guard a single miss moves the rate by 0.10. Any tolerance wide enough to absorb noise also hides a real regression, so the rule is no regression at all. |
| Gate on the holdout set too | Then people would tune to it, and we would lose the only estimate we have of overfitting to the dev set. |
| Public jailbreak datasets | Pretrained classifiers have already trained on them, so they flatter the score, and they are not in the Ledgerly domain the checklist asks for. |
| Counting `FLAG` as a catch | `FLAG` only writes a log line. A guard could claim a catch while letting the attack through. Only `BLOCK` and `REDACT` count. |
| Re-running the LLM judge on every push | A judge call per case per push costs real money on a ~$20 budget. Decisions are cached under `sha256(guard, policy version, case)` and the cache is carried between CI runs. |

## Consequences

- A miss is attributable to one guard, and the report names the exact case ids that were missed
  or falsely blocked, so a red build is actionable without a local repro.
- Guard owners cannot raise their catch rate by quietly loosening a threshold: the FPR side of
  the gate moves in the opposite direction.
- The only way past the gate is to edit `baseline.json` and explain why in the PR description.
  That is deliberate — it keeps the escape hatch visible in review rather than in a config flag.
- The dev-to-holdout gap is a standing, published measure of how much the team has tuned to the
  dev set. It is expected to grow; a large gap is the signal to write more dev cases.
- Because the policy version is part of the cache key, changing a guard without bumping its
  policy version would silently reuse stale decisions. PR-03 adds the CI check that a changed
  policy file must carry a new version.
- Costs: a second required CI job, and a dataset that only pays off if people keep writing
  honest cases for it. Robustness variants (base64, leetspeak, spacing) are reported but not
  gated, because failing them today would block every PR before the guards that handle them
  exist.
