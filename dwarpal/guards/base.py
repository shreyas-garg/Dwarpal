"""The contract every guard implements.

A guard looks at a request (INPUT stage) or a model reply (OUTPUT stage) and returns a
GuardResult saying what to do. The pipeline owns ordering, timing and enforcement; a guard
only decides.

Writing a new guard:
  1. Copy dwarpal/guards/max_length.py to dwarpal/guards/<your_guard>.py.
  2. Change the @register name, `stages`, and `check()`.
  3. Add policies/<your_guard>.yaml.
  The module is discovered automatically; no other file needs editing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from dwarpal.policy import Policy


class Stage(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class Action(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"  # stop the request, return a refusal
    REDACT = "redact"  # replace text (see redacted_text / redacted_messages) and continue
    FLAG = "flag"  # log only, does not change anything and does not count as "caught"


def message_text(message: dict[str, Any]) -> str:
    """Plain text of one chat message. Handles string content and OpenAI content-part lists."""
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
    return "\n".join(parts)


@dataclass
class GuardContext:
    messages: list[dict[str, Any]]  # chat messages, after any earlier redactions
    response_text: str | None = None  # the model's reply; set only at OUTPUT stage
    context_docs: list[str] = field(default_factory=list)  # supplied context, for faithfulness
    response_schema: dict[str, Any] | None = None  # expected JSON schema, for output_schema
    request_id: str = ""

    def user_messages(self) -> list[dict[str, Any]]:
        return [m for m in self.messages if m.get("role") == "user"]

    def user_text(self) -> str:
        """All user turns joined together. Most input guards should check this."""
        return "\n".join(message_text(m) for m in self.user_messages())

    def last_user_text(self) -> str:
        users = self.user_messages()
        return message_text(users[-1]) if users else ""


@dataclass
class GuardResult:
    guard: str  # policy name
    policy_version: str
    action: Action
    score: float = 0.0  # 0..1, higher = more likely bad
    reason: str = ""
    # REDACT at OUTPUT stage: the new reply text.
    redacted_text: str | None = None
    # REDACT at INPUT stage: the full replacement message list.
    redacted_messages: list[dict[str, Any]] | None = None
    stage: Stage | None = None  # filled by the pipeline
    latency_ms: float = 0.0  # filled by the pipeline
    cost_usd: float = 0.0  # set this if your guard calls a paid model
    error: bool = False  # True when the guard crashed and on_error decided the action
    # PR-03: True when the policy ran in shadow mode — the decision was logged, not enforced.
    shadow: bool = False  # filled by the pipeline

    @property
    def caught(self) -> bool:
        return self.action in (Action.BLOCK, Action.REDACT)

    def to_dict(self) -> dict[str, Any]:
        return {
            "guard": self.guard,
            "version": self.policy_version,
            "stage": self.stage.value if self.stage else None,
            "action": self.action.value,
            "score": round(self.score, 4),
            "reason": self.reason,
            "latency_ms": round(self.latency_ms, 3),
            "cost_usd": self.cost_usd,
            "error": self.error,
            "shadow": self.shadow,
        }


class Guard(ABC):
    name: ClassVar[str]  # set by @register
    stages: ClassVar[frozenset[Stage]]  # stages this guard supports

    def __init__(self, policy: Policy):
        self.policy = policy

    async def setup(self) -> None:  # noqa: B027 - optional hook, empty by default
        """Load models or other expensive state. Called once at app startup."""

    @abstractmethod
    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        """Inspect ctx and return a result.

        Do CPU-heavy work (model inference) with `await asyncio.to_thread(...)` so the event
        loop is not blocked.
        """

    # Helpers so guards don't repeat boilerplate.

    def result(
        self, action: Action, score: float = 0.0, reason: str = "", **kw: Any
    ) -> GuardResult:
        return GuardResult(
            guard=self.policy.name,
            policy_version=self.policy.version,
            action=action,
            score=score,
            reason=reason,
            **kw,
        )

    def allow(self, score: float = 0.0, reason: str = "") -> GuardResult:
        return self.result(Action.ALLOW, score, reason)

    def decide(self, score: float, reason: str = "", **kw: Any) -> GuardResult:
        """Apply the policy: score >= threshold -> the policy's action, otherwise allow."""
        if score >= self.policy.threshold:
            return self.result(self.policy.action, score, reason, **kw)
        return self.allow(score, reason)
