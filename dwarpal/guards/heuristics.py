"""Layer 1 for the input-attack guards: weighted regex patterns from policy params.

Every pattern lives in the policy YAML (`params.patterns`), so adding a phrase is a policy
change — reviewed, versioned, and re-scored by the eval gate — not a code change. Each pattern
carries a weight in (0, 1]: how strongly a match alone says "attack". Matches are combined
noisy-OR style (score = 1 - prod(1 - w)), so several weak signals add up without any single
clumsy phrase being able to block on its own unless its weight says so.

This module registers no guard; prompt_injection and jailbreak both build on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CompiledPattern:
    name: str
    regex: re.Pattern[str]
    weight: float


class PatternError(ValueError):
    """A params.patterns entry is malformed. Surfaces at startup, not per request."""


def compile_patterns(raw: list[dict] | None) -> list[CompiledPattern]:
    patterns: list[CompiledPattern] = []
    for i, entry in enumerate(raw or []):
        if not isinstance(entry, dict):
            raise PatternError(f"params.patterns[{i}] must be a mapping")
        missing = {"name", "regex", "weight"} - entry.keys()
        if missing:
            raise PatternError(f"params.patterns[{i}] is missing {sorted(missing)}")
        weight = float(entry["weight"])
        if not 0.0 < weight <= 1.0:
            raise PatternError(f"params.patterns[{i}] ({entry['name']}): weight must be in (0, 1]")
        try:
            regex = re.compile(entry["regex"], re.IGNORECASE | re.DOTALL)
        except re.error as exc:
            raise PatternError(f"params.patterns[{i}] ({entry['name']}): bad regex: {exc}") from exc
        patterns.append(CompiledPattern(str(entry["name"]), regex, weight))
    return patterns


def score_texts(texts: list[str], patterns: list[CompiledPattern]) -> tuple[float, list[str]]:
    """Noisy-OR of every pattern that matches any text. Returns (score, matched names).

    A pattern counts once however many texts or times it matches: repeating a phrase
    should not launder two weak matches into a strong score.
    """
    matched: list[str] = []
    stay_clean = 1.0  # probability-style complement: prod(1 - weight) over matches
    for pattern in patterns:
        if any(pattern.regex.search(t) for t in texts):
            matched.append(pattern.name)
            stay_clean *= 1.0 - pattern.weight
    return 1.0 - stay_clean, matched
