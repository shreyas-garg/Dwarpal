"""Undo cheap disguises before the input-attack guards look at text. (PR-04)

  NFKC        full-width and styled letters become plain ones ("ｉｇｎｏｒｅ" -> "ignore")
  invisible   zero-width and other format characters (Unicode Cf) are dropped
  spacing     "i g n o r e  a l l" -> "ignore all"
  leetspeak   "B1tc01n" -> "Bitcoin", only in words that mix letters and digits, so phone
              numbers, amounts and years stay as they are (variants() only)

The guards score these copies as well as the original, never instead of it, so a normal
message is unaffected.
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


_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)
_MIXED = re.compile(r"\S*[A-Za-z]\S*")


def deleet(text: str) -> str:
    def fix(m: re.Match[str]) -> str:
        word = m.group(0)
        letters, digits = sum(c.isalpha() for c in word), sum(c.isdigit() for c in word)
        # Leetspeak is mostly letters ("B1tc01n"); ids are mostly digits ("INV-2024-000123").
        if re.search(r"[\d@$]", word) and letters >= digits:
            return word.translate(_LEET)
        return word

    return _MIXED.sub(fix, text)


def variants(text: str) -> list[str]:
    """De-disguised copies of text worth scoring too: normalised, then also de-leeted."""
    plain = normalize(text)
    return [v for v in dict.fromkeys([plain, deleet(plain)]) if v != text]
