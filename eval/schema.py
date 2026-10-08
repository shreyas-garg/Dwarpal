"""One red-team case, and how a dataset file is loaded and validated.

Datasets are JSON Lines: one case per line, see eval/README.md for the field rules.
Cases aimed at a guard that is not registered yet are skipped with a warning, so this PR's
holdout set can name guards that only arrive in PRs 03-06.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from dwarpal.guards.base import Stage
from dwarpal.guards.registry import discover, registered_guards

log = logging.getLogger("eval.schema")

DATASET_DIR = Path(__file__).parent / "datasets"


class DatasetError(ValueError):
    """A dataset file is malformed. Raised before any scoring happens."""


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    stage: Stage
    label: Literal["attack", "safe"]
    target_guard: str | None  # registry key of the guard this case is aimed at
    category: str
    author: str
    input: str
    context: list[str] = Field(default_factory=list)  # supplied docs, for faithfulness
    response: str | None = None  # canned reply, so output cases never call the model
    # PR-06: the JSON Schema the request asked for, sent as dwarpal.response_schema.
    response_schema: dict[str, Any] | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _check_shape(self) -> EvalCase:
        if self.label == "attack" and not self.target_guard:
            raise ValueError("attack cases must name a target_guard")
        if self.label == "safe" and self.target_guard:
            raise ValueError("safe cases must set target_guard to null")
        if self.stage is Stage.OUTPUT and not self.response:
            raise ValueError("output-stage cases must carry a canned response")
        if self.target_guard == "faithfulness" and not self.context:
            raise ValueError("faithfulness cases must carry context")
        return self


@dataclass
class Dataset:
    name: str
    path: Path
    cases: list[EvalCase]
    skipped: dict[str, int] = field(default_factory=dict)  # unregistered guard -> cases dropped

    @property
    def attacks(self) -> list[EvalCase]:
        return [c for c in self.cases if c.label == "attack"]

    @property
    def safe(self) -> list[EvalCase]:
        return [c for c in self.cases if c.label == "safe"]


def load_dataset(path: Path, name: str | None = None) -> Dataset:
    if not path.is_file():
        raise DatasetError(f"dataset not found: {path}")
    discover()
    known = set(registered_guards())

    cases: list[EvalCase] = []
    skipped: Counter[str] = Counter()
    seen: dict[str, int] = {}
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = EvalCase.model_validate_json(line)
        except (ValidationError, ValueError) as exc:
            raise DatasetError(f"{path.name}:{lineno}: {exc}") from None
        if case.id in seen:
            raise DatasetError(
                f"{path.name}:{lineno}: duplicate id {case.id!r} "
                f"(first seen on line {seen[case.id]})"
            )
        seen[case.id] = lineno
        if case.target_guard and case.target_guard not in known:
            skipped[case.target_guard] += 1
            continue
        cases.append(case)

    for guard, count in sorted(skipped.items()):
        log.warning("%s: skipped %d case(s) for unregistered guard %r", path.name, count, guard)
    return Dataset(name=name or path.stem, path=path, cases=cases, skipped=dict(skipped))


def load_suites(dataset_dir: Path = DATASET_DIR) -> dict[str, Dataset]:
    """The two scored suites: `dev` gates the merge, `holdout` is only reported."""
    suites = {
        "dev": load_dataset(dataset_dir / "redteam.jsonl", "dev"),
        "holdout": load_dataset(dataset_dir / "holdout.jsonl", "holdout"),
    }
    shared = {c.id for c in suites["dev"].cases} & {c.id for c in suites["holdout"].cases}
    if shared:
        raise DatasetError(f"ids used in both datasets: {sorted(shared)}")
    return suites


def dump_case(case: EvalCase) -> str:
    return json.dumps(case.model_dump(mode="json"), ensure_ascii=False)
