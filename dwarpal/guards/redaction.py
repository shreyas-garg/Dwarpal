"""Placeholder redaction shared by the pii and secrets guards.

The same value gets the same placeholder within a request ("<EMAIL_1> ... <EMAIL_1>"), so
the model can still follow the sentence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    entity: str  # e.g. "EMAIL", "PAN", "SECRET"
    value: str


def resolve_overlaps(spans: list[Span]) -> list[Span]:
    """Longest span wins on overlap, e.g. a card number over the Aadhaar-shaped digits in it."""
    kept: list[Span] = []
    for span in sorted(spans, key=lambda s: (-(s.end - s.start), s.start)):
        if all(span.end <= k.start or span.start >= k.end for k in kept):
            kept.append(span)
    return sorted(kept, key=lambda s: s.start)


@dataclass
class Redactor:
    _placeholders: dict[tuple[str, str], str] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)

    def placeholder(self, entity: str, value: str) -> str:
        key = (entity, value)
        if key not in self._placeholders:
            self._counts[entity] = self._counts.get(entity, 0) + 1
            self._placeholders[key] = f"<{entity}_{self._counts[entity]}>"
        return self._placeholders[key]

    def mapping(self) -> dict[str, str]:
        """placeholder -> original value, for putting the user's own values back later."""
        return {ph: value for (_, value), ph in self._placeholders.items()}

    def apply(self, text: str, spans: list[Span]) -> str:
        out, cursor = [], 0
        for span in resolve_overlaps(spans):
            out.append(text[cursor : span.start])
            out.append(self.placeholder(span.entity, span.value))
            cursor = span.end
        out.append(text[cursor:])
        return "".join(out)


def redact_user_messages(
    messages: list[dict[str, Any]],
    find: Callable[[str], list[Span]],
    redactor: Redactor,
) -> list[dict[str, Any]]:
    """Copy of messages with user turns redacted. System and assistant turns are left alone."""
    out = []
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or content is None:
            out.append(message)
        elif isinstance(content, str):
            out.append({**message, "content": redactor.apply(content, find(content))})
        else:
            parts = [
                {**p, "text": redactor.apply(p.get("text", ""), find(p.get("text", "")))}
                if isinstance(p, dict) and p.get("type") == "text"
                else p
                for p in content
            ]
            out.append({**message, "content": parts})
    return out


def summarize(spans: list[Span]) -> str:
    """E.g. "EMAIL×2, PAN×1". Types only, never the values."""
    counts: dict[str, int] = {}
    for span in spans:
        counts[span.entity] = counts.get(span.entity, 0) + 1
    return ", ".join(f"{entity}×{n}" for entity, n in sorted(counts.items()))
