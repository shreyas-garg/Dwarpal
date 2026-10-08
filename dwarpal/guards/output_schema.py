"""Checks that a reply is JSON matching the schema the client asked for. (PR-06)

Runs only when the request carries a schema: `dwarpal.response_schema`, or an OpenAI
`response_format` of type json_schema / json_object (the pipeline turns either into
ctx.response_schema). Everything else is allowed untouched.

  1. Strip one surrounding code fence (```json ... ```), parse, validate with jsonschema.
  2. Valid: allow. If a fence had to be stripped, hand back the bare JSON so the client's
     json.loads works; that changes the reply but catches nothing (restored=True).
  3. Invalid, and params.repair_once: ask the model once to fix it, with the schema and the
     first validation error. A fixed reply replaces the original (REDACT); the call's cost is
     on the result.
  4. Still invalid: the policy's action (block).

A schema that is not itself valid JSON Schema is the client's bug; it blocks too, with a
reason that says so, rather than letting unchecked output through.

Policy params:
  repair_once:  bool, default true
  unwrap_fences: bool, default true
  llm:          {model, temperature, max_tokens, extra: {...}, price_per_1m_tokens: {input,
                output}} for the repair call; model defaults to UPSTREAM_MODEL
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import exceptions as js_exceptions
from jsonschema import validators

from dwarpal.guards.base import Action, Guard, GuardContext, GuardResult, Stage
from dwarpal.guards.registry import register
from dwarpal.llm import LLMError, get_llm, strip_code_fences

REPAIR_SYSTEM = (
    "You repair JSON. Reply with only a JSON value that matches the given JSON Schema: no "
    "prose, no code fences. Keep the data from the original reply; convert types and fix "
    "syntax as needed, but do not add facts that are not in it."
)
MAX_REASON = 200


def _short(text: str, limit: int = MAX_REASON) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def validator_for(schema: dict[str, Any]):
    """A validator for the schema's declared draft (2020-12 when it declares none)."""
    cls = validators.validator_for(schema, default=validators.Draft202012Validator)
    cls.check_schema(schema)
    return cls(schema)


def first_problem(validator, text: str) -> tuple[Any, str | None]:
    """(parsed value, None) when text is valid, else (None, what is wrong)."""
    try:
        value = json.loads(text)
    except ValueError as exc:
        msg = getattr(exc, "msg", str(exc))
        return None, f"not valid JSON: {msg} (line {getattr(exc, 'lineno', '?')})"
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    if not errors:
        return value, None
    err = errors[0]
    where = "/".join(str(p) for p in err.absolute_path) or "(root)"
    more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
    return None, _short(f"schema mismatch at {where}: {err.message}{more}")


@register("output_schema")
class OutputSchemaGuard(Guard):
    stages = frozenset({Stage.OUTPUT})
    cacheable = True

    def __init__(self, policy):
        super().__init__(policy)
        params = policy.params
        self.repair_once = bool(params.get("repair_once", True))
        self.unwrap_fences = bool(params.get("unwrap_fences", True))
        self.llm_config: dict[str, Any] = params.get("llm") or {}

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        schema = ctx.response_schema
        if schema is None:
            return self.allow(reason="no schema requested")
        try:
            validator = validator_for(schema)
        except js_exceptions.SchemaError as exc:
            return self.decide(1.0, reason=_short(f"response_schema is invalid: {exc.message}"))

        raw = ctx.response_text or ""
        text = strip_code_fences(raw)
        _, problem = first_problem(validator, text)
        if problem is None:
            if self.unwrap_fences and text != raw.strip():
                return self.result(
                    Action.REDACT,
                    0.0,
                    "valid; code fence removed",
                    redacted_text=text,
                    restored=True,
                )
            return self.allow(reason="valid")

        if not self.repair_once:
            return self.decide(1.0, reason=problem)
        repaired, cost, failure = await self._repair(schema, validator, raw, problem)
        if repaired is not None:
            return self.result(
                Action.REDACT,
                1.0,
                _short(f"repaired; was {problem}"),
                redacted_text=repaired,
                cost_usd=cost,
            )
        return self.decide(
            1.0, reason=_short(f"{problem}; repair failed: {failure}"), cost_usd=cost
        )

    async def _repair(
        self, schema: dict[str, Any], validator, raw: str, problem: str
    ) -> tuple[str | None, float, str]:
        """One model call. Returns (fixed text or None, cost, why it failed)."""
        config = self.llm_config
        messages = [
            {"role": "system", "content": REPAIR_SYSTEM},
            {
                "role": "user",
                "content": f"JSON Schema:\n{json.dumps(schema)}\n\nProblem: {problem}\n\n"
                f"Original reply:\n{raw}",
            },
        ]
        try:
            completion = await get_llm().complete(
                messages,
                model=config.get("model"),
                temperature=config.get("temperature", 0),
                max_tokens=config.get("max_tokens"),
                **(config.get("extra") or {}),
            )
        except LLMError as exc:
            return None, 0.0, str(exc)
        cost = completion.cost_usd(config.get("price_per_1m_tokens"))
        fixed = strip_code_fences(completion.text)
        _, still = first_problem(validator, fixed)
        if still is not None:
            return None, cost, still
        return fixed, cost, ""
