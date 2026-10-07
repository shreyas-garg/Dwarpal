import pytest

from dwarpal.policy import PolicyError, load_policies
from tests.conftest import REPO_POLICIES

VALID = """
name: length
version: 1.0.0
guard: max_length
stages: [input]
params: {max_chars: 10}
"""


def test_repo_policies_load():
    policies = load_policies(REPO_POLICIES)
    assert "max_length@1.0.0" in [p.ref for p in policies]


def test_valid_policy_defaults(policy_dir):
    [p] = load_policies(policy_dir(VALID))
    assert p.ref == "length@1.0.0"
    assert p.action == "block"
    assert p.on_error == "fail_closed"
    assert p.params == {"max_chars": 10}


@pytest.mark.parametrize(
    "text, message",
    [
        (VALID.replace("1.0.0", "1.0"), "MAJOR.MINOR.PATCH"),
        (VALID.replace("guard: max_length", "guard: nope"), "unknown guard 'nope'"),
        (VALID.replace("[input]", "[output]"), "does not support stage"),
        (VALID.replace("[input]", "[]"), "at least one"),
        (VALID + "colour: blue\n", "Extra inputs are not permitted"),
        (VALID + "threshold: 1.5\n", "less than or equal to 1"),
        ("just a string", "mapping"),
    ],
)
def test_invalid_policies_fail_loudly(policy_dir, text, message):
    with pytest.raises(PolicyError, match=message):
        load_policies(policy_dir(text))


def test_duplicate_name_and_version_rejected(policy_dir):
    # PR-03: the same name may appear twice with different versions (shadow rollout,
    # covered in test_shadow_mode.py); the same name AND version may not.
    with pytest.raises(PolicyError, match="duplicate policy"):
        load_policies(policy_dir(VALID, VALID))


def test_disabled_and_enabled_filter(policy_dir):
    other = VALID.replace("name: length", "name: other")
    off = VALID.replace("name: length", "name: switched_off") + "enabled: false\n"
    d = policy_dir(VALID, other, off)
    assert [p.name for p in load_policies(d)] == ["length", "other"]
    assert [p.name for p in load_policies(d, {"other"})] == ["other"]
    with pytest.raises(PolicyError, match="unknown policies"):
        load_policies(d, {"missing"})
