"""Versioned YAML policies. One file per policy in policies/."""

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from dwarpal.guards.base import Action, Stage
from dwarpal.guards.registry import discover, get_guard_class

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class PolicyError(ValueError):
    """A policy file is invalid. Raised at startup so bad policies never serve traffic."""


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    guard: str  # registry key of the Guard class
    stages: list[Stage]
    enabled: bool = True
    mode: Literal["enforce", "shadow"] = "enforce"
    action: Action = Action.BLOCK
    threshold: float = Field(0.5, ge=0.0, le=1.0)
    tier: Literal["cheap", "model", "llm"] = "cheap"
    on_error: Literal["fail_open", "fail_closed"] = "fail_closed"
    timeout_ms: int = Field(1000, gt=0)
    params: dict[str, Any] = Field(default_factory=dict)
    changelog: list[str] = Field(default_factory=list)

    @field_validator("version", mode="before")
    @classmethod
    def _semver(cls, v: object) -> str:
        v = str(v)  # unquoted `version: 1.0` arrives from YAML as a float
        if not SEMVER.match(v):
            raise ValueError(f"version must be MAJOR.MINOR.PATCH, got {v!r}")
        return v

    @field_validator("stages")
    @classmethod
    def _non_empty(cls, v: list[Stage]) -> list[Stage]:
        if not v:
            raise ValueError("stages must list at least one of: input, output")
        return v

    @property
    def ref(self) -> str:
        """name@version, used in logs and response headers."""
        return f"{self.name}@{self.version}"


def load_policy_file(path: Path) -> Policy:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError(f"{path}: expected a mapping at the top level")
    try:
        policy = Policy.model_validate(data)
    except ValueError as exc:
        raise PolicyError(f"{path}: {exc}") from exc

    try:
        guard_cls = get_guard_class(policy.guard)
    except KeyError as exc:
        raise PolicyError(f"{path}: {exc.args[0]}") from None
    unsupported = set(policy.stages) - set(guard_cls.stages)
    if unsupported:
        raise PolicyError(
            f"{path}: guard {policy.guard!r} does not support stage(s) "
            f"{sorted(s.value for s in unsupported)}"
        )
    return policy


def load_policies(policy_dir: Path, enabled: set[str] | None = None) -> list[Policy]:
    """Load and validate every *.yaml in policy_dir.

    Returns enabled policies only, sorted by file name for a stable order.
    `enabled` restricts to those policy names (None = all).

    Two versions of the same policy may run side by side — e.g. prompt_injection@1.1.0 in
    shadow next to 1.0.0 enforcing, so a new version is tried on real traffic before it
    blocks anything — but at most one of them may enforce, and versions must differ.
    """
    discover()
    if not policy_dir.is_dir():
        raise PolicyError(f"policy directory not found: {policy_dir}")

    policies: list[Policy] = []
    seen: dict[tuple[str, str], Path] = {}  # (name, version) -> file
    enforcing: dict[str, Path] = {}  # name -> file of the enforce-mode version
    names: set[str] = set()
    for path in sorted(policy_dir.glob("*.yaml")):
        policy = load_policy_file(path)
        key = (policy.name, policy.version)
        if key in seen:
            raise PolicyError(f"{path}: duplicate policy {policy.ref} (also in {seen[key]})")
        seen[key] = path
        names.add(policy.name)
        if not (policy.enabled and (enabled is None or policy.name in enabled)):
            continue
        if policy.mode == "enforce":
            if policy.name in enforcing:
                raise PolicyError(
                    f"{path}: a second enforcing version of {policy.name!r} "
                    f"(also in {enforcing[policy.name]}); put one of them in shadow mode"
                )
            enforcing[policy.name] = path
        policies.append(policy)

    if enabled:
        missing = enabled - names
        if missing:
            raise PolicyError(f"ENABLED_POLICIES names unknown policies: {sorted(missing)}")
    return policies
