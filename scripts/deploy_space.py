#!/usr/bin/env python3
"""Push the current commit to the Hugging Face Space. (PR-05)

Run by .github/workflows/deploy.yml on every push to main; it can also be run by hand:

  HF_TOKEN=hf_... uv run --no-project --with huggingface-hub \
      python scripts/deploy_space.py --space <owner>/<space> --sha "$(git rev-parse HEAD)"

What it uploads is `git archive HEAD` (tracked files only, never .env or local caches) with
two changes made in a temp copy, not in the repo:
  - README.md gets the Space front matter (sdk: docker, app_port: 7860) on top;
  - the Dockerfile's `ARG GIT_SHA=dev` becomes the commit SHA, so /healthz reports exactly
    what was deployed and the smoke test can wait for it.
Files the commit no longer has are deleted from the Space in the same commit.
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

FRONT_MATTER = """---
title: Dwarpal
emoji: 🛡️
colorFrom: indigo
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Versioned guardrails proxy for LLM apps
---

"""


def stage(target: Path, sha: str) -> None:
    archive = subprocess.run(["git", "archive", "HEAD"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")

    readme = target / "README.md"
    readme.write_text(FRONT_MATTER + readme.read_text())
    dockerfile = target / "Dockerfile"
    text = dockerfile.read_text()
    if "ARG GIT_SHA=dev" not in text:
        sys.exit("Dockerfile has no `ARG GIT_SHA=dev` line to stamp")
    dockerfile.write_text(text.replace("ARG GIT_SHA=dev", f"ARG GIT_SHA={sha}"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--space", required=True, help="<owner>/<space name>")
    parser.add_argument("--sha", required=True, help="commit being deployed")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        sys.exit("HF_TOKEN is not set")

    from huggingface_hub import HfApi

    api = HfApi(token=token)
    api.create_repo(args.space, repo_type="space", space_sdk="docker", exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage(Path(tmp), args.sha)
        api.upload_folder(
            folder_path=tmp,
            repo_id=args.space,
            repo_type="space",
            commit_message=f"deploy {args.sha[:12]}",
            delete_patterns="*",  # mirror the commit: drop files it no longer has
        )
    print(f"pushed {args.sha[:12]} to https://huggingface.co/spaces/{args.space}")


if __name__ == "__main__":
    main()
