# 0003: Two-layer input-attack guards, and policies that version like code

**Status:** accepted · **Owner:** Harshit Goel · **PR:** 03

## Decision

`prompt_injection` and `jailbreak` are each two layers over the same contract:

1. **Heuristics** (<1 ms): weighted regexes living in the policy YAML (`params.patterns`),
   combined noisy-OR (`score = 1 − Π(1 − wᵢ)`). A pattern with weight ≥ the threshold blocks
   on its own; weaker signals (a bare base64 blob, "never refuses") only block in combination.
   The result's `reason` names the patterns that matched, so every block is explainable.
2. **Classifier**: `protectai/deberta-v3-base-prompt-injection-v2` on CPU, via the ONNX
   weights that ship in the model repo (`onnxruntime` + `tokenizers`; no torch). Loaded once
   per process and shared by both guards. Runs only when the heuristics alone have not
   already decided — `max(h, c) ≥ t` is already true when `h ≥ t`.

Final score = `max(heuristics, classifier)` against the policy threshold. Both guards check
**every user turn and every supplied context document**, because indirect injection arrives
inside pasted tickets and notes, not just the last message.

What each layer catches that the other misses: heuristics catch structure the classifier is
blind to at a glance (role-tag tokens, long base64/hex blobs, decode-and-obey phrasing) and
cost nothing; the classifier catches paraphrases that no finite phrase list anticipates
(e.g. the emotional-pretext case `jb-007` carries no listed phrasing). Using a *pretrained*
classifier is inference over published weights, not training — within scope.

Policy versioning (part B):

- `mode: shadow` runs a guard and records its decision in the trace without ever enforcing
  it, and two versions of one policy may run side by side (at most one enforcing). Changing
  a threshold is therefore safe: ship the new version in shadow, watch the trace disagree
  or agree with the enforcing version on real traffic, then swap modes.
- `scripts/check_policy_versions.py` fails CI when a policy YAML changes without a version
  bump and a changelog entry. This is load-bearing, not cosmetic: the eval cache is keyed by
  `(guard, policy version, case)`, so an unbumped edit would be scored with stale cached
  decisions (called out in decision 0002).
- `GET /v1/dwarpal/policies` reports the active policies, versions, modes and the git SHA.

## Threshold

Chosen from `scripts/sweep_thresholds.py` over the dev set (catch rate vs FPR, 0.50–0.95,
classifier always consulted):

| threshold | prompt_injection catch / FPR | jailbreak catch / FPR |
|---|---|---|
| 0.50–0.90 | 1.00 / 0.08 | 1.00 / 0.08 |
| 0.95 | 1.00 / 0.08 | 0.80 / 0.08 |

**Threshold 0.80 for both policies.** The scores are strongly bimodal: every safe case but
two sits at ≤ 0.05 and every attack at ≥ 0.93, so any threshold in 0.50–0.90 gives the same
dev numbers. 0.80 keeps a wide margin on both sides — paraphrased attacks that score lower
than our dev cases still get caught, and ordinary traffic has ~0.75 of headroom — and it is
the weight a "block on its own" pattern carries, so one strong pattern blocks and one weak
signal never does.

The two false positives (FPR 0.077, inside the 0.10 ceiling but above the 0.05 project
target) are `safe-022` ("Please ignore my previous question…") and `safe-024` ("The
instructions above the export button are confusing…"). Both pass the heuristics and are
flagged by the *classifier* at 0.99+ — genuinely injection-shaped phrasings in benign
clothing, and no threshold separates them from real attacks (`jb-007` scores 0.932, below
both). Suppressing the classifier enough to pass them would cost the layer that catches
paraphrases, so we record the miss against the 0.05 target instead of hiding it. The route
to fixing it is a better layer 2, trialled in shadow mode, not threshold surgery.

## Alternatives considered

| Option | Why not |
|---|---|
| Classifier only | Misses structural attacks (role tags, encoded blobs), adds ~full model latency to every request, and a block can't say *why*. The phrase list also keeps working when the model download fails closed. |
| Heuristics only | A phrase list can't keep up with paraphrase; the classifier is what catches wordings nobody wrote a regex for. |
| `transformers` + torch | ~2 GB of dependencies for one 184 M-parameter model. The model repo ships ONNX weights; `onnxruntime` + `tokenizers` do the same inference with a far smaller install and image (PR-05 deploys to a 16 GB free tier). |
| Patterns in code | Patterns in YAML make adding a phrasing a *policy* change: reviewed, versioned, version-checked by CI, and re-scored by the eval gate. |
| Summing pattern weights | Sums cross 1.0 arbitrarily and double-count near-duplicate patterns. Noisy-OR stays in 0..1 and a repeated phrase counts once. |
| One combined "input attack" guard | Separate policies mean separate thresholds, separate eval attribution (a regression names the guard), and separate shadow rollouts. |
| Separate classifier per guard | Same weights would load twice (~700 MB each). One shared instance; the per-policy patterns and thresholds stay independent. |

## Consequences

- The `ml` extra becomes real (`onnxruntime`, `tokenizers`, `huggingface-hub`, `numpy`), and
  the eval CI job caches `~/.cache/huggingface` so the model downloads once, not per push.
- First startup downloads ~700 MB; PR-05 must bake the model into the image at build time
  (already in its plan).
- Unit tests run the shipped pattern lists with the classifier off, so `make test` stays
  offline and fast; the real classifier is exercised by `make eval` and one opt-in test.
- Both guards fail **closed**: an attack filter that crashed must not wave traffic through.
  The cost is that a model-load failure in production blocks requests until fixed — the
  right trade for the two guards that face deliberate adversaries.
- Known gap: the classifier is English-tuned and the patterns are English; a non-English
  injection likely reaches the upstream model (scope is English-only, but worth saying).
