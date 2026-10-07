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

DEFAULT_MODEL = "dslim/distilbert-NER"
MAX_TOKENS = 512
WINDOW_CHARS = 1500  # comfortably under 512 tokens of English
WINDOW_OVERLAP = 200

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

        The model reads 512 tokens, so long text goes in overlapping windows (like
        classifier.py) and an entity seen twice in an overlap is kept once.
        """
        if len(text) <= WINDOW_CHARS:
            return self._window(text, 0)
        seen: dict[tuple[int, int], Entity] = {}
        for offset in range(0, len(text), WINDOW_CHARS - WINDOW_OVERLAP):
            for ent in self._window(text[offset : offset + WINDOW_CHARS], offset):
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

        out: list[Entity] = []
        current: tuple[str, int, int, list[float]] | None = None  # label, start, end, scores
        for i, (start, end) in enumerate(enc.offsets):
            if start == end:  # [CLS], [SEP], padding
                continue
            tag = self.id2label[int(probs[i].argmax())]
            prob = float(probs[i].max())
            subword = (
                enc.word_ids[i] is not None and i > 0 and enc.word_ids[i] == enc.word_ids[i - 1]
            )
            label = tag[2:] if tag != "O" else None
            if current and (subword or (label == current[0] and tag.startswith("I-"))):
                current = (current[0], current[1], end, current[3] + [prob])
                continue
            if current:
                out.append(
                    Entity(current[1], current[2], current[0], sum(current[3]) / len(current[3]))
                )
                current = None
            if label:
                current = (label, start, end, [prob])
        if current:
            out.append(
                Entity(current[1], current[2], current[0], sum(current[3]) / len(current[3]))
            )
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
