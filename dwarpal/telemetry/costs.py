"""Cost of the upstream call, from its `usage` field and config/pricing.yaml. (PR-07)

Guard LLM calls (faithfulness judge, schema repair) are not priced here: they put their own
cost on GuardResult.cost_usd, and a request's cost is upstream + the sum of those.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("dwarpal.telemetry")


def billed_tokens(usage: dict[str, Any] | None) -> tuple[int, int]:
    """(input, output) tokens from an OpenAI-style usage object.

    Gemini bills thinking tokens as output. Depending on the endpoint they are counted in
    completion_tokens or only in total_tokens, so output is whichever of the two is larger.
    """
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or 0)
    return prompt, max(completion, total - prompt)


class Pricing:
    """USD per 1M input / output tokens, per model."""

    def __init__(self, models: dict[str, dict[str, float]]):
        self.models = models
        self._unknown: set[str] = set()

    @classmethod
    def load(cls, path: Path) -> Pricing:
        if not path.is_file():
            log.warning("pricing file %s not found; upstream calls are costed at 0", path)
            return cls({})
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(data.get("models") or {})

    def cost(self, model: str | None, input_tokens: int, output_tokens: int) -> float:
        if not model or not (input_tokens or output_tokens):
            return 0.0
        name = model.removeprefix("models/")  # Gemini sometimes answers with the full resource name
        prices = self.models.get(name)
        if prices is None:
            if name not in self._unknown:
                self._unknown.add(name)
                log.warning("no price for model %r in the pricing file; costed at 0", name)
            return 0.0
        return (
            input_tokens * float(prices.get("input", 0.0))
            + output_tokens * float(prices.get("output", 0.0))
        ) / 1_000_000
