#!/usr/bin/env python3
"""Download every model the enabled policies use into the Hugging Face cache. (PR-05)

Runs at Docker build time so a Space never downloads a model at startup; the image then sets
HF_HUB_OFFLINE=1, so a model missing from the image fails loudly instead of being fetched.

It runs each guard's own setup(), so it downloads exactly the files and revisions the guards
load, with no second list of model ids to keep in sync with policies/*.yaml.

  uv run python scripts/fetch_models.py            # all policies in policies/
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dwarpal.guards.registry import get_guard_class  # noqa: E402
from dwarpal.policy import load_policies  # noqa: E402


async def main() -> None:
    for policy in load_policies(ROOT / "policies"):
        start = time.perf_counter()
        await get_guard_class(policy.guard)(policy).setup()
        print(f"{policy.ref}: ready in {time.perf_counter() - start:.1f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
