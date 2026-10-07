"""Layer 2 for the input-attack guards: a pretrained prompt-injection classifier on CPU.

Uses the ONNX weights that ship inside the model repo, so the `ml` extra needs only
onnxruntime + tokenizers — no torch, which keeps the install and the Docker image small.
The model is downloaded once (Hugging Face cache) and loaded once per process; the
prompt_injection and jailbreak guards share the same instance.

No model training happens anywhere here — inference over published weights only, which the
project scope allows.

This module registers no guard and imports its heavy dependencies lazily, so the registry's
module discovery works on a core-only install.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

log = logging.getLogger("dwarpal.guards.classifier")

DEFAULT_MODEL = "protectai/deberta-v3-base-prompt-injection-v2"

# The model reads 512 tokens. Long inputs are scored in overlapping character windows and the
# worst window wins, so an attack sentence buried at the end of a pasted blob is still seen.
MAX_TOKENS = 512
WINDOW_CHARS = 1500
WINDOW_OVERLAP = 200

_instances: dict[str, InjectionClassifier] = {}
_lock = threading.Lock()


def get_classifier(model_id: str = DEFAULT_MODEL) -> InjectionClassifier:
    """Process-wide singleton per model id. Safe to call from several guards' setup()."""
    with _lock:
        if model_id not in _instances:
            _instances[model_id] = InjectionClassifier(model_id)
        return _instances[model_id]


class InjectionClassifier:
    def __init__(self, model_id: str = DEFAULT_MODEL):
        try:
            import numpy as np  # noqa: F401
            import onnxruntime
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError(
                "the classifier needs the 'ml' extra: run `uv sync --all-extras` "
                f"(missing: {exc.name})"
            ) from exc

        self.model_id = model_id
        model_path = hf_hub_download(model_id, "onnx/model.onnx")
        tokenizer_path = hf_hub_download(model_id, "onnx/tokenizer.json")
        config_path = hf_hub_download(model_id, "onnx/config.json")

        self.tokenizer = Tokenizer.from_file(tokenizer_path)
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.session = onnxruntime.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.session.get_inputs()}

        config = json.loads(open(config_path).read())
        id2label = {int(k): v for k, v in config["id2label"].items()}
        bad = [i for i, label in id2label.items() if label.upper() != "SAFE"]
        if len(bad) != 1:
            raise RuntimeError(f"{model_id}: expected one non-SAFE label, got {id2label}")
        self.attack_index = bad[0]
        log.info("loaded %s (attack label: %s)", model_id, id2label[self.attack_index])

    def _windows(self, text: str) -> list[str]:
        if len(text) <= WINDOW_CHARS:
            return [text]
        step = WINDOW_CHARS - WINDOW_OVERLAP
        return [text[i : i + WINDOW_CHARS] for i in range(0, len(text), step)]

    def _score_window(self, text: str) -> float:
        import numpy as np

        encoding = self.tokenizer.encode(text)
        feed: dict[str, Any] = {
            "input_ids": np.array([encoding.ids], dtype=np.int64),
            "attention_mask": np.array([encoding.attention_mask], dtype=np.int64),
        }
        if "token_type_ids" in self.input_names:
            feed["token_type_ids"] = np.array([encoding.type_ids], dtype=np.int64)
        logits = self.session.run(None, feed)[0][0]
        exp = np.exp(logits - logits.max())
        return float(exp[self.attack_index] / exp.sum())

    def score(self, text: str) -> float:
        """P(attack) in 0..1 for one text. CPU-bound: call via asyncio.to_thread."""
        if not text.strip():
            return 0.0
        return max(self._score_window(w) for w in self._windows(text))
