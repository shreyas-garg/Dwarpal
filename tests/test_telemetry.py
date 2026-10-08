"""PR-07: request log, cost per request, admin endpoints, Langfuse spans."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from dwarpal.app import create_app
from dwarpal.config import Settings
from dwarpal.guards.base import Action, Guard, GuardContext, GuardResult, Stage
from dwarpal.guards.registry import register
from dwarpal.telemetry import Telemetry, parse_window
from dwarpal.telemetry.costs import Pricing, billed_tokens
from dwarpal.telemetry.langfuse import LangfuseExporter
from dwarpal.telemetry.store import RequestStore
from dwarpal.testing.mock_upstream import app as mock_app
from tests.conftest import chat
from tests.test_proxy import REDACT
from tests.test_shadow_mode import ENFORCE_MAX_LENGTH, SHADOW_MAX_LENGTH

ADMIN = {"X-Admin-Token": "s3cret"}
REPO_PRICING = Path(__file__).resolve().parent.parent / "config" / "pricing.yaml"

PRICED = """
name: priced
version: 1.0.0
guard: test_priced
stages: [output]
"""


@register("test_priced")
class PricedGuard(Guard):
    """Allows every reply, as if it had paid for one model call to decide."""

    stages = frozenset({Stage.OUTPUT})

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        return self.result(Action.ALLOW, cost_usd=0.002)


def logged(client: TestClient) -> list[dict]:
    resp = client.get("/v1/dwarpal/requests", headers=ADMIN)
    assert resp.status_code == 200
    return resp.json()["requests"]


def test_request_is_logged_with_guard_times_and_no_prompt(make_client, tmp_path):
    client = make_client(admin_token="s3cret", pricing_file=REPO_PRICING)
    text = "What is the refund window for annual plans?"
    resp = chat(client, text)

    (row,) = logged(client)
    assert row["request_id"] == resp.headers["X-Dwarpal-Request-Id"]
    assert row["policies"] == ["max_length@1.0.0"]
    (guard,) = row["guards"]
    assert guard == {**guard, "guard": "max_length", "stage": "input", "action": "allow"}
    assert guard["latency_ms"] == resp.json()["dwarpal"]["results"][0]["latency_ms"]
    assert row["total_ms"] >= row["upstream_ms"] > 0
    assert row["added_ms"] == pytest.approx(row["total_ms"] - row["upstream_ms"], abs=0.002)
    header = float(resp.headers["X-Dwarpal-Added-Latency-Ms"])
    assert row["added_ms"] == pytest.approx(header, abs=0.051)
    # Hashed, not stored: the text is nowhere in the database files.
    assert row["prompt"] is None
    assert row["prompt_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    database = b"".join(p.read_bytes() for p in tmp_path.glob("requests.db*"))
    assert text.encode() not in database


def test_cost_is_upstream_usage_at_list_price_plus_guard_calls(make_client, policy_dir, tmp_path):
    prices = tmp_path / "config" / "pricing.yaml"  # not next to the policies in tmp_path
    prices.parent.mkdir()
    prices.write_text("models:\n  gemini-2.5-flash: {input: 1.0, output: 10.0}\n")
    client = make_client(policy_dir(PRICED), admin_token="s3cret", pricing_file=prices)
    usage = chat(client, "How do I export invoices?").json()["usage"]

    (row,) = logged(client)
    assert row["model"] == "gemini-2.5-flash"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (
        usage["prompt_tokens"],
        usage["completion_tokens"],
    )
    upstream = (usage["prompt_tokens"] * 1.0 + usage["completion_tokens"] * 10.0) / 1_000_000
    assert row["upstream_cost_usd"] == pytest.approx(upstream)
    assert row["guard_cost_usd"] == pytest.approx(0.002)
    assert row["cost_usd"] == pytest.approx(upstream + 0.002)


def test_log_prompts_keeps_the_redacted_text_only(make_client, policy_dir):
    client = make_client(policy_dir(REDACT), admin_token="s3cret", log_prompts=True)
    chat(client, "my password is hunter2")
    (row,) = logged(client)
    assert row["prompt"] == "my password is [REDACTED]"


def test_admin_endpoints_need_the_admin_token(make_client):
    off = make_client()
    assert off.get("/v1/dwarpal/stats", headers=ADMIN).status_code == 403
    assert off.get("/v1/dwarpal/requests", headers=ADMIN).status_code == 403

    client = make_client(admin_token="s3cret", api_keys="client-key")
    for path in ("/v1/dwarpal/stats", "/v1/dwarpal/requests"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-Admin-Token": "wrong"}).status_code == 401
        # A client API key is not an admin token.
        assert client.get(path, headers={"Authorization": "Bearer client-key"}).status_code == 401
        assert client.get(path, headers=ADMIN).status_code == 200
    assert client.get("/v1/dwarpal/stats?window=forever", headers=ADMIN).status_code == 400


def test_stats_latency_block_rate_cost_and_policies(make_client):
    client = make_client(admin_token="s3cret", pricing_file=REPO_PRICING)
    chat(client, "hello")
    chat(client, "how do I add my GSTIN?")
    chat(client, "x" * 5000)  # max_length blocks it

    stats = client.get("/v1/dwarpal/stats?window=1h", headers=ADMIN).json()
    assert stats["window"] == "1h"
    assert (stats["requests"], stats["blocked"]) == (3, 1)
    assert stats["block_rate"] == pytest.approx(1 / 3, abs=1e-4)
    guard = stats["guards"]["max_length@1.0.0"]
    assert (guard["runs"], guard["blocks"], guard["errors"]) == (3, 1, 0)
    assert guard["block_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert guard["latency_ms"]["p99"] >= guard["latency_ms"]["p50"] >= 0
    added = stats["latency_ms"]["added"]
    assert added["p99"] >= added["p95"] >= added["p50"] >= 0
    assert stats["cost_usd"]["mean_per_request"] > 0  # two requests reached the model
    assert stats["active_policies"] == [
        {"ref": "max_length@1.0.0", "mode": "enforce", "tier": "cheap", "stages": ["input"]}
    ]
    assert len(logged(client)) == 3


def test_shadow_versus_enforce_disagreements(make_client, policy_dir):
    client = make_client(policy_dir(ENFORCE_MAX_LENGTH, SHADOW_MAX_LENGTH), admin_token="s3cret")
    chat(client, "hi")  # both versions allow
    differ = chat(client, "short, but longer than ten characters")  # only the shadow would block
    chat(client, "x" * 5000)  # enforce blocks first, so the shadow version never runs

    (pair,) = client.get("/v1/dwarpal/stats", headers=ADMIN).json()["shadow"]
    assert pair == {
        "shadow": "max_length@1.1.0",
        "enforce": "max_length@1.0.0",
        "compared": 2,
        "disagreements": 1,
        "example_request_ids": [differ.headers["X-Dwarpal-Request-Id"]],
    }


def test_queued_rows_are_written_on_shutdown(tmp_path):
    settings = Settings(
        _env_file=None,
        upstream_base_url="http://mock/v1/",
        enabled_policies="max_length",
        demo_enabled=False,
    )
    with TestClient(create_app(settings, httpx.ASGITransport(app=mock_app))) as client:
        chat(client, "hello")
        chat(client, "hello again")
    assert len(RequestStore(tmp_path / "requests.db").recent(10)) == 2


def test_langfuse_trace_has_a_span_per_guard_and_one_for_the_upstream_call():
    memory = InMemorySpanExporter()
    langfuse = LangfuseExporter("https://langfuse.example", "pk", "sk", exporter=memory)
    guard = {"score": 0.0, "error": False, "shadow": False, "cached": False, "cost_usd": 0.0}
    row = {
        "request_id": "abc123",
        "ts": 1_760_000_000.0,
        "policies": ["prompt_injection@1.1.1", "pii@1.0.1", "toxicity@1.1.0"],
        "guards": [
            {
                **guard,
                "guard": "prompt_injection",
                "version": "1.1.1",
                "stage": "input",
                "action": "allow",
                "latency_ms": 25.5,
            },
            {
                **guard,
                "guard": "pii",
                "version": "1.0.1",
                "stage": "input",
                "action": "redact",
                "latency_ms": 1.5,
            },
            {
                **guard,
                "guard": "toxicity",
                "version": "1.1.0",
                "stage": "output",
                "action": "allow",
                "latency_ms": 20.0,
            },
        ],
        "blocked_by": None,
        "blocked_stage": None,
        "model": "gemini-2.5-flash",
        "prompt_tokens": 700,
        "completion_tokens": 120,
        "upstream_ms": 300.0,
        "total_ms": 350.0,
        "added_ms": 50.0,
        "upstream_cost_usd": 0.00051,
        "guard_cost_usd": 0.0,
        "cost_usd": 0.00051,
        "prompt_sha256": "f" * 64,
        "prompt": None,
    }
    langfuse.export(row)
    langfuse.flush()

    spans = {s.name: s for s in memory.get_finished_spans()}
    assert set(spans) == {
        "dwarpal.request",
        "prompt_injection@1.1.1",
        "pii@1.0.1",
        "upstream",
        "toxicity@1.1.0",
    }
    root = spans["dwarpal.request"]
    for span in spans.values():
        assert span.context.trace_id == root.context.trace_id
        assert span.attributes["langfuse.trace.tags"] == tuple(row["policies"])
        assert span.attributes["langfuse.trace.metadata.request_id"] == "abc123"
        if span is not root:
            assert span.parent.span_id == root.context.span_id

    def duration_ms(name: str) -> float:
        return (spans[name].end_time - spans[name].start_time) / 1_000_000

    assert duration_ms("dwarpal.request") == 350.0
    assert duration_ms("pii@1.0.1") == 1.5
    assert duration_ms("upstream") == 300.0
    assert spans["pii@1.0.1"].attributes["langfuse.observation.type"] == "guardrail"
    assert spans["pii@1.0.1"].attributes["langfuse.observation.level"] == "WARNING"  # it redacted
    assert spans["pii@1.0.1"].attributes["langfuse.version"] == "1.0.1"
    upstream = spans["upstream"].attributes
    assert upstream["langfuse.observation.type"] == "generation"
    assert upstream["langfuse.observation.model.name"] == "gemini-2.5-flash"
    assert json.loads(upstream["langfuse.observation.usage_details"]) == {
        "input": 700,
        "output": 120,
        "total": 820,
    }
    # Input guards start with the request (they run side by side); output guards start after
    # the upstream call and the slowest one ends with the request. Nothing sticks out of the
    # root, so Langfuse's trace latency is the request's total.
    assert spans["pii@1.0.1"].start_time == spans["prompt_injection@1.1.1"].start_time
    assert spans["pii@1.0.1"].start_time == root.start_time
    assert spans["toxicity@1.1.0"].end_time == root.end_time
    assert spans["upstream"].end_time == spans["toxicity@1.1.0"].start_time
    for span in spans.values():
        assert root.start_time <= span.start_time <= span.end_time <= root.end_time
    langfuse.shutdown()


def test_langfuse_is_off_without_both_keys(tmp_path):
    def telemetry(**kw) -> Telemetry:
        return Telemetry.from_settings(
            Settings(_env_file=None, request_db=tmp_path / "requests.db", **kw)
        )

    assert telemetry().langfuse is None
    assert telemetry(langfuse_public_key="pk-lf-1").langfuse is None
    with_keys = telemetry(langfuse_public_key="pk-lf-1", langfuse_secret_key="sk-lf-1")
    assert with_keys.langfuse is not None
    with_keys.langfuse.shutdown()


def test_thinking_tokens_are_billed_as_output():
    # Gemini's OpenAI layer may count thinking tokens only in total_tokens.
    thinking = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 520}
    plain = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    assert billed_tokens(thinking) == (100, 420)
    assert billed_tokens(plain) == (100, 20)
    assert billed_tokens(None) == (0, 0)


def test_pricing_table():
    pricing = Pricing.load(REPO_PRICING)
    # $0.30 / $2.50 per 1M tokens (config/pricing.yaml)
    assert pricing.cost("gemini-2.5-flash", 1_000_000, 1_000_000) == pytest.approx(2.80)
    assert pricing.cost("models/gemini-2.5-flash", 1000, 0) == pytest.approx(0.0003)
    assert pricing.cost("some-unpriced-model", 1000, 1000) == 0.0
    assert pricing.cost(None, 1000, 1000) == 0.0
    assert Pricing.load(Path("does/not/exist.yaml")).cost("gemini-2.5-flash", 10, 10) == 0.0


def test_parse_window():
    assert parse_window("15m") == 900
    assert parse_window("1h") == 3600
    assert parse_window("7d") == 7 * 86400
    for bad in ("", "1", "0h", "1w", "-1h", "forever"):
        with pytest.raises(ValueError):
            parse_window(bad)
