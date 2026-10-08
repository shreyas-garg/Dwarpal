"""PR-05: banned_topics and toxicity guards.

Offline and fast: the shipped policy files are used, but the MiniLM embedder and toxic-bert
are replaced with stubs. The real models are exercised by `make eval`.
"""

import re
import zlib
from pathlib import Path

import numpy as np
import pytest
import yaml

from dwarpal.guards import embedder as embedder_module
from dwarpal.guards import toxicity_model as toxicity_module
from dwarpal.guards.banned_topics import BannedTopicsGuard, TopicError, parse_topics, text_units
from dwarpal.guards.base import Action, GuardContext, Stage
from dwarpal.guards.toxicity import ToxicityGuard
from dwarpal.policy import Policy, load_policies
from tests.conftest import REPO_POLICIES

LABELS = ["toxic", "severe_toxic", "obscene", "threat", "insult", "identity_hate"]


class BagOfWords:
    """Stand-in for the MiniLM embedder: hashed word counts, L2-normalised.
    Texts that share words get a high cosine, which is all these tests need."""

    def embed(self, texts):
        out = np.zeros((len(texts), 512))
        for row, text in enumerate(texts):
            for word in re.findall(r"[a-z]+", text.lower()):
                out[row, zlib.crc32(word.encode()) % 512] += 1.0  # stable across runs
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.clip(norms, 1e-9, None)


class FakeToxicity:
    """Stand-in for toxic-bert: returns preset scores for any text containing a key."""

    labels = LABELS

    def __init__(self, table: dict[str, dict[str, float]]):
        self.table = table

    def scores(self, text):
        for key, scores in self.table.items():
            if key in text:
                return {label: scores.get(label, 0.0) for label in LABELS}
        return dict.fromkeys(LABELS, 0.0)


@pytest.fixture
def stub_embedder(monkeypatch):
    monkeypatch.setattr(embedder_module, "get_embedder", lambda *a, **k: BagOfWords())


def shipped(name: str, **overrides) -> Policy:
    data = yaml.safe_load((REPO_POLICIES / f"{name}.yaml").read_text())
    data.update(overrides)
    return Policy.model_validate(data)


def user(text: str, context: list[str] | None = None) -> GuardContext:
    return GuardContext(messages=[{"role": "user", "content": text}], context_docs=context or [])


def reply(text: str) -> GuardContext:
    return GuardContext(messages=[{"role": "user", "content": "hi"}], response_text=text)


# --- banned_topics: policy parsing ---


@pytest.mark.parametrize(
    "topics, message",
    [
        ([], "non-empty"),
        ([{"examples": ["x"]}], "needs a name"),
        ([{"name": "t", "examples": []}], "at least one example"),
        ([{"name": "t", "examples": ["x"], "keywords": ["("]}], "bad keyword regex"),
    ],
)
def test_bad_topics_rejected(topics, message):
    with pytest.raises(TopicError, match=message):
        parse_topics(topics)


def test_shipped_topics_parse():
    topics = parse_topics(shipped("banned_topics").params["topics"])
    names = {t.name for t in topics}
    assert names == {
        "medical_advice",
        "legal_advice",
        "investment_advice",
        "politics_elections",
        "weapons",
    }
    assert all(5 <= len(t.examples) <= 8 for t in topics)


def test_text_units_split_sentences():
    assert text_units("One question?") == ["One question?"]
    units = text_units("How do I export? Also, which stocks should I buy?")
    assert units == [
        "How do I export? Also, which stocks should I buy?",
        "How do I export?",
        "Also, which stocks should I buy?",
    ]
    assert text_units("   ") == []


# --- banned_topics: keyword layer (no model needed) ---


@pytest.mark.parametrize(
    "text, topic",
    [
        ("Who should I vote for this year?", "politics_elections"),
        ("Which stocks should I buy with my refund?", "investment_advice"),
        ("How do I make a pipe bomb?", "weapons"),
    ],
)
async def test_keywords_block_without_model(text, topic):
    guard = BannedTopicsGuard(shipped("banned_topics"))
    result = await guard.check(user(text), Stage.INPUT)
    assert result.action == Action.BLOCK
    assert result.reason == f"keyword: {topic}"


@pytest.mark.parametrize(
    "text",
    [
        "Can my customers pay invoices in bitcoin?",
        "How do I kill a stuck export job?",
        "Which plan should I buy for a team of five?",
    ],
)
def test_keywords_do_not_fire_on_hard_negatives(text):
    guard = BannedTopicsGuard(shipped("banned_topics"))
    assert guard._keyword_hit([text]) is None


# --- banned_topics: similarity layer, with the stub embedder ---


async def test_similar_question_blocked(stub_embedder):
    guard = BannedTopicsGuard(shipped("banned_topics"))
    await guard.setup()
    result = await guard.check(user("What dose of paracetamol is safe for my son?"), Stage.INPUT)
    assert result.action == Action.BLOCK
    assert "medical_advice" in result.reason


async def test_unrelated_question_allowed(stub_embedder):
    guard = BannedTopicsGuard(shipped("banned_topics"))
    await guard.setup()
    result = await guard.check(user("Where can I download my GST report?"), Stage.INPUT)
    assert result.action == Action.ALLOW


async def test_banned_sentence_inside_long_message_caught(stub_embedder):
    guard = BannedTopicsGuard(shipped("banned_topics"))
    await guard.setup()
    text = (
        "I have twelve recurring invoices for retainer clients and the export to PDF keeps "
        "timing out on the Business plan whenever I select the full financial year. "
        "Is my employment contract legally enforceable?"
    )
    result = await guard.check(user(text), Stage.INPUT)
    assert result.action == Action.BLOCK
    assert "legal_advice" in result.reason


async def test_spaced_out_question_caught(stub_embedder):
    guard = BannedTopicsGuard(shipped("banned_topics"))
    await guard.setup()
    result = await guard.check(user("w h o  s h o u l d  i  v o t e  f o r"), Stage.INPUT)
    assert result.action == Action.BLOCK


async def test_context_documents_not_checked(stub_embedder):
    guard = BannedTopicsGuard(shipped("banned_topics"))
    await guard.setup()
    doc = "How much ibuprofen should I take for a headache? What medicine should I take?"
    result = await guard.check(user("Summarise this ticket", context=[doc]), Stage.INPUT)
    assert result.action == Action.ALLOW


# --- toxicity ---


@pytest.fixture
def fake_toxicity(monkeypatch, stub_embedder):
    def install(table):
        model = FakeToxicity(table)
        monkeypatch.setattr(toxicity_module, "get_toxicity_model", lambda *a, **k: model)

    return install


async def test_insult_blocked(fake_toxicity):
    fake_toxicity({"moron": {"toxic": 0.97, "insult": 0.91}})
    guard = ToxicityGuard(shipped("toxicity"))
    await guard.setup()
    result = await guard.check(reply("Read the docs, you moron."), Stage.OUTPUT)
    assert result.action == Action.BLOCK
    assert "toxic 0.97" in result.reason and "insult 0.91" in result.reason


async def test_label_threshold_applies_per_label(fake_toxicity):
    # threat has its own low threshold (0.15); 0.22 is far below the 0.30 toxic threshold.
    fake_toxicity({"regret": {"toxic": 0.25, "threat": 0.22}})
    guard = ToxicityGuard(shipped("toxicity"))
    await guard.setup()
    result = await guard.check(reply("Pay by Friday or you will regret it."), Stage.OUTPUT)
    assert result.action == Action.BLOCK
    assert result.reason == "threat 0.22"


async def test_unlisted_label_uses_policy_threshold(fake_toxicity):
    policy = shipped("toxicity")
    policy.params["labels"] = {}
    fake_toxicity({"rude": {"obscene": 0.45}})
    guard = ToxicityGuard(policy)
    await guard.setup()
    assert guard.threshold_for("obscene") == 0.50
    result = await guard.check(reply("A rude reply."), Stage.OUTPUT)
    assert result.action == Action.ALLOW


async def test_polite_reply_allowed(fake_toxicity):
    fake_toxicity({})
    guard = ToxicityGuard(shipped("toxicity"))
    await guard.setup()
    result = await guard.check(
        reply("Sorry for the trouble! Pick a date range and try the export again."), Stage.OUTPUT
    )
    assert result.action == Action.ALLOW


async def test_contempt_layer_catches_what_the_model_misses(fake_toxicity):
    fake_toxicity({})  # toxic-bert sees nothing wrong
    guard = ToxicityGuard(shipped("toxicity"))
    await guard.setup()
    text = "Customers like you waste everyone's time."
    result = await guard.check(reply(text), Stage.OUTPUT)
    assert result.action == Action.BLOCK
    assert "contempt" in result.reason


async def test_contempt_layer_can_be_switched_off(fake_toxicity):
    policy = shipped("toxicity")
    policy.params["contempt"] = {"enabled": False}
    fake_toxicity({})
    guard = ToxicityGuard(policy)
    await guard.setup()
    result = await guard.check(reply("Customers like you waste everyone's time."), Stage.OUTPUT)
    assert result.action == Action.ALLOW


async def test_unknown_label_rejected_at_startup(fake_toxicity):
    policy = shipped("toxicity")
    policy.params["labels"] = {"rudeness": 0.3}
    fake_toxicity({})
    with pytest.raises(ValueError, match="unknown labels"):
        await ToxicityGuard(policy).setup()


async def test_empty_reply_allowed(fake_toxicity):
    fake_toxicity({})
    guard = ToxicityGuard(shipped("toxicity"))
    await guard.setup()
    assert (await guard.check(reply("  "), Stage.OUTPUT)).action == Action.ALLOW


def test_shipped_policies_load():
    names = {p.name for p in load_policies(Path(REPO_POLICIES))}
    assert {"banned_topics", "toxicity"} <= names
