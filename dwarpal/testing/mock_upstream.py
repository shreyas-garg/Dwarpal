"""A fake OpenAI-compatible LLM for tests, local dev without an API key, and load tests.

Run it:   make mock          (listens on :9000)
Point Dwarpal at it:   UPSTREAM_BASE_URL=http://localhost:9000/v1/

What it replies:
  1. body["mock_response"] if the request has it (send via extra_body={"mock_response": ...})
  2. otherwise $MOCK_RESPONSE if set
  3. otherwise "Mock answer to: <last user message>"
It waits $MOCK_LATENCY_MS milliseconds (default 0) before answering, to imitate a real model.
"""

import asyncio
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI

from dwarpal.guards.base import message_text

app = FastAPI(title="Dwarpal mock upstream")


class MockState:
    """Inspectable from tests."""

    calls: int = 0
    last_payload: dict[str, Any] | None = None

    @classmethod
    def reset(cls) -> None:
        cls.calls = 0
        cls.last_payload = None


@app.post("/v1/chat/completions")
async def chat(body: dict[str, Any]) -> dict[str, Any]:
    MockState.calls += 1
    MockState.last_payload = body

    latency_ms = float(os.environ.get("MOCK_LATENCY_MS", "0"))
    if latency_ms > 0:
        await asyncio.sleep(latency_ms / 1000)

    messages = body.get("messages") or []
    users = [m for m in messages if m.get("role") == "user"]
    last_user = message_text(users[-1]) if users else ""
    text = (
        body.get("mock_response")
        or os.environ.get("MOCK_RESPONSE")
        or (f"Mock answer to: {last_user}")
    )

    prompt_tokens = sum(len(message_text(m)) for m in messages) // 4
    completion_tokens = len(text) // 4
    return {
        "id": f"chatcmpl-mock-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "mock-model"),
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
