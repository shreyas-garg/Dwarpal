# Architecture

## Request path

1. A client sends an OpenAI chat-completions request to `POST /v1/chat/completions`.
   Extra Dwarpal options go in a `dwarpal` field (the OpenAI SDK sends it via `extra_body`):
   - `context`: list of strings, the documents the answer must be grounded in (faithfulness).
   - `response_schema`: JSON Schema the reply must match (output_schema).
2. `app.py` validates the body, rejects `stream=true`, applies the configured model, and calls
   `Pipeline.run`.
3. `pipeline.py` runs the **input** guards, scheduled by tier (PR-06): `cheap` guards one
   after another, then `model` guards at the same time, then `llm` guards last, stopping at the
   first tier that blocks. Results are applied in policy-file order whatever ran first, so the
   outcome matches running them one by one (`PIPELINE_STRATEGY=sequential` does exactly that).
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
| `dwarpal/llm.py` | Model calls made by guards (faithfulness judge, schema repair), with token cost |
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
| `mode` | `enforce`, or `shadow`: the guard runs and its decision lands in the trace, but it never blocks or redacts. Two versions of one policy may run side by side (e.g. `1.1.0` in shadow next to `1.0.0` enforcing) as long as at most one enforces, so a new version is tried on real traffic before it can block. `GET /v1/dwarpal/policies` shows what is active. |
| `action` | What happens when score ≥ threshold: `block`, `redact` or `flag`. |
| `threshold` | 0..1. |
| `tier` | `cheap` (sequential, first), `model` (concurrent), `llm` (sequential, last). |
| `on_error` | `fail_closed` blocks when the guard crashes, `fail_open` allows. |
| `timeout_ms` | Per-guard time budget. A guard that overruns is decided by `on_error`. |
| `params` | Guard-specific settings. |
| `changelog` | One line per version. |

Policies are validated at startup; an invalid policy stops the server instead of serving
traffic with a broken filter.

## Error handling

- A guard that raises or times out is handled by its policy's `on_error`; the result is marked
  `error: true`. Attack and data-leak guards fail closed; toxicity and faithfulness fail open
  (docs/decisions/0007).
- Guards marked `cacheable` reuse their decision for an identical input under the same policy
  version (`GUARD_CACHE_SIZE`, in memory); a reused result has `cached: true` and no cost.
- Upstream HTTP errors are passed through with their status code and body; network errors
  become 502.
