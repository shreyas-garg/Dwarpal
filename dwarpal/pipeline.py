"""Runs input guards, calls the upstream model, then runs output guards.

Guards run one after another in policy-file order. The first BLOCK stops the request;
a REDACT replaces the text and the next guard sees the redacted version.
(PR-06 adds tiers, concurrency and timeouts; PR-07 adds telemetry.)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from dwarpal.guards.base import Action, Guard, GuardContext, GuardResult, Stage
from dwarpal.guards.registry import get_guard_class
from dwarpal.policy import Policy
from dwarpal.upstream import UpstreamClient

log = logging.getLogger("dwarpal.pipeline")


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


class Pipeline:
    def __init__(self, policies: list[Policy], upstream: UpstreamClient):
        self.policies = policies
        self.upstream = upstream
        self.guards: list[tuple[Policy, Guard]] = [
            (p, get_guard_class(p.guard)(p)) for p in policies
        ]

    @property
    def policy_refs(self) -> list[str]:
        return [p.ref for p in self.policies]

    def guards_for(self, stage: Stage) -> list[tuple[Policy, Guard]]:
        return [(p, g) for p, g in self.guards if stage in p.stages]

    async def setup(self) -> None:
        for _, guard in self.guards:
            await guard.setup()

    async def run(self, payload: dict[str, Any], request_id: str) -> PipelineTrace:
        """payload is the client's chat-completions body (already validated by the app)."""
        start = time.perf_counter()
        options = payload.pop("dwarpal", None) or {}
        trace = PipelineTrace(request_id=request_id, policies=self.policy_refs)
        ctx = GuardContext(
            messages=payload["messages"],
            context_docs=list(options.get("context") or []),
            response_schema=options.get("response_schema"),
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

    async def _run_stage(self, stage: Stage, ctx: GuardContext, trace: PipelineTrace) -> bool:
        """Run every guard for this stage. Returns True if the request was blocked."""
        for policy, guard in self.guards_for(stage):
            t0 = time.perf_counter()
            try:
                result = await guard.check(ctx, stage)
            except Exception as exc:  # a broken guard must not crash the proxy
                log.exception("guard %s failed", policy.ref)
                action = Action.BLOCK if policy.on_error == "fail_closed" else Action.ALLOW
                result = GuardResult(
                    guard=policy.name,
                    policy_version=policy.version,
                    action=action,
                    reason=f"guard error ({policy.on_error}): {type(exc).__name__}: {exc}",
                    error=True,
                )
            result.stage = stage
            result.latency_ms = _ms_since(t0)
            trace.results.append(result)

            if result.action == Action.BLOCK:
                trace.blocked_by = policy.ref
                trace.blocked_stage = stage
                return True
            if result.action == Action.REDACT:
                if stage == Stage.INPUT and result.redacted_messages is not None:
                    ctx.messages = result.redacted_messages
                elif stage == Stage.OUTPUT and result.redacted_text is not None:
                    ctx.response_text = result.redacted_text
        return False
