"""PR-06: the faithfulness guard. The judge is a FakeLLM; no API key needed."""

import json

import pytest

from dwarpal.guards.base import Action, GuardContext, Stage
from dwarpal.guards.faithfulness import FaithfulnessGuard, load_prompt, parse_verdict
from dwarpal.llm import LLMError
from dwarpal.policy import load_policy_file
from tests.conftest import REPO_POLICIES, chat

POLICY = load_policy_file(REPO_POLICIES / "faithfulness.yaml")
FAQ = [
    "Pro costs ₹499/month. Business costs ₹1,499/month.",
    "Annual plans can be refunded within 30 days. Monthly plans are not refunded.",
]


def verdict(*labels: str) -> str:
    return json.dumps(
        {"claims": [{"claim": f"claim {i}", "label": x} for i, x in enumerate(labels)]}
    )


def make_guard(**params) -> FaithfulnessGuard:
    return FaithfulnessGuard(POLICY.model_copy(update={"params": {**POLICY.params, **params}}))


def ctx(reply: str = "Pro costs ₹499 a month.", context=FAQ) -> GuardContext:
    return GuardContext(
        messages=[{"role": "user", "content": "How much is Pro?"}],
        response_text=reply,
        context_docs=list(context),
    )


async def check(context=FAQ, **params):
    return await make_guard(**params).check(ctx(context=context), Stage.OUTPUT)


async def test_no_context_means_no_judge_call(fake_llm):
    llm = fake_llm()
    result = await check(context=[])
    assert result.action == Action.ALLOW and result.reason == "no context supplied"
    assert llm.calls == []


async def test_supported_reply_is_allowed(fake_llm):
    fake_llm(verdict("SUPPORTED", "SUPPORTED"))
    result = await check()
    assert result.action == Action.ALLOW and result.score == 0.0


async def test_share_of_unsupported_claims_is_the_score(fake_llm):
    fake_llm(verdict("SUPPORTED", "SUPPORTED", "UNSUPPORTED"))
    result = await check()
    assert result.score == pytest.approx(1 / 3)
    assert result.action == Action.BLOCK  # threshold 0.30
    assert "1/3 claims not supported" in result.reason and "unsupported: claim 2" in result.reason


async def test_below_threshold_is_allowed(fake_llm):
    fake_llm(verdict("SUPPORTED", "SUPPORTED", "SUPPORTED", "UNSUPPORTED"))
    result = await check()
    assert result.score == 0.25 and result.action == Action.ALLOW


async def test_one_contradiction_blocks_however_long_the_reply(fake_llm):
    fake_llm(verdict("SUPPORTED", "SUPPORTED", "SUPPORTED", "SUPPORTED", "CONTRADICTED"))
    result = await check()
    assert result.score == 1.0 and result.action == Action.BLOCK


async def test_contradiction_rule_can_be_turned_off(fake_llm):
    fake_llm(verdict("SUPPORTED", "SUPPORTED", "SUPPORTED", "SUPPORTED", "CONTRADICTED"))
    result = await check(block_on_contradiction=False)
    assert result.score == 0.2 and result.action == Action.ALLOW


async def test_reply_without_claims_is_allowed(fake_llm):
    fake_llm('{"claims": []}')
    result = await check()
    assert result.action == Action.ALLOW and result.reason == "no factual claims"


async def test_judge_call_and_cost(fake_llm):
    llm = fake_llm("```json\n" + verdict("SUPPORTED") + "\n```")  # fenced output still parses
    result = await check()
    (call,) = llm.calls
    assert call["model"] == "gemini-2.5-flash"
    assert call["temperature"] == 0 and call["reasoning_effort"] == "none"
    assert call["response_format"]["type"] == "json_schema"
    prompt = call["messages"][0]["content"]
    assert "[1] Pro costs ₹499/month." in prompt and "[2] Annual plans" in prompt
    assert "<question>\nHow much is Pro?" in prompt
    assert "<reply>\nPro costs ₹499 a month." in prompt
    assert "{{" not in prompt  # every placeholder filled
    assert result.cost_usd == pytest.approx((1000 * 0.30 + 200 * 2.50) / 1e6)


@pytest.mark.parametrize(
    "bad",
    ["not json", '{"verdict": "fine"}', '{"claims": [{"claim": "x", "label": "MAYBE"}]}'],
)
def test_malformed_judge_output_raises(bad):
    with pytest.raises(LLMError):
        parse_verdict(bad)


def test_prompt_needs_every_placeholder(tmp_path):
    broken = tmp_path / "judge.txt"
    broken.write_text("Judge this: {{reply}}", encoding="utf-8")
    with pytest.raises(ValueError, match="context"):
        load_prompt(str(broken))
    assert "{{context}}" in load_prompt(POLICY.params["prompt"])


def test_repo_policy_runs_last_and_fails_open():
    assert POLICY.tier == "llm" and POLICY.on_error == "fail_open"
    assert POLICY.params["prompt"] == "prompts/faithfulness_judge.v1.txt"


# Through the proxy.


def test_unfaithful_reply_is_blocked(make_client, fake_llm):
    client = make_client(enabled_policies="faithfulness")
    fake_llm(verdict("CONTRADICTED"))
    resp = chat(
        client, "How much is Pro?", mock_response="Pro is $15/month.", dwarpal={"context": FAQ}
    )
    assert resp.headers["X-Dwarpal-Blocked-By"] == "faithfulness@1.0.0"
    assert resp.json()["dwarpal"]["guard_cost_usd"] > 0


def test_judge_failure_fails_open(make_client, fake_llm):
    client = make_client(enabled_policies="faithfulness")
    fake_llm(LLMError("judge unavailable"))
    resp = chat(client, "How much is Pro?", dwarpal={"context": FAQ})
    body = resp.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    (result,) = body["dwarpal"]["results"]
    assert result["error"] is True and "judge unavailable" in result["reason"]


def test_same_reply_and_context_is_judged_once(make_client, fake_llm):
    client = make_client(enabled_policies="faithfulness")
    llm = fake_llm(verdict("SUPPORTED"))
    first = chat(client, "How much is Pro?", dwarpal={"context": FAQ}).json()["dwarpal"]
    second = chat(client, "How much is Pro?", dwarpal={"context": FAQ}).json()["dwarpal"]
    assert len(llm.calls) == 1
    assert first["results"][0]["cached"] is False and first["guard_cost_usd"] > 0
    assert second["results"][0]["cached"] is True and second["guard_cost_usd"] == 0
