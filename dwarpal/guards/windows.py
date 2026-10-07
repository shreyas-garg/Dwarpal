"""Split long text into pieces a 512-token model can read whole. (PR-04)

Windows are cut on token boundaries of the full text, so a token-dense input ("x x x ...")
can't push the end of a window past the model's limit, and every token lands in at least
one window. Returns character ranges; callers run the model on each slice.
"""

from __future__ import annotations

from typing import Any

MAX_CONTENT_TOKENS = 500  # leaves room for the model's special tokens inside 512
OVERLAP_TOKENS = 64


def token_windows(
    full_tokenizer: Any,
    text: str,
    max_tokens: int = MAX_CONTENT_TOKENS,
    overlap: int = OVERLAP_TOKENS,
) -> list[tuple[int, int]]:
    """Character (start, end) ranges. `full_tokenizer` must not truncate."""
    offsets = [(a, b) for a, b in full_tokenizer.encode(text).offsets if b > a]
    if len(offsets) <= max_tokens:
        return [(0, len(text))]
    ranges, first = [], 0
    while first < len(offsets):
        last = min(first + max_tokens, len(offsets)) - 1
        ranges.append((offsets[first][0], offsets[last][1]))
        if last == len(offsets) - 1:
            break
        first = last + 1 - overlap
    return ranges
