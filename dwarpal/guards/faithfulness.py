"""Blocks replies that make claims the supplied context does not support. (PR-06)

Runs only when the request supplied context (`dwarpal.context`): the project scope is
"faithful to the documents you gave us", not "true about the world".

An LLM judge (Gemini Flash by default, temperature 0, JSON output) splits the reply into
claims and labels each SUPPORTED / UNSUPPORTED / CONTRADICTED against the context. The prompt
is a versioned file under policies/prompts/; a new prompt is a new file plus a policy version
bump, never an in-place edit (scripts/check_policy_versions.py enforces this).

  score = share of claims that are not SUPPORTED (0 when the reply makes no claims)

With params.block_on_contradiction (default true), any CONTRADICTED claim scores 1.0: one
wrong price buried in an otherwise correct answer is still a wrong price, and averaging would
dilute it below the threshold.

A judge that fails (no key, timeout, unparsable output) raises, so the policy's on_error
decides. The policy fails open: see docs/decisions/0007.

Policy params:
  prompt:                  path under policies/, e.g. prompts/faithfulness_judge.v1.txt
  block_on_contradiction:  bool, default true
  max_context_chars:       context sent to the judge is cut to this, default 12000
  judge:                   {model, temperature, max_tokens, extra: {...},
                            price_per_1m_tokens: {input, output}}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dwarpal.guards.base import Guard, GuardContext, GuardResult, Stage
from dwarpal.guards.registry import register
from dwarpal.llm import LLMError, get_llm, strip_code_fences

POLICY_ROOT = Path(__file__).resolve().parent.parent.parent / "policies"
PLACEHOLDERS = ("{{context}}", "{{question}}", "{{reply}}")
LABELS = ("SUPPORTED", "UNSUPPORTED", "CONTRADICTED")

# Structured output: the judge must return exactly this shape.
JUDGE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "faithfulness_verdict",
        "schema": {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {"type": "string"},
                            "label": {"type": "string", "enum": list(LABELS)},
                            "evidence": {"type": "string"},
                        },
                        "required": ["claim", "label"],
                    },
                }
            },
            "required": ["claims"],
        },
    },
}


def load_prompt(relative: str) -> str:
    path = Path(relative)
    if not path.is_absolute():
        path = POLICY_ROOT / path
    text = path.read_text(encoding="utf-8")
    missing = [p for p in PLACEHOLDERS if p not in text]
    if missing:
        raise ValueError(f"faithfulness: prompt {path} lacks placeholder(s) {missing}")
    return text


def parse_verdict(text: str) -> list[dict[str, str]]:
    """The judge's claims, validated. Raises LLMError on anything malformed."""
    try:
        data = json.loads(strip_code_fences(text))
    except ValueError as exc:
        raise LLMError(f"judge output is not JSON: {text[:120]!r}") from exc
    claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(claims, list):
        raise LLMError(f"judge output has no claims list: {text[:120]!r}")
    verdicts = []
    for item in claims:
        label = str((item or {}).get("label", "")).strip().upper() if isinstance(item, dict) else ""
        if label not in LABELS:
            raise LLMError(f"judge gave an unknown label: {item!r}")
        verdicts.append({"claim": str(item.get("claim", "")).strip(), "label": label})
    return verdicts


@register("faithfulness")
class FaithfulnessGuard(Guard):
    stages = frozenset({Stage.OUTPUT})
    cacheable = True

    def __init__(self, policy):
        super().__init__(policy)
        params = policy.params
        if not params.get("prompt"):
            raise ValueError("faithfulness: params.prompt is required")
        self.prompt = load_prompt(params["prompt"])  # fails at startup, not mid-request
        self.block_on_contradiction = bool(params.get("block_on_contradiction", True))
        self.max_context_chars = int(params.get("max_context_chars", 12000))
        self.judge: dict[str, Any] = params.get("judge") or {}

    def build_prompt(self, ctx: GuardContext) -> str:
        docs = "\n\n".join(f"[{i}] {d.strip()}" for i, d in enumerate(ctx.context_docs, 1))
        return (
            self.prompt.replace("{{context}}", docs[: self.max_context_chars])
            .replace("{{question}}", ctx.last_user_text())
            .replace("{{reply}}", ctx.response_text or "")
        )

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        if not any(d.strip() for d in ctx.context_docs):
            return self.allow(reason="no context supplied")
        if not (ctx.response_text or "").strip():
            return self.allow(reason="empty reply")

        judge = self.judge
        completion = await get_llm().complete(
            [{"role": "user", "content": self.build_prompt(ctx)}],
            model=judge.get("model"),
            temperature=judge.get("temperature", 0),
            max_tokens=judge.get("max_tokens"),
            response_format=JUDGE_FORMAT,
            **(judge.get("extra") or {}),
        )
        verdicts = parse_verdict(completion.text)
        result = self._score(verdicts)
        # Set here, not through decide(): an allowed reply's judge call cost money too.
        result.cost_usd = completion.cost_usd(judge.get("price_per_1m_tokens"))
        return result

    def _score(self, verdicts: list[dict[str, str]]) -> GuardResult:
        if not verdicts:
            return self.allow(reason="no factual claims")
        bad = [v for v in verdicts if v["label"] != "SUPPORTED"]
        score = len(bad) / len(verdicts)
        if self.block_on_contradiction and any(v["label"] == "CONTRADICTED" for v in bad):
            score = 1.0
        reason = f"{len(bad)}/{len(verdicts)} claims not supported"
        if bad:
            shown = "; ".join(f"{v['label'].lower()}: {v['claim'][:80]}" for v in bad[:3])
            reason += f" ({shown})"
        return self.decide(score, reason=reason)
