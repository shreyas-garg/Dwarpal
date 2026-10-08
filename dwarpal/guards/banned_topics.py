"""Blocks requests on topics a Ledgerly support bot must not advise on. (PR-05)

Each topic in policies/banned_topics.yaml has a name, a one-line description and a handful
of example phrasings. Two layers, score = max of the two:

  Layer 1  optional keyword regexes per topic, <1 ms, for phrasings with no innocent reading.
  Layer 2  cosine similarity between the user's text and every example phrasing, using
           all-MiniLM-L6-v2 (see embedder.py). The examples are embedded once at startup.

Each user turn is scored whole and sentence by sentence, so one off-topic question tucked
behind a long, legitimate billing question is not averaged away, and a de-disguised copy
(normalize.py) is scored too. Supplied context documents
are not checked: a pasted contract or medical invoice is data, not a request for advice.

Adding a topic or a phrasing is a policy change, not a code change: edit the YAML and bump
its version.

Policy params:
  embedder:  {model, revision} — defaults to sentence-transformers/all-MiniLM-L6-v2
  topics:    list of {name, description, examples: [str], keywords: [regex]}
  The policy threshold is the minimum cosine similarity that counts as on-topic.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from dwarpal.guards.base import Guard, GuardContext, GuardResult, Stage, message_text
from dwarpal.guards.normalize import normalize
from dwarpal.guards.registry import register

# Split after sentence-ending punctuation or a line break.
_SENTENCES = re.compile(r"(?<=[.?!])\s+|\n+")
MAX_UNITS = 64  # caps the embedding batch; max_length already blocks very long input


class TopicError(ValueError):
    """A topic in the policy file is malformed. Raised at startup."""


@dataclass
class Topic:
    name: str
    description: str
    examples: list[str]
    keywords: list[re.Pattern[str]] = field(default_factory=list)


def parse_topics(raw: Any) -> list[Topic]:
    if not isinstance(raw, list) or not raw:
        raise TopicError("banned_topics needs a non-empty `topics` list")
    topics = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not item.get("name"):
            raise TopicError(f"topic #{i} needs a name")
        examples = item.get("examples") or []
        if not examples or not all(isinstance(e, str) and e.strip() for e in examples):
            raise TopicError(f"topic {item['name']!r} needs at least one example phrasing")
        try:
            keywords = [re.compile(k, re.IGNORECASE) for k in item.get("keywords") or []]
        except re.error as exc:
            raise TopicError(f"topic {item['name']!r}: bad keyword regex: {exc}") from exc
        topics.append(Topic(item["name"], item.get("description", ""), examples, keywords))
    return topics


def text_units(text: str) -> list[str]:
    """The whole text, then each sentence when there is more than one."""
    text = text.strip()
    if not text:
        return []
    sentences = [s.strip() for s in _SENTENCES.split(text) if s.strip()]
    return [text] + (sentences if len(sentences) > 1 else [])


@register("banned_topics")
class BannedTopicsGuard(Guard):
    stages = frozenset({Stage.INPUT})

    def __init__(self, policy):
        super().__init__(policy)
        self.topics = parse_topics(policy.params.get("topics"))
        # Row i of the example matrix belongs to topic example_topic[i].
        self.example_topic = [t.name for t in self.topics for _ in t.examples]
        self._embedder = None
        self._examples = None  # (n_examples, dim) unit vectors, filled in setup()

    async def setup(self) -> None:
        from dwarpal.guards.embedder import DEFAULT_MODEL, get_embedder

        config = self.policy.params.get("embedder") or {}
        model_id = config.get("model", DEFAULT_MODEL)
        self._embedder = await asyncio.to_thread(get_embedder, model_id, config.get("revision"))
        phrasings = [e for t in self.topics for e in t.examples]
        self._examples = await asyncio.to_thread(self._embedder.embed, phrasings)

    def _keyword_hit(self, texts: list[str]) -> str | None:
        for topic in self.topics:
            if any(k.search(t) for k in topic.keywords for t in texts):
                return topic.name
        return None

    def _similarity(self, units: list[str]) -> tuple[float, str]:
        sims = self._embedder.embed(units) @ self._examples.T  # (units, examples)
        best = int(sims.max(axis=0).argmax())
        return float(sims[:, best].max()), self.example_topic[best]

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        texts = [t for m in ctx.user_messages() if (t := message_text(m)).strip()]
        if not texts:
            return self.allow(reason="no user text")
        # Also score a de-disguised copy ("s h o u l d  i  b u y"), only when it differs.
        texts.extend(n for t in list(texts) if (n := normalize(t)) != t)

        if topic := self._keyword_hit(texts):
            return self.decide(1.0, reason=f"keyword: {topic}")
        if self._embedder is None:
            return self.allow(reason="embedder not loaded")

        units = [u for t in texts for u in text_units(t)][:MAX_UNITS]
        score, topic = await asyncio.to_thread(self._similarity, units)
        reason = f"nearest topic: {topic} ({score:.3f})"
        return self.decide(max(0.0, min(1.0, score)), reason=reason)
