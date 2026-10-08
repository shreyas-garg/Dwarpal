"""PR-03: the CI check that a changed policy file carries a version bump."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_policy_versions import prompt_violations, violations  # noqa: E402

OLD = """
name: prompt_injection
version: 1.0.0
guard: prompt_injection
stages: [input]
threshold: 0.8
changelog: ["1.0.0: initial"]
"""

PATH = "policies/prompt_injection.yaml"


def test_unchanged_file_passes():
    assert violations(OLD, OLD, PATH) == []


def test_change_without_bump_fails():
    edited = OLD.replace("threshold: 0.8", "threshold: 0.6")
    problems = violations(OLD, edited, PATH)
    assert len(problems) == 2  # version not bumped, changelog not extended
    assert "version stayed 1.0.0" in problems[0]


def test_change_with_bump_and_changelog_passes():
    edited = OLD.replace("version: 1.0.0", "version: 1.1.0").replace(
        'changelog: ["1.0.0: initial"]',
        'changelog: ["1.0.0: initial", "1.1.0: lower threshold after sweep"]',
    )
    assert violations(OLD, edited, PATH) == []


def test_bump_without_changelog_entry_fails():
    edited = OLD.replace("version: 1.0.0", "version: 1.1.0")
    problems = violations(OLD, edited, PATH)
    assert problems == [f"{PATH}: changed but the changelog gained no entry"]


def test_new_policy_needs_a_changelog():
    bare = OLD.replace('changelog: ["1.0.0: initial"]', "changelog: []")
    assert violations(None, bare, PATH) != []
    assert violations(None, OLD, PATH) == []


def test_prompt_files_are_never_edited_in_place():
    """PR-06: a judge prompt changes by adding a new file, so a version names one prompt."""
    path = "policies/prompts/faithfulness_judge.v1.txt"
    assert prompt_violations("A", path) == []
    assert prompt_violations("D", path) == []
    (problem,) = prompt_violations("M", path)
    assert "immutable" in problem
