# 0001: Ship Dwarpal as an OpenAI-compatible proxy

**Status:** accepted · **Owner:** Shreyas · **PR:** 01

## Decision

Dwarpal runs as an HTTP proxy that implements `POST /v1/chat/completions`. Clients point their
OpenAI SDK at it by changing `base_url`. Guards are configured by versioned YAML policies.

## Alternatives considered

| Option | Why not |
|---|---|
| Python library / decorator | Only works for Python apps; every app must upgrade to get a policy change; no central log. |
| Sidecar per app | More moving parts than a single-tenant project needs. |
| Guardrails inside the prompt | Not measurable per check, easy to jailbreak, costs tokens on every call. |

## Consequences

- Works with any language and any OpenAI-compatible client, with zero code changes.
- One place to version policies and log every decision.
- Cost: one extra network hop and the guards' own latency. We measure this as *added latency*
  (PR-07).
- Blocked requests return HTTP 200 with `finish_reason: "content_filter"` instead of a 4xx, so
  existing clients handle them like a normal provider-side filter instead of retrying an error.
- Streaming is out of scope for v1: output guards need the full reply, so streaming would
  require buffering, which removes the benefit of streaming.
