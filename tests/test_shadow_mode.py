"""PR-03: shadow mode, side-by-side policy versions, and the policies endpoint."""

import pytest

from dwarpal.policy import PolicyError, load_policies
from tests.conftest import chat

SHADOW_MAX_LENGTH = """
name: max_length
version: 1.1.0
guard: max_length
stages: [input]
mode: shadow
params: {max_chars: 10}
changelog: ["1.1.0: shadow trial of a much tighter limit"]
"""

ENFORCE_MAX_LENGTH = """
name: max_length
version: 1.0.0
guard: max_length
stages: [input]
mode: enforce
params: {max_chars: 4000}
changelog: ["1.0.0: initial"]
"""


def test_shadow_decision_is_logged_not_enforced(make_client, policy_dir):
    client = make_client(policy_dir(SHADOW_MAX_LENGTH))
    resp = chat(client, "well over ten characters, which shadow would block")
    body = resp.json()
    # The request went through untouched...
    assert resp.status_code == 200
    assert body["choices"][0]["finish_reason"] == "stop"
    assert "X-Dwarpal-Blocked-By" not in resp.headers
    # ...but the trace records what the shadow version would have done.
    (result,) = body["dwarpal"]["results"]
    assert result == {**result, "action": "block", "shadow": True}


def test_two_versions_side_by_side(make_client, policy_dir):
    """A shadow candidate runs next to the enforcing version; only enforce blocks."""
    client = make_client(policy_dir(ENFORCE_MAX_LENGTH, SHADOW_MAX_LENGTH))
    resp = chat(client, "short, but longer than ten characters")
    body = resp.json()
    assert resp.headers["X-Dwarpal-Policies"] == "max_length@1.0.0,max_length@1.1.0"
    assert body["choices"][0]["finish_reason"] == "stop"
    by_version = {r["version"]: r for r in body["dwarpal"]["results"]}
    assert by_version["1.0.0"] == {**by_version["1.0.0"], "action": "allow", "shadow": False}
    assert by_version["1.1.0"] == {**by_version["1.1.0"], "action": "block", "shadow": True}

    # The enforcing version still blocks on its own terms.
    blocked = chat(client, "x" * 5000)
    assert blocked.headers["X-Dwarpal-Blocked-By"] == "max_length@1.0.0"


def test_two_enforcing_versions_rejected(policy_dir):
    enforce_bumped = ENFORCE_MAX_LENGTH.replace("1.0.0", "1.2.0")
    directory = policy_dir(ENFORCE_MAX_LENGTH, enforce_bumped)
    with pytest.raises(PolicyError, match="second enforcing version"):
        load_policies(directory)


def test_duplicate_name_and_version_rejected(policy_dir):
    directory = policy_dir(ENFORCE_MAX_LENGTH, ENFORCE_MAX_LENGTH)
    with pytest.raises(PolicyError, match="duplicate policy"):
        load_policies(directory)


def test_policies_endpoint(make_client, policy_dir):
    client = make_client(policy_dir(ENFORCE_MAX_LENGTH, SHADOW_MAX_LENGTH), git_sha="deadbeef")
    body = client.get("/v1/dwarpal/policies").json()
    assert body["git_sha"] == "deadbeef"
    assert [(p["name"], p["version"], p["mode"]) for p in body["policies"]] == [
        ("max_length", "1.0.0", "enforce"),
        ("max_length", "1.1.0", "shadow"),
    ]


def test_policies_endpoint_respects_auth(make_client):
    client = make_client(api_keys="k1")
    assert client.get("/v1/dwarpal/policies").status_code == 401
