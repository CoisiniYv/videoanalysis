"""Behavior rules for the savant_security mainline pipeline.

Public API:
    - ``BehaviorRule`` — abstract base class for rules.
    - ``REGISTRY`` — module-level ``RuleRegistry`` singleton.
    - ``register_rule`` — decorator concrete rules use to register.
    - ``build_rules`` — instantiate concrete rules from a CameraConfig.

Adding a new rule:
    1. Drop a module under ``custom/rules/<name>.py`` defining a
       ``BehaviorRule`` subclass decorated with
       ``@register_rule("<rule_type>")``.
    2. Import the module here so registration runs at package import.
    3. Add a harness test under ``harness/tests/``.

Pending rule modules — TODO for B2.1 / B2.2 / B2.3:
    - loitering.py        — B2.1
    - crowd_gathering.py  — B2.2
    - fall.py             — B2.3
The pyfunc tolerates unknown ``rule_type`` values in cameras.yml — they
are logged and skipped — so a partial set is safe.
"""

from custom.rules.base import BehaviorRule, FrameBehaviorRule
from custom.rules.registry import REGISTRY, RuleRegistry, build_rules, register_rule

# Importing rule modules triggers registration via @register_rule.
from custom.rules import intrusion  # noqa: F401  side-effect: registers "intrusion"
from custom.rules import fall  # noqa: F401  side-effect: registers "fall"
from custom.rules import crowd_gathering  # noqa: F401
from custom.rules import chasing  # noqa: F401

__all__ = [
    "BehaviorRule",
    "FrameBehaviorRule",
    "REGISTRY",
    "RuleRegistry",
    "build_rules",
    "register_rule",
]
