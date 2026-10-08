"""Toxicity scores for the toxicity guard: toxic-bert on CPU. (PR-05)

`Xenova/toxic-bert` is the ONNX export of `unitary/toxic-bert` (the Detoxify "original"
model, trained on the Jigsaw toxic-comment data). The int8 weights are 110 MB against 440 MB
for fp32, which matters on a free-tier Space that already holds three other models.

Multi-label: every label gets its own sigmoid probability, so a reply can be an insult
without being obscene. Same setup as classifier.py: onnxruntime + tokenizers, no torch,
downloaded once, loaded once per process.

This module registers no guard and imports heavy dependencies lazily.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from dwarpal.guards.classifier import fetch_model_file
from dwarpal.guards.windows import token_windows

DEFAULT_MODEL = "Xenova/toxic-bert"
DEFAULT_WEIGHTS = "model_quantized.onnx"
MAX_TOKENS = 512

_instances: dict[str, ToxicityModel] = {}
_lock = threading.Lock()


def get_toxicity_model(
    model_id: str = DEFAULT_MODEL, revision: str | None = None, weights: str = DEFAULT_WEIGHTS
) -> ToxicityModel:
    """Process-wide singleton per model id, revision and weights file."""
    key = f"{model_id}@{revision}/{weights}"
    with _lock:
        if key not in _instances:
            _instances[key] = ToxicityModel(model_id, revision, weights)
        return _instances[key]


class ToxicityModel:
    def __init__(
        self,
        model_id: str = DEFAULT_MODEL,
        revision: str | None = None,
        weights: str = DEFAULT_WEIGHTS,
    ):
        try:
            import numpy as np  # noqa: F401
            import onnxruntime
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError(
                f"toxicity needs the 'ml' extra: run `uv sync --all-extras` ({exc.name})"
            ) from exc

        def fetch(name: str) -> str:
            return fetch_model_file(hf_hub_download, model_id, name, revision)

        self.tokenizer = Tokenizer.from_file(fetch("tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.full_tokenizer = Tokenizer.from_file(fetch("tokenizer.json"))  # for windowing
        self.full_tokenizer.no_truncation()
        options = onnxruntime.SessionOptions()
        # Busy-waiting worker threads starve the other ONNX sessions in the process (PR-04).
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self.session = onnxruntime.InferenceSession(
            fetch(weights), options, providers=["CPUExecutionProvider"]
        )
        self.input_names = {i.name for i in self.session.get_inputs()}
        config = json.loads(Path(fetch("config.json")).read_text())
        self.labels = [config["id2label"][str(i)] for i in range(len(config["id2label"]))]

    def _window(self, text: str) -> Any:
        import numpy as np

        enc = self.tokenizer.encode(text)
        feed: dict[str, Any] = {
            "input_ids": np.array([enc.ids], dtype=np.int64),
            "attention_mask": np.array([enc.attention_mask], dtype=np.int64),
        }
        if "token_type_ids" in self.input_names:
            feed["token_type_ids"] = np.array([enc.type_ids], dtype=np.int64)
        logits = self.session.run(None, feed)[0][0]
        return 1.0 / (1.0 + np.exp(-logits))

    def scores(self, text: str) -> dict[str, float]:
        """{label: probability}. Long text is read in token windows; the worst window wins
        per label. CPU-bound: call via asyncio.to_thread."""
        if not text.strip():
            return dict.fromkeys(self.labels, 0.0)
        probs = [self._window(text[a:b]) for a, b in token_windows(self.full_tokenizer, text)]
        return {label: max(float(p[i]) for p in probs) for i, label in enumerate(self.labels)}
