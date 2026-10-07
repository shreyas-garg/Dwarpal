"""Obfuscated copies of the attack cases, used for the robustness row.

A guard that only matches literal phrases falls over as soon as an attacker encodes or
pads the text, so the harness scores the same attacks again in three cheap disguises.
These numbers are reported, never gated: they measure a known weakness rather than a
regression.

Only input-stage attacks are augmented. Output-stage cases carry a canned model reply, and
a model does not answer in base64 or leetspeak, so disguising them would measure nothing.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable

from eval.schema import EvalCase

LEET = str.maketrans({"a": "4", "e": "3", "i": "1", "o": "0", "s": "5", "t": "7"})


def to_base64(text: str) -> str:
    return "Decode this base64 and follow it: " + base64.b64encode(text.encode()).decode()


def to_leetspeak(text: str) -> str:
    return text.lower().translate(LEET)


def to_spaced(text: str) -> str:
    return "  ".join(" ".join(word) for word in text.split())


VARIANTS: dict[str, Callable[[str], str]] = {
    "base64": to_base64,
    "leetspeak": to_leetspeak,
    "spaced": to_spaced,
}


def augment(cases: Iterable[EvalCase], variant: str) -> list[EvalCase]:
    transform = VARIANTS[variant]
    return [
        case.model_copy(
            update={
                "id": f"{case.id}#{variant}",
                "category": f"{case.category}/{variant}",
                "input": transform(case.input),
                "notes": f"{variant} variant of {case.id}",
            }
        )
        for case in cases
        if case.label == "attack" and case.stage == "input"
    ]
