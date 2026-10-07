#!/usr/bin/env python3
"""Fail CI when a policy file changed but its version didn't. (PR-03)

Why this matters beyond tidiness: the eval cache is keyed by (guard, policy version, case),
so editing a policy without bumping its version would silently reuse stale cached decisions
and the gate would score the old behaviour. docs/decisions/0002 calls this check out.

Rules, for every policies/*.yaml that differs from the base ref (default origin/main):
  modified  -> its `version` must have changed AND its `changelog` must have a new entry
  added     -> must carry a non-empty `changelog`
  deleted   -> fine here; the eval gate complains if a baselined policy disappears

Usage: scripts/check_policy_versions.py [base-ref]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

POLICY_DIR = "policies"


def _field(text: str, path: str, field: str) -> object:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SystemExit(f"{path}: invalid YAML: {exc}") from exc
    return (data or {}).get(field)


def violations(old_text: str | None, new_text: str, path: str) -> list[str]:
    """What is wrong with this change. old_text is None for an added file."""
    if old_text is None:
        if not _field(new_text, path, "changelog"):
            return [f"{path}: new policy must carry a changelog entry"]
        return []
    if old_text == new_text:
        return []

    problems: list[str] = []
    old_version = _field(old_text, path, "version")
    new_version = _field(new_text, path, "version")
    if str(old_version) == str(new_version):
        problems.append(
            f"{path}: changed but version stayed {new_version} — bump it "
            "(the eval cache is keyed by version, so an unbumped change scores stale results)"
        )
    old_log = _field(old_text, path, "changelog") or []
    new_log = _field(new_text, path, "changelog") or []
    if new_log == old_log:
        problems.append(f"{path}: changed but the changelog gained no entry")
    return problems


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else "origin/main"
    try:
        merge_base = _git("merge-base", base, "HEAD").strip()
    except subprocess.CalledProcessError:
        merge_base = base  # shallow clone without common history: compare the ref directly

    status = _git(
        "diff", "--name-status", "--no-renames", merge_base, "HEAD", "--", f"{POLICY_DIR}/"
    )
    problems: list[str] = []
    checked = 0
    for line in status.splitlines():
        kind, _, path = line.partition("\t")
        if not path.endswith(".yaml") or kind.startswith("D"):
            continue
        checked += 1
        new_text = Path(path).read_text()
        old_text = None
        if kind.startswith("M"):
            old_text = _git("show", f"{merge_base}:{path}")
        problems.extend(violations(old_text, new_text, path))

    if problems:
        print("policy version check failed:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"policy version check: {checked} changed file(s), all carry a version bump")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
