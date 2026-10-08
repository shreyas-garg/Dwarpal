"""Per-request telemetry: guard times, cost and added latency for every proxied request. (PR-07)

The pipeline calls `await telemetry.record(trace, ctx)` when a request is finished. That only
builds a row and queues it; a background task writes it to the SQLite request log
(data/requests.db) and, when both LANGFUSE_* keys are set, sends it to Langfuse. Logging
therefore never slows a request down, and a telemetry failure is logged, never raised.

Nothing is recorded until an app starts a Telemetry (the app does it at startup), so the eval
harness and the benchmarks, which call Pipeline.run() directly, never write to the log.

  costs.py     cost of the upstream call from its usage field and config/pricing.yaml
  store.py     the SQLite request log and the stats computed from it
  langfuse.py  one Langfuse trace per request, over OpenTelemetry
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from dwarpal.telemetry.costs import Pricing, billed_tokens
from dwarpal.telemetry.store import RequestStore, summarize

if TYPE_CHECKING:
    from dwarpal.config import Settings
    from dwarpal.guards.base import GuardContext
    from dwarpal.pipeline import PipelineTrace
    from dwarpal.telemetry.langfuse import LangfuseExporter

log = logging.getLogger("dwarpal.telemetry")

QUEUE_SIZE = 10_000  # rows waiting to be written; beyond that new rows are dropped, not awaited
BATCH_SIZE = 500
_WINDOW = re.compile(r"^(\d+)([smhd])$")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}

_active: Telemetry | None = None


async def record(trace: PipelineTrace, ctx: GuardContext) -> None:
    """The pipeline's hook: queue a finished request for the log. No-op without a running app."""
    if _active is None:
        return
    try:
        _active.add(trace, ctx)
    except Exception:  # telemetry must never fail a request
        log.exception("telemetry failed for request %s", trace.request_id)


def parse_window(text: str) -> int:
    """'15m', '1h', '24h', '7d' -> seconds."""
    match = _WINDOW.match(text.strip().lower())
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"window must look like 15m, 1h, 24h or 7d, got {text!r}")
    return int(match.group(1)) * _UNIT_S[match.group(2)]


def request_row(
    trace: PipelineTrace, ctx: GuardContext, pricing: Pricing, log_prompts: bool
) -> dict[str, Any]:
    """One request-log row. See store.SCHEMA for the columns."""
    response = trace.response or {}
    prompt_tokens, completion_tokens = billed_tokens(response.get("usage"))
    model = response.get("model")
    upstream_cost = pricing.cost(model, prompt_tokens, completion_tokens)
    # The user turns as they went upstream, i.e. after any redaction. Without LOG_PROMPTS only
    # the hash is kept: enough to tell whether two requests sent the same text, not to read it.
    text = ctx.user_text()
    return {
        "request_id": trace.request_id,
        "ts": time.time(),
        "policies": trace.policies,
        # Reasons are left out: some quote the text they matched.
        "guards": [{k: v for k, v in r.to_dict().items() if k != "reason"} for r in trace.results],
        "blocked_by": trace.blocked_by,
        "blocked_stage": trace.blocked_stage.value if trace.blocked_stage else None,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "upstream_ms": round(trace.upstream_latency_ms, 3),
        "total_ms": round(trace.total_latency_ms, 3),
        "added_ms": round(trace.added_latency_ms, 3),
        "upstream_cost_usd": upstream_cost,
        "guard_cost_usd": trace.guard_cost_usd,
        "cost_usd": upstream_cost + trace.guard_cost_usd,
        "prompt_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "prompt": text if log_prompts else None,
    }


class Telemetry:
    def __init__(
        self,
        store: RequestStore,
        pricing: Pricing,
        log_prompts: bool = False,
        langfuse: LangfuseExporter | None = None,
    ):
        self.store = store
        self.pricing = pricing
        self.log_prompts = log_prompts
        self.langfuse = langfuse
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(QUEUE_SIZE)
        self._writer: asyncio.Task[None] | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> Telemetry:
        langfuse = None
        secret = settings.langfuse_secret_key.get_secret_value()
        if settings.langfuse_public_key and secret:
            from dwarpal.telemetry.langfuse import LangfuseExporter

            langfuse = LangfuseExporter(
                settings.langfuse_base_url, settings.langfuse_public_key, secret
            )
            log.info("sending traces to Langfuse at %s", settings.langfuse_base_url)
        else:
            log.info("Langfuse keys not set; requests are logged to %s only", settings.request_db)
        return cls(
            RequestStore(settings.request_db),
            Pricing.load(settings.pricing_file),
            settings.log_prompts,
            langfuse,
        )

    async def start(self) -> None:
        """Start the background writer and route the pipeline's traces here."""
        global _active
        self._writer = asyncio.create_task(self._write_loop())
        _active = self

    async def stop(self) -> None:
        """Stop taking traces, write everything queued, send what Langfuse still buffers."""
        global _active
        if _active is self:
            _active = None
        if self._writer is not None:
            await self._queue.put(None)
            await self._writer
        if self.langfuse is not None:
            await asyncio.to_thread(self.langfuse.shutdown)

    def add(self, trace: PipelineTrace, ctx: GuardContext) -> None:
        row = request_row(trace, ctx, self.pricing, self.log_prompts)
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            log.warning("telemetry queue full; request %s not logged", trace.request_id)

    async def flush(self, timeout_s: float = 2.0) -> None:
        """Wait until queued rows are written (at most timeout_s), so a read right after a
        request sees it."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout_s)

    async def stats(self, window_s: float) -> dict[str, Any]:
        await self.flush()
        rows = await asyncio.to_thread(self.store.since, time.time() - window_s)
        return summarize(rows)

    async def recent(self, limit: int) -> list[dict[str, Any]]:
        await self.flush()
        return await asyncio.to_thread(self.store.recent, limit)

    async def _write_loop(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while len(batch) < BATCH_SIZE and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            rows = [r for r in batch if r is not None]
            try:
                if rows:
                    await asyncio.to_thread(self._write, rows)
            except Exception:
                log.exception("could not write %d request(s) to the request log", len(rows))
            finally:
                for _ in batch:
                    self._queue.task_done()
            if len(rows) < len(batch):  # stop() queued None
                return

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.store.insert(rows)
        if self.langfuse is not None:
            for row in rows:
                self.langfuse.export(row)
