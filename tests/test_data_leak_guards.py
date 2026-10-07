"""PR-04: pii and secrets guards.

Runs the shipped policy files with the name model switched off, so tests stay offline. The
model is covered by a stub here and for real by `make eval` and one opt-in test. Vendor-format
keys are built from two halves at runtime so push protection doesn't trip on this file.
"""

from pathlib import Path

import pytest
import yaml

from dwarpal.guards.base import Action, GuardContext, Stage
from dwarpal.guards.ner import Entity
from dwarpal.guards.pii import PiiGuard, luhn_ok, verhoeff_ok
from dwarpal.guards.redaction import Redactor, Span, resolve_overlaps
from dwarpal.guards.secrets import SecretsGuard, shannon_bits
from dwarpal.policy import Policy
from tests.conftest import REPO_POLICIES, chat


def shipped_data(name: str) -> dict:
    data = yaml.safe_load((REPO_POLICIES / f"{name}.yaml").read_text())
    if name == "pii":
        data["params"]["ner"]["enabled"] = False
    return data


def shipped(name: str, **overrides) -> Policy:
    data = shipped_data(name)
    data.update(overrides)
    return Policy.model_validate(data)


@pytest.fixture
def offline_policies(tmp_path: Path) -> Path:
    for name in ("pii", "secrets"):
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(shipped_data(name)))
    return tmp_path


def user(text: str) -> GuardContext:
    return GuardContext(messages=[{"role": "user", "content": text}])


def reply(text: str) -> GuardContext:
    return GuardContext(messages=[{"role": "user", "content": "hi"}], response_text=text)


PII = PiiGuard(shipped("pii"))
SECRETS = SecretsGuard(shipped("secrets"))


# --- checksums ---


def test_verhoeff_accepts_valid_and_rejects_one_digit_off():
    assert verhoeff_ok("7342 9158 6061")
    assert not verhoeff_ok("7342 9158 6062")


def test_luhn():
    assert luhn_ok("4111 1111 1111 1111")
    assert not luhn_ok("4111 1111 1111 1112")


# --- pii: input ---


async def test_redacts_with_numbered_placeholders_and_keeps_the_sentence():
    result = await PII.check(
        user("Mail arjun@example.com and cc arjun@example.com, then call +91 98765 43210."),
        Stage.INPUT,
    )
    assert result.action == Action.REDACT
    assert result.redacted_messages[0]["content"] == (
        "Mail <EMAIL_1> and cc <EMAIL_1>, then call <PHONE_1>."
    )
    assert "arjun" not in result.reason  # reasons name types, never values


@pytest.mark.parametrize(
    "text, entity",
    [
        ("card 4111 1111 1111 1111 failed", "CARD"),
        ("PAN KPRTS4821M on file", "PAN"),
        ("mobile 9123456780", "PHONE"),
        ("pay at priya.k@okhdfcbank", "UPI"),
    ],
)
async def test_each_entity_is_redacted(text, entity):
    result = await PII.check(user(text), Stage.INPUT)
    assert result.action == Action.REDACT
    assert f"<{entity}_1>" in result.redacted_messages[0]["content"]


async def test_aadhaar_blocks_instead_of_redacting():
    result = await PII.check(user("My Aadhaar is 7342 9158 6061"), Stage.INPUT)
    assert result.action == Action.BLOCK
    assert "AADHAAR" in result.reason


@pytest.mark.parametrize(
    "text",
    [
        "Invoice INV-2024-000123 for ₹12,500 is unpaid since 3 March 2025.",
        "Our GSTIN is 29ABCDE1234F1Z5 and IFSC HDFC0000123.",  # GSTIN embeds a PAN
        "Email support@ledgerly.example about ticket 48213.",  # allow-listed
        "Job 3f6c2a9e-8b1d-4c7a-9e2f-5d4b3a1c0e98 has 12 items.",
        "Card ending 4111 1111 1111 1112 is not Luhn-valid.",
        "Aadhaar-shaped 7342 9158 6062 fails Verhoeff.",
    ],
)
async def test_hard_negatives_pass(text):
    result = await PII.check(user(text), Stage.INPUT)
    assert result.action == Action.ALLOW, result.reason


async def test_only_user_turns_are_redacted_and_content_parts_work():
    ctx = GuardContext(
        messages=[
            {"role": "system", "content": "Escalations go to ops@example.com."},
            {"role": "user", "content": [{"type": "text", "text": "I am a@example.com"}]},
        ]
    )
    result = await PII.check(ctx, Stage.INPUT)
    system, usr = result.redacted_messages
    assert system["content"] == "Escalations go to ops@example.com."
    assert usr["content"][0]["text"] == "I am <EMAIL_1>"
    assert ctx.messages[1]["content"][0]["text"] == "I am a@example.com"  # input not mutated


def test_db_password_is_not_mistaken_for_an_email():
    assert PII.find("postgres://app:hunter22@db.example.com/x") == []


def test_unknown_entity_in_policy_fails_at_startup():
    with pytest.raises(ValueError, match="SSN"):
        PiiGuard(shipped("pii", params={"entities": ["SSN"]}))


async def test_flag_policy_flags_instead_of_redacting():
    guard = PiiGuard(shipped("pii", action="flag"))
    result = await guard.check(user("mail a@example.com"), Stage.INPUT)
    assert result.action == Action.FLAG


@pytest.mark.parametrize(
    "text, caught",
    [
        ("call me on 9123456780", True),
        ("my mobile no. 98200 11223", True),
        ("order 9123456780 is late", False),  # no cue word: could be any id
        ("ref 9876543210 from the bank statement", False),
    ],
)
async def test_bare_phone_needs_a_cue_word(text, caught):
    result = await PII.check(user(text), Stage.INPUT)
    assert result.caught is caught, result.reason


async def test_street_address_is_redacted():
    result = await PII.check(
        user("ship it to flat 7B Carter Road, Mumbai 400050 please"), Stage.INPUT
    )
    assert result.redacted_messages[0]["content"] == "ship it to <ADDRESS_1> please"


class StubTagger:
    def __init__(self, *spans: tuple[str, str, float]):
        self.spans = spans

    def entities(self, text: str) -> list[Entity]:
        out = []
        for word, label, score in self.spans:
            i = text.find(word)
            if i >= 0:
                out.append(Entity(i, i + len(word), label, score))
        return out


def with_names(*spans) -> PiiGuard:
    data = shipped_data("pii")
    data["params"]["ner"]["enabled"] = True
    guard = PiiGuard(Policy.model_validate(data))
    guard.tagger = StubTagger(*spans)
    return guard


async def test_names_from_the_model_are_redacted():
    guard = with_names(("Meera Nair", "PER", 0.89), ("Mumbai", "LOC", 0.99))
    result = await guard.check(user("Invoice Meera Nair in Mumbai"), Stage.INPUT)
    assert result.redacted_messages[0]["content"] == "Invoice <NAME_1> in Mumbai"


async def test_one_word_name_needs_a_cue():
    guard = with_names(("Mock", "PER", 0.9), ("Priya", "PER", 0.9))
    alone = await guard.check(reply("Mock answer to: how do I export?"), Stage.OUTPUT)
    assert alone.action == Action.ALLOW
    cued = await guard.check(reply("Please ask Priya about the export."), Stage.OUTPUT)
    assert cued.redacted_text == "Please ask <NAME_1> about the export."


async def test_never_names_low_scores_and_partial_words():
    guard = with_names(("Ledgerly", "PER", 0.97), ("Arjun", "PER", 0.3), ("adhaar", "PER", 0.9))
    result = await guard.check(user("Ledgerly says Arjun must send the aadhaar form"), Stage.INPUT)
    assert result.action == Action.ALLOW, result.reason


# --- pii: the user's own values come back in the reply ---


async def test_reply_gets_the_users_own_values_back():
    ctx = user("I am a@example.com")
    first = await PII.check(ctx, Stage.INPUT)
    assert first.redacted_messages[0]["content"] == "I am <EMAIL_1>"
    ctx.response_text = "Done, we will mail <EMAIL_1>."  # same context, like the pipeline
    result = await PII.check(ctx, Stage.OUTPUT)
    assert result.redacted_text == "Done, we will mail a@example.com."
    assert result.score == 0.0


async def test_new_pii_in_reply_is_still_redacted_after_restore():
    ctx = user("I am a@example.com")
    await PII.check(ctx, Stage.INPUT)
    ctx.response_text = "<EMAIL_1>, also ping b@example.com"
    result = await PII.check(ctx, Stage.OUTPUT)
    assert result.redacted_text == "a@example.com, also ping <EMAIL_1>"
    assert result.score == 1.0


async def test_values_never_cross_requests():
    await PII.check(user("I am a@example.com"), Stage.INPUT)
    result = await PII.check(reply("mail <EMAIL_1>"), Stage.OUTPUT)  # a different request
    assert result.action == Action.ALLOW


async def test_restore_can_be_turned_off():
    guard = PiiGuard(
        shipped("pii", params={**shipped_data("pii")["params"], "restore_in_reply": False})
    )
    ctx = user("I am a@example.com")
    await guard.check(ctx, Stage.INPUT)
    ctx.response_text = "mail <EMAIL_1>"
    assert (await guard.check(ctx, Stage.OUTPUT)).action == Action.ALLOW


# --- pii: output ---


async def test_reply_is_redacted_but_invoice_id_survives():
    result = await PII.check(
        reply("Raised by Sanjay (sanjay@example.com) on INV-2024-000417."), Stage.OUTPUT
    )
    assert result.redacted_text == "Raised by Sanjay (<EMAIL_1>) on INV-2024-000417."


# --- secrets ---

LEDGERLY_KEY = "lg_live_" + "4be19f0c7a2d4e83b56f91c0d27a8e3f"
AWS_KEY = "AKIA" + "Q3EXAMPLE7DWARPAL"[:16]
GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow..."


@pytest.mark.parametrize(
    "text",
    [
        f"Use the service key {LEDGERLY_KEY}.",
        f"aws key {AWS_KEY}",
        f"token {GITHUB_TOKEN}",
        PEM,
        "Connect with mysql://reports:Ledg3r!2024@10.0.4.12:3306/billing",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.Qm9ndXNTaWduYXR1cmU",
        "password = Tr0ub4dor&3",
    ],
)
async def test_leaks_in_replies_are_blocked(text):
    result = await SECRETS.check(reply(text), Stage.OUTPUT)
    assert result.action == Action.BLOCK, f"{text!r}: {result.reason}"


async def test_pasted_key_in_input_is_redacted_not_blocked():
    result = await SECRETS.check(
        user(f"Config: LEDGERLY_API_KEY={LEDGERLY_KEY} region=ap-south-1. What's wrong?"),
        Stage.INPUT,
    )
    assert result.action == Action.REDACT
    assert result.redacted_messages[0]["content"] == (
        "Config: LEDGERLY_API_KEY=<SECRET_1> region=ap-south-1. What's wrong?"
    )


async def test_connection_string_redacts_only_the_password():
    result = await SECRETS.check(user("db is postgres://app:S3cr3t!x@db:5432/x"), Stage.INPUT)
    assert result.redacted_messages[0]["content"] == "db is postgres://app:<SECRET_1>@db:5432/x"


async def test_random_token_alone_does_not_block_but_with_a_keyword_does():
    token = "Zq8Lm2Xv7Rp4Tz9Bn1Ky6Hc3Wd5Fg0J"
    alone = await SECRETS.check(reply(f"Reference {token} attached."), Stage.OUTPUT)
    assert alone.action == Action.ALLOW
    keyed = await SECRETS.check(reply(f"Your signing secret is {token}."), Stage.OUTPUT)
    assert keyed.action == Action.BLOCK


async def test_keyword_must_come_before_the_token():
    sha = "3f6c2a9e8b1d4c7a9e2f5d4b3a1c0e98aa11bb22"
    after = await SECRETS.check(reply(f"Commit {sha} fixed the token refresh bug."), Stage.OUTPUT)
    assert after.action == Action.ALLOW
    before = await SECRETS.check(reply(f"The webhook token is {sha}."), Stage.OUTPUT)
    assert before.action == Action.BLOCK  # hex keys count too, with their own entropy limit


@pytest.mark.parametrize(
    "text",
    [
        "Set api_key = your_api_key_123 in the config file.",
        "Use password: changeme2024 for the sandbox, then change it.",
        "export LEDGERLY_TOKEN=${LEDGERLY_TOKEN_2}",
    ],
)
async def test_template_values_are_not_secrets(text):
    result = await SECRETS.check(reply(text), Stage.OUTPUT)
    assert result.action == Action.ALLOW, f"{text!r}: {result.reason}"


async def test_each_user_turn_is_scored_alone():
    token = "Zq8Lm2Xv7Rp4Tz9Bn1Ky6Hc3Wd5Fg0J"
    ctx = GuardContext(
        messages=[
            {"role": "user", "content": "what is a signing secret?"},
            {"role": "assistant", "content": "It verifies webhooks."},
            {"role": "user", "content": f"ok, reference {token}"},
        ]
    )
    result = await SECRETS.check(ctx, Stage.INPUT)
    assert result.action == Action.ALLOW  # the keyword and the token are in different turns


@pytest.mark.parametrize(
    "text",
    [
        "Your API key is under Settings → API; click Regenerate if you lost it.",
        "Send it as Authorization: Bearer <YOUR_API_KEY>.",
        "Export job a91f3c07-55e2-4d1b-b8a0-2c6e9f4d7b13 is queued.",
        "Commit 3f6c2a9e8b1d4c7a9e2f5d4b3a1c0e98aa11bb22 fixed the token refresh bug.",
        "Password: must be at least 12 characters.",
    ],
)
async def test_secret_hard_negatives_pass(text):
    result = await SECRETS.check(reply(text), Stage.OUTPUT)
    assert result.action == Action.ALLOW, f"{text!r}: {result.reason}"


def test_entropy_separates_keys_from_words():
    assert shannon_bits("Zq8Lm2Xv7Rp4Tz9Bn1Ky6Hc3Wd5Fg0J") > 4.0
    assert shannon_bits("invoice_reminder_settings_page") < 4.0


def test_bad_secret_pattern_fails_at_startup():
    with pytest.raises(ValueError, match="weight"):
        SecretsGuard(shipped("secrets", params={"patterns": [{"name": "x", "regex": "x"}]}))


# --- redaction helper ---


def test_longest_overlapping_span_wins():
    spans = [Span(0, 14, "AADHAAR", "x"), Span(0, 19, "CARD", "y"), Span(20, 25, "PAN", "z")]
    assert [s.entity for s in resolve_overlaps(spans)] == ["CARD", "PAN"]


def test_placeholders_count_per_entity():
    r = Redactor()
    assert [
        r.placeholder("EMAIL", "a"),
        r.placeholder("PAN", "b"),
        r.placeholder("EMAIL", "c"),
    ] == [
        "<EMAIL_1>",
        "<PAN_1>",
        "<EMAIL_2>",
    ]


# --- through the proxy ---


def test_upstream_sees_redacted_input_and_user_gets_values_back(make_client, offline_policies):
    from dwarpal.testing.mock_upstream import MockState

    client = make_client(offline_policies)
    resp = chat(client, f"I am a@example.com and my key is {LEDGERLY_KEY}")
    assert resp.status_code == 200
    sent = MockState.last_payload["messages"][0]["content"]
    assert sent == "I am <EMAIL_1> and my key is <SECRET_1>"
    assert "X-Dwarpal-Blocked-By" not in resp.headers
    # The mock echoes the prompt; the email comes back, the key stays hidden.
    answer = resp.json()["choices"][0]["message"]["content"]
    assert answer == "Mock answer to: I am a@example.com and my key is <SECRET_1>"


def test_secret_in_reply_is_blocked_at_proxy(make_client, offline_policies):
    client = make_client(offline_policies)
    resp = chat(client, "What key should I use?", mock_response=f"Use {LEDGERLY_KEY}.")
    assert resp.headers["X-Dwarpal-Blocked-By"] == "secrets@1.0.1"
    assert resp.json()["choices"][0]["finish_reason"] == "content_filter"
    assert LEDGERLY_KEY not in resp.text


# --- the real name model, opt-in because it downloads ~250 MB ---


@pytest.mark.skipif(
    not Path.home().joinpath(".cache/huggingface/hub/models--dslim--distilbert-NER").exists(),
    reason="name model not cached locally; `make eval` covers it",
)
async def test_real_name_model_finds_indian_names():
    pytest.importorskip("onnxruntime")
    data = shipped_data("pii")
    data["params"]["ner"]["enabled"] = True
    guard = PiiGuard(Policy.model_validate(data))
    await guard.setup()
    result = await guard.check(
        user("Raise an invoice for Meera Nair, then email Rohit Verma."), Stage.INPUT
    )
    assert result.redacted_messages[0]["content"] == (
        "Raise an invoice for <NAME_1>, then email <NAME_2>."
    )


# --- regressions from the PR-04 review ---


async def test_db_password_with_bang_is_not_turned_into_an_email():
    url = "mysql://reports:Ledg3r!2024@db.example.com/billing"
    assert PII.find(url) == []
    result = await SECRETS.check(user(f"why does {url} fail?"), Stage.INPUT)
    assert result.redacted_messages[0]["content"] == (
        "why does mysql://reports:<SECRET_1>@db.example.com/billing fail?"
    )


def test_pii_then_secrets_still_blocks_a_leaked_db_url(make_client, offline_policies):
    client = make_client(offline_policies)
    leak = "Connect with mysql://reports:Ledg3r!2024@db.example.com/billing"
    resp = chat(client, "how do I query directly?", mock_response=leak)
    assert resp.headers["X-Dwarpal-Blocked-By"] == "secrets@1.0.1"


@pytest.mark.parametrize("text", ["mailto:arjun@example.com", "Email:arjun@example.com"])
async def test_email_after_a_colon_is_found(text):
    result = await PII.check(user(text), Stage.INPUT)
    assert "<EMAIL_1>" in result.redacted_messages[0]["content"]


async def test_secret_split_across_content_parts_is_redacted():
    token = "Zk3qP9vX2mL7rT8wQ4nB6yH1jC5dF0aS"
    ctx = GuardContext(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "my api token is"},
                    {"type": "text", "text": token},
                ],
            }
        ]
    )
    result = await SECRETS.check(ctx, Stage.INPUT)
    assert result.action == Action.REDACT
    assert result.redacted_messages[0]["content"][1]["text"] == "<SECRET_1>"


async def test_whole_pem_block_is_redacted():
    pem = "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBg\nab12==\n-----END " + "PRIVATE KEY-----"
    result = await SECRETS.check(user(f"is this right?\n{pem}\nthanks"), Stage.INPUT)
    assert result.redacted_messages[0]["content"] == "is this right?\n<SECRET_1>\nthanks"


async def test_reason_counts_each_value_once():
    result = await PII.check(user("call 9123456780, 9123456781 or 9123456782"), Stage.INPUT)
    assert result.reason == "found PHONE×3"


async def test_shadow_run_does_not_touch_restore_state():
    shadow = PiiGuard(shipped("pii", version="1.1.0", mode="shadow"))
    ctx = user("I am a@example.com")
    await PII.check(ctx, Stage.INPUT)
    await shadow.check(ctx, Stage.INPUT)
    assert list(ctx.state) == ["pii@1.0.1"]


async def test_spans_are_found_once_per_turn(monkeypatch):
    guard = PiiGuard(shipped("pii"))
    calls = []
    real = guard.find
    monkeypatch.setattr(guard, "find", lambda t: calls.append(t) or real(t))
    ctx = GuardContext(
        messages=[
            {"role": "user", "content": "no pii here"},
            {"role": "user", "content": "mail a@example.com"},
        ]
    )
    await guard.check(ctx, Stage.INPUT)
    assert sorted(calls) == ["mail a@example.com", "no pii here"]


def test_windows_cover_every_token_and_stay_under_the_limit():
    from types import SimpleNamespace

    from dwarpal.guards.windows import token_windows

    class OneTokenPerWord:  # stands in for a real tokenizer
        def encode(self, text):
            offsets, pos = [], 0
            for word in text.split(" "):
                offsets.append((pos, pos + len(word)))
                pos += len(word) + 1
            return SimpleNamespace(offsets=offsets)

    text = "x " * 1200 + "for Meera Nair"
    ranges = token_windows(OneTokenPerWord(), text, max_tokens=500, overlap=64)
    assert all(len(text[a:b].split()) <= 500 for a, b in ranges)
    assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
    assert "Meera Nair" in text[ranges[-1][0] : ranges[-1][1]]


@pytest.mark.skipif(
    not Path.home().joinpath(".cache/huggingface/hub/models--dslim--distilbert-NER").exists(),
    reason="name model not cached locally",
)
def test_real_model_reads_past_512_tokens():
    from dwarpal.guards.ner import get_tagger

    params = shipped_data("pii")["params"]["ner"]
    tagger = get_tagger(params["model"], params["revision"])
    faq = (REPO_POLICIES.parent / "demo" / "ledgerly_faq.md").read_text()
    text = faq + "\n" + faq + "\nPlease email Rohit Verma today."  # about 1200 tokens
    assert len(tagger.full_tokenizer.encode(text).ids) > 1000
    tail = text.index("Rohit Verma")
    assert any(e.start <= tail < e.end for e in tagger.entities(text))


def test_a_word_gets_the_average_label_of_its_subtokens():
    """ "R" ORG, "##oh" PER, "##it" PER: the word is a person. No model needed."""
    from types import SimpleNamespace

    import numpy as np

    from dwarpal.guards import ner

    labels = {0: "O", 1: "B-PER", 2: "I-PER", 3: "B-ORG", 4: "I-ORG"}
    rows = {"R": 3, "##oh": 1, "##it": 1, "Verma": 2}  # the most likely label per sub-token
    tokens = ["[CLS]", "email", "R", "##oh", "##it", "Verma", "[SEP]"]
    text = "email Rohit Verma"
    enc = SimpleNamespace(
        ids=list(range(len(tokens))),
        attention_mask=[1] * len(tokens),
        word_ids=[None, 0, 1, 1, 1, 2, None],
        offsets=[(0, 0), (0, 5), (6, 7), (7, 9), (9, 11), (12, 17), (0, 0)],
    )
    logits = np.full((len(tokens), len(labels)), -5.0)
    for i, tok in enumerate(tokens):
        logits[i, rows.get(tok, 0)] = 5.0

    tagger = ner.NameTagger.__new__(ner.NameTagger)
    tagger.tokenizer = SimpleNamespace(encode=lambda t: enc)
    tagger.session = SimpleNamespace(run=lambda _, feed: [logits[None]])
    tagger.input_names = {"input_ids", "attention_mask"}
    tagger.id2label = labels
    found = tagger._window(text, 0)
    assert [(text[e.start : e.end], e.label) for e in found] == [("Rohit Verma", "PER")]


# --- Indian address shapes and "password is X" ---


@pytest.mark.parametrize(
    "text",
    [
        "address is Flat 302, Sai Krupa Apartments, 14th Cross, HSR Layout, Bengaluru 560102",
        "ship to Plot 7B, MIDC Area, Andheri East, Mumbai 400093",
        "deliver at 12 2nd Main Road, Bengaluru",
    ],
)
async def test_indian_address_shapes(text):
    result = await PII.check(user(text), Stage.INPUT)
    assert "<ADDRESS_1>" in result.redacted_messages[0]["content"], result.reason


@pytest.mark.parametrize(
    "text",
    [
        "our Pune office handles invoices above 100000 every month",
        "flat fee of 499 for invoices over 250000",
        "house rent invoice for 18000, client in 560102 area?",
    ],
)
async def test_amounts_and_pins_alone_are_not_addresses(text):
    assert (await PII.check(user(text), Stage.INPUT)).action == Action.ALLOW


async def test_password_is_value():
    result = await SECRETS.check(user("my password is Ganesha@2024# and it fails"), Stage.INPUT)
    assert result.redacted_messages[0]["content"] == "my password is <SECRET_1> and it fails"
    plain = await SECRETS.check(user("my password is not working after reset"), Stage.INPUT)
    assert plain.action == Action.ALLOW


# --- disguised data in replies ---


async def test_spaced_out_key_in_reply_is_blocked():
    from eval.augment import to_spaced

    result = await SECRETS.check(reply(to_spaced(f"the key is {LEDGERLY_KEY}")), Stage.OUTPUT)
    assert result.action == Action.BLOCK


async def test_spaced_out_email_in_reply_is_blocked():
    result = await PII.check(reply("write to s a n j a y @ e x a m p l e . c o m"), Stage.OUTPUT)
    assert result.action == Action.BLOCK
    assert "disguised" in result.reason


def test_classifier_files_fall_back_to_repo_root_offline():
    from huggingface_hub.utils import LocalEntryNotFoundError

    from dwarpal.guards.classifier import fetch_model_file

    asked = []

    def download(model_id, name, revision=None):
        asked.append(name)
        if name.startswith("onnx/"):
            raise LocalEntryNotFoundError("not cached")
        return f"/cache/{name}"

    assert (
        fetch_model_file(download, "org/model", "tokenizer.json", "abc") == "/cache/tokenizer.json"
    )
    assert asked == ["onnx/tokenizer.json", "tokenizer.json"]


# --- ids that look like plain numbers or dates need a cue word ---


@pytest.mark.parametrize(
    "text, entity",
    [
        ("my a/c no is 50100234567812, IFSC HDFC0000123", "BANK_ACCOUNT"),
        ("pay to IBAN GB82 WEST 1234 5698 7654 32 please", "IBAN"),
        ("DOB 14/08/1991 for the KYC form", "DOB"),
        ("I was born on 3 March 1988", "DOB"),
        ("passport number J8369854 expires next year", "PASSPORT"),
        ("my voter id is ABC1234567", "VOTER_ID"),
    ],
)
async def test_cued_ids_are_redacted(text, entity):
    result = await PII.check(user(text), Stage.INPUT)
    assert f"<{entity}_1>" in result.redacted_messages[0]["content"], result.reason


@pytest.mark.parametrize(
    "text",
    [
        "order 501002345678 was delivered on 14/08/2025",  # long number and a date, no cue
        "invoice dated 3 March 2025 for 120000",
        "IBAN GB82 WEST 1234 5698 7654 33 fails, why?",  # one digit off: mod-97 fails
        "is my passport needed for KYC?",
        "who is a voter in GST terms?",
    ],
)
async def test_uncued_numbers_and_dates_pass(text):
    result = await PII.check(user(text), Stage.INPUT)
    assert result.action == Action.ALLOW, result.reason


async def test_any_url_with_a_password_is_caught():
    result = await SECRETS.check(
        reply("open https://admin:Hunter22x@billing.ledgerly.example"), Stage.OUTPUT
    )
    assert result.action == Action.BLOCK


# --- from the Copilot review on #4 ---


async def test_restored_value_in_history_is_redacted_again():
    ctx = GuardContext(
        messages=[
            {"role": "user", "content": "I am a@example.com"},
            {"role": "assistant", "content": "Got it, a@example.com is on file."},  # restored reply
            {"role": "user", "content": "thanks, now update my plan"},
        ]
    )
    result = await PII.check(ctx, Stage.INPUT)
    sent = [m["content"] for m in result.redacted_messages]
    assert sent == ["I am <EMAIL_1>", "Got it, <EMAIL_1> is on file.", "thanks, now update my plan"]


@pytest.mark.parametrize(
    "guard, parts, hidden",
    [
        (PII, ["my phone is", "9123456780"], "9123456780"),
        (SECRETS, ["my password is", "Hunter22x"], "Hunter22x"),
    ],
)
async def test_value_split_from_its_cue_across_parts(guard, parts, hidden):
    content = [{"type": "text", "text": t} for t in parts]
    result = await guard.check(
        GuardContext(messages=[{"role": "user", "content": content}]), Stage.INPUT
    )
    assert result.action == Action.REDACT
    assert hidden not in str(result.redacted_messages)


async def test_plain_and_spaced_email_in_one_reply_blocks():
    result = await PII.check(
        reply("mail a@example.com or s a n j a y @ e x a m p l e . c o m"), Stage.OUTPUT
    )
    assert result.action == Action.BLOCK


def test_demo_not_mounted_when_api_keys_are_set(make_client):
    pytest.importorskip("gradio")
    client = make_client(demo_enabled=True, api_keys="secret-key")
    assert client.get("/demo/").status_code == 404
