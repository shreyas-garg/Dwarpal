import json
from pathlib import Path

import pytest

from dwarpal.guards.registry import discover
from dwarpal.policy import Policy
from eval.augment import augment
from eval.harness import Cache, gate, percentile, score_policy, wilson
from eval.report import render_markdown
from eval.schema import DatasetError, EvalCase, load_dataset, load_suites

SAFE = {
    "id": "x-001",
    "stage": "input",
    "label": "safe",
    "target_guard": None,
    "category": "ordinary",
    "author": "tests",
    "input": "How do I export invoices?",
}
ATTACK = {**SAFE, "id": "x-002", "label": "attack", "target_guard": "max_length"}

discover()  # the repo guards; policies built by hand here skip the loader that does this

LIMIT = Policy.model_validate(
    {
        "name": "max_length",
        "version": "1.0.0",
        "guard": "max_length",
        "stages": ["input"],
        "params": {"max_chars": 20},
    }
)


def write_jsonl(path: Path, *cases: dict) -> Path:
    path.write_text("".join(json.dumps(c) + "\n" for c in cases))
    return path


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache")


def test_wilson_widens_on_small_samples():
    lo, hi = wilson(9, 10)
    assert lo < 0.9 < hi
    assert (hi - lo) > (wilson(90, 100)[1] - wilson(90, 100)[0])
    assert wilson(0, 0) == [0.0, 1.0]


def test_percentile_picks_an_observed_value():
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
    assert percentile([], 0.5) == 0.0


@pytest.mark.parametrize(
    "case, message",
    [
        ({**ATTACK, "target_guard": None}, "must name a target_guard"),
        ({**SAFE, "target_guard": "max_length"}, "must set target_guard to null"),
        ({**SAFE, "stage": "output"}, "must carry a canned response"),
        (
            {**ATTACK, "stage": "output", "response": "hi", "target_guard": "faithfulness"},
            "must carry context",
        ),
        ({**SAFE, "colour": "blue"}, "Extra inputs are not permitted"),
    ],
)
def test_invalid_cases_are_rejected(tmp_path, case, message):
    with pytest.raises(DatasetError, match=message):
        load_dataset(write_jsonl(tmp_path / "d.jsonl", case))


def test_duplicate_ids_are_rejected(tmp_path):
    with pytest.raises(DatasetError, match="duplicate id"):
        load_dataset(write_jsonl(tmp_path / "d.jsonl", SAFE, SAFE))


def test_cases_for_unregistered_guards_are_skipped(tmp_path, caplog):
    future = {**ATTACK, "id": "x-003", "target_guard": "faithfulness", "context": ["a"]}
    dataset = load_dataset(write_jsonl(tmp_path / "d.jsonl", SAFE, future))

    assert [c.id for c in dataset.cases] == ["x-001"]
    assert dataset.skipped == {"faithfulness": 1}
    assert "faithfulness" in caplog.text


def test_repo_datasets_load():
    suites = load_suites()
    dev, holdout = suites["dev"], suites["holdout"]

    # PR-02 shipped 18 safe cases; PR-03 added 8 hard negatives and 20 input attacks;
    # PR-04 added 18 hard negatives (13 input, 5 output) and 16 pii / secrets attacks.
    assert len(dev.safe) == 44
    assert len(dev.attacks) == 36
    assert {c.target_guard for c in dev.attacks} == {
        "prompt_injection",
        "jailbreak",
        "pii",
        "secrets",
    }
    assert len(holdout.safe) == 15
    assert sum(holdout.skipped.values()) + len(holdout.attacks) == 16
    assert {c.author for c in dev.cases} == {"harshit-sachan", "harshit-goel", "yash"}


async def test_score_policy_separates_catch_rate_from_false_positives(cache):
    cases = [
        EvalCase(**{**ATTACK, "id": "a-1", "input": "x" * 50}),
        EvalCase(**{**ATTACK, "id": "a-2", "input": "x" * 50}),
        EvalCase(**{**ATTACK, "id": "a-3", "input": "short"}),  # under the limit, a miss
        EvalCase(**{**SAFE, "id": "s-1", "input": "short"}),
        EvalCase(**{**SAFE, "id": "s-2", "input": "y" * 50}),  # over the limit, a false positive
    ]

    score = await score_policy(LIMIT, cases, cache)

    assert (score.attacks, score.catch_rate, score.missed) == (3, 0.6667, ["a-3"])
    assert (score.safe, score.fpr, score.false_positives) == (2, 0.5, ["s-2"])


async def test_output_stage_cases_are_not_scored_by_an_input_policy(cache):
    reply = {**SAFE, "id": "o-1", "stage": "output", "response": "z" * 50}
    score = await score_policy(LIMIT, [EvalCase(**reply)], cache)
    assert (score.safe, score.fpr) == (0, None)


async def test_cache_returns_the_first_decision(cache):
    case = EvalCase(**{**ATTACK, "input": "x" * 50})
    first = await score_policy(LIMIT, [case], cache)
    assert cache.hits == 0

    again = await score_policy(LIMIT, [case], cache)
    assert cache.hits == 1
    assert again.catch_rate == first.catch_rate


async def test_a_new_policy_version_invalidates_the_cache(cache):
    case = EvalCase(**{**ATTACK, "input": "x" * 50})
    await score_policy(LIMIT, [case], cache)
    bumped = LIMIT.model_copy(update={"version": "1.0.1"})

    await score_policy(bumped, [case], cache)
    assert cache.hits == 0


BASELINE = {"policies": {"pi": {"catch_rate": 0.8, "fpr": 0.05}}}
THRESHOLDS = {"defaults": {"min_catch_rate": 0.7, "max_fpr": 0.1}}


def score(**kw):
    return {"pi": {"version": "1.0.0", "catch_rate": 0.8, "fpr": 0.05, **kw}}


@pytest.mark.parametrize(
    "scores, reason",
    [
        (score(catch_rate=0.7), "catch rate fell"),
        (score(fpr=0.06), "false positives rose"),
        ({}, "no longer scored"),
    ],
)
def test_gate_blocks_regressions_against_the_baseline(scores, reason):
    [failure] = gate(scores, BASELINE, THRESHOLDS)
    assert reason in failure


def test_gate_blocks_a_new_policy_below_the_floors():
    scores = {"new": {"version": "1.0.0", "catch_rate": 0.6, "fpr": 0.2}}
    floor, ceiling = gate(scores, {"policies": {}}, THRESHOLDS)
    assert "below the 0.70 floor" in floor
    assert "exceed the 0.10 ceiling" in ceiling


def test_gate_passes_on_an_improvement():
    assert gate(score(catch_rate=0.9, fpr=0.0), BASELINE, THRESHOLDS) == []


def test_gate_ignores_a_policy_with_no_attack_cases():
    scores = {"new": {"version": "1.0.0", "catch_rate": None, "fpr": 0.0}}
    assert gate(scores, BASELINE | {"policies": {}}, THRESHOLDS) == []


@pytest.mark.parametrize("variant", ["base64", "leetspeak", "spaced"])
def test_augment_rewrites_input_attacks_only(variant):
    cases = [EvalCase(**ATTACK), EvalCase(**SAFE)]
    [out] = augment(cases, variant)

    assert out.id == f"x-002#{variant}"
    assert out.input != ATTACK["input"]
    assert out.target_guard == "max_length"


def test_report_renders_a_failed_gate():
    report = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "policies": ["max_length@1.0.0"],
        "cache_hits": 0,
        "suites": {
            name: {
                "cases": 1,
                "skipped": {"pii": 2},
                "policies": {
                    "max_length": {
                        "version": "1.0.0",
                        "attacks": 2,
                        "catch_rate": 0.5,
                        "catch_ci": [0.1, 0.9],
                        "safe": 1,
                        "fpr": 1.0,
                        "p50_ms": 0.4,
                        "missed": ["a-3"],
                        "false_positives": ["s-2"],
                    }
                },
                "end_to_end": {
                    "attacks": 2,
                    "catch_rate": 0.5,
                    "safe": 1,
                    "fpr": 1.0,
                    "added_latency_p50_ms": 0.4,
                    "added_latency_p95_ms": 0.9,
                },
            }
            for name in ("dev", "holdout")
        },
        "robustness": {"base64": {"attacks": 2, "catch_rate": 0.0}},
        "gate": {"ok": False, "failures": ["max_length: catch rate fell"]},
    }

    markdown = render_markdown(report)

    assert "❌ Eval gate failed" in markdown
    assert "- max_length: catch rate fell" in markdown
    assert "| max_length | 1.0.0 | 2 | 0.500 | 0.10–0.90 | 1 | 1.000 | 0.40 |" in markdown
    assert "guard not registered yet: pii 2" in markdown
