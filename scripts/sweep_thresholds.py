#!/usr/bin/env python3
"""Threshold sweep for the input-attack guards: catch rate vs FPR, 0.50 to 0.95. (PR-03)

Scores every dev case once per policy (with the classifier always consulted), then applies
each candidate threshold to the recorded scores — one model pass, many thresholds. The
chosen threshold and the reasoning live in docs/decisions/0003-injection-jailbreak-guards.md.

Usage:
  uv run python scripts/sweep_thresholds.py [policy ...]   # default: prompt_injection jailbreak
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dwarpal.config import get_settings  # noqa: E402
from dwarpal.guards.registry import get_guard_class  # noqa: E402
from dwarpal.policy import load_policies  # noqa: E402
from eval.schema import load_suites  # noqa: E402

THRESHOLDS = [round(0.50 + 0.05 * i, 2) for i in range(10)]  # 0.50 .. 0.95


async def case_scores(policy, cases):
    """Raw guard scores per case, with the threshold lifted so layer 2 always runs."""
    from eval.harness import _context_for

    probe = policy.model_copy(update={"threshold": 1.0})
    guard = get_guard_class(probe.guard)(probe)
    await guard.setup()
    scores = {}
    for case in cases:
        result = await guard.check(_context_for(case), case.stage)
        scores[case.id] = result.score
    return scores


def sweep(name: str, attack_scores: dict[str, float], safe_scores: dict[str, float]) -> None:
    print(f"\n## {name}  ({len(attack_scores)} attacks, {len(safe_scores)} safe)\n")
    print("| threshold | catch rate | missed | FPR | false positives |")
    print("|---|---|---|---|---|")
    for t in THRESHOLDS:
        missed = sorted(cid for cid, s in attack_scores.items() if s < t)
        fps = sorted(cid for cid, s in safe_scores.items() if s >= t)
        catch = (len(attack_scores) - len(missed)) / len(attack_scores)
        fpr = len(fps) / len(safe_scores)
        print(
            f"| {t:.2f} | {catch:.2f} | {', '.join(missed) or '—'} "
            f"| {fpr:.2f} | {', '.join(fps) or '—'} |"
        )
    print("\nattack scores (lowest first):")
    for cid, s in sorted(attack_scores.items(), key=lambda kv: kv[1])[:5]:
        print(f"  {cid}: {s:.3f}")
    print("safe scores (highest first):")
    for cid, s in sorted(safe_scores.items(), key=lambda kv: -kv[1])[:5]:
        print(f"  {cid}: {s:.3f}")


async def main(names: list[str]) -> None:
    settings = get_settings()
    policies = [p for p in load_policies(settings.policy_dir) if p.name in names]
    missing = set(names) - {p.name for p in policies}
    if missing:
        raise SystemExit(f"unknown or disabled policies: {sorted(missing)}")
    dev = load_suites()["dev"]

    for policy in policies:
        stages = set(policy.stages)
        attacks = [
            c
            for c in dev.cases
            if c.label == "attack" and c.target_guard == policy.guard and c.stage in stages
        ]
        safe = [c for c in dev.cases if c.label == "safe" and c.stage in stages]
        attack_scores = await case_scores(policy, attacks)
        safe_scores = await case_scores(policy, safe)
        sweep(policy.ref, attack_scores, safe_scores)


if __name__ == "__main__":
    names = sys.argv[1:] or ["prompt_injection", "jailbreak"]
    asyncio.run(main(names))
