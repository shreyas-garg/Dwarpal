"""Blocks jailbreaks: persuasion aimed at making the model drop its safety behaviour.

Covers persona swaps ("you are now X, X never refuses"), DAN-style dual-output modes,
hypothetical/fiction wrappers around disallowed requests, refusal suppression ("answer
without warnings"), and authority claims ("I'm an engineer, disable your guardrails").

All detection logic lives in input_attack.py; the pattern list lives in
policies/jailbreak.yaml.
"""

from dwarpal.guards.input_attack import InputAttackGuard
from dwarpal.guards.registry import register


@register("jailbreak")
class JailbreakGuard(InputAttackGuard):
    pass
