"""Sentence embeddings for the banned_topics guard: all-MiniLM-L6-v2 on CPU. (PR-05)

Same setup as classifier.py and ner.py: the ONNX weights that ship in the model repo,
onnxruntime + tokenizers, no torch. Downloaded once to the Hugging Face cache, loaded once
per process.

Output matches sentence-transformers: mean of the token vectors over the attention mask,
then L2-normalised, so a dot product of two embeddings is their cosine similarity.

This module registers no guard and imports heavy dependencies lazily.
"""

from __future__ import annotations

import threading
from typing import Any

from dwarpal.guards.classifier import fetch_model_file

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
# The model was trained on 128-token pairs and sentence-transformers caps it at 256. Callers
# split long text into sentences, so truncation only bites on a single very long sentence.
MAX_TOKENS = 256

_instances: dict[str, SentenceEmbedder] = {}
_lock = threading.Lock()


def get_embedder(model_id: str = DEFAULT_MODEL, revision: str | None = None) -> SentenceEmbedder:
    """Process-wide singleton per model id and revision."""
    key = f"{model_id}@{revision}"
    with _lock:
        if key not in _instances:
            _instances[key] = SentenceEmbedder(model_id, revision)
        return _instances[key]


class SentenceEmbedder:
    def __init__(self, model_id: str = DEFAULT_MODEL, revision: str | None = None):
        try:
            import numpy as np  # noqa: F401
            import onnxruntime
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError(
                f"the embedder needs the 'ml' extra: run `uv sync --all-extras` ({exc.name})"
            ) from exc

        self.model_id = model_id
        self.tokenizer = Tokenizer.from_file(
            fetch_model_file(hf_hub_download, model_id, "tokenizer.json", revision)
        )
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding()
        options = onnxruntime.SessionOptions()
        # Busy-waiting worker threads starve the other ONNX sessions in the process (PR-04).
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self.session = onnxruntime.InferenceSession(
            fetch_model_file(hf_hub_download, model_id, "model.onnx", revision),
            options,
            providers=["CPUExecutionProvider"],
        )
        self.input_names = {i.name for i in self.session.get_inputs()}

    def embed(self, texts: list[str]) -> Any:
        """(len(texts), dim) array of unit vectors. CPU-bound: call via asyncio.to_thread."""
        import numpy as np

        encodings = self.tokenizer.encode_batch(texts)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feed: dict[str, Any] = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": mask,
        }
        if "token_type_ids" in self.input_names:
            feed["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)
        tokens = self.session.run(None, feed)[0]  # (batch, seq, dim)
        weights = mask[:, :, None].astype(tokens.dtype)
        pooled = (tokens * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
        return pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
