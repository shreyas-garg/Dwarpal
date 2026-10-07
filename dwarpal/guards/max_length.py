"""Reference guard: blocks requests whose user text is too long.

This is the template for every other guard. Copy it, rename it, and replace check().
Very long inputs are a cheap first defence: many injection payloads hide instructions in
large pasted blobs, and they also cost money upstream.

Policy params:
  max_chars (int): total characters allowed across all user turns. Default 4000.
"""

from dwarpal.guards.base import Guard, GuardContext, GuardResult, Stage
from dwarpal.guards.registry import register


@register("max_length")  # must match `guard:` in policies/max_length.yaml
class MaxLengthGuard(Guard):
    stages = frozenset({Stage.INPUT})

    async def check(self, ctx: GuardContext, stage: Stage) -> GuardResult:
        max_chars = int(self.policy.params.get("max_chars", 4000))
        length = len(ctx.user_text())

        # Score: how far past the limit we are, as a 0..1 number. Exactly at the limit = 0,
        # double the limit or more = 1. Anything over the limit is at least 0.5 so the
        # default threshold (0.5) blocks it.
        if length <= max_chars:
            return self.allow(score=0.0)
        overflow = min(1.0, 0.5 + 0.5 * (length - max_chars) / max_chars)
        return self.decide(overflow, reason=f"input is {length} chars, limit is {max_chars}")
