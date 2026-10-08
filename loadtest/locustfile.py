"""Locust load test for Dwarpal, against the mock upstream. (PR-07)

`make bench` runs this headless for every scenario and concurrency (loadtest/bench.py). To
watch a run live instead, start the mock and a proxy the way bench.py does, then:

    uv run --group loadtest locust -f loadtest/locustfile.py --host http://localhost:8000

Traffic is the dev set's safe cases, round-robin: ordinary support questions and hard
negatives. Every request therefore reaches the upstream and every enabled guard runs; attack
traffic would be cut short by the first block and make Dwarpal look faster than it is. Each
request names its own canned reply (`mock_response`), so the mock's default answer, the judge
stand-in, only ever goes to the faithfulness judge.

With BENCH_CONTEXT=1 every request also sends the FAQ as dwarpal.context, as the demo does
when "send the FAQ as context" is ticked, so the faithfulness judge checks every reply.
"""

import itertools
import json
import os
from pathlib import Path

from locust import FastHttpUser, constant, task

ROOT = Path(__file__).resolve().parent.parent
DEV_SET = ROOT / "eval" / "datasets" / "redteam.jsonl"
FAQ = (ROOT / "demo" / "ledgerly_faq.md").read_text(encoding="utf-8")
REPLY = "You can do that from Settings in Ledgerly; the help centre FAQ has the exact steps."
WITH_CONTEXT = os.environ.get("BENCH_CONTEXT") == "1"


def safe_requests() -> list[dict]:
    payloads = []
    for line in DEV_SET.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        case = json.loads(line)
        if case["label"] != "safe":
            continue
        options = {}
        if case.get("response_schema") is not None:
            options["response_schema"] = case["response_schema"]
        if WITH_CONTEXT or case.get("context"):
            options["context"] = case.get("context") or [FAQ]
        payloads.append(
            {
                "model": "mock",
                "messages": [{"role": "user", "content": case["input"]}],
                "mock_response": case.get("response") or REPLY,
                "dwarpal": options,
            }
        )
    return payloads


REQUESTS = safe_requests()
_counter = itertools.count()


class ChatUser(FastHttpUser):
    wait_time = constant(0)  # closed loop: the number of users is the concurrency

    @task
    def chat(self) -> None:
        n = next(_counter)
        payload = REQUESTS[n % len(REQUESTS)]
        if "context" in payload["dwarpal"]:
            # Guard LLM calls at temperature 0 are memoized per process (dwarpal/llm.py); a
            # unique line keeps every judge call a real call, as it is for real traffic.
            context = [*payload["dwarpal"]["context"], f"Load-test request {n}."]
            payload = {**payload, "dwarpal": {**payload["dwarpal"], "context": context}}
        with self.client.post("/v1/chat/completions", json=payload, catch_response=True) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
