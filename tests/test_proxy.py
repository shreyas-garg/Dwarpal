from openai import OpenAI

from dwarpal.testing.mock_upstream import MockState
from tests.conftest import chat

REDACT = """
name: redact_secret
version: 1.2.0
guard: test_redact_word
stages: [input, output]
action: redact
params: {word: hunter2}
"""

BLOCK_OUT = """
name: no_banana
version: 2.0.0
guard: test_block_output
stages: [output]
params: {word: banana}
"""

CRASH = """
name: crashy
version: 0.1.0
guard: test_crash
stages: [input]
on_error: {mode}
"""


def test_healthz_and_models(make_client):
    client = make_client(git_sha="abc123")
    health = client.get("/healthz").json()
    assert health["status"] == "ok"
    assert health["git_sha"] == "abc123"
    assert "max_length@1.0.0" in health["policies"]
    assert client.get("/v1/models").json()["data"][0]["id"] == "gemini-2.5-flash"


def test_pass_through(make_client):
    client = make_client()
    resp = chat(client, "What is the refund window?")
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "Mock answer to: What is the refund window?"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert resp.headers["X-Dwarpal-Policies"] == "max_length@1.0.0"
    assert "X-Dwarpal-Blocked-By" not in resp.headers
    assert len(resp.headers["X-Dwarpal-Request-Id"]) == 16
    assert body["dwarpal"]["results"][0]["action"] == "allow"
    # single tenant: the configured model is always used
    assert MockState.last_payload["model"] == "gemini-2.5-flash"


def test_dwarpal_options_are_not_forwarded(make_client):
    client = make_client()
    chat(client, "hi", dwarpal={"context": ["Refunds within 30 days."]})
    assert "dwarpal" not in MockState.last_payload


def test_long_input_blocked_before_upstream(make_client):
    client = make_client()
    resp = chat(client, "x" * 5000)
    body = resp.json()
    assert resp.status_code == 200
    assert body["choices"][0]["finish_reason"] == "content_filter"
    assert body["choices"][0]["message"]["content"] == "Sorry, I can't help with that request."
    assert resp.headers["X-Dwarpal-Blocked-By"] == "max_length@1.0.0"
    assert body["dwarpal"]["blocked_stage"] == "input"
    assert MockState.calls == 0


def test_input_and_output_redaction(make_client, policy_dir):
    client = make_client(policy_dir(REDACT))
    resp = chat(client, "my password is hunter2", mock_response="sure, hunter2 noted")
    forwarded = MockState.last_payload["messages"][0]["content"]
    assert forwarded == "my password is [REDACTED]"
    assert resp.json()["choices"][0]["message"]["content"] == "sure, [REDACTED] noted"
    actions = [(r["stage"], r["action"]) for r in resp.json()["dwarpal"]["results"]]
    assert actions == [("input", "redact"), ("output", "redact")]


def test_output_block(make_client, policy_dir):
    client = make_client(policy_dir(BLOCK_OUT))
    resp = chat(client, "fruit?", mock_response="have a banana")
    body = resp.json()
    assert body["choices"][0]["finish_reason"] == "content_filter"
    assert "banana" not in body["choices"][0]["message"]["content"]
    assert resp.headers["X-Dwarpal-Blocked-By"] == "no_banana@2.0.0"
    assert body["dwarpal"]["blocked_stage"] == "output"
    assert body["usage"]["total_tokens"] > 0  # upstream tokens were still spent
    assert MockState.calls == 1


def test_guard_crash_fail_closed_blocks(make_client, policy_dir):
    client = make_client(policy_dir(CRASH.format(mode="fail_closed")))
    resp = chat(client, "hello")
    assert resp.headers["X-Dwarpal-Blocked-By"] == "crashy@0.1.0"
    assert resp.json()["dwarpal"]["results"][0]["error"] is True


def test_guard_crash_fail_open_allows(make_client, policy_dir):
    client = make_client(policy_dir(CRASH.format(mode="fail_open")))
    resp = chat(client, "hello")
    assert "X-Dwarpal-Blocked-By" not in resp.headers
    assert resp.json()["choices"][0]["finish_reason"] == "stop"


def test_bad_requests(make_client):
    client = make_client()
    assert client.post("/v1/chat/completions", json={"model": "m"}).status_code == 400
    resp = chat(client, "hi", stream=True)
    assert resp.status_code == 400
    assert "stream" in resp.json()["error"]["message"]


def test_api_key_auth(make_client):
    client = make_client(api_keys="k1,k2")
    assert chat(client, "hi").status_code == 401
    ok = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer k2"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert ok.status_code == 200


def test_openai_sdk_drop_in(make_client):
    """The official SDK works with only base_url changed."""
    client = make_client()
    sdk = OpenAI(base_url="http://testserver/v1", api_key="unused", http_client=client)
    completion = sdk.chat.completions.create(
        model="gemini-flash",
        messages=[{"role": "user", "content": "Hello"}],
        extra_body={"dwarpal": {"context": ["doc"]}},
    )
    assert completion.choices[0].message.content == "Mock answer to: Hello"

    blocked = sdk.chat.completions.create(
        model="gemini-flash", messages=[{"role": "user", "content": "y" * 9000}]
    )
    assert blocked.choices[0].finish_reason == "content_filter"
