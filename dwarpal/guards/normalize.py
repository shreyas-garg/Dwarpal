"""Undo cheap disguises before the input-attack guards look at text. (PR-04)

  NFKC        full-width and styled letters become plain ones ("ｉｇｎｏｒｅ" -> "ignore")
  invisible   zero-width and other format characters (Unicode Cf) are dropped
  spacing     "i g n o r e  a l l" -> "ignore all"

The guards score the normalised copy as well as the original, never instead of it, so a
normal message is unaffected.
"""

from __future__ import annotations

import re
import unicodedata

# Two or more single characters in a row, each followed by one space ("a r j u n @ x . i n").
_SPACED = re.compile(r"(?<!\S)(?:\S ){1,}\S(?!\S)")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = _SPACED.sub(lambda m: m.group(0).replace(" ", ""), text)
    return re.sub(r" {2,}", " ", text)
