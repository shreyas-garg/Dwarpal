"""FastAPI app: an OpenAI-compatible proxy with guardrails in front of the upstream model."""

import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from dwarpal import __version__
from dwarpal.config import Settings, get_settings
from dwarpal.pipeline import Pipeline, PipelineTrace
from dwarpal.policy import load_policies
from dwarpal.upstream import UpstreamClient, UpstreamError

log = logging.getLogger("dwarpal")


def openai_error(
    status: int, message: str, err_type: str = "invalid_request_error"
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "param": None, "code": None}},
    )


def refusal_completion(trace: PipelineTrace, model: str, message: str) -> dict[str, Any]:
    """An OpenAI-shaped completion for a blocked request (finish_reason=content_filter)."""
    usage = (trace.response or {}).get("usage") or {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    return {
        "id": f"chatcmpl-dwarpal-{trace.request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": message},
                "finish_reason": "content_filter",
            }
        ],
        "usage": usage,
    }


def create_app(
    settings: Settings | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the app. Tests pass `upstream_transport` to route upstream calls to the mock."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        policies = load_policies(settings.policy_dir, settings.enabled_policy_names)
        upstream = UpstreamClient(
            settings.upstream_base_url,
            settings.upstream_api_key.get_secret_value(),
            settings.upstream_timeout_s,
            transport=upstream_transport,
        )
        pipeline = Pipeline(policies, upstream)
        await pipeline.setup()
        app.state.pipeline = pipeline
        log.info("dwarpal ready with policies: %s", ", ".join(pipeline.policy_refs) or "none")
        yield
        await upstream.aclose()

    app = FastAPI(title="Dwarpal", version=__version__, lifespan=lifespan)
    app.state.settings = settings

    def check_auth(request: Request) -> JSONResponse | None:
        allowed = settings.api_key_set
        if not allowed:
            return None
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip()
        if not any(secrets.compare_digest(token, k) for k in allowed):
            return openai_error(401, "invalid Dwarpal API key", "authentication_error")
        return None

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        pipeline: Pipeline = request.app.state.pipeline
        return {
            "status": "ok",
            "version": __version__,
            "git_sha": settings.git_sha,
            "policies": pipeline.policy_refs,
        }

    # PR-03: what is guarding traffic right now — policy versions, modes, and the deployed SHA.
    @app.get("/v1/dwarpal/policies")
    async def policies(request: Request) -> Any:
        if (denied := check_auth(request)) is not None:
            return denied
        pipeline: Pipeline = request.app.state.pipeline
        return {
            "git_sha": settings.git_sha,
            "policies": [
                {
                    "name": p.name,
                    "version": p.version,
                    "mode": p.mode,
                    "stages": [s.value for s in p.stages],
                    "action": p.action.value,
                    "threshold": p.threshold,
                    "tier": p.tier,
                }
                for p in pipeline.policies
            ],
        }

    @app.get("/v1/models")
    async def models(request: Request) -> Any:
        if (denied := check_auth(request)) is not None:
            return denied
        return {
            "object": "list",
            "data": [{"id": settings.upstream_model, "object": "model", "owned_by": "dwarpal"}],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        if (denied := check_auth(request)) is not None:
            return denied
        try:
            payload = await request.json()
        except ValueError:
            return openai_error(400, "request body must be JSON")
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            return openai_error(400, "'messages' must be a list")
        if payload.get("stream"):
            return openai_error(400, "stream=true is not supported by Dwarpal v1")
        if settings.override_model or not payload.get("model"):
            payload["model"] = settings.upstream_model

        pipeline: Pipeline = request.app.state.pipeline
        request_id = uuid.uuid4().hex[:16]
        try:
            trace = await pipeline.run(payload, request_id)
        except UpstreamError as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.body)

        if trace.blocked:
            body = refusal_completion(trace, payload["model"], settings.refusal_message)
        else:
            body = trace.response or {}
        if settings.expose_trace:
            body["dwarpal"] = trace.to_dict()

        headers = {
            "X-Dwarpal-Request-Id": request_id,
            "X-Dwarpal-Policies": ",".join(trace.policies),
        }
        if trace.blocked_by:
            headers["X-Dwarpal-Blocked-By"] = trace.blocked_by
        return JSONResponse(content=body, headers=headers)

    return app


app = create_app()
