#!/usr/bin/env python3
"""A small real run: requests through the proxy to the real upstream (Gemini), for true
end-to-end latency and real cost per request. (PR-07)

Start the proxy against Gemini first (`make dev`, with UPSTREAM_API_KEY and ADMIN_TOKEN in
.env), then in a second terminal:

    uv run python loadtest/real_run.py              # 50 requests, one after another

Use a freshly started proxy: its guard decision cache would answer questions it has already
seen (from an earlier run) without running the guards, which understates added latency.

The requests are the safe questions from the dev and holdout sets, sent with the demo's system
prompt (the Ledgerly FAQ) and no dwarpal.context, so the faithfulness judge does not run and
spends no quota. Gemini's free tier allows about 20 requests a day per model, so 50 needs a key
with billing on (cost: well under $0.10); the run stops at the first 429 and reports what it
has. Cost per request comes from the proxy's request log: list price from config/pricing.yaml,
plus guard LLM calls. Writes results/real_run.json and results/real_run.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from dwarpal.config import Settings  # noqa: E402
from dwarpal.telemetry.store import percentile  # noqa: E402

FAQ = (ROOT / "demo" / "ledgerly_faq.md").read_text(encoding="utf-8")
# The demo's system prompt (dwarpal/demo.py), so token counts match what the demo sends.
SYSTEM_PROMPT = (
    "You are Ledgerly's support assistant. Answer briefly, using only the FAQ below.\n\n" + FAQ
)


def questions() -> list[str]:
    found = []
    for name in ("redteam.jsonl", "holdout.jsonl"):
        for line in (ROOT / "eval" / "datasets" / name).read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                case = json.loads(line)
                if case["label"] == "safe" and case["stage"] == "input":
                    found.append(case["input"])
    return found


def run(url: str, token: str, n: int, rpm: float) -> dict[str, Any]:
    sent, errors, stopped = [], [], None
    with httpx.Client(base_url=url, timeout=120) as client:
        health = client.get("/healthz").json()
        for i, question in enumerate(questions()[:n], 1):
            started = time.perf_counter()
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini",  # the proxy sends its own UPSTREAM_MODEL
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": question},
                    ],
                },
            )
            client_ms = (time.perf_counter() - started) * 1000
            if resp.status_code == 429:
                stopped = f"429 after {i - 1} requests: {resp.text[:300]}"
                print(stopped)
                break
            if resp.status_code != 200:
                errors.append({"status": resp.status_code, "body": resp.text[:300]})
                print(f"{i:>3}: HTTP {resp.status_code}")
                continue
            request_id = resp.headers["X-Dwarpal-Request-Id"]
            added = resp.headers["X-Dwarpal-Added-Latency-Ms"]
            sent.append({"request_id": request_id, "client_ms": client_ms})
            print(f"{i:>3}: {client_ms:7.0f} ms, added {added} ms")
            if rpm and (pause := 60 / rpm - client_ms / 1000) > 0:
                time.sleep(pause)
        log = client.get(
            "/v1/dwarpal/requests", params={"limit": 1000}, headers={"X-Admin-Token": token}
        )
        log.raise_for_status()

    rows = {r["request_id"]: r for r in log.json()["requests"]}
    done = [{**rows[s["request_id"]], "client_ms": s["client_ms"]} for s in sent]

    def spread(key: str) -> dict[str, float | None]:
        values = [r[key] for r in done]
        return {"p50": percentile(values, 0.5), "p99": percentile(values, 0.99)}

    count = len(done)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "proxy": {"url": url, "git_sha": health.get("git_sha"), "policies": health["policies"]},
        "model": next((r["model"] for r in done if r["model"]), None),
        "requests": count,
        "blocked": sum(1 for r in done if r["blocked_by"]),
        "errors": errors,
        "stopped_early": stopped,
        "latency_ms": {
            "client": spread("client_ms"),
            "upstream": spread("upstream_ms"),
            "added": spread("added_ms"),
        },
        "tokens_per_request": {
            "input": sum(r["prompt_tokens"] for r in done) / count if count else None,
            "output": sum(r["completion_tokens"] for r in done) / count if count else None,
        },
        "cost_per_request_usd": sum(r["cost_usd"] for r in done) / count if count else None,
        "cost_total_usd": sum(r["cost_usd"] for r in done),
    }


def render(report: dict[str, Any]) -> str:
    lat, tokens = report["latency_ms"], report["tokens_per_request"]

    def row(label: str, key: str) -> str:
        p50, p99 = ("—" if v is None else f"{v:.0f}" for v in (lat[key]["p50"], lat[key]["p99"]))
        return f"| {label} | {p50} | {p99} |"

    lines = [
        "# Real run",
        "",
        f"`loadtest/real_run.py` · {report['generated_at']} · {report['requests']} requests to "
        f"`{report['model']}` through the proxy, one after another, no context (no judge calls).",
        "",
        "| Metric | p50 ms | p99 ms |",
        "|---|---:|---:|",
        row("End-to-end latency (client)", "client"),
        row("Upstream model call", "upstream"),
        row("Added by Dwarpal", "added"),
        "",
    ]
    if report["requests"]:
        lines += [
            f"Cost per request: **${report['cost_per_request_usd']:.6f}** at list price "
            f"(mean {tokens['input']:.0f} input / {tokens['output']:.0f} output tokens, thinking "
            f"included); ${report['cost_total_usd']:.4f} for the whole run. "
            f"Blocked: {report['blocked']}. Errors: {len(report['errors'])}.",
        ]
    if report["stopped_early"]:
        lines += ["", f"Stopped early: {report['stopped_early']}"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--n", type=int, default=50, help="number of requests (max 56)")
    parser.add_argument("--rpm", type=float, default=0, help="pace to this many a minute")
    parser.add_argument("--admin-token", default=None, help="default: ADMIN_TOKEN from .env")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "real_run.json")
    args = parser.parse_args()

    token = args.admin_token or Settings().admin_token.get_secret_value()
    if not token:
        raise SystemExit("set ADMIN_TOKEN in .env (the proxy needs the same one) or --admin-token")
    report = run(args.url, token, args.n, args.rpm)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown = render(report)
    args.out.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print("\n" + markdown + f"\nwrote {args.out} and {args.out.with_suffix('.md')}")
