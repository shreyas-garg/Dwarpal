"""PR-06: the output_schema guard, on its own and through the proxy."""

import json
from pathlib import Path

import pytest

from dwarpal.guards.base import Action, GuardContext, Stage
from dwarpal.guards.output_schema import OutputSchemaGuard
from dwarpal.llm import LLMError
from dwarpal.pipeline import schema_from_response_format
from dwarpal.policy import load_policy_file
from dwarpal.testing.mock_upstream import MockState
from tests.conftest import REPO_POLICIES, chat

POLICY = load_policy_file(REPO_POLICIES / "output_schema.yaml")
PLAN = {
    "type": "object",
    "properties": {"plan": {"type": "string"}, "price_inr": {"type": "integer"}},
    "required": ["plan", "price_inr"],
    "additionalProperties": False,
}


def make_guard(**params) -> OutputSchemaGuard:
    return OutputSchemaGuard(POLICY.model_copy(update={"params": {**POLICY.params, **params}}))


def ctx(reply: str, schema: dict | None = PLAN) -> GuardContext:
    return GuardContext(
        messages=[{"role": "user", "content": "Pro plan price as JSON"}],
        response_text=reply,
        response_schema=schema,
    )


async def check(reply: str, schema: dict | None = PLAN, **params):
    return await make_guard(**params).check(ctx(reply, schema), Stage.OUTPUT)


async def test_no_schema_means_nothing_to_check(fake_llm):
    llm = fake_llm()
    result = await check("plain prose, not JSON", schema=None)
    assert result.action == Action.ALLOW
    assert llm.calls == []


@pytest.mark.parametrize(
    "reply",
    [
        '{"plan": "Pro", "price_inr": 499}',
        '\n  {\n\t"plan" :"Pro" ,\n  "price_inr":   499 }\n\n',  # odd whitespace is still JSON
    ],
)
async def test_valid_reply_is_allowed(reply):
    result = await check(reply)
    assert result.action == Action.ALLOW
    assert not result.caught


async def test_code_fence_is_unwrapped_without_counting_as_a_catch():
    result = await check('```json\n{"plan": "Pro", "price_inr": 499}\n```')
    assert result.action == Action.REDACT
    assert result.redacted_text == '{"plan": "Pro", "price_inr": 499}'
    assert result.restored and not result.caught


async def test_broken_reply_is_repaired_once(fake_llm):
    llm = fake_llm('{"plan": "Pro", "price_inr": 499}')
    result = await check("Sure! {plan: 'Pro', price_inr: 499,}")
    assert result.action == Action.REDACT and result.caught
    assert result.redacted_text == '{"plan": "Pro", "price_inr": 499}'
    assert "repaired; was not valid JSON" in result.reason
    # The repair saw the schema, the reply and what was wrong, and it was paid for.
    (call,) = llm.calls
    prompt = call["messages"][-1]["content"]
    assert json.dumps(PLAN) in prompt and "price_inr: 499," in prompt and "not valid JSON" in prompt
    assert call["temperature"] == 0 and call["reasoning_effort"] == "minimal"
    assert result.cost_usd == pytest.approx((1000 * 0.30 + 200 * 2.50) / 1e6)


async def test_repair_that_is_still_wrong_blocks(fake_llm):
    fake_llm('{"plan": "Pro", "price_inr": "four ninety nine"}')
    result = await check('{"plan": "Pro", "price_inr": "499"}')
    assert result.action == Action.BLOCK
    assert "schema mismatch at price_inr" in result.reason
    assert "repair failed" in result.reason
    assert result.cost_usd > 0  # the failed attempt still cost money


async def test_repair_call_failing_blocks(fake_llm):
    fake_llm(LLMError("no API key configured"))
    result = await check('{"plan": "Pro"}')
    assert result.action == Action.BLOCK
    assert "required property" in result.reason and "no API key" in result.reason


async def test_without_repair_a_bad_reply_blocks_with_no_call(fake_llm):
    llm = fake_llm()
    result = await check('{"plan": "Pro", "price_inr": 499, "extra": true}', repair_once=False)
    assert result.action == Action.BLOCK
    assert "Additional properties" in result.reason
    assert llm.calls == []


async def test_invalid_client_schema_blocks():
    result = await check("{}", schema={"type": "not-a-type"})
    assert result.action == Action.BLOCK
    assert "response_schema is invalid" in result.reason


def test_schema_from_response_format():
    assert schema_from_response_format({"type": "json_object"}) == {"type": "object"}
    assert (
        schema_from_response_format(
            {"type": "json_schema", "json_schema": {"name": "plan", "schema": PLAN}}
        )
        == PLAN
    )
    assert schema_from_response_format({"type": "text"}) is None
    assert schema_from_response_format(None) is None


# Through the proxy.


def test_response_format_is_enforced_on_the_reply(make_client, fake_llm):
    client = make_client(enabled_policies="output_schema")
    fake_llm(LLMError("upstream down"))
    resp = chat(
        client,
        "Pro plan price as JSON",
        mock_response="The Pro plan costs 499 rupees.",
        response_format={"type": "json_schema", "json_schema": {"name": "p", "schema": PLAN}},
    )
    assert resp.headers["X-Dwarpal-Blocked-By"] == "output_schema@1.0.0"
    assert resp.json()["choices"][0]["finish_reason"] == "content_filter"
    # response_format is the client's own field, so it still went upstream.
    assert MockState.last_payload["response_format"]["type"] == "json_schema"


def test_dwarpal_schema_repairs_the_reply(make_client, fake_llm):
    client = make_client(enabled_policies="output_schema")
    fake_llm('{"plan": "Pro", "price_inr": 499}')
    resp = chat(
        client,
        "Pro plan price as JSON",
        mock_response="{'plan': 'Pro', 'price_inr': 499}",
        dwarpal={"response_schema": PLAN},
    )
    body = resp.json()
    assert "X-Dwarpal-Blocked-By" not in resp.headers
    assert json.loads(body["choices"][0]["message"]["content"]) == {"plan": "Pro", "price_inr": 499}
    (result,) = body["dwarpal"]["results"]
    assert result["action"] == "redact" and result["cost_usd"] > 0


def test_repo_policy_is_cheap_tier_and_fails_closed():
    assert POLICY.tier == "cheap" and POLICY.on_error == "fail_closed"
    assert Path(REPO_POLICIES / "output_schema.yaml").is_file()
