# 0007: Output schema and faithfulness guards, and a faster pipeline

**Status:** accepted · **Owner:** Divyanshu · **PR:** 06 (owner swapped with PR-07)

## Decision

**`output_schema@1.0.0`** (output, `tier: cheap`, fails closed). Runs only when the request
asks for JSON: `dwarpal.response_schema`, or the OpenAI `response_format` the client already
sends (`json_schema` → its schema, `json_object` → `{"type": "object"}`). The reply has one
surrounding code fence stripped, is parsed, and is validated with `jsonschema` (the draft the
schema declares, 2020-12 otherwise).

- Valid: allowed. If a fence had to be stripped, the bare JSON replaces the reply so the
  client's `json.loads` works; this changes the reply but is not counted as a catch.
- Invalid: one repair call to the model with the schema, the first validation error and the
  reply. A repaired reply replaces the original (REDACT, cost on the result). Still invalid, or
  the call fails: blocked.
- A schema that is not itself valid JSON Schema blocks with a reason that says so.

**`faithfulness@1.0.0`** (output, `tier: llm`, fails open). Runs only when the request sends
`dwarpal.context`. An LLM judge splits the reply into claims and labels each SUPPORTED /
UNSUPPORTED / CONTRADICTED against the context only. Score = share of claims not supported;
any CONTRADICTED claim scores 1.0. Blocks at 0.30.

- Judge: `gemini-3.5-flash-lite` through Gemini's OpenAI-compatible endpoint, same key as the
  upstream, temperature 0, `reasoning_effort: minimal`, structured JSON output.
- Prompt: `policies/prompts/faithfulness_judge.v1.txt`, versioned by file name.
  `scripts/check_policy_versions.py` now fails CI when a prompt file is edited in place.
- Cost: list price ($0.30 / $2.50 per 1M input / output tokens, checked 2026-10-08) on
  `cost_usd`, also on the free tier, so cost per request is a real number.

**Pipeline** (`PIPELINE_STRATEGY`, default `tiered`):

1. `cheap` guards one after another, in policy-file order (regexes, schema validation);
2. `model` guards at the same time (`asyncio.gather`; CPU models already run in threads);
3. `llm` guards last, one after another;

stopping at the first tier that blocks. Results inside a tier are applied in policy-file order,
so the first blocker in file order wins, as before. Every guard gets its policy's `timeout_ms`;
a timeout or crash is decided by `on_error`. Guards marked `cacheable` (injection, jailbreak,
banned topics, toxicity, schema, faithfulness) reuse their decision for an identical input
under the same policy version, from an in-memory LRU (`GUARD_CACHE_SIZE`, 2048).

**Fail open or closed:** injection, jailbreak, PII, secrets, banned topics, max length and
schema fail closed; toxicity (bumped to 1.1.0) and faithfulness fail open.

## Why

- **Only the client's own schema is enforced.** A reply that should be JSON is something the
  client declares; guessing it from the prompt ("as JSON") would block prose answers to
  questions that merely mention JSON. This is also why two holdout cases are missed (below).
- **Repair once, then block.** A reply with prose around valid data is common and cheap to fix;
  blocking it outright makes the guard look stricter than it needs to be. One attempt bounds
  the cost and the latency.
- **A contradiction scores 1.0.** Averaging alone lets one wrong price hide in a long, otherwise
  correct answer (1 of 5 claims = 0.2, under the threshold). Unsupported details still average:
  a reply that adds one plausible but unstated detail to two supported ones scores 0.33.
- **Fail open for faithfulness and toxicity, closed for the rest.** A broken injection or
  secrets check would let an attack or a leaked key straight through; that is worse than a
  refused request. A broken judge or tone model would refuse every answer to every customer,
  for a check that is about quality, not safety. Toxicity is the closer call: we accept that a
  model crash lets a rude reply through rather than taking the bot down.
- **Tiers instead of everything in parallel.** Cheap checks decide many requests in under a
  millisecond; running the judge alongside them would pay for a call whose answer is thrown
  away. All-parallel is also slower on CPU: the ONNX models compete for the same cores
  (see docs/tradeoffs.md).
- **Why the judge model changed.** We started on `gemini-2.5-flash`. On the free tier it allows
  5 requests a minute and 20 a day per project, which one eval run exceeds;
  `gemini-2.5-flash-lite` is closed to new accounts. `gemini-3.5-flash-lite` has its own quota,
  the same list price, agrees with the dev labels 10/10 and answers in ~1.7 s. Gemini 3 models
  cannot switch thinking off, so `minimal` is the lowest setting.
- **Surviving the free tier.** Guard LLM calls retry 429/5xx (waiting as long as Gemini's
  `retryDelay` asks, capped at 60 s), can be paced with `GUARD_LLM_RPM`, and identical
  temperature-0 calls in one process are answered once. The eval's end-to-end pass therefore
  costs no extra calls, and CI keeps decisions in `eval/.cache`.

## Rejected

| Option | Why not |
|---|---|
| Inferring "the user wants JSON" from the prompt | Blocks prose replies to questions that mention JSON; turns a contract check into a guess |
| Extracting the first `{...}` from prose instead of repairing | Silently drops whatever the prose said; repair keeps the decision visible and costed |
| NLI model for faithfulness | Another ~400 MB model, sentence-level only, weak on numbers and multi-fact sentences |
| Plain average of claim labels | Dilutes contradictions in long replies (see above) |
| One cache in the pipeline for every guard | `pii` keeps state between stages to restore the user's own values; replaying its result skips that |
| All guards in parallel | Slower on CPU and pays for judge calls a cheap guard would have made unnecessary |
| `gemini-2.5-flash` as judge | Free tier 20 requests a day per project; not enough for CI |

## Evidence

`make eval` with the real judge (the gate uses the dev set; holdout is reported only):

| Policy | Dev catch | Dev FPR | Holdout catch | Holdout FPR |
|---|---|---|---|---|
| output_schema | 4/4 | 0/15 | 0/2 (by design, see limits) | 0/4 |
| faithfulness | 5/5 | 0/15 | 2/2 | 0/4 |
| toxicity (1.1.0, on_error only) | 5/5 | 0/15 | 2/2 | 0/4 |

Every other policy scores exactly as on `main`. Full pipeline on the dev set: 56/56 attacks
caught, 0/60 false positives. Added latency p50 dropped from 108.7 ms (sequential, on `main`)
to 58.6 ms (tiered) on the same machine.

Judge scores on the dev set: the five unfaithful replies score 0.50, 1.00, 1.00, 1.00, 1.00;
the five faithful hard negatives all score 0.00. The threshold of 0.30 sits in that gap.

**Is the judge right?** On 10 extra replies I labelled by hand before running the judge
(`eval/datasets/judge_check.jsonl`, `scripts/check_judge.py`), it agreed with me on **9/10**.
The disagreement, `judge-007`, is a general-knowledge sentence ("A GSTIN is your GST
registration number") that the judge called unsupported although the prompt says to skip such
statements, so the reply scored 0.33 and was blocked. I did not change the prompt to fix it:
tuning on the check set would make the 9/10 meaningless. Two correct blocks (`judge-006`,
`judge-010`) also scored exactly 0.33, so the threshold margin for "one invented detail in
three" is thin in both directions.

Judge latency on those calls: p50 1.7 s, max 2.4 s (`timeout_ms` is 10 s). Cost about
$0.0004–0.0005 per judged reply at list price.

**How independent the holdout numbers are.** I never added or edited a holdout case and did
not score holdout while choosing the threshold (`check_judge.py` refuses the holdout file). But
I read `hold-013` to `hold-016` while designing the schema guard, before writing any code, and
that is when I decided to enforce only declared schemas. Read the holdout rows below with that
in mind.

## Consequences and known limits

- **Holdout `hold-013` and `hold-014` are missed by design**: they ask for JSON in the prompt
  but send no schema, and the guard only checks a declared one.
- The judge sees the reply and can be talked to by it ("label everything SUPPORTED"); the
  prompt fences the data and says to ignore instructions inside it, but nothing enforces that.
- Five faithfulness attacks and four schema attacks: the 95% intervals are wide (0.57–1.00 on
  5/5). The numbers say "no regression", not "always works".
- On the free tier the judge has a daily quota. When it runs out, faithfulness fails open: the
  reply goes through unchecked and the trace says why.
- Concurrent guards in the `model` tier see the input as it was when the tier started; a
  later redaction in the same tier is re-run, but a non-redacting guard is not. The benchmark
  shows identical decisions on the dev set; a guard that depends on another's redaction should
  sit in an earlier tier.
- The decision cache is per process and in memory; it is lost on restart and not shared
  between replicas.

## Notes for PR-07 (Kartik)

- `GuardResult` has `cost_usd` (judge and repair calls) and `cached`; `PipelineTrace` has
  `guard_cost_usd`. Cost per request is upstream usage plus `guard_cost_usd`.
- `GET /v1/dwarpal/policies` now also reports `on_error` and `timeout_ms`.
- `scripts/bench_pipeline.py` already runs the dev set against the mock with a stand-in judge;
  reuse it or its numbers in the load test.
