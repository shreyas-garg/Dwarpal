"""Name detection for the pii guard: a pretrained BERT NER model on CPU.

Same setup as classifier.py: ONNX weights from the model repo, onnxruntime + tokenizers, no
torch. Downloaded once to the Hugging Face cache, loaded once per process.

Only PER (person) spans are used. Locations and organisations are not personal data on
their own ("Mumbai", "Razorpay"), and tagging them would mostly add false positives.

This module registers no guard and imports heavy dependencies lazily.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from dwarpal.guards.windows import token_windows

DEFAULT_MODEL = "dslim/distilbert-NER"
MAX_TOKENS = 512

_instances: dict[str, NameTagger] = {}
_lock = threading.Lock()


def get_tagger(model_id: str = DEFAULT_MODEL, revision: str | None = None) -> NameTagger:
    with _lock:
        key = f"{model_id}@{revision}"
        if key not in _instances:
            _instances[key] = NameTagger(model_id, revision)
        return _instances[key]


@dataclass(frozen=True)
class Entity:
    start: int
    end: int
    label: str  # PER, LOC, ORG, MISC
    score: float  # mean probability over the entity's tokens


class NameTagger:
    def __init__(self, model_id: str = DEFAULT_MODEL, revision: str | None = None):
        try:
            import json

            import onnxruntime
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError(
                f"name detection needs the 'ml' extra: run `uv sync --all-extras` ({exc.name})"
            ) from exc

        def fetch(name: str) -> str:
            return hf_hub_download(model_id, f"onnx/{name}", revision=revision)

        self.tokenizer = Tokenizer.from_file(fetch("tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.full_tokenizer = Tokenizer.from_file(fetch("tokenizer.json"))  # for windowing
        self.full_tokenizer.no_truncation()
        options = onnxruntime.SessionOptions()
        # Idle worker threads busy-wait by default and starve the other ONNX sessions in this
        # process (the PR-03 classifier): ~160 ms vs ~40 ms measured with both models loaded.
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self.session = onnxruntime.InferenceSession(
            fetch("model.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self.input_names = {i.name for i in self.session.get_inputs()}
        config = json.loads(Path(fetch("config.json")).read_text())
        self.id2label = {int(k): v for k, v in config["id2label"].items()}

    def entities(self, text: str) -> list[Entity]:
        """BIO tags merged into character spans. CPU-bound: call via asyncio.to_thread.

        The model reads 512 tokens, so long text goes in overlapping token windows and an
        entity seen twice in an overlap is kept once.
        """
        ranges = token_windows(self.full_tokenizer, text)
        if len(ranges) == 1:
            return self._window(text, 0)
        seen: dict[tuple[int, int], Entity] = {}
        for start, end in ranges:
            for ent in self._window(text[start:end], start):
                seen.setdefault((ent.start, ent.end), ent)
        return sorted(seen.values(), key=lambda e: e.start)

    def _window(self, text: str, offset: int) -> list[Entity]:
        import numpy as np

        if not text.strip():
            return []
        enc = self.tokenizer.encode(text)
        feed = {
            "input_ids": np.array([enc.ids], dtype=np.int64),
            "attention_mask": np.array([enc.attention_mask], dtype=np.int64),
        }
        if "token_type_ids" in self.input_names:
            feed["token_type_ids"] = np.array([enc.type_ids], dtype=np.int64)
        logits = self.session.run(None, feed)[0][0]
        probs = np.exp(logits - logits.max(axis=-1, keepdims=True))
        probs /= probs.sum(axis=-1, keepdims=True)

        # One label per word: average the probabilities of its sub-tokens ("R", "##oh", "##it"),
        # like the "average" aggregation in Hugging Face pipelines. Taking only the first
        # sub-token's label turned "Rohit" into an organisation.
        words: dict[int, tuple[int, int, list[int]]] = {}  # word id -> start, end, token rows
        for row, (word, (start, end)) in enumerate(zip(enc.word_ids, enc.offsets, strict=True)):
            if word is None or start == end:  # special tokens
                continue
            first, _, rows = words.get(word, (start, end, []))
            words[word] = (first, end, rows + [row])

        out: list[Entity] = []
        for start, end, rows in words.values():
            avg = probs[rows].mean(axis=0)
            tag = self.id2label[int(avg.argmax())]
            if tag == "O":
                continue
            label, score = tag[2:], float(avg.max())
            prev = out[-1] if out else None
            if prev and tag.startswith("I-") and prev.label == label:
                out[-1] = Entity(prev.start, end, label, (prev.score + score) / 2)
            else:
                out.append(Entity(start, end, label, score))
        merged = _merge_adjacent(text, out)
        return [Entity(e.start + offset, e.end + offset, e.label, e.score) for e in merged]


def _merge_adjacent(text: str, entities: list[Entity]) -> list[Entity]:
    """ "Meera" + "Nair" tagged as two people with only a space between: one name."""
    merged: list[Entity] = []
    for ent in entities:
        prev = merged[-1] if merged else None
        if prev and prev.label == ent.label and text[prev.end : ent.start].strip() == "":
            merged[-1] = Entity(prev.start, ent.end, ent.label, (prev.score + ent.score) / 2)
        else:
            merged.append(ent)
    return merged
