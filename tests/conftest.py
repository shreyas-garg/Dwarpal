"""Shared fixtures. No API key needed: the upstream is the in-process mock."""

from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from dwarpal.app import create_app
from dwarpal.config import Settings
from dwarpal.guards.base import Action, Guard, GuardContext, GuardResult, Stage
from dwarpal.guards.registry import register
from dwarpal.llm import Completion, set_llm
from dwarpal.testing.mock_upstream import MockState
from dwarpal.testing.mock_upstream import app as mock_app

REPO_POLICIES = Path(__file__).resolve().parent.parent / "policies"


# Test-only guards, used to exercise redact / block / error paths in the pipeline.


@register("test_redact_word")
class RedactWordGuard(Guard):
    """Replaces params.word with [REDACTED] in user messages and in the reply."""

    stages = frozenset({Stage.INPUT, Stage.OUTPUT})

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        word = self.policy.params["word"]
        if stage == Stage.INPUT:
            if word not in ctx.user_text():
                return self.allow()
            new = [
                {**m, "content": m["content"].replace(word, "[REDACTED]")}
                if m.get("role") == "user"
                else m
                for m in ctx.messages
            ]
            return self.result(Action.REDACT, 1.0, "word found", redacted_messages=new)
        if word in (ctx.response_text or ""):
            redacted = ctx.response_text.replace(word, "[REDACTED]")
            return self.result(Action.REDACT, 1.0, "word found", redacted_text=redacted)
        return self.allow()


@register("test_block_output")
class BlockOutputGuard(Guard):
    """Blocks replies containing params.word."""

    stages = frozenset({Stage.OUTPUT})

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        hit = self.policy.params["word"] in (ctx.response_text or "")
        return self.decide(1.0 if hit else 0.0, "banned word in reply" if hit else "")


@register("test_crash")
class CrashGuard(Guard):
    stages = frozenset({Stage.INPUT})

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _reset_mock() -> None:
    MockState.reset()


# PR-06: guards that call an LLM (faithfulness, output_schema repair) get this instead.


class FakeLLM:
    """Replies with the queued texts in order (the last one repeats); an Exception is raised.
    Every call is recorded, so tests can check the prompt and parameters."""

    def __init__(self, *replies: str | Exception, prompt_tokens=1000, completion_tokens=200):
        self.replies = list(replies) or ["{}"]
        self.calls: list[dict] = []
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    async def complete(self, messages, *, model=None, **params) -> Completion:
        self.calls.append({"messages": messages, "model": model, **params})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return Completion(reply, self.prompt_tokens, self.completion_tokens)


@pytest.fixture
def fake_llm() -> Iterator[Callable[..., FakeLLM]]:
    """Install a FakeLLM for every guard LLM call. With make_client, call this after the
    client is created: the app installs its own client at startup."""

    def install(*replies: str | Exception, **kw) -> FakeLLM:
        llm = FakeLLM(*replies, **kw)
        set_llm(llm)
        return llm

    yield install
    set_llm(None)


@pytest.fixture
def policy_dir(tmp_path: Path) -> Callable[..., Path]:
    """Write policy YAML strings into a temp dir and return its path."""

    def make(*yamls: str) -> Path:
        for i, text in enumerate(yamls):
            (tmp_path / f"{i:02d}.yaml").write_text(text)
        return tmp_path

    return make


@pytest.fixture
def make_client() -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def make(policy_dir: Path = REPO_POLICIES, **settings_kw) -> TestClient:
        # PR-03 onwards some repo policies load real models in setup(). Unit tests run
        # against max_length unless a test enables other policies explicitly.
        if policy_dir is REPO_POLICIES:
            settings_kw.setdefault("enabled_policies", "max_length")
        settings_kw.setdefault("demo_enabled", False)  # PR-04: tests/test_demo.py turns it on
        settings = Settings(
            _env_file=None,
            upstream_base_url="http://mock/v1/",
            upstream_api_key="test-key",
            policy_dir=policy_dir,
            **settings_kw,
        )
        app = create_app(settings, upstream_transport=httpx.ASGITransport(app=mock_app))
        client = TestClient(app)
        client.__enter__()  # runs the lifespan (loads policies)
        clients.append(client)
        return client

    yield make
    for c in clients:
        c.__exit__(None, None, None)


def chat(client: TestClient, text: str, **extra) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        json={"model": "anything", "messages": [{"role": "user", "content": text}], **extra},
    )
