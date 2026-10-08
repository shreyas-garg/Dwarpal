"""Scores every enabled policy against the red-team sets and fails the build on a regression.

Three numbers per policy:
  catch rate  blocked-or-redacted / its own attack cases, with a Wilson 95% interval
  FPR         blocked-or-redacted / every safe case of the same stage
  p50 ms      median time the guard took on one case

Each guard is scored alone, so a miss is attributable. The full pipeline is then run over the
same cases for the end-to-end numbers. The dev suite gates the merge; the holdout suite and
the robustness variants are reported only.

Usage: make eval  (see --help for --no-cache, --no-gate, --update-baseline)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from dwarpal.config import get_settings
from dwarpal.guards.base import Action, Guard, GuardContext, GuardResult, Stage
from dwarpal.guards.registry import get_guard_class
from dwarpal.pipeline import Pipeline
from dwarpal.policy import Policy, load_policies
from dwarpal.testing.mock_upstream import app as mock_app
from dwarpal.upstream import UpstreamClient
from eval.augment import VARIANTS, augment
from eval.report import render_markdown
from eval.schema import Dataset, EvalCase, dump_case, load_suites

log = logging.getLogger("eval.harness")

EVAL_DIR = Path(__file__).parent
CACHE_DIR = EVAL_DIR / ".cache"
REPORT_DIR = EVAL_DIR / "reports"
THRESHOLDS_FILE = EVAL_DIR / "thresholds.yaml"
BASELINE_FILE = EVAL_DIR / "baseline.json"

# One missed case out of ten moves a rate by 0.1, so the gate compares exactly. The epsilon
# only absorbs float noise.
EPSILON = 1e-9


def wilson(successes: int, total: int, z: float = 1.96) -> list[float]:
    """95% confidence interval for a rate. With ~10 cases per guard the point estimate alone
    is misleading, so every catch rate is reported with its interval."""
    if total == 0:
        return [0.0, 1.0]
    p = successes / total
    denominator = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denominator
    spread = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denominator
    return [round(max(0.0, centre - spread), 4), round(min(1.0, centre + spread), 4)]


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(q * len(ordered)) - 1)
    return round(ordered[index], 3)


class Cache:
    """Guard decisions keyed by sha256(guard, version, case).

    A guard only changes when its code or its policy version changes, and the policy version
    is part of the key, so a cached decision is still the decision that code would make.
    Latencies are stored with the decision and come from the run that computed it; use
    --no-cache for fresh timings.
    """

    def __init__(self, directory: Path, enabled: bool = True):
        self.directory = directory
        self.enabled = enabled
        self.hits = 0
        if enabled:
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(*parts: str) -> str:
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self.directory / f"{key}.json"
        if not path.is_file():
            return None
        self.hits += 1
        return json.loads(path.read_text())

    def put(self, key: str, value: dict[str, Any]) -> None:
        if self.enabled:
            (self.directory / f"{key}.json").write_text(json.dumps(value))


@dataclass
class Outcome:
    case_id: str
    caught: bool  # BLOCK or REDACT; FLAG does not count, it changes nothing
    action: str
    latency_ms: float


@dataclass
class PolicyScore:
    policy: str
    version: str
    attacks: int
    catch_rate: float | None  # None when no attack case targets this policy yet
    catch_ci: list[float] | None
    safe: int
    fpr: float | None
    p50_ms: float
    missed: list[str]
    false_positives: list[str]


def _context_for(case: EvalCase) -> GuardContext:
    return GuardContext(
        messages=[{"role": "user", "content": case.input}],
        response_text=case.response if case.stage is Stage.OUTPUT else None,
        context_docs=list(case.context),
        response_schema=case.response_schema,  # PR-06
        request_id=f"eval-{case.id}",
    )


async def _check(guard: Guard, policy: Policy, case: EvalCase) -> GuardResult:
    try:
        return await guard.check(_context_for(case), case.stage)
    except Exception as exc:
        log.warning("%s crashed on %s: %s", policy.ref, case.id, exc)
        action = Action.BLOCK if policy.on_error == "fail_closed" else Action.ALLOW
        return GuardResult(policy.name, policy.version, action, reason=str(exc), error=True)


async def _score_case(guard: Guard, policy: Policy, case: EvalCase, cache: Cache) -> Outcome:
    key = cache.key(policy.guard, policy.version, dump_case(case))
    if (cached := cache.get(key)) is not None:
        return Outcome(case.id, **cached)
    started = time.perf_counter()
    result = await _check(guard, policy, case)
    decision = {
        "caught": result.caught,
        "action": result.action.value,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    # PR-06: a crash or timeout (e.g. the judge without an API key) is not this guard's
    # decision on this case. Caching it would replay the failure after the cause is fixed.
    if not result.error:
        cache.put(key, decision)
    return Outcome(case.id, **decision)


async def score_policy(policy: Policy, cases: list[EvalCase], cache: Cache) -> PolicyScore:
    """Run one guard, alone, over its own attacks and over every safe case of the same stage."""
    guard = get_guard_class(policy.guard)(policy)
    await guard.setup()
    stages = set(policy.stages)
    attacks = [
        c
        for c in cases
        if c.label == "attack" and c.target_guard == policy.guard and c.stage in stages
    ]
    safe = [c for c in cases if c.label == "safe" and c.stage in stages]

    # Sequential on purpose: concurrent runs would make the per-case latencies meaningless.
    attack_outcomes = [await _score_case(guard, policy, c, cache) for c in attacks]
    safe_outcomes = [await _score_case(guard, policy, c, cache) for c in safe]
    caught = sum(o.caught for o in attack_outcomes)
    false_positives = [o.case_id for o in safe_outcomes if o.caught]

    return PolicyScore(
        policy=policy.name,
        version=policy.version,
        attacks=len(attacks),
        catch_rate=round(caught / len(attacks), 4) if attacks else None,
        catch_ci=wilson(caught, len(attacks)) if attacks else None,
        safe=len(safe),
        fpr=round(len(false_positives) / len(safe), 4) if safe else None,
        p50_ms=percentile([o.latency_ms for o in attack_outcomes + safe_outcomes], 0.5),
        missed=[o.case_id for o in attack_outcomes if not o.caught],
        false_positives=false_positives,
    )


def _payload_for(case: EvalCase) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "mock",
        "messages": [{"role": "user", "content": case.input}],
    }
    if case.response:
        payload["mock_response"] = case.response  # output cases never call a real model
    options: dict[str, Any] = {}
    if case.context:
        options["context"] = case.context
    if case.response_schema is not None:  # PR-06
        options["response_schema"] = case.response_schema
    if options:
        payload["dwarpal"] = options
    return payload


async def _score_pipeline_case(
    pipeline: Pipeline, case: EvalCase, cache: Cache, fingerprint: str
) -> Outcome:
    key = cache.key("pipeline", fingerprint, dump_case(case))
    if (cached := cache.get(key)) is not None:
        return Outcome(case.id, **cached)
    trace = await pipeline.run(_payload_for(case), f"eval-{case.id}")
    # PR-04: count only enforced catches; a shadow redact or a pii restore changes nothing.
    redacted = any(r.caught and not r.shadow for r in trace.results)
    decision = {
        "caught": trace.blocked or redacted,
        "action": trace.blocked_by or ("redact" if redacted else "allow"),
        "latency_ms": round(trace.added_latency_ms, 3),
    }
    if not any(r.error for r in trace.results):  # PR-06, as in _score_case
        cache.put(key, decision)
    return Outcome(case.id, **decision)


async def score_end_to_end(
    policies: list[Policy], cases: list[EvalCase], cache: Cache
) -> dict[str, Any]:
    """Every case through the real pipeline, against the in-process mock model."""
    upstream = UpstreamClient(
        "http://mock/v1/", "eval", transport=httpx.ASGITransport(app=mock_app)
    )
    pipeline = Pipeline(policies, upstream)
    await pipeline.setup()
    fingerprint = ",".join(pipeline.policy_refs)
    try:
        outcomes = {
            c.id: await _score_pipeline_case(pipeline, c, cache, fingerprint) for c in cases
        }
    finally:
        await upstream.aclose()

    attacks = [c for c in cases if c.label == "attack"]
    safe = [c for c in cases if c.label == "safe"]
    attack_outcomes = [outcomes[c.id] for c in attacks]
    safe_outcomes = [outcomes[c.id] for c in safe]

    caught = sum(o.caught for o in attack_outcomes)
    false_positives = [o.case_id for o in safe_outcomes if o.caught]
    latencies = [o.latency_ms for o in attack_outcomes + safe_outcomes]
    return {
        "attacks": len(attacks),
        "catch_rate": round(caught / len(attacks), 4) if attacks else None,
        "catch_ci": wilson(caught, len(attacks)) if attacks else None,
        "safe": len(safe),
        "fpr": round(len(false_positives) / len(safe), 4) if safe else None,
        "added_latency_p50_ms": percentile(latencies, 0.5),
        "added_latency_p95_ms": percentile(latencies, 0.95),
        "missed": [o.case_id for o in attack_outcomes if not o.caught],
        "false_positives": false_positives,
    }


async def score_suite(policies: list[Policy], dataset: Dataset, cache: Cache) -> dict[str, Any]:
    return {
        "cases": len(dataset.cases),
        "skipped": dataset.skipped,
        "policies": {p.name: asdict(await score_policy(p, dataset.cases, cache)) for p in policies},
        "end_to_end": await score_end_to_end(policies, dataset.cases, cache),
    }


async def score_robustness(
    policies: list[Policy], dataset: Dataset, cache: Cache
) -> dict[str, Any]:
    """Catch rate on obfuscated copies of the dev attacks. Reported, never gated."""
    variants: dict[str, Any] = {}
    for variant in VARIANTS:
        cases = augment(dataset.attacks, variant)
        scores = [asdict(await score_policy(p, cases, cache)) for p in policies]
        attacks = sum(s["attacks"] for s in scores)
        caught = sum(s["attacks"] - len(s["missed"]) for s in scores)
        variants[variant] = {
            "attacks": attacks,
            "catch_rate": round(caught / attacks, 4) if attacks else None,
        }
    return variants


def load_thresholds(path: Path = THRESHOLDS_FILE) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


def load_baseline(path: Path = BASELINE_FILE) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.is_file() else {}


def gate(
    scores: dict[str, dict[str, Any]], baseline: dict[str, Any], thresholds: dict[str, Any]
) -> list[str]:
    """Reasons the merge should be blocked. Empty means green."""
    defaults = thresholds.get("defaults") or {}
    overrides = thresholds.get("policies") or {}
    recorded = baseline.get("policies") or {}
    failures: list[str] = []

    for name, score in scores.items():
        limits = {**defaults, **(overrides.get(name) or {})}
        previous = recorded.get(name) or {}
        catch, fpr = score["catch_rate"], score["fpr"]

        if catch is not None:
            floor = limits.get("min_catch_rate")
            if floor is not None and catch < floor - EPSILON:
                failures.append(f"{name}: catch rate {catch:.3f} is below the {floor:.2f} floor")
            was = previous.get("catch_rate")
            if was is not None and catch < was - EPSILON:
                failures.append(f"{name}: catch rate fell {was:.3f} → {catch:.3f} against baseline")
        if fpr is not None:
            ceiling = limits.get("max_fpr")
            if ceiling is not None and fpr > ceiling + EPSILON:
                failures.append(
                    f"{name}: false positives {fpr:.3f} exceed the {ceiling:.2f} ceiling"
                )
            was = previous.get("fpr")
            if was is not None and fpr > was + EPSILON:
                failures.append(
                    f"{name}: false positives rose {was:.3f} → {fpr:.3f} against baseline"
                )

    for name in sorted(set(recorded) - set(scores)):
        failures.append(f"{name}: in the baseline but no longer scored (removed or disabled)")
    return failures


def baseline_from(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "note": "Dev-set numbers on main. The eval gate fails any PR that scores below these.",
        "updated": report["generated_at"],
        "policies": {
            name: {"version": s["version"], "catch_rate": s["catch_rate"], "fpr": s["fpr"]}
            for name, s in report["suites"]["dev"]["policies"].items()
        },
    }


async def run(cache_enabled: bool = True) -> dict[str, Any]:
    settings = get_settings()
    policies = load_policies(settings.policy_dir, settings.enabled_policy_names)
    if not policies:
        raise SystemExit("no enabled policies to score")
    suites = load_suites()
    cache = Cache(CACHE_DIR, cache_enabled)

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "policies": [p.ref for p in policies],
        "suites": {name: await score_suite(policies, ds, cache) for name, ds in suites.items()},
    }
    report["robustness"] = await score_robustness(policies, suites["dev"], cache)
    report["cache_hits"] = cache.hits
    return report


def write_reports(report: dict[str, Any], out_dir: Path = REPORT_DIR) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    # utf-8 explicitly: the report has ✅/❌, which Windows' default encoding cannot write.
    (out_dir / "latest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown = render_markdown(report)
    (out_dir / "latest.md").write_text(markdown, encoding="utf-8")
    return markdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make eval", description="Score the red-team sets and gate on regressions."
    )
    parser.add_argument("--no-cache", action="store_true", help="recompute every guard decision")
    parser.add_argument("--no-gate", action="store_true", help="report only, always exit 0")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="write the dev numbers to eval/baseline.json (explain why in your PR description)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    report = asyncio.run(run(cache_enabled=not args.no_cache))
    dev = report["suites"]["dev"]["policies"]
    failures = gate(dev, load_baseline(), load_thresholds())
    report["gate"] = {"ok": not failures, "failures": failures}

    print(write_reports(report))

    if args.update_baseline:
        BASELINE_FILE.write_text(json.dumps(baseline_from(report), indent=2) + "\n")
        print(f"\nwrote {BASELINE_FILE}")
        return 0
    return 1 if failures and not args.no_gate else 0


if __name__ == "__main__":
    raise SystemExit(main())
