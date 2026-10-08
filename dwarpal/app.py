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
from dwarpal.feedback import FeedbackLog
from dwarpal.limits import LOOPBACK, DailyCap, RateLimiter, client_ip
from dwarpal.llm import LLMClient, set_llm
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
        # PR-06: guards that call a model (faithfulness judge, schema repair) share the
        # upstream endpoint and key, through a client this app owns and closes.
        llm = LLMClient.from_settings(settings, transport=upstream_transport)
        set_llm(llm)
        pipeline = Pipeline(
            policies,
            upstream,
            strategy=settings.pipeline_strategy,
            cache_size=settings.guard_cache_size,
        )
        await pipeline.setup()
        app.state.pipeline = pipeline
        log.info("dwarpal ready with policies: %s", ", ".join(pipeline.policy_refs) or "none")
        yield
        set_llm(None)
        await llm.aclose()
        await upstream.aclose()

    app = FastAPI(title="Dwarpal", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.feedback = FeedbackLog(settings.feedback_path)  # PR-04
    # PR-05: per-IP rate limit and daily cap, both off unless configured.
    rate_limiter = RateLimiter(settings.rate_limit_per_minute)
    daily_cap = DailyCap(settings.daily_request_cap)

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
    # PR-04: rate a reply. The online quality signal, alongside the offline eval set.
    @app.post("/v1/dwarpal/feedback")
    async def feedback(request: Request) -> Any:
        if (denied := check_auth(request)) is not None:
            return denied
        try:
            body = await request.json()
            entry = request.app.state.feedback.record(
                str(body.get("request_id", "")), body.get("rating")
            )
        except (ValueError, AttributeError) as exc:
            return openai_error(400, str(exc))
        return entry

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
                    "on_error": p.on_error,  # PR-06
                    "timeout_ms": p.timeout_ms,
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

        # PR-05: budget protection. The demo calls from loopback, so it skips the per-IP limit
        # and is bounded by the daily cap alone.
        ip = client_ip(request, settings.trust_forwarded_for)
        if ip not in LOOPBACK and not rate_limiter.allow(ip):
            return openai_error(
                429, "rate limit exceeded, try again in a minute", "rate_limit_error"
            )
        if not daily_cap.allow():
            return openai_error(
                429,
                "the demo's daily request limit is used up, try again tomorrow (UTC)",
                "rate_limit_error",
            )
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

    # PR-04: Gradio demo at /demo, same container and URL. Skipped without the `demo` extra,
    # and when API keys are set: the demo uses a key, so it would let any visitor in.
    if settings.demo_enabled and settings.api_key_set:
        log.warning("API_KEYS is set, so /demo is not mounted (it would bypass the key check)")
    elif settings.demo_enabled:
        try:
            from dwarpal.demo import mount_demo
        except ImportError:
            log.info("gradio not installed; /demo disabled")
        else:
            app = mount_demo(app, settings)

    return app


app = create_app()
