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


def get_classifier(
    model_id: str = DEFAULT_MODEL, revision: str | None = None
) -> InjectionClassifier:
    """Process-wide singleton per model id. Safe to call from several guards' setup()."""
    key = f"{model_id}@{revision}"
    with _lock:
        if key not in _instances:
            _instances[key] = InjectionClassifier(model_id, revision)
        return _instances[key]


def fetch_model_file(download, model_id: str, name: str, revision: str | None) -> str:
    """PR-04: repos keep the tokenizer and config next to the ONNX file or at the root
    (Horizon-Labs does the latter). A pinned revision keeps evals reproducible."""
    from huggingface_hub.utils import EntryNotFoundError, LocalEntryNotFoundError

    try:
        return download(model_id, f"onnx/{name}", revision=revision)
    except (EntryNotFoundError, LocalEntryNotFoundError):  # online 404, or offline cache miss
        return download(model_id, name, revision=revision)


class InjectionClassifier:
    def __init__(self, model_id: str = DEFAULT_MODEL, revision: str | None = None):
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
        model_path = fetch_model_file(hf_hub_download, model_id, "model.onnx", revision)
        tokenizer_path = fetch_model_file(hf_hub_download, model_id, "tokenizer.json", revision)
        config_path = fetch_model_file(hf_hub_download, model_id, "config.json", revision)

        self.tokenizer = Tokenizer.from_file(tokenizer_path)
        self.tokenizer.enable_truncation(MAX_TOKENS)
        options = onnxruntime.SessionOptions()
        # PR-04: idle worker threads busy-wait by default and starve other ONNX sessions in the
        # process (the pii name model); with both loaded, inference ran ~4x slower.
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self.session = onnxruntime.InferenceSession(
            model_path, options, providers=["CPUExecutionProvider"]
        )
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
