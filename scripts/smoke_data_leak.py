#!/usr/bin/env python3
"""Smoke test for the pii and secrets guards against a running Dwarpal. (PR-04)

  make mock && make dev-mock                     # in two terminals, then:
  uv run python scripts/smoke_data_leak.py --mock

  uv run python scripts/smoke_data_leak.py --url https://<space>.hf.space/v1   # real model

Talks to Dwarpal through the OpenAI SDK, like any client. --mock adds the reply-side checks:
the mock model returns whatever reply we ask for, so we can make it "leak" on purpose. Against
a real model only the input-side checks run. Exits 1 if any check fails.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

KEY = "lg_live_" + "4be19f0c7a2d4e83b56f91c0d27a8e3f"  # fake, Ledgerly-shaped


@dataclass
class Result:
    status: int
    text: str
    finish: str
    headers: dict[str, str]
    trace: dict[str, Any]

    def action(self, guard: str, stage: str) -> str | None:
        for r in self.trace.get("results", []):
            if r["guard"] == guard and r["stage"] == stage and not r["shadow"]:
                return r["action"]
        return None

    @property
    def blocked_by(self) -> str:
        return self.headers.get("x-dwarpal-blocked-by", "")


def call(client: OpenAI, text: str, mock_reply: str | None = None) -> Result:
    extra = {"mock_response": mock_reply} if mock_reply else None
    raw = client.chat.completions.with_raw_response.create(
        model="any", messages=[{"role": "user", "content": text}], extra_body=extra
    )
    body = raw.http_response.json()
    choice = body["choices"][0]
    return Result(
        raw.http_response.status_code,
        choice["message"]["content"] or "",
        choice["finish_reason"],
        {k.lower(): v for k, v in raw.http_response.headers.items()},
        body.get("dwarpal") or {},
    )


Check = tuple[str, str, str | None, Callable[[Result], str | None]]  # name, prompt, reply, test


def expect(cond: bool, why: str) -> str | None:
    return None if cond else why


INPUT_CHECKS: list[Check] = [
    (
        "normal question passes untouched",
        "How do I export last quarter's invoices as PDFs?",
        None,
        lambda r: expect(not r.blocked_by and r.action("pii", "input") == "allow", "was changed"),
    ),
    (
        "email, phone and PAN are redacted, not blocked",
        "Add client Arjun Mehta, arjun.mehta@example.com, +91 98765 43210, PAN KPRTS4821M.",
        None,
        lambda r: expect(r.action("pii", "input") == "redact" and not r.blocked_by, "not redacted"),
    ),
    (
        "Aadhaar is blocked",
        "Here is my Aadhaar for KYC: 7342 9158 6061",
        None,
        lambda r: expect(r.blocked_by.startswith("pii@"), f"blocked by {r.blocked_by or 'nobody'}"),
    ),
    (
        "GSTIN, invoice id and support email are left alone",
        "Invoice INV-2024-000123 for GSTIN 29ABCDE1234F1Z5, I wrote to support@ledgerly.example",
        None,
        lambda r: expect(r.action("pii", "input") == "allow", "flagged a hard negative"),
    ),
    (
        "a pasted key is redacted before the model sees it",
        f"My sync fails with LEDGERLY_API_KEY={KEY}. What's wrong?",
        None,
        lambda r: expect(r.action("secrets", "input") == "redact", "secrets did not redact"),
    ),
]

MOCK_CHECKS: list[Check] = [
    (
        "the user gets their own email back in the reply",
        "My email is arjun.mehta@example.com, please confirm it.",
        None,  # the mock echoes what it received, which is the redacted prompt
        lambda r: expect("arjun.mehta@example.com" in r.text and "<EMAIL_1>" not in r.text, r.text),
    ),
    (
        "a key leaked in the reply is blocked",
        "Which key should I use?",
        f"Use the shared service key {KEY} for now.",
        lambda r: expect(r.blocked_by.startswith("secrets@") and KEY not in r.text, r.blocked_by),
    ),
    (
        "another customer's details in the reply are redacted",
        "Who raised invoice INV-2024-000417?",
        "That was Sanjay Rao (sanjay.rao@example.com, +91 90040 12345).",
        lambda r: expect("sanjay.rao@example.com" not in r.text and not r.blocked_by, r.text),
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8000/v1")
    parser.add_argument("--key", default="dwarpal-smoke", help="Dwarpal API key, if auth is on")
    parser.add_argument(
        "--mock", action="store_true", help="upstream is the mock: run reply checks"
    )
    args = parser.parse_args()

    client = OpenAI(base_url=args.url, api_key=args.key, max_retries=0, timeout=60)
    checks = INPUT_CHECKS + (MOCK_CHECKS if args.mock else [])
    failed = 0
    for name, prompt, mock_reply, test in checks:
        try:
            result = call(client, prompt, mock_reply)
            problem = test(result)
        except Exception as exc:  # report and keep going
            problem = f"{type(exc).__name__}: {exc}"
        if problem is None:
            print(f"PASS  {name}")
        else:
            failed += 1
            print(f"FAIL  {name}\n      {problem}")
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
