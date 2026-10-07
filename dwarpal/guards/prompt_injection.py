"""Blocks prompt injection: text that tries to override the system instructions.

Covers direct overrides ("ignore previous instructions"), fake role tags (<|system|>,
### Instruction), prompt-leak requests, encoded payloads (long base64/hex blobs), and
indirect injection hidden in pasted documents or supplied context.

All detection logic lives in input_attack.py; the pattern list lives in
policies/prompt_injection.yaml.
"""

from dwarpal.guards.input_attack import InputAttackGuard
from dwarpal.guards.registry import register


@register("prompt_injection")
class PromptInjectionGuard(InputAttackGuard):
    pass
