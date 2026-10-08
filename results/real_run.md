# Real run

`loadtest/real_run.py` · 2026-10-09T02:45:02+0530 · 50 requests to `gemini-3.5-flash-lite` through the proxy, one after another, no context (no judge calls).

| Metric | p50 ms | p99 ms |
|---|---:|---:|
| End-to-end latency (client) | 1235 | 15200 |
| Upstream model call | 1173 | 15058 |
| Added by Dwarpal | 63 | 141 |

Cost per request: **$0.000238** at list price (mean 589 input / 24 output tokens, thinking included); $0.0119 for the whole run. Blocked: 0. Errors: 0.
