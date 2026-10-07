"""Detects credentials: API keys, tokens, private keys, passwords in connection strings.

Score = noisy-OR of pattern weights from the policy YAML plus an entropy check for random
looking tokens (separate limits for hex and base64, as in detect-secrets). A random token alone
stays under the threshold; with a keyword like "secret" or "token" just before it, it blocks.
Values that are clearly templates ("your_key", "${TOKEN}") are skipped, like gitleaks.

Output stage uses the policy action (block). Input stage uses params.input_action (redact),
so a user who pastes their own key still gets an answer but the key never reaches the model.
Patterns are case-sensitive; a named group `secret` marks the part to redact.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from dwarpal.guards.base import Action, Guard, GuardContext, GuardResult, Stage, message_text
from dwarpal.guards.normalize import normalize
from dwarpal.guards.redaction import Redactor, Span, redact_user_messages
from dwarpal.guards.registry import register

_TOKEN = re.compile(r"(?<![\w+/=\-])[A-Za-z0-9+/_\-]{16,}={0,2}(?![\w+/=\-])")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class SecretPattern:
    name: str
    regex: re.Pattern[str]
    weight: float
    redact: bool = True


def compile_secret_patterns(raw: list[dict] | None) -> list[SecretPattern]:
    patterns = []
    for i, entry in enumerate(raw or []):
        missing = {"name", "regex", "weight"} - set(entry)
        if missing:
            raise ValueError(f"secrets: params.patterns[{i}] is missing {sorted(missing)}")
        weight = float(entry["weight"])
        if not 0.0 < weight <= 1.0:
            raise ValueError(f"secrets: params.patterns[{i}] weight must be in (0, 1]")
        try:
            regex = re.compile(entry["regex"])
        except re.error as exc:
            raise ValueError(f"secrets: params.patterns[{i}] ({entry['name']}): {exc}") from exc
        patterns.append(SecretPattern(entry["name"], regex, weight, entry.get("redact", True)))
    return patterns


def shannon_bits(text: str) -> float:
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


_HEX = re.compile(r"^[0-9a-fA-F]+$")
_KEYWORD = re.compile(r"(?i)\b(?:api[ _\-]?key|secret|token|passw(?:or)?d|pwd|credential)s?\b")
# Values that look like secrets but are templates or examples (gitleaks calls these stopwords).
_DEFAULT_STOPWORDS = ["your", "xxxx", "changeme", "placeholder", "dummy", "redacted", "${", "{{"]


def _parts(message: dict) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    return [
        p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text"
    ]


@register("secrets")
class SecretsGuard(Guard):
    stages = frozenset({Stage.INPUT, Stage.OUTPUT})

    def __init__(self, policy):
        super().__init__(policy)
        params = policy.params
        self.patterns = compile_secret_patterns(params.get("patterns"))
        self.entropy = {
            "enabled": True,
            "min_length": 24,
            "min_bits_base64": 4.0,
            "min_bits_hex": 3.0,
            "keyword_window": 40,
            "weight": 0.6,
            "weight_with_keyword": 0.9,
            **(params.get("entropy") or {}),
        }
        self.stopwords = [w.lower() for w in params.get("stopwords", _DEFAULT_STOPWORDS)]
        self.input_action = Action(params.get("input_action", "redact"))

    def _is_stopword(self, value: str) -> bool:
        low = value.lower()
        return any(w in low for w in self.stopwords)

    def _random_tokens(self, text: str) -> tuple[float, list[Span]]:
        """Weight of the strongest random-looking token, and the tokens to redact."""
        cfg = self.entropy
        if not cfg["enabled"]:
            return 0.0, []
        weight, spans = 0.0, []
        for m in _TOKEN.finditer(text):
            token = m.group(0)
            if len(token) < cfg["min_length"] or _UUID.match(token) or self._is_stopword(token):
                continue
            if not (re.search(r"\d", token) and re.search(r"[A-Za-z]", token)):
                continue
            limit = cfg["min_bits_hex"] if _HEX.match(token) else cfg["min_bits_base64"]
            if shannon_bits(token) < limit:
                continue
            # A keyword right before the token ("secret is ...", "token: ...") makes it strong.
            before = text[max(0, m.start() - cfg["keyword_window"]) : m.start()]
            w = cfg["weight_with_keyword"] if _KEYWORD.search(before) else cfg["weight"]
            weight = max(weight, w)
            spans.append(Span(m.start(), m.end(), "SECRET", token))
        return weight, spans

    def scan(self, text: str) -> tuple[float, list[str], list[Span]]:
        """(score, names of the signals that fired, spans to redact)."""
        clean, fired, spans = 1.0, [], []
        for p in self.patterns:
            hits = []
            for m in p.regex.finditer(text):
                group = "secret" if "secret" in p.regex.groupindex else 0
                if not self._is_stopword(m.group(group)):
                    hits.append(Span(m.start(group), m.end(group), "SECRET", m.group(group)))
            if hits:
                fired.append(p.name)
                clean *= 1.0 - p.weight
                if p.redact:
                    spans.extend(hits)
        weight, tokens = self._random_tokens(text)
        if tokens:
            fired.append("high_entropy_token")
            clean *= 1.0 - weight
            spans.extend(tokens)
        return 1.0 - clean, fired, spans

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        if stage == Stage.OUTPUT:
            texts = [ctx.response_text or ""]
            # A key spelled out with spaces ("l g _ l i v e ...") is an exfiltration trick.
            if (plain := normalize(texts[0])) != texts[0]:
                texts.append(plain)
        else:
            # Each user turn is scored on its own, all its content parts together.
            texts = [message_text(m) for m in ctx.user_messages()]
        scans = [self.scan(t) for t in texts]
        score = max((s for s, _, _ in scans), default=0.0)
        fired = sorted({name for _, names, _ in scans for name in names})
        reason = f"signals: {', '.join(fired)}" if fired else ""
        if score < self.policy.threshold:
            return self.allow(score, reason)

        action = self.policy.action if stage == Stage.OUTPUT else self.input_action
        if action != Action.REDACT:
            return self.result(action, score, reason)
        redactor = Redactor()
        if stage == Stage.OUTPUT:
            redacted = redactor.apply(texts[0], scans[0][2])
            return self.result(action, score, reason, redacted_text=redacted)
        # In a turn that crossed the threshold, redact every candidate in each of its parts,
        # so a keyword in one part and the key in the next can't slip through.
        hot = {
            part
            for m, (s, _, _) in zip(ctx.user_messages(), scans, strict=True)
            if s >= self.policy.threshold
            for part in _parts(m)
        }
        messages = redact_user_messages(
            ctx.messages, lambda t: self.scan(t)[2] if t in hot else [], redactor
        )
        return self.result(action, score, reason, redacted_messages=messages)
