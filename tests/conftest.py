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
