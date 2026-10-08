"""Runs input guards, calls the upstream model, then runs output guards.

The first BLOCK stops the request; a REDACT replaces the text and later guards see the
redacted version. (PR-07 adds telemetry.)

PR-06: how a stage's guards are scheduled, chosen by PIPELINE_STRATEGY (see docs/tradeoffs.md):

  tiered      the default. By each policy's `tier`:
                cheap  one after another, in policy-file order; regexes and schema checks
                model  all at the same time (CPU models run in threads)
                llm    one after another, last, so a block upstream of them saves the call
              stopping at the first tier that blocks.
  sequential  every guard one after another in policy-file order (the PR-01 behaviour).
  parallel    every guard of the stage at the same time.

Every guard also gets its policy's `timeout_ms`; a guard that times out or raises is decided
by the policy's `on_error`. Guards marked `cacheable` have their decisions reused for an
identical input under the same policy version.

Concurrent guards all see the text as it was when their batch started. Their results are then
applied in policy-file order, exactly as if they had run one after another, with one fix-up:
when a second guard in the same batch redacts, it is re-run on the already-redacted text, so
the text that leaves a stage is the same whichever strategy ran.
"""

import asyncio
import dataclasses
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from dwarpal.guards.base import Action, Guard, GuardContext, GuardResult, Stage
from dwarpal.guards.registry import get_guard_class
from dwarpal.policy import Policy
from dwarpal.upstream import UpstreamClient

log = logging.getLogger("dwarpal.pipeline")

Strategy = Literal["tiered", "sequential", "parallel"]
TIERS = ("cheap", "model", "llm")
CONCURRENT_TIERS = {"model"}


def _ms_since(start: float) -> float:
    return (time.perf_counter() - start) * 1000


@dataclass
class PipelineTrace:
    request_id: str
    policies: list[str]  # refs of every active policy, e.g. ["max_length@1.0.0"]
    results: list[GuardResult] = field(default_factory=list)
    blocked_by: str | None = None  # policy ref that blocked, if any
    blocked_stage: Stage | None = None
    response: dict[str, Any] | None = None  # upstream response (after output redaction)
    upstream_latency_ms: float = 0.0
    total_latency_ms: float = 0.0

    @property
    def blocked(self) -> bool:
        return self.blocked_by is not None

    @property
    def added_latency_ms(self) -> float:
        """Time Dwarpal added on top of the upstream call."""
        return max(0.0, self.total_latency_ms - self.upstream_latency_ms)

    @property
    def guard_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "policies": self.policies,
            "blocked_by": self.blocked_by,
            "blocked_stage": self.blocked_stage.value if self.blocked_stage else None,
            "results": [r.to_dict() for r in self.results],
            "upstream_latency_ms": round(self.upstream_latency_ms, 3),
            "total_latency_ms": round(self.total_latency_ms, 3),
            "added_latency_ms": round(self.added_latency_ms, 3),
            "guard_cost_usd": self.guard_cost_usd,
        }


def response_text(response: dict[str, Any]) -> str | None:
    try:
        content = response["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return content if isinstance(content, str) else None


def _set_response_text(response: dict[str, Any], text: str) -> None:
    response["choices"][0]["message"]["content"] = text


# PR-06: the schema a reply must match. dwarpal.response_schema wins; otherwise the OpenAI
# response_format the client already sends upstream is enforced on the way back too.
def schema_from_response_format(response_format: Any) -> dict[str, Any] | None:
    if not isinstance(response_format, dict):
        return None
    if response_format.get("type") == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
        return schema if isinstance(schema, dict) else None
    if response_format.get("type") == "json_object":
        return {"type": "object"}
    return None


class ResultCache:
    """Bounded LRU of guard decisions, keyed by sha256(policy ref, stage, guard input)."""

    def __init__(self, max_entries: int):
        self.max_entries = max_entries
        self._entries: OrderedDict[str, GuardResult] = OrderedDict()

    @staticmethod
    def key(policy: Policy, stage: Stage, ctx: GuardContext) -> str:
        material = json.dumps(
            [
                policy.ref,
                stage.value,
                ctx.messages,
                ctx.response_text if stage == Stage.OUTPUT else None,
                ctx.context_docs,
                ctx.response_schema,
            ],
            sort_keys=True,
            default=str,
            ensure_ascii=False,
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def get(self, key: str) -> GuardResult | None:
        result = self._entries.get(key)
        if result is not None:
            self._entries.move_to_end(key)
        return result

    def put(self, key: str, result: GuardResult) -> None:
        self._entries[key] = result
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


class _Outcome(Enum):
    CONTINUE = "continue"
    REDACTED = "redacted"
    BLOCKED = "blocked"


class Pipeline:
    def __init__(
        self,
        policies: list[Policy],
        upstream: UpstreamClient,
        strategy: Strategy = "tiered",
        cache_size: int = 0,
    ):
        self.policies = policies
        self.upstream = upstream
        self.strategy = strategy
        self.cache = ResultCache(cache_size) if cache_size > 0 else None
        self.guards: list[tuple[Policy, Guard]] = [
            (p, get_guard_class(p.guard)(p)) for p in policies
        ]

    @property
    def policy_refs(self) -> list[str]:
        return [p.ref for p in self.policies]

    def guards_for(self, stage: Stage) -> list[tuple[Policy, Guard]]:
        return [(p, g) for p, g in self.guards if stage in p.stages]

    def batches(self, stage: Stage) -> list[tuple[list[tuple[Policy, Guard]], bool]]:
        """The stage's guards as (batch, run_concurrently) in the order they run."""
        guards = self.guards_for(stage)
        if self.strategy == "sequential":
            return [(guards, False)] if guards else []
        if self.strategy == "parallel":
            return [(guards, True)] if guards else []
        batches = []
        for tier in TIERS:
            members = [(p, g) for p, g in guards if p.tier == tier]
            if members:
                batches.append((members, tier in CONCURRENT_TIERS))
        return batches

    async def setup(self) -> None:
        for _, guard in self.guards:
            await guard.setup()

    async def run(self, payload: dict[str, Any], request_id: str) -> PipelineTrace:
        """payload is the client's chat-completions body (already validated by the app)."""
        start = time.perf_counter()
        options = payload.pop("dwarpal", None) or {}
        trace = PipelineTrace(request_id=request_id, policies=self.policy_refs)
        schema = options.get("response_schema")
        if schema is None:
            schema = schema_from_response_format(payload.get("response_format"))
        ctx = GuardContext(
            messages=payload["messages"],
            context_docs=list(options.get("context") or []),
            response_schema=schema,
            request_id=request_id,
        )

        if await self._run_stage(Stage.INPUT, ctx, trace):
            trace.total_latency_ms = _ms_since(start)
            return trace

        payload["messages"] = ctx.messages  # forward the redacted version
        upstream_start = time.perf_counter()
        response = await self.upstream.chat(payload)
        trace.upstream_latency_ms = _ms_since(upstream_start)
        trace.response = response

        original = response_text(response)
        if original is not None:
            ctx.response_text = original
            await self._run_stage(Stage.OUTPUT, ctx, trace)
            if not trace.blocked and ctx.response_text != original:
                _set_response_text(response, ctx.response_text)

        trace.total_latency_ms = _ms_since(start)
        return trace

    # PR-04: run one stage's guards with no upstream call. The demo's "check a model reply"
    # tab uses it to show output guards on a reply the visitor pastes in.
    async def check_stage(self, stage: Stage, ctx: GuardContext, request_id: str) -> PipelineTrace:
        start = time.perf_counter()
        trace = PipelineTrace(request_id=request_id, policies=self.policy_refs)
        await self._run_stage(stage, ctx, trace)
        trace.total_latency_ms = _ms_since(start)
        return trace

    async def _run_stage(self, stage: Stage, ctx: GuardContext, trace: PipelineTrace) -> bool:
        """Run every guard for this stage. Returns True if the request was blocked."""
        for members, concurrent in self.batches(stage):
            if concurrent and len(members) > 1:
                if await self._run_concurrent(members, stage, ctx, trace):
                    return True
                continue
            for policy, guard in members:
                result = await self._check(policy, guard, stage, ctx)
                trace.results.append(result)
                if self._apply(policy, result, stage, ctx, trace) is _Outcome.BLOCKED:
                    return True
        return False

    async def _run_concurrent(
        self,
        members: list[tuple[Policy, Guard]],
        stage: Stage,
        ctx: GuardContext,
        trace: PipelineTrace,
    ) -> bool:
        results = await asyncio.gather(*(self._check(p, g, stage, ctx) for p, g in members))
        blocked = redacted = False
        for (policy, guard), result in zip(members, results, strict=True):
            if blocked:  # it ran, so it stays in the trace, but it decides nothing
                trace.results.append(result)
                continue
            if redacted and result.action == Action.REDACT and not result.shadow:
                # Computed on text an earlier guard in this batch has since redacted.
                result = await self._check(policy, guard, stage, ctx)
            trace.results.append(result)
            outcome = self._apply(policy, result, stage, ctx, trace)
            blocked = outcome is _Outcome.BLOCKED
            redacted = redacted or outcome is _Outcome.REDACTED
        return blocked

    async def _check(
        self, policy: Policy, guard: Guard, stage: Stage, ctx: GuardContext
    ) -> GuardResult:
        """One guard, timed, with its timeout, error handling and cache."""
        t0 = time.perf_counter()
        key = None
        cached = None
        if self.cache is not None and guard.cacheable:
            key = ResultCache.key(policy, stage, ctx)
            cached = self.cache.get(key)

        if cached is not None:
            result = dataclasses.replace(cached, cached=True, cost_usd=0.0)
        else:
            try:
                result = await asyncio.wait_for(
                    guard.check(ctx, stage), timeout=policy.timeout_ms / 1000
                )
            except TimeoutError:
                log.warning("guard %s timed out after %d ms", policy.ref, policy.timeout_ms)
                result = self._error_result(policy, f"timed out after {policy.timeout_ms} ms")
            except Exception as exc:  # a broken guard must not crash the proxy
                log.exception("guard %s failed", policy.ref)
                result = self._error_result(policy, f"{type(exc).__name__}: {exc}")
            if key is not None and not result.error:
                self.cache.put(key, dataclasses.replace(result))

        result.stage = stage
        result.latency_ms = _ms_since(t0)
        result.shadow = policy.mode == "shadow"
        return result

    @staticmethod
    def _error_result(policy: Policy, what: str) -> GuardResult:
        action = Action.BLOCK if policy.on_error == "fail_closed" else Action.ALLOW
        return GuardResult(
            guard=policy.name,
            policy_version=policy.version,
            action=action,
            reason=f"guard error ({policy.on_error}): {what}",
            error=True,
        )

    @staticmethod
    def _apply(
        policy: Policy,
        result: GuardResult,
        stage: Stage,
        ctx: GuardContext,
        trace: PipelineTrace,
    ) -> _Outcome:
        # PR-03: a shadow policy's decision is recorded in the trace but never enforced,
        # so a new version can run on real traffic before it is allowed to block.
        if result.shadow:
            return _Outcome.CONTINUE
        if result.action == Action.BLOCK:
            trace.blocked_by = policy.ref
            trace.blocked_stage = stage
            return _Outcome.BLOCKED
        if result.action == Action.REDACT:
            if stage == Stage.INPUT and result.redacted_messages is not None:
                ctx.messages = result.redacted_messages
                return _Outcome.REDACTED
            if stage == Stage.OUTPUT and result.redacted_text is not None:
                ctx.response_text = result.redacted_text
                return _Outcome.REDACTED
        return _Outcome.CONTINUE
