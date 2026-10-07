"""Detects personal data in user input and model replies, then redacts or blocks it.

Structured ids use a regex plus a checksum where the format has one (Aadhaar: Verhoeff,
cards: Luhn), so invoice numbers and amounts are not mistaken for ids. A bare 10 digit phone
number also needs a cue word just before it, like Presidio's context words. Names come from a
pretrained NER model (ner.py). GSTIN and IFSC are left out on purpose: they are public
business ids that Ledgerly users mention all the time.

Values the user typed are kept on the request's context, so when the model's reply uses
<EMAIL_1> the user sees their own email again (LLM Guard calls this deanonymize). New
personal data in the reply is still redacted. This assumes user turns hold the user's own data;
retrieved documents belong in dwarpal.context, not in a user message.

Params: entities, block_on, allow (exact values to skip), restore_in_reply,
ner: {enabled, model, revision, min_score, never_names}.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass

from dwarpal.guards.base import Action, Guard, GuardContext, GuardResult, Stage, message_text
from dwarpal.guards.normalize import normalize
from dwarpal.guards.redaction import (
    Redactor,
    Span,
    redact_user_messages,
    resolve_overlaps,
    summarize,
)
from dwarpal.guards.registry import register

# --- checksums ---

_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def verhoeff_ok(value: str) -> bool:
    check = 0
    for i, digit in enumerate(reversed(_digits(value))):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][int(digit)]]
    return check == 0


def iban_ok(value: str) -> bool:
    s = re.sub(r"\s", "", value).upper()
    digits = "".join(str(int(ch, 36)) for ch in s[4:] + s[:4])
    return int(digits) % 97 == 1


def luhn_ok(value: str) -> bool:
    total = 0
    for i, digit in enumerate(reversed(_digits(value))):
        d = int(digit)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


# --- recognisers ---


@dataclass(frozen=True)
class Recogniser:
    entity: str
    regex: re.Pattern[str]
    validate: Callable[[str], bool] | None = None
    # When set, one of these words must appear in the 40 chars before the match.
    context: re.Pattern[str] | None = None


# scheme://user:password@ -- an "email" inside this is a password, left to the secrets guard.
_URL_USERINFO = re.compile(r"[A-Za-z][\w+.\-]*://[^\s/?#@]*@")
_BANK_CUES = re.compile(r"(?i)\b(a/?c|acct|account|bank)\b")
_DOB_CUES = re.compile(r"(?i)\b(dob|d\.o\.b|date of birth|born|birthday)\b")
_PASSPORT_CUES = re.compile(r"(?i)\bpassport\b")
_VOTER_CUES = re.compile(r"(?i)\b(voter|epic)\b")
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_PHONE_CUES = re.compile(r"(?i)\b(phone|mobile|mob|cell|call|whatsapp|contact|number|no)\b")
_STREET = (
    r"(?:Road|Rd|Street|St|Marg|Lane|Nagar|Colony|Society|Layout|Cross|Main|Avenue|Chowk|Bagh"
    r"|Sector|Apartments?)"
)

RECOGNISERS = [
    Recogniser(
        "EMAIL",
        re.compile(
            r"(?<![\w.%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}\b"
        ),
    ),
    # UPI ids look like emails without a TLD; only well-known bank handles, to stay precise.
    Recogniser(
        "UPI",
        re.compile(
            r"(?<![\w.\-])[A-Za-z0-9._\-]{2,64}@(?:ok(?:sbi|hdfcbank|icici|axis)|ybl|ibl|axl|paytm|apl|upi)\b(?!\.)"
        ),
    ),
    # Indian mobile with a +91 / 0 prefix: no cue word needed.
    Recogniser("PHONE", re.compile(r"(?<![\w+])(?:\+91[\s\-]?|0)[6-9]\d{4}[\s\-]?\d{5}(?!\w)")),
    # Bare 10 digits starting 6-9: only next to a cue word, otherwise it may be an order id.
    Recogniser("PHONE", re.compile(r"(?<![\w+])[6-9]\d{4}[\s\-]?\d{5}(?!\w)"), context=_PHONE_CUES),
    # PAN: AAAAA9999A, but never inside a longer run (a GSTIN embeds a PAN).
    Recogniser("PAN", re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Za-z0-9])")),
    Recogniser(
        "AADHAAR",
        re.compile(r"(?<![\d\-])[2-9]\d{3}[\s\-]?\d{4}[\s\-]?\d{4}(?![\d\-])"),
        verhoeff_ok,
    ),
    Recogniser("CARD", re.compile(r"(?<![\d\-])\d(?:[\s\-]?\d){12,18}(?![\d\-])"), luhn_ok),
    # The ids below look like plain numbers or dates, so each needs its cue word close by.
    Recogniser("BANK_ACCOUNT", re.compile(r"(?<![\d\-])\d{9,18}(?![\d\-])"), context=_BANK_CUES),
    Recogniser(
        "IBAN",
        re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b"),
        iban_ok,
    ),
    Recogniser(
        "DOB",
        re.compile(
            rf"(?i)\b(?:\d{{1,2}}[/.\-]\d{{1,2}}[/.\-](?:19|20)\d{{2}}|\d{{1,2}}\s+{_MONTHS},?\s+(?:19|20)\d{{2}})\b"
        ),
        context=_DOB_CUES,
    ),
    Recogniser(
        "PASSPORT", re.compile(r"\b[A-PR-WY][1-9]\d\s?\d{4}[1-9]\b"), context=_PASSPORT_CUES
    ),
    Recogniser("VOTER_ID", re.compile(r"\b[A-Z]{3}\d{7}\b"), context=_VOTER_CUES),
    # House number, up to three words ("14th", "HSR"), a street word, optional city and PIN.
    Recogniser(
        "ADDRESS",
        re.compile(
            rf"\b\d{{1,4}}[A-Z]?,?\s+(?:[A-Z0-9][A-Za-z0-9]*\s+){{1,3}}{_STREET}\b"
            r"(?:,?\s+[A-Z][a-z]+)?(?:,?\s+[1-9]\d{5})?"
        ),
    ),
    # The common Indian shape: "Flat 302, <building>, <area>, <city> 560102". A unit word
    # starts it and a 6 digit PIN ends it, so a bare PIN or a flat number alone never matches.
    Recogniser(
        "ADDRESS",
        re.compile(
            r"(?i)\b(?:flat|house|plot|door|shop|office|h\.?\s?no\.?)\s*(?:no\.?\s*)?(?=[\w/-]*\d)[\w/-]{1,6}"
            r"[^\n]{0,120}?\b[1-9]\d{2}\s?\d{3}\b"
        ),
    ),
]
ENTITIES = {r.entity for r in RECOGNISERS} | {"NAME"}

_CUE_WORDS = (
    r"for|by|from|with|client|customer|name(?: is)?|named|called"
    r"|mr|mrs|ms|dr|shri|smt|ask|contact"
)
_NAME_CUES = re.compile(rf"(?i)\b({_CUE_WORDS})\b[ :]\s*$")
# Cheap check before the model, like gitleaks' keyword prefilter: the filters below only keep
# full names or a name after a cue word, so text with neither can skip the model.
_MAYBE_NAME = re.compile(rf"[A-Z][a-z]+\s+[A-Z][a-z]+|(?i:\b(?:{_CUE_WORDS})\b)[ :]\s*[A-Z][a-z]")
_NEXT_CAPITALISED = re.compile(r" ([A-Z][a-z]+)\b")


@register("pii")
class PiiGuard(Guard):
    stages = frozenset({Stage.INPUT, Stage.OUTPUT})

    def __init__(self, policy):
        super().__init__(policy)
        params = policy.params
        unknown = (set(params.get("entities") or []) | set(params.get("block_on") or [])) - ENTITIES
        if unknown:
            raise ValueError(f"pii: unknown entity types {sorted(unknown)}")
        entities = set(params.get("entities") or ENTITIES)
        self.recognisers = [r for r in RECOGNISERS if r.entity in entities]
        self.block_on = set(params.get("block_on") or [])
        self.allow_values = {v.lower() for v in params.get("allow") or []}
        self.restore = bool(params.get("restore_in_reply", True))
        self.ner_config = params.get("ner") or {}
        self.names_on = "NAME" in entities and bool(self.ner_config.get("enabled"))
        self.never_names = {w.lower() for w in self.ner_config.get("never_names") or []}
        self.tagger = None

    async def setup(self) -> None:
        if self.names_on:
            from dwarpal.guards.ner import DEFAULT_MODEL, get_tagger

            model = self.ner_config.get("model", DEFAULT_MODEL)
            revision = self.ner_config.get("revision")
            self.tagger = await asyncio.to_thread(get_tagger, model, revision)

    # --- detection ---

    def _pattern_spans(self, text: str) -> list[Span]:
        userinfo = [m.span() for m in _URL_USERINFO.finditer(text)]
        spans = []
        for rec in self.recognisers:
            for m in rec.regex.finditer(text):
                value = m.group(0)
                if value.lower() in self.allow_values:
                    continue
                if rec.validate and not rec.validate(value):
                    continue
                if rec.context and not rec.context.search(text[max(0, m.start() - 40) : m.start()]):
                    continue
                if rec.entity == "EMAIL" and any(a <= m.start() < b for a, b in userinfo):
                    continue
                spans.append(Span(m.start(), m.end(), rec.entity, value))
        return spans

    def _name_spans(self, text: str) -> list[Span]:
        if self.tagger is None or not _MAYBE_NAME.search(text):
            return []
        min_score = float(self.ner_config.get("min_score", 0.5))
        spans = []
        for ent in self.tagger.entities(text):
            if ent.label != "PER" or ent.score < min_score:
                continue
            # Widen to whole words: the model sometimes tags only part of one ("adhaar").
            start, end = ent.start, ent.end
            while start > 0 and text[start - 1].isalnum():
                start -= 1
            while end < len(text) and text[end].isalnum():
                end += 1
            # Surnames often get another label ("Meera" PER, "Nair" ORG): take up to two
            # capitalised words right after a name as part of it.
            for _ in range(2):
                m = _NEXT_CAPITALISED.match(text, end)
                if not m or m.group(1).lower() in self.never_names:
                    break
                end = m.end()
            value = text[start:end]
            words = re.findall(r"\w+", value)
            if {w.lower() for w in words} & self.never_names or value.lower() in self.allow_values:
                continue
            # A lone capitalised word is weak evidence ("Mock answer to..."); a full name is not.
            if len(words) == 1 and not _NAME_CUES.search(text[max(0, start - 30) : start]):
                continue
            spans.append(Span(start, end, "NAME", value))
        return spans

    def find(self, text: str) -> list[Span]:
        return resolve_overlaps(self._pattern_spans(text) + self._name_spans(text))

    def _restore(self, ctx: GuardContext, text: str) -> tuple[str, set[str]]:
        mapping = ctx.state.get(self.policy.ref) or {}
        for placeholder, value in mapping.items():
            text = text.replace(placeholder, value)
        return text, set(mapping.values())

    # --- the check ---

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        if stage == Stage.OUTPUT:
            return await self._check_output(ctx)
        return await self._check_input(ctx)

    def _decide(self, spans: list[Span]) -> tuple[Action, str]:
        reason = f"found {summarize(spans)}"
        blocking = {s.entity for s in spans} & self.block_on
        if blocking:
            return Action.BLOCK, f"{reason}; blocks on {', '.join(sorted(blocking))}"
        return self.policy.action, reason

    async def _off_loop(self, fn, *args):
        # The NER model is CPU work; regexes alone are too quick to be worth a thread hop.
        return await asyncio.to_thread(fn, *args) if self.tagger else fn(*args)

    async def _check_input(self, ctx: GuardContext) -> GuardResult:
        # One pass per user turn; redaction reuses these spans instead of running the model again.
        texts = [message_text(m) for m in ctx.user_messages()]
        found = await self._off_loop(lambda: {t: self.find(t) for t in set(texts)})
        spans = [s for t in texts for s in found[t]]
        if not spans:
            return self.allow()
        action, reason = self._decide(spans)
        if action != Action.REDACT:
            return self.result(action, 1.0, reason)
        redactor = Redactor()
        messages = await self._off_loop(
            redact_user_messages,
            ctx.messages,
            lambda t: found[t] if t in found else self.find(t),  # content parts land here
            redactor,
        )
        # Keyed by policy ref, and never from a shadow run, so a v2 on trial can't overwrite v1.
        if self.restore and self.policy.mode != "shadow":
            ctx.state[self.policy.ref] = redactor.mapping()
        return self.result(Action.REDACT, 1.0, reason, redacted_messages=messages)

    async def _check_output(self, ctx: GuardContext) -> GuardResult:
        original = ctx.response_text or ""
        restored, own_values = self._restore(ctx, original)
        spans = [s for s in await self._off_loop(self.find, restored) if s.value not in own_values]
        if not spans and (plain := normalize(restored)) != restored:
            # "a r j u n @ ..." in a reply is data being smuggled out; there is no clean span to
            # redact in the original, so the reply is blocked.
            hidden = [s for s in self.find(plain) if s.value not in own_values]
            if hidden:
                return self.result(
                    Action.BLOCK, 1.0, f"found {summarize(hidden)} in disguised form"
                )
        if not spans:
            if restored == original:
                return self.allow()
            # Only the user's own values came back; nothing new to hide.
            return self.result(
                Action.REDACT,
                0.0,
                "restored the user's own values",
                redacted_text=restored,
                restored=True,
            )
        action, reason = self._decide(spans)
        if action != Action.REDACT:
            return self.result(action, 1.0, reason)
        redacted = Redactor().apply(restored, spans)
        return self.result(Action.REDACT, 1.0, reason, redacted_text=redacted)
