#!/usr/bin/env python3
"""Sequential vs tiered vs all-parallel guard scheduling, on the same requests. (PR-06)

Every dev-set case is sent through the real pipeline with every repo policy enabled, once per
strategy, against the in-process mock upstream, so the numbers are Dwarpal's own overhead.
The LLM guards (faithfulness judge, schema repair) get a stand-in that waits --judge-ms and
reports fixed token counts, so the experiment needs no API key and costs nothing; set
--judge-ms to the latency you measured against the real judge.

Reported per strategy: added latency p50 / p99, judge calls and guard cost per request, and
whether every decision (blocked by, forwarded input, final reply) matches the sequential run.
The decision cache is off, so every request runs every guard it reaches.

  uv run python scripts/bench_pipeline.py                # all three, 3 rounds
  uv run python scripts/bench_pipeline.py --faq-context --out results/pipeline_strategies_faq.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from dwarpal.llm import Completion, set_llm  # noqa: E402
from dwarpal.pipeline import Pipeline  # noqa: E402
from dwarpal.policy import load_policies  # noqa: E402
from dwarpal.testing.mock_upstream import app as mock_app  # noqa: E402
from dwarpal.upstream import UpstreamClient  # noqa: E402
from eval.harness import _payload_for, percentile  # noqa: E402
from eval.schema import load_dataset  # noqa: E402

STRATEGIES = ("sequential", "tiered", "parallel")
VERDICT = json.dumps({"claims": [{"claim": "stand-in", "label": "SUPPORTED"}]})


class StandInJudge:
    """Answers every guard LLM call after a fixed delay, with typical judge token counts."""

    def __init__(self, latency_ms: float, prompt_tokens: int, completion_tokens: int):
        self.latency_s = latency_ms / 1000
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.calls = 0

    async def complete(self, messages, *, model=None, **params) -> Completion:
        self.calls += 1
        await asyncio.sleep(self.latency_s)
        return Completion(VERDICT, self.prompt_tokens, self.completion_tokens)


def decision(trace) -> dict[str, Any]:
    response = trace.response or {}
    try:
        reply = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        reply = None
    return {"blocked_by": trace.blocked_by, "reply": None if trace.blocked else reply}


async def run_strategy(strategy, policies, cases, judge, rounds) -> dict[str, Any]:
    upstream = UpstreamClient("http://mock/v1/", transport=httpx.ASGITransport(app=mock_app))
    pipeline = Pipeline(policies, upstream, strategy=strategy, cache_size=0)
    await pipeline.setup()
    for case in cases[:5]:  # warm-up: first inference on each model is slow
        await pipeline.run(_payload_for(case), "warmup")

    latencies, costs, decisions = [], [], {}
    calls_before = judge.calls
    for _ in range(rounds):
        for case in cases:
            payload = _payload_for(case)
            trace = await pipeline.run(payload, f"bench-{case.id}")
            latencies.append(trace.added_latency_ms)
            costs.append(trace.guard_cost_usd)
            # run() swaps in the redacted messages, so this is what went upstream.
            decisions[case.id] = {**decision(trace), "forwarded": payload["messages"]}
    await upstream.aclose()
    requests = rounds * len(cases)
    return {
        "requests": requests,
        "added_latency_p50_ms": percentile(latencies, 0.5),
        "added_latency_p99_ms": percentile(latencies, 0.99),
        "added_latency_mean_ms": round(sum(latencies) / len(latencies), 3),
        "judge_calls_per_request": round((judge.calls - calls_before) / requests, 4),
        "guard_cost_per_request_usd": sum(costs) / requests,
        "decisions": decisions,
    }


async def main(args) -> dict[str, Any]:
    policies = load_policies(ROOT / "policies")
    cases = load_dataset(ROOT / "eval" / "datasets" / "redteam.jsonl").cases
    if args.faq_context:
        # Every reply checked against the FAQ, as the demo can send it: now an output guard
        # that blocks early (secrets, toxicity) can save the judge call.
        faq = (ROOT / "demo" / "ledgerly_faq.md").read_text(encoding="utf-8")
        cases = [
            c.model_copy(update={"context": [faq]}) if c.stage == "output" and not c.context else c
            for c in cases
        ]
    judge = StandInJudge(args.judge_ms, args.prompt_tokens, args.completion_tokens)
    set_llm(judge)

    results = {}
    for strategy in args.strategies:
        print(f"{strategy}: {args.rounds} x {len(cases)} requests ...", flush=True)
        results[strategy] = await run_strategy(strategy, policies, cases, judge, args.rounds)

    reference = results[args.strategies[0]]["decisions"]
    for r in results.values():
        mismatches = [cid for cid, d in r.pop("decisions").items() if d != reference[cid]]
        r["decisions_differ_from_" + args.strategies[0]] = mismatches
    return {
        "policies": [p.ref for p in policies],
        "cases": len(cases),
        "attacks": sum(c.label == "attack" for c in cases),
        "faq_context_on_every_reply": args.faq_context,
        "judge_stand_in": {
            "latency_ms": args.judge_ms,
            "prompt_tokens": args.prompt_tokens,
            "completion_tokens": args.completion_tokens,
        },
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor() or platform.machine(),
            "cpus": os.cpu_count(),
            "python": platform.python_version(),
        },
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "strategies": results,
    }


def render(report: dict[str, Any]) -> str:
    first = next(iter(report["strategies"]))
    lines = [
        "| Strategy | Added p50 ms | Added p99 ms | Judge calls / req | Guard cost / req (USD) "
        f"| Decisions differing from {first} |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, r in report["strategies"].items():
        differ = r[f"decisions_differ_from_{first}"]
        lines.append(
            f"| {name} | {r['added_latency_p50_ms']:.1f} | {r['added_latency_p99_ms']:.1f} "
            f"| {r['judge_calls_per_request']:.3f} | {r['guard_cost_per_request_usd']:.6f} "
            f"| {len(differ)}{' ' + str(differ) if differ else ''} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--strategies", nargs="+", default=list(STRATEGIES), choices=STRATEGIES)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--judge-ms", type=float, default=1700.0)  # measured p50, 3.5 Flash-Lite
    parser.add_argument("--prompt-tokens", type=int, default=700)
    parser.add_argument("--completion-tokens", type=int, default=120)
    parser.add_argument("--faq-context", action="store_true", help="send the FAQ with every reply")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "pipeline_strategies.json")
    args = parser.parse_args()

    report = asyncio.run(main(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(render(report))
    print(f"\nwrote {args.out}")
