"""Blocks hostile, abusive or demeaning model replies. (PR-05)

Two layers; the reply is blocked when either crosses its threshold:

  Layer 1  toxic-bert (see toxicity_model.py), one probability per label — toxic,
           severe_toxic, obscene, threat, insult, identity_hate — each with its own
           threshold in the YAML. A label without one uses the policy threshold.
  Layer 2  optional: cosine similarity to a few contemptuous support replies, using the same
           MiniLM embedder as banned_topics. toxic-bert was trained on forum comments and
           misses condescension without swear words ("maybe running a business isn't for
           someone as slow as you" scores 0.11 toxic); this layer is for that gap.

Only the reply is checked. A customer swearing at the bot is frustrated, not a reason to
refuse service.

Policy params:
  model:     {model, revision, weights} — defaults to Xenova/toxic-bert, int8 weights
  labels:    {label: threshold}
  contempt:  {enabled, threshold, examples: [str], embedder: {model, revision}}
"""

from __future__ import annotations

import asyncio
import re

from dwarpal.guards.base import Guard, GuardContext, GuardResult, Stage
from dwarpal.guards.registry import register

_SENTENCES = re.compile(r"(?<=[.?!])\s+|\n+")


@register("toxicity")
class ToxicityGuard(Guard):
    stages = frozenset({Stage.OUTPUT})
    cacheable = True  # PR-06: pure function of the text, safe to reuse per version

    def __init__(self, policy):
        super().__init__(policy)
        self.label_thresholds = {
            str(k): float(v) for k, v in (policy.params.get("labels") or {}).items()
        }
        self.contempt = policy.params.get("contempt") or {}
        if self.contempt.get("enabled") and not self.contempt.get("examples"):
            raise ValueError("toxicity: contempt.enabled needs a list of examples")
        self._model = None
        self._embedder = None
        self._contempt_examples = None

    async def setup(self) -> None:
        from dwarpal.guards.toxicity_model import (
            DEFAULT_MODEL,
            DEFAULT_WEIGHTS,
            get_toxicity_model,
        )

        config = self.policy.params.get("model") or {}
        self._model = await asyncio.to_thread(
            get_toxicity_model,
            config.get("model", DEFAULT_MODEL),
            config.get("revision"),
            config.get("weights", DEFAULT_WEIGHTS),
        )
        unknown = set(self.label_thresholds) - set(self._model.labels)
        if unknown:
            raise ValueError(
                f"toxicity: unknown labels {sorted(unknown)}, model has {self._model.labels}"
            )

        if self.contempt.get("enabled"):
            from dwarpal.guards.embedder import DEFAULT_MODEL as EMBEDDER
            from dwarpal.guards.embedder import get_embedder

            emb = self.contempt.get("embedder") or {}
            self._embedder = await asyncio.to_thread(
                get_embedder, emb.get("model", EMBEDDER), emb.get("revision")
            )
            self._contempt_examples = await asyncio.to_thread(
                self._embedder.embed, list(self.contempt["examples"])
            )

    def threshold_for(self, label: str) -> float:
        return self.label_thresholds.get(label, self.policy.threshold)

    def _contempt_score(self, text: str) -> float:
        sentences = [s.strip() for s in _SENTENCES.split(text) if s.strip()]
        units = [text] + (sentences if len(sentences) > 1 else [])
        return float((self._embedder.embed(units) @ self._contempt_examples.T).max())

    def _score(self, text: str) -> tuple[dict[str, float], float | None]:
        labels = self._model.scores(text)
        contempt = self._contempt_score(text) if self._embedder is not None else None
        return labels, contempt

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        text = ctx.response_text or ""
        if not text.strip():
            return self.allow(reason="empty reply")
        if self._model is None:
            return self.allow(reason="model not loaded")

        labels, contempt = await asyncio.to_thread(self._score, text)
        hits = [f"{label} {p:.2f}" for label, p in labels.items() if p >= self.threshold_for(label)]
        top_label = max(labels, key=labels.get)
        score = labels[top_label]
        if contempt is not None and contempt >= float(self.contempt.get("threshold", 0.55)):
            hits.append(f"contempt {contempt:.2f}")
            score = max(score, contempt)

        if hits:
            return self.result(self.policy.action, score, reason=", ".join(hits))
        reason = f"top: {top_label} {labels[top_label]:.2f}"
        if contempt is not None:
            reason += f", contempt {contempt:.2f}"
        return self.allow(score, reason)
