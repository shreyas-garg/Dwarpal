"""PR-04: the Gradio demo at /demo, and the pipeline hook its reply checker uses."""

import pytest

from dwarpal.guards.base import GuardContext, Stage

pytest.importorskip("gradio")

from dwarpal.demo import (  # noqa: E402
    CHAT_EXAMPLES,
    FAQ,
    REPLY_EXAMPLES,
    active_examples,
    make_check_reply,
    make_preview,
    make_respond,
    trace_rows,
    verdict,
)
from tests.test_data_leak_guards import shipped_data  # noqa: E402

TRACE = {
    "blocked_by": None,
    "blocked_stage": None,
    "added_latency_ms": 1.25,
    "results": [
        {
            "stage": "input",
            "guard": "pii",
            "version": "1.0.0",
            "action": "redact",
            "score": 1.0,
            "latency_ms": 0.04,
            "reason": "found EMAIL×1",
            "shadow": False,
        },
        {
            "stage": "input",
            "guard": "secrets",
            "version": "1.1.0",
            "action": "block",
            "score": 0.9,
            "latency_ms": 0.05,
            "reason": "signals: jwt",
            "shadow": True,
        },
    ],
}


def test_demo_is_mounted(make_client):
    client = make_client(demo_enabled=True)
    resp = client.get("/demo/")
    assert resp.status_code == 200
    assert "gradio" in resp.text.lower()


def test_demo_off_by_setting(make_client):
    assert make_client().get("/demo/").status_code == 404


def test_trace_rows_mark_shadow_decisions():
    rows = trace_rows(TRACE)
    assert rows[0][:3] == ["input", "pii@1.0.0", "redact"]
    assert rows[1][2] == "block (shadow)"


def test_verdict_ignores_shadow_and_reports_redaction():
    assert verdict(TRACE).startswith("**Allowed after redaction** by `pii`.")
    restore_only = {**TRACE, "results": [{**TRACE["results"][0], "restored": True}]}
    assert verdict(restore_only).startswith("**Allowed.**")
    blocked = {**TRACE, "blocked_by": "pii@1.0.1", "blocked_stage": "input"}
    assert "Blocked" in verdict(blocked)


def test_examples_only_for_active_policies():
    labels = active_examples({"pii"})
    assert "Normal question" in labels
    assert all(CHAT_EXAMPLES[label][0] in (None, "pii") for label in labels)


def test_check_stage_runs_output_guards_without_upstream(make_client):
    from dwarpal.testing.mock_upstream import MockState

    client = make_client(enabled_policies="pii,secrets")
    pipeline = client.app.state.pipeline
    ctx = GuardContext(messages=[], response_text="Mail sanjay@example.com")
    trace = client.portal.call(pipeline.check_stage, Stage.OUTPUT, ctx, "t")
    assert not trace.blocked
    assert ctx.response_text == "Mail <EMAIL_1>"
    assert MockState.calls == 0


# --- the two handlers, end to end against the real app (mock upstream) ---


@pytest.fixture
def demo_client(make_client, tmp_path):
    import yaml
    from openai import OpenAI

    for name in ("pii", "secrets"):
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(shipped_data(name)))
    client = make_client(tmp_path)
    sdk = OpenAI(base_url="http://testserver/v1", api_key="x", http_client=client)
    return client, sdk


def test_chat_redacts_input_and_shows_the_trace(demo_client):
    _, sdk = demo_client
    client, _ = demo_client
    respond = make_respond(sdk, "m", make_preview(lambda: client.app.state.pipeline))
    history, box, rows, text = respond(CHAT_EXAMPLES["Personal data → redacted"][1], [], FAQ, False)
    assert "What the model received:** Create a client: Arjun Mehta, &lt;EMAIL_1&gt;" in text
    assert box == ""
    assert history[0]["content"].startswith("Create a client")  # what the user typed
    # The mock echoes what the model received, and the email comes back through restore.
    assert "arjun.mehta@example.com" in history[1]["content"]
    assert "Allowed after redaction" in text
    assert any(r[1] == "pii@1.0.1" and r[2] == "redact" for r in rows)


def test_chat_shows_a_block(demo_client):
    _, sdk = demo_client
    history, _, rows, text = make_respond(sdk, "m")(
        CHAT_EXAMPLES["Aadhaar → blocked"][1], [], FAQ, False
    )
    assert "Blocked" in text and "pii@1.0.1" in text
    assert history[1]["content"] == "Sorry, I can't help with that request."


def test_a_blocked_turn_does_not_block_the_next_one(demo_client):
    _, sdk = demo_client
    respond = make_respond(sdk, "m")
    history, *_ = respond(CHAT_EXAMPLES["Aadhaar → blocked"][1], [], FAQ, False)
    assert history[1]["metadata"]["title"].startswith("Blocked by pii@")
    history, _, _, text = respond("How do I export invoices?", history, FAQ, False)
    assert text.startswith("**Allowed.**")
    assert len(history) == 4  # the blocked exchange stays on screen


def test_chat_survives_an_upstream_error(demo_client):
    from openai import OpenAI

    client, _ = demo_client
    broken = OpenAI(base_url="http://testserver/nope", api_key="x", http_client=client)
    history, _, rows, text = make_respond(broken, "m")("hello", [], FAQ, False)
    assert history[1]["content"].startswith("⚠️ Upstream error")
    assert rows == [] and text == ""


async def test_reply_checker_blocks_a_leaked_key(make_client, tmp_path):
    import yaml

    for name in ("pii", "secrets"):
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(shipped_data(name)))
    client = make_client(tmp_path)
    check = make_check_reply(lambda: client.app.state.pipeline, "refused")
    shown, rows, text = await check(REPLY_EXAMPLES["Leaked API key"], "")
    assert shown == "refused" and "secrets@1.0.1" in text
    shown, _, text = await check(REPLY_EXAMPLES["Another customer's details"], "")
    assert "<EMAIL_1>" in shown and "Allowed after redaction" in text
    shown, _, text = await check(REPLY_EXAMPLES["Safe reply"], "")
    assert shown == REPLY_EXAMPLES["Safe reply"] and text.startswith("**Allowed.**")
