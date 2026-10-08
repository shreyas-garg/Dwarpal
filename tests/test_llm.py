"""PR-06: the client guards use for their own model calls."""

import httpx
import pytest

from dwarpal.llm import Completion, LLMClient, LLMError, strip_code_fences

OK = {
    "choices": [{"message": {"role": "assistant", "content": '{"ok": true}'}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5},
}


def client(*statuses: int, retries: int = 2, key: str = "k") -> tuple[LLMClient, list]:
    """A client whose upstream answers with these statuses in order, then 200."""
    seen: list[httpx.Request] = []
    queue = list(statuses)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = queue.pop(0) if queue else 200
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "high demand"}})
        return httpx.Response(200, json=OK)

    llm = LLMClient(
        "http://judge/v1/",
        key,
        "judge-model",
        transport=httpx.MockTransport(handle),
        retries=retries,
        backoff_s=0,
    )
    return llm, seen


async def test_reply_text_usage_and_extra_fields():
    llm, seen = client()
    reply = await llm.complete([{"role": "user", "content": "hi"}], temperature=0, top_p=None)
    assert reply == Completion('{"ok": true}', 12, 5)
    body = seen[0].read().decode()
    assert '"model":"judge-model"' in body.replace(" ", "") and "top_p" not in body


async def test_overload_is_retried():
    llm, seen = client(503, 429)
    assert (await llm.complete([])).text == '{"ok": true}'
    assert len(seen) == 3


async def test_gives_up_after_the_retries():
    llm, seen = client(503, 503, 503, retries=2)
    with pytest.raises(LLMError, match="503"):
        await llm.complete([])
    assert len(seen) == 3


async def test_client_errors_are_not_retried():
    llm, seen = client(400)
    with pytest.raises(LLMError, match="400"):
        await llm.complete([])
    assert len(seen) == 1


async def test_no_key_fails_before_any_request():
    llm, seen = client(key="")
    with pytest.raises(LLMError, match="UPSTREAM_API_KEY"):
        await llm.complete([])
    assert seen == []


def test_cost_at_list_price():
    assert Completion("x", 1_000_000, 1_000_000).cost_usd({"input": 0.3, "output": 2.5}) == 2.8
    assert Completion("x", 10, 10).cost_usd(None) == 0.0


@pytest.mark.parametrize(
    "text",
    ['```json\n{"a": 1}\n```', '```\n{"a": 1}\n```', '```{"a": 1}```', '  {"a": 1}  '],
)
def test_code_fences_are_stripped(text):
    assert strip_code_fences(text) == '{"a": 1}'


async def test_rate_limit_waits_as_long_as_gemini_asks():
    llm, _ = client()
    body = [{"error": {"code": 429, "details": [{"retryDelay": "29s"}]}}]
    from dwarpal.upstream import UpstreamError

    assert llm._retry_wait(UpstreamError(429, body), 0) == 29.0
    assert llm._retry_wait(UpstreamError(429, [{"retryDelay": "600s"}]), 0) == 60.0  # capped
    assert llm._retry_wait(UpstreamError(503, body), 1) == 0.0  # backoff_s=0 in these tests


async def test_identical_deterministic_requests_are_sent_once():
    llm, seen = client()
    messages = [{"role": "user", "content": "judge this"}]
    first = await llm.complete(messages, temperature=0)
    again = await llm.complete(messages, temperature=0)
    await llm.complete(messages, temperature=0.7)  # sampled: always sent
    assert first == again and len(seen) == 2


async def test_pacing_spaces_requests_out():
    import time

    llm, seen = client()
    llm.min_interval_s = 0.05
    start = time.monotonic()
    for _ in range(3):
        await llm.complete([], temperature=0.5)
    assert time.monotonic() - start >= 0.1 and len(seen) == 3
