"""Shared machinery for the two input-attack guards (prompt_injection, jailbreak).

Both guards are the same two layers over different pattern lists:

  Layer 1  weighted regexes from the policy YAML, <1 ms, explainable (the reason names
           the patterns that matched).
  Layer 2  a pretrained classifier (see classifier.py), run only when the heuristics
           alone have not already decided, since max(h, c) >= threshold is already true
           when h >= threshold.

Final score = max(heuristics, classifier), compared with the policy threshold by decide().

Every user turn is checked, and so is every supplied context document — indirect injection
arrives in pasted tickets and notes, not only in the last message.

Policy params:
  patterns:   list of {name, regex, weight} — see heuristics.py
  classifier: {enabled: bool, model: str} — layer 2; enabled defaults to false so that
              policy files, not code, decide when the model download happens
"""

from __future__ import annotations

import asyncio

from dwarpal.guards.base import Guard, GuardContext, GuardResult, Stage, message_text
from dwarpal.guards.heuristics import CompiledPattern, compile_patterns, score_texts
from dwarpal.guards.normalize import normalize


class InputAttackGuard(Guard):
    stages = frozenset({Stage.INPUT})
    cacheable = True  # PR-06: pure function of the text, safe to reuse per version

    def __init__(self, policy):
        super().__init__(policy)
        self.patterns: list[CompiledPattern] = compile_patterns(policy.params.get("patterns"))
        self._classifier = None

    def _classifier_config(self) -> dict:
        return self.policy.params.get("classifier") or {}

    async def setup(self) -> None:
        config = self._classifier_config()
        if config.get("enabled"):
            from dwarpal.guards.classifier import DEFAULT_MODEL, get_classifier

            model_id = config.get("model", DEFAULT_MODEL)
            # First call downloads and loads the model; off the event loop.
            revision = config.get("revision")  # PR-04: pin the model like any other dependency
            self._classifier = await asyncio.to_thread(get_classifier, model_id, revision)

    def _texts(self, ctx: GuardContext) -> list[str]:
        texts = [message_text(m) for m in ctx.user_messages()]
        texts.extend(ctx.context_docs)
        # PR-04: also score a de-disguised copy (spaced letters, invisible chars), only when it
        # differs, so ordinary messages cost nothing extra.
        texts.extend(n for t in list(texts) if (n := normalize(t)) != t)
        return [t for t in texts if t.strip()]

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        texts = self._texts(ctx)
        if not texts:
            return self.allow(reason="no user text")

        score, matched = score_texts(texts, self.patterns)
        parts = [f"patterns: {', '.join(matched)}"] if matched else []

        if self._classifier is not None and score < self.policy.threshold:
            classifier = self._classifier
            model_score = await asyncio.to_thread(lambda: max(classifier.score(t) for t in texts))
            parts.append(f"classifier: {model_score:.3f}")
            score = max(score, model_score)

        return self.decide(score, reason="; ".join(parts))
