"""Langfuse tracing, sent over OpenTelemetry. (PR-07)

One trace per request: a root span, one `guardrail` observation per guard that ran (shadow ones
too) and one `generation` for the upstream call, all tagged with the active policy versions.
Langfuse v4 takes traces on its OTLP endpoint; the older /api/public/ingestion API is being shut
down on Langfuse Cloud. Attribute names follow the Langfuse SDK (langfuse._client.attributes).

The pipeline measures durations, not start times, so spans are laid out from the request's
start (input guards, one after another) and its end (upstream call, then output guards). Each
duration is exact; for guards that ran at the same time (the `model` tier) the start is not.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter


def _ns(ms: float) -> int:
    return int(ms * 1_000_000)


def _metadata(prefix: str, values: dict[str, Any]) -> dict[str, Any]:
    # As the SDK does it: strings and ints as they are, anything else as JSON.
    return {
        f"{prefix}.{key}": value if isinstance(value, str | int) else json.dumps(value)
        for key, value in values.items()
        if value is not None
    }


class LangfuseExporter:
    def __init__(
        self,
        base_url: str,
        public_key: str,
        secret_key: str,
        exporter: SpanExporter | None = None,  # tests pass an in-memory exporter
    ):
        if exporter is None:
            auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
            exporter = OTLPSpanExporter(
                endpoint=base_url.rstrip("/") + "/api/public/otel/v1/traces",
                headers={"Authorization": f"Basic {auth}", "x-langfuse-ingestion-version": "4"},
            )
        # Our own provider, not the global one: nothing else in the process is traced.
        self._provider = TracerProvider(resource=Resource.create({"service.name": "dwarpal"}))
        self._provider.add_span_processor(BatchSpanProcessor(exporter))
        self._tracer = self._provider.get_tracer("dwarpal")

    def export(self, row: dict[str, Any]) -> None:
        """Turn one request-log row into a trace. Sent in the background by the processor."""
        end = int(row["ts"] * 1_000_000_000)
        start = end - _ns(row["total_ms"])
        # Langfuse v4 reads trace-level attributes from every span, so each span carries them.
        shared = {
            "langfuse.trace.name": "chat.completions",
            "langfuse.trace.tags": list(row["policies"]),
            "langfuse.trace.metadata.request_id": row["request_id"],
        }
        root = self._tracer.start_span(
            "dwarpal.request",
            start_time=start,
            attributes={
                **shared,
                "langfuse.observation.type": "span",
                "langfuse.observation.input": json.dumps(
                    {"prompt_sha256": row["prompt_sha256"], "prompt": row["prompt"]}
                ),
                "langfuse.observation.output": json.dumps({"blocked_by": row["blocked_by"]}),
                **_metadata(
                    "langfuse.observation.metadata",
                    {
                        "blocked_stage": row["blocked_stage"],
                        "added_latency_ms": row["added_ms"],
                        "upstream_latency_ms": row["upstream_ms"],
                        "cost_usd": row["cost_usd"],
                    },
                ),
            },
        )
        parent = trace.set_span_in_context(root)

        def child(name: str, at: int, duration_ms: float, attributes: dict[str, Any]) -> int:
            span = self._tracer.start_span(
                name, context=parent, start_time=at, attributes={**shared, **attributes}
            )
            span.end(end_time=at + _ns(duration_ms))
            return at + _ns(duration_ms)

        inputs = [g for g in row["guards"] if g["stage"] == "input"]
        outputs = [g for g in row["guards"] if g["stage"] == "output"]
        at = start
        for g in inputs:
            at = child(f"{g['guard']}@{g['version']}", at, g["latency_ms"], _guard_attributes(g))

        called = row["model"] is not None
        tail_ms = sum(g["latency_ms"] for g in outputs) + (row["upstream_ms"] if called else 0)
        at = max(start, end - _ns(tail_ms))
        if called:
            usage = {
                "input": row["prompt_tokens"],
                "output": row["completion_tokens"],
                "total": row["prompt_tokens"] + row["completion_tokens"],
            }
            at = child(
                "upstream",
                at,
                row["upstream_ms"],
                {
                    "langfuse.observation.type": "generation",
                    "langfuse.observation.model.name": row["model"],
                    "langfuse.observation.usage_details": json.dumps(usage),
                    "langfuse.observation.cost_details": json.dumps(
                        {"total": row["upstream_cost_usd"]}
                    ),
                },
            )
        for g in outputs:
            at = child(f"{g['guard']}@{g['version']}", at, g["latency_ms"], _guard_attributes(g))
        root.end(end_time=end)

    def flush(self) -> None:
        self._provider.force_flush()

    def shutdown(self) -> None:
        self._provider.shutdown()  # sends whatever is still buffered


def _guard_attributes(g: dict[str, Any]) -> dict[str, Any]:
    caught = g["action"] in ("block", "redact") and not g.get("restored")
    return {
        "langfuse.observation.type": "guardrail",
        "langfuse.version": g["version"],
        "langfuse.observation.level": "ERROR" if g["error"] else "WARNING" if caught else "DEFAULT",
        "langfuse.observation.output": json.dumps({"action": g["action"], "score": g["score"]}),
        **_metadata(
            "langfuse.observation.metadata",
            {
                "stage": g["stage"],
                "shadow": g["shadow"],
                "cached": g["cached"],
                "error": g["error"],
                "cost_usd": g["cost_usd"],
            },
        ),
    }
