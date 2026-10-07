# Architecture

## Request path

1. A client sends an OpenAI chat-completions request to `POST /v1/chat/completions`.
   Extra Dwarpal options go in a `dwarpal` field (the OpenAI SDK sends it via `extra_body`):
   - `context`: list of strings, the documents the answer must be grounded in (faithfulness).
   - `response_schema`: JSON Schema the reply must match (output_schema).
2. `app.py` validates the body, rejects `stream=true`, applies the configured model, and calls
   `Pipeline.run`.
3. `pipeline.py` runs the **input** guards in policy-file order.
   - `BLOCK`: stop. The upstream is never called.
   - `REDACT`: replace the messages with the guard's `redacted_messages` and continue.
   - `ALLOW` / `FLAG`: continue.
4. The (possibly redacted) request goes to the upstream model through `upstream.py`
   (any OpenAI-compatible endpoint; Gemini Flash by default). The `dwarpal` field is stripped.
5. The **output** guards run on the reply text with the same rules. `REDACT` swaps in
   `redacted_text`.
6. The response goes back to the client:
   - allowed: the upstream response, possibly with redacted content;
   - blocked: an OpenAI-shaped completion with a refusal message and
     `finish_reason: "content_filter"`.
   Headers: `X-Dwarpal-Request-Id`, `X-Dwarpal-Policies` (every active `name@version`), and
   `X-Dwarpal-Blocked-By` when blocked. With `EXPOSE_TRACE=true` the body also carries a
   `dwarpal` object with each guard's action, score, reason and latency.

## Components

| File | Responsibility |
|---|---|
| `dwarpal/app.py` | HTTP layer, auth, OpenAI-shaped responses and errors |
| `dwarpal/config.py` | Settings from env / `.env` |
| `dwarpal/upstream.py` | Async client for the wrapped LLM |
| `dwarpal/pipeline.py` | Guard ordering, enforcement, timing, `PipelineTrace` |
| `dwarpal/policy.py` | YAML policy model, validation, loading |
| `dwarpal/guards/base.py` | `Guard`, `GuardContext`, `GuardResult`, `Stage`, `Action` |
| `dwarpal/guards/registry.py` | `@register` and auto-discovery of guard modules |
| `dwarpal/testing/mock_upstream.py` | Fake LLM for tests, offline dev and load tests |

## Policies

One YAML file per policy in `policies/`. Fields:

| Field | Meaning |
|---|---|
| `name`, `version` | Identity. `version` is semver and must change whenever the file changes. |
| `guard` | Which registered guard class runs this policy. |
| `stages` | `input`, `output` or both; must be stages the guard supports. |
| `enabled` | Off = not loaded. |
| `mode` | `enforce`, or `shadow` (log only; PR-03). |
| `action` | What happens when score ≥ threshold: `block`, `redact` or `flag`. |
| `threshold` | 0..1. |
| `tier` | `cheap`, `model` or `llm`; used for ordering and concurrency (PR-06). |
| `on_error` | `fail_closed` blocks when the guard crashes, `fail_open` allows. |
| `timeout_ms` | Per-guard time budget (enforced from PR-06). |
| `params` | Guard-specific settings. |
| `changelog` | One line per version. |

Policies are validated at startup; an invalid policy stops the server instead of serving
traffic with a broken filter.

## Error handling

- A guard that raises is handled by its policy's `on_error`; the result is marked `error: true`.
- Upstream HTTP errors are passed through with their status code and body; network errors
  become 502.
