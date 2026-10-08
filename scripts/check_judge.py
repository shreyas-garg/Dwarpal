#!/usr/bin/env python3
"""How often does the faithfulness judge agree with a human? (PR-06)

Runs the real judge (needs UPSTREAM_API_KEY) over replies a person labelled by hand, and
prints, per reply, the human label, the judge's score and decision, and the claims it called
unsupported. The summary line is the agreement rate quoted in docs/decisions/0007.

  uv run python scripts/check_judge.py                                  # the 10 check cases
  uv run python scripts/check_judge.py --dataset eval/datasets/redteam.jsonl   # dev scores

Only the check set and the dev set: holdout cases are never scored while tuning.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dwarpal.guards.base import GuardContext, Stage  # noqa: E402
from dwarpal.guards.faithfulness import FaithfulnessGuard  # noqa: E402
from dwarpal.policy import load_policy_file  # noqa: E402
from eval.schema import load_dataset  # noqa: E402


async def main(dataset: Path, gap_s: float = 0.0) -> int:
    if "holdout" in dataset.name:
        raise SystemExit("holdout cases are not scored while tuning (eval/README.md)")
    policy = load_policy_file(ROOT / "policies" / "faithfulness.yaml")
    guard = FaithfulnessGuard(policy)
    cases = [c for c in load_dataset(dataset).cases if c.stage is Stage.OUTPUT and c.context]

    agree, latencies, cost = 0, [], 0.0
    print(f"{policy.ref}, threshold {policy.threshold}\n")
    print(f"{'case':<11} {'human':<7} {'judge':<7} {'score':>5}  reason")
    for i, case in enumerate(cases):
        if i and gap_s:  # pause between calls instead of inside them, so latency is real
            await asyncio.sleep(gap_s)
        ctx = GuardContext(
            messages=[{"role": "user", "content": case.input}],
            response_text=case.response,
            context_docs=list(case.context),
        )
        start = time.perf_counter()
        result = await guard.check(ctx, Stage.OUTPUT)
        latencies.append((time.perf_counter() - start) * 1000)
        cost += result.cost_usd
        human = "block" if case.label == "attack" else "allow"
        judge = "block" if result.caught else "allow"
        agree += human == judge
        mark = "" if human == judge else "  <-- disagrees"
        print(f"{case.id:<11} {human:<7} {judge:<7} {result.score:5.2f}  {result.reason}{mark}")

    n = len(cases)
    print(
        f"\nagreement {agree}/{n} ({agree / n:.0%}) · judge latency p50 "
        f"{statistics.median(latencies):.0f} ms, max {max(latencies):.0f} ms · "
        f"cost {cost / n:.6f} USD per reply"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dataset", type=Path, default=ROOT / "eval" / "datasets" / "judge_check.jsonl"
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=0.0,
        help="seconds between calls (free tier: 13, with GUARD_LLM_RPM=0)",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.dataset, args.gap)))
