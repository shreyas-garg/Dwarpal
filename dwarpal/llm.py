"""Model calls made by guards themselves, not by the client's request. (PR-06)

Two guards need an LLM: faithfulness asks a judge to label claims, and output_schema asks the
model once to fix broken JSON. Both go through this module so that:

  - they use the same OpenAI-compatible endpoint and key as the upstream (UPSTREAM_*), with
    no second set of credentials to configure;
  - every call reports its token usage, so the guard can put a real `cost_usd` on its result;
  - tests and the benchmark can swap in a fake with set_llm() and never need an API key.

A missing API key is not an error until a guard actually makes a call: the Docker build runs
every guard's setup() without a key, and most requests never reach an LLM guard.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from dwarpal.config import Settings, get_settings
from dwarpal.upstream import UpstreamClient, UpstreamError


class LLMError(Exception):
    """The call could not be made or its reply was unusable. Guards let it propagate, so the
    pipeline applies the policy's on_error."""


@dataclass
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def cost_usd(self, prices: dict[str, float] | None) -> float:
        """List-price cost. prices = {input: USD per 1M tokens, output: USD per 1M tokens}.

        Charged even on a free tier, so "cost per request" stays a meaningful number.
        """
        prices = prices or {}
        return (
            self.prompt_tokens * float(prices.get("input", 0.0))
            + self.completion_tokens * float(prices.get("output", 0.0))
        ) / 1_000_000


class LLM(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], *, model: str | None = None, **params: Any
    ) -> Completion: ...


class LLMClient:
    """Chat completions against the upstream endpoint, returning text + token usage."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key
        self.default_model = default_model
        self._upstream = UpstreamClient(base_url, api_key, timeout_s, transport=transport)

    @classmethod
    def from_settings(
        cls, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> LLMClient:
        return cls(
            settings.upstream_base_url,
            settings.upstream_api_key.get_secret_value(),
            settings.upstream_model,
            settings.upstream_timeout_s,
            transport=transport,
        )

    async def complete(
        self, messages: list[dict[str, Any]], *, model: str | None = None, **params: Any
    ) -> Completion:
        """params are extra request fields: temperature, max_tokens, response_format,
        reasoning_effort, ... (None values are dropped)."""
        if not self.api_key:
            raise LLMError("no API key configured for guard LLM calls (set UPSTREAM_API_KEY)")
        payload = {"model": model or self.default_model, "messages": messages}
        payload.update({k: v for k, v in params.items() if v is not None})
        try:
            body = await self._upstream.chat(payload)
        except UpstreamError as exc:
            raise LLMError(f"LLM call failed with {exc.status_code}: {exc.body}") from exc
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM reply has no message content: {str(body)[:200]}") from exc
        if not isinstance(text, str):
            raise LLMError("LLM reply content is not text")
        usage = body.get("usage") or {}
        return Completion(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    async def aclose(self) -> None:
        await self._upstream.aclose()


# One client per event loop: an httpx client must not be shared across loops, and the app,
# the eval harness and each test run their own.
_override: LLM | None = None
_default: tuple[asyncio.AbstractEventLoop, LLMClient] | None = None


def set_llm(client: LLM | None) -> None:
    """Use this client for every guard call (the app's own, a fake in tests). None resets."""
    global _override
    _override = client


def get_llm() -> LLM:
    global _default
    if _override is not None:
        return _override
    loop = asyncio.get_running_loop()
    if _default is None or _default[0] is not loop:
        _default = (loop, LLMClient.from_settings(get_settings()))
    return _default[1]


def strip_code_fences(text: str) -> str:
    """Remove one surrounding ```json ... ``` fence, which models add even when told not to."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```") and len(stripped) >= 6:
        stripped = stripped[3:-3]
        first_line, _, rest = stripped.partition("\n")
        if first_line.strip().isalpha() or not first_line.strip():  # language tag, e.g. json
            stripped = rest
        stripped = stripped.strip()
    return stripped
