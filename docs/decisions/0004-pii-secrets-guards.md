# 0004: PII and secrets guards

**Status:** accepted · **Owner:** Yash · **PR:** 04

## Decision

**pii** (input and output) finds personal data and replaces it with numbered placeholders
(`<EMAIL_1>`, `<NAME_1>`). The same value keeps the same placeholder within a request, so the
model can still follow the sentence. Aadhaar blocks instead (`params.block_on`): there are
legal limits on sharing it, so the user should be told not to send it, not have it dropped
quietly.

| Entity | How it is found |
|---|---|
| EMAIL, UPI | regex (UPI: known bank handles only) |
| PHONE | +91 / 0 prefix, or a bare 10 digit number with a cue word ("phone", "mobile") just before it |
| PAN | regex, never inside a longer run (a GSTIN contains a PAN) |
| AADHAAR, CARD | regex + checksum (Verhoeff, Luhn) |
| IBAN | regex + mod-97 checksum (Ledgerly bills EUR and GBP clients) |
| BANK_ACCOUNT, DOB, PASSPORT, VOTER_ID | regex, only with a cue word just before ("a/c", "DOB", "passport", "voter") |
| ADDRESS | number + street words + Road / Marg / Cross..., or "Flat/Plot <no> ... <6 digit PIN>" |
| NAME | `dslim/distilbert-NER` (ONNX), person tags only |

When the model's reply uses a placeholder, the user gets their own value back. New personal
data in the reply (another customer's email) is still redacted. The values live on the
request's `GuardContext.state`, so nothing is kept after the request ends and nothing can cross
to another request.

**secrets** (input and output) combines weighted patterns from the YAML (vendor key formats,
`user:password@host`, `key = value`, `Bearer ...`) with an entropy check for random looking
tokens, noisy-OR like PR-03. A random token alone (0.60) does not block; with "secret",
"token" or "password" just before it (0.90) it does. On output it blocks, a reply that leaks a
key is not worth repairing. On input it redacts, so a user who pastes their own key while
debugging still gets an answer but the key never reaches the model.

## Ideas taken from existing tools

| System | Idea | Where it is here |
|---|---|---|
| Presidio | Checksums on structured ids; context words near a match; allow-lists | Verhoeff / Luhn, phone and one-word-name cues, `allow`, `never_names` |
| LLM Guard | BERT NER for names; a vault that restores the user's values in the reply | `ner.py`, `restore_in_reply` |
| gitleaks | Keyword prefilter before the expensive check; entropy on the secret part only; stopwords | name prefilter, `secret` group, `stopwords` |
| detect-secrets | Separate entropy limits for hex and base64; keyword detector | `min_bits_hex`, `min_bits_base64`, keyword before the token |

## Choosing the name detector (measured on our dev + holdout cases)

| Model | Real names found | Safe text taken for a name | p50 |
|---|---|---|---|
| spaCy `en_core_web_sm` | 3 / 4 (missed "Meera Nair") | GSTIN, Android, "Click Regenerate" | fast |
| **dslim/distilbert-NER** | **4 / 4** | Ledgerly, Aadhaar (now in `never_names`) | 21 ms |
| dslim/bert-base-NER | 4 / 4 | Ledgerly, Aadhaar | 45 ms |

spaCy would have pushed pii's false positive rate from 0 to 8.8%. distilbert-NER runs on the
`onnxruntime` + `tokenizers` stack PR-03 already added, so it brings no new dependency.
Three rules on top of the model:

- A name must be two words, or one word right after a cue ("for", "client", "Mr"). Without
  this, "Mock answer to: ..." made every mock reply look like it named a person.
- Surnames often get another label ("Meera" PER, "Nair" ORG), so up to two capitalised words
  after a name are taken as part of it.
- The model only runs when the text could hold such a name (about 1 in 7 eval texts), which
  took pii from 22 ms to under 1 ms p50.

## What we don't treat as PII

| Value | Why not |
|---|---|
| GSTIN (`27AAPFU0939F1ZV`) | Public business id printed on every invoice. Users ask about it constantly (`safe-003`, `safe-030`, `hold-019`). |
| IFSC (`HDFC0000123`) | Identifies a bank branch, not a person. |
| Invoice ids, amounts, dates | Long digit runs, but no checksum passes. |
| City names | "Mumbai" alone says nothing about a person. Street addresses are redacted. |
| `support@ledgerly.example` | Our own public address. |

## Alternatives considered

| Option | Why not |
|---|---|
| Presidio as a library | Its value is the recogniser framework and NER. We needed Indian formats and checksums (our own code anyway) and a better NER than its spaCy default. |
| Block on every PII hit | Users type their own email and phone all the time. Redaction keeps them helped. |
| Redact Aadhaar like the rest | We would be accepting a number the user should not be sending at all. |
| Block secrets on input too | Turns away a user who made an honest mistake. Redacting removes the risk and keeps the answer. |
| Entropy check only | Flags UUIDs, commit hashes and base64 blobs. It needs a keyword before it. |
| Keep restore values in a guard-level dict | Hidden shared state, PII lingering after blocked requests. The request context is the right owner. |

## Numbers (`make eval`, dev set)

| Policy | Attacks caught | Safe cases blocked | p50 |
|---|---|---|---|
| pii@1.0.0 | 10 / 10 (95% CI 0.72-1.00) | 0 / 44 | 0.1 ms (about 25 ms when a name is present) |
| secrets@1.0.0 | 6 / 6 (95% CI 0.61-1.00) | 0 / 44 | 0.02 ms |

Holdout (written before this PR): pii 2/2, secrets 2/2, no false positives on 15 safe cases.
`scripts/smoke_data_leak.py --mock` checks the same behaviour against a running server; it
passes 8/8 locally and inside the Docker image.

## Disguised data

Replies are also checked after normalisation (see 0005), and a hit there blocks,
since "a r j u n @ ..." or a key with spaces in it is data being smuggled out. Input is not
normalised for these two guards: a user does not disguise their own email.

## Considered and left out

| Idea | Why not now |
|---|---|
| IP addresses | Version strings look the same; little value for an invoicing bot. |
| Crypto wallets, vehicle numbers | Not part of Ledgerly support traffic. |
| Live secret verification (TruffleHog style) | Sends the user's key to the vendor to test it. |

## Known gaps

- **Disguised data in user input is not caught** (robustness rows: pii and secrets score 0 on
  spaced and base64 input). Replies are covered, see above.
- **The name model is unsure about Indian names in business text.** "email Rohit Verma today."
  can come back as an organisation, while "... today about the refund." comes back as a person.
  Organisations are not redacted, because that would also hit "Zoho Books" or "Razorpay".
- **The model can sometimes guess a redacted value from context** ("my email is <EMAIL_1>,
  same as my name at gmail"). Redaction removes the value, not every hint about it.
- **Restore trusts user turns.** It assumes a user message holds the user's own data. An app
  that pastes retrieved documents into a user message should send them as `dwarpal.context`.
- **If the name model can't be downloaded, startup fails**, the same as the PR-03 classifier.
  For a privacy guard we prefer a loud failure to silently running without name detection.
  PR-05 bakes the model into the image, so a running Space never downloads it.

## Found while testing, fixed in this PR

- **PR-03's classifier blocked plain account questions** ("please confirm my email address",
  1.00). Swapped for a better model, with the evidence in [0005](0005-injection-classifier-swap.md).
- **ONNX threads busy-waited by default**, slowing every model in the process about 4x. Off in
  both `ner.py` and `classifier.py`.
- **The end-to-end eval counted shadow and restore results as catches.** It now counts only
  enforced catches (`eval/harness.py`).

## Notes for the PRs after this one

- **PR-05 (Om):** bake `dslim/distilbert-NER` and `Horizon-Labs/prompt-injection-guard-base`
  (revisions in the policy files) into the image, and set `HF_HUB_OFFLINE=1` at run time.
  The demo calls the proxy from 127.0.0.1, so a per-IP rate limit treats every demo visitor as
  one client; the daily cap is what protects the budget there.
- **PR-06 (Kartik):** pii is `tier: model`. `asyncio.to_thread` work can't be cancelled, so a
  `timeout_ms` stops waiting but the inference keeps running. jailbreak and prompt_injection
  run the same model on the same text; sharing one result halves their cost.
- **PR-07 (Divyanshu):** a result with `restored: true` gave the user their own values back; it
  is not a catch. `GuardContext.state` holds raw user values for one request, never log it.

