# Contributing

Each team member raises **one PR**, merged in this order:

| PR | Owner | Scope |
|---|---|---|
| 01 | Shreyas | Foundation: proxy, guard interface, policy loader, CI, Docker |
| 02 | Harshit Sachan | Eval harness, CI eval gate, safe + holdout sets |
| 03 | Harshit Goel | Prompt injection + jailbreak guards, policy versioning, shadow mode |
| 04 | Yash | PII + secrets guards, demo app |
| 05 | Om | Banned topics + toxicity guards, deployment to HF Spaces |
| 06 | Divyanshu | Output schema + faithfulness guards, faster pipeline |
| 07 | Kartik | Telemetry, dashboard, load test, final numbers in README |

PRs 03–06 can be written in parallel once 02 has merged, but merge in number order.

## Rules

1. Nobody pushes to `main`. Everything goes through your PR.
2. Stay in your own files. Shared files (`pipeline.py`, `app.py`, `README.md`) get small, clearly
   marked additions only. Ask the group before changing anything bigger.
3. When the PR before yours merges: `git fetch origin && git rebase origin/main`, fix conflicts,
   push, merge once CI is green.
4. CI must be green: lint + tests, and (from PR-02) the eval gate.
5. Squash-merge with a clear title, e.g. `feat: injection + jailbreak guards v1.0.0`.
6. Add a short decision note in `docs/decisions/` (what you chose, what you rejected, why).

## Daily loop

```bash
git checkout main && git pull
git checkout -b feat/<your-area>
# work, commit often
make lint test
git fetch origin && git rebase origin/main
git push -u origin feat/<your-area>
```

## Adding a guard

1. Copy `dwarpal/guards/max_length.py` to `dwarpal/guards/<name>.py`. It is discovered
   automatically; no registration file to edit.
2. Set `@register("<name>")`, `stages`, and implement `async def check(ctx, stage)`.
   - Input stage: read `ctx.user_text()` or `ctx.messages`. To redact, return
     `self.result(Action.REDACT, score, reason, redacted_messages=[...full new list...])`.
   - Output stage: read `ctx.response_text`. To redact, return `redacted_text=...`.
   - Use `self.decide(score, reason)` to apply the policy's threshold and action.
   - Load models in `async def setup(self)`; run inference with `await asyncio.to_thread(...)`.
3. Add `policies/<name>.yaml` (copy `max_length.yaml`). Start at `version: 1.0.0`.
4. Put heavy dependencies in the `ml` or `pii` extra in `pyproject.toml`.
5. Add tests in `tests/test_<name>.py`. Use the mock upstream; tests must pass without an API key.
