"""PR-06: tiered / sequential / parallel scheduling, timeouts and the decision cache."""

import asyncio
import time

import httpx
import pytest

from dwarpal.guards.base import Action, Guard, GuardContext, GuardResult, Stage
from dwarpal.guards.registry import register
from dwarpal.pipeline import Pipeline, ResultCache
from dwarpal.policy import Policy
from dwarpal.testing.mock_upstream import MockState
from dwarpal.testing.mock_upstream import app as mock_app
from dwarpal.upstream import UpstreamClient

SPANS: dict[str, tuple[float, float]] = {}  # policy name -> (start, end), perf_counter seconds


@register("test_sleep")
class SleepGuard(Guard):
    """Sleeps params.ms, records when it ran, then returns params.verdict."""

    stages = frozenset({Stage.INPUT, Stage.OUTPUT})

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        start = time.perf_counter()
        await asyncio.sleep(self.policy.params.get("ms", 0) / 1000)
        SPANS[self.policy.name] = (start, time.perf_counter())
        verdict = self.policy.params.get("verdict", "allow")
        return self.result(Action(verdict), 1.0 if verdict == "block" else 0.0)


@register("test_counting")
class CountingGuard(Guard):
    """Cacheable; counts how often check() really runs. params.fail_first raises once."""

    stages = frozenset({Stage.INPUT})
    cacheable = True
    calls = 0

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        type(self).calls += 1
        if self.policy.params.get("fail_first") and type(self).calls == 1:
            raise RuntimeError("transient")
        return self.allow(reason=ctx.user_text())


def policy(name: str, guard: str = "test_sleep", tier: str = "cheap", **fields) -> Policy:
    return Policy.model_validate(
        {
            "name": name,
            "version": "1.0.0",
            "guard": guard,
            "stages": fields.pop("stages", ["input"]),
            "tier": tier,
            **fields,
        }
    )


def pipeline(*policies: Policy, strategy="tiered", cache_size=0) -> Pipeline:
    upstream = UpstreamClient("http://mock/v1/", transport=httpx.ASGITransport(app=mock_app))
    return Pipeline(list(policies), upstream, strategy=strategy, cache_size=cache_size)


async def run(p: Pipeline, text: str = "hello", **extra):
    payload = {"model": "m", "messages": [{"role": "user", "content": text}], **extra}
    return await p.run(payload, "req-1")


def overlapped(a: str, b: str) -> bool:
    return SPANS[a][0] < SPANS[b][1] and SPANS[b][0] < SPANS[a][1]


@pytest.fixture(autouse=True)
def _reset():
    SPANS.clear()
    CountingGuard.calls = 0


TIERED = [
    policy("llm1", tier="llm", params={"ms": 10}),
    policy("model1", tier="model", params={"ms": 150}),
    policy("cheap1", tier="cheap", params={"ms": 1}),
    policy("model2", tier="model", params={"ms": 150}),
]


async def test_tiers_run_cheap_then_model_together_then_llm():
    trace = await run(pipeline(*TIERED))

    # Trace order is tier order; within a tier, policy-file order.
    assert [r.guard for r in trace.results] == ["cheap1", "model1", "model2", "llm1"]
    assert overlapped("model1", "model2")  # side by side, not 300 ms in a row
    assert SPANS["cheap1"][1] <= SPANS["model1"][0]
    assert SPANS["llm1"][0] >= max(SPANS["model1"][1], SPANS["model2"][1])


async def test_sequential_runs_in_file_order_one_at_a_time():
    trace = await run(pipeline(*TIERED, strategy="sequential"))
    assert [r.guard for r in trace.results] == ["llm1", "model1", "cheap1", "model2"]
    assert not overlapped("model1", "model2")


async def test_parallel_runs_everything_at_once():
    await run(pipeline(*TIERED, strategy="parallel"))
    assert overlapped("llm1", "model1") and overlapped("cheap1", "model2")


async def test_cheap_block_skips_the_expensive_tiers():
    cheap_block = policy("cheap1", tier="cheap", params={"verdict": "block"})
    trace = await run(pipeline(cheap_block, *TIERED[:2], TIERED[3]))
    assert trace.blocked_by == "cheap1@1.0.0"
    assert [r.guard for r in trace.results] == ["cheap1"]
    assert MockState.calls == 0


async def test_model_block_skips_the_llm_tier():
    model_block = policy("model2", tier="model", params={"ms": 5, "verdict": "block"})
    trace = await run(pipeline(TIERED[0], TIERED[1], TIERED[2], model_block))
    assert trace.blocked_by == "model2@1.0.0"
    assert "llm1" not in [r.guard for r in trace.results]
    # The other model-tier guard ran alongside, so it stays in the trace.
    assert "model1" in [r.guard for r in trace.results]


async def test_first_blocker_in_file_order_wins_inside_a_concurrent_tier():
    slow_block = policy("a_slow", tier="model", params={"ms": 80, "verdict": "block"})
    fast_block = policy("b_fast", tier="model", params={"ms": 1, "verdict": "block"})
    trace = await run(pipeline(slow_block, fast_block))
    assert trace.blocked_by == "a_slow@1.0.0"  # same answer the sequential pipeline gives


@pytest.mark.parametrize("on_error, blocked", [("fail_closed", True), ("fail_open", False)])
async def test_timeout_is_decided_by_on_error(on_error, blocked):
    slow = policy("slow", params={"ms": 500}, timeout_ms=30, on_error=on_error)
    start = time.perf_counter()
    trace = await run(pipeline(slow))
    assert time.perf_counter() - start < 0.3  # did not wait for the guard
    (result,) = trace.results
    assert result.error and "timed out after 30 ms" in result.reason
    assert trace.blocked is blocked


@pytest.mark.parametrize("strategy", ["tiered", "sequential", "parallel"])
async def test_every_strategy_leaves_the_same_text(strategy):
    """Two redactors that run side by side must both apply, as they do one after another."""
    redactors = [
        policy("r1", "test_redact_word", tier="model", action="redact", params={"word": "alpha"}),
        policy("r2", "test_redact_word", tier="model", action="redact", params={"word": "beta"}),
    ]
    await run(pipeline(*redactors, strategy=strategy), "alpha and beta")
    assert MockState.last_payload["messages"][0]["content"] == "[REDACTED] and [REDACTED]"


async def test_identical_input_reuses_the_decision():
    p = pipeline(policy("counted", "test_counting"), cache_size=8)
    first = await run(p, "same text")
    second = await run(p, "same text")
    await run(p, "different text")
    assert CountingGuard.calls == 2
    assert not first.results[0].cached and second.results[0].cached
    assert second.results[0].reason == "same text"


async def test_cache_is_off_by_default_and_skips_errors():
    no_cache = pipeline(policy("counted", "test_counting"))
    await run(no_cache)
    await run(no_cache)
    assert CountingGuard.calls == 2

    CountingGuard.calls = 0
    flaky = pipeline(policy("flaky", "test_counting", params={"fail_first": True}), cache_size=8)
    assert (await run(flaky)).results[0].error  # fail_closed by default
    assert not (await run(flaky)).results[0].error  # retried, not replayed
    assert CountingGuard.calls == 2


def test_result_cache_evicts_least_recently_used():
    cache = ResultCache(2)
    a, b, c = (GuardResult("g", "1.0.0", Action.ALLOW, reason=x) for x in "abc")
    cache.put("a", a)
    cache.put("b", b)
    cache.get("a")  # a is now the most recent
    cache.put("c", c)
    assert cache.get("b") is None and cache.get("a") is a and len(cache) == 2


def test_settings_reach_the_app(make_client):
    client = make_client(pipeline_strategy="sequential", guard_cache_size=0)
    state = client.app.state.pipeline
    assert state.strategy == "sequential" and state.cache is None
    (row,) = client.get("/v1/dwarpal/policies").json()["policies"]
    assert row["on_error"] == "fail_closed" and row["timeout_ms"] == 50
