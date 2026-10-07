# 0005: Swap the input-attack classifier, and score a de-disguised copy

**Status:** accepted · **Owner:** Yash (with Harshit Goel's guards from PR-03) · **PR:** 04

## Decision

`prompt_injection@1.1.0` and `jailbreak@1.1.0` use
[`Horizon-Labs/prompt-injection-guard-base`](https://huggingface.co/Horizon-Labs/prompt-injection-guard-base)
(Apache-2.0, not gated, ONNX, pinned revision) instead of
`protectai/deberta-v3-base-prompt-injection-v2`. Heuristics, threshold (0.80) and the rest of
the guard are unchanged.

Both guards also score a normalised copy of each text (NFKC, invisible characters removed,
"s p a c e d" letters joined), only when it differs from the original.

## Why

While testing PR-04 the old classifier blocked plain questions about the user's own account:
"please confirm my email address" scored 1.00, "which email do you have on file for me?" 0.97.
That is the "over-defense" problem the InjecGuard paper names: the model reacts to trigger
words. No threshold could fix it, the scores sat at 0.96-1.00. We added 8 such questions to
the dev set (`safe-051` ... `safe-058`) so the gate measures it from now on.

## Evidence

Classifier layer alone, threshold 0.80:

| Data | Old (protectai v2) | New (Horizon-Labs) |
|---|---|---|
| Our attacks, dev + holdout (24) | 21 caught | 23 caught |
| Our safe inputs, dev + holdout (49) | 6 blocked | 0 blocked |
| 20 benign account questions | 5 blocked | 0 blocked |
| [deepset/prompt-injections](https://huggingface.co/datasets/deepset/prompt-injections) test: attacks (60) | 21 caught | 31 caught |
| deepset test: benign (56) | 0 blocked | 0 blocked |
| [NotInject](https://huggingface.co/datasets/leolee99/NotInject) benign with trigger words (339) | 59.9% allowed | 96.5% allowed |

The two public sets were not used to choose anything, so they are the check that this is not
fitted to our own cases. Both models miss many deepset attacks, a lot of which are German or
very indirect.

Full guards after the swap (`scripts/sweep_thresholds.py`): catch 1.00 and FPR 0.00 at every
threshold from 0.60 to 0.95, lowest attack 0.988, highest safe case 0.584. Dev FPR went from
0.067 to 0.000 for both guards; end to end, from 0.059 to 0.000 (dev) and 0.200 to 0.000
(holdout).

Spaced-out attacks (robustness row): jailbreak 3/10 caught before normalisation, 10/10 after;
prompt_injection 8/10 to 10/10.

## Alternatives considered

| Option | Why not |
|---|---|
| Raise the threshold | The false positives scored 0.96-1.00, above most real attacks. |
| Allow-list "confirm my email"-style phrasings | Brittle, and a known bypass route. |
| Classifier only on untrusted context, heuristics + classifier agreement on user turns (Prompt Guard / Azure advice) | Sound, but it lowers recall on paraphrased user attacks (`jb-007` was caught only by the classifier). Not needed once the classifier stopped over-firing; worth revisiting if a new model over-fires again. |
| patronus-studio/wolf-defender, testsavantai defender-base | Measured: fewer catches (20/24) or false positives left (3/49). |
| Meta Prompt Guard 2, protectai small v2 | Gated on the hub. |

## Also changed

- `classifier.py` takes a pinned `revision`, finds the tokenizer and config at the repo root
  when they are not under `onnx/`, and works from an offline cache.
- ONNX thread spinning is off, as in `ner.py` (see 0004): with two models in one process the
  default made each inference several times slower.
