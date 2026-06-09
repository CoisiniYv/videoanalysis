"""Rule registry — single entrypoint for instantiating behavior rules.

Every concrete rule registers itself via the ``@register_rule`` decorator
keyed on its ``rule_type``. ``build_rules(camera_config, cooldown)``
iterates over the camera config's rules and returns concrete
``BehaviorRule`` instances.

Future rules (loitering, crowd_gathering, fall, ...) just add a module
under ``custom/rules/`` that imports ``register_rule`` and decorates the
class. They do not touch ``BehaviorRulesPyFunc`` or this registry.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Type

from custom.models.camera_config import CameraConfig, RuleConfig, ZoneConfig
from custom.services.cooldown import CooldownTracker

from custom.rules.base import BehaviorRule, FrameBehaviorRule


RuleInstance = BehaviorRule | FrameBehaviorRule
RuleFactory = Callable[[RuleConfig, ZoneConfig, CooldownTracker], RuleInstance]


class RuleRegistry:
    """In-process registry mapping ``rule_type`` → concrete rule factory."""

    def __init__(self) -> None:
        self._factories: Dict[str, RuleFactory] = {}

    def register(self, rule_type: str, factory: RuleFactory) -> None:
        if not rule_type:
            raise ValueError("rule_type must be non-empty")
        if rule_type in self._factories:
            raise ValueError(f"rule_type already registered: {rule_type}")
        self._factories[rule_type] = factory

    def has(self, rule_type: str) -> bool:
        return rule_type in self._factories

    def get_factory(self, rule_type: str) -> RuleFactory:
        if rule_type not in self._factories:
            raise KeyError(f"unknown rule_type: {rule_type}")
        return self._factories[rule_type]

    def known_rule_types(self) -> List[str]:
        return sorted(self._factories.keys())


# Module-level singleton. Concrete rule modules register on import.
REGISTRY = RuleRegistry()


def register_rule(rule_type: str) -> Callable[[Type[RuleInstance]], Type[RuleInstance]]:
    """Decorator that registers a ``BehaviorRule`` subclass with ``REGISTRY``."""

    def _decorator(cls: Type[RuleInstance]) -> Type[RuleInstance]:
        cls.rule_type = rule_type
        REGISTRY.register(rule_type, cls)
        return cls

    return _decorator


def build_rules(
    camera_config: CameraConfig,
    cooldown: CooldownTracker,
) -> List[RuleInstance]:
    """Instantiate every enabled rule declared in *camera_config*.

    Disabled rules are skipped at build time so the pyfunc loop does not
    waste work calling ``evaluate`` on a no-op rule. Rules whose
    ``rule_type`` is not registered are skipped with a console warning
    (the camera config can declare future rule_types that have not been
    implemented yet — the pipeline must still start).

    Returns the rules in stable iteration order so per-frame logs are
    deterministic.
    """
    rules: List[RuleInstance] = []
    for rule_name, rule_cfg in camera_config.rules.items():
        if not rule_cfg.enabled:
            continue
        if not REGISTRY.has(rule_cfg.rule_type):
            print(
                f"stage=savant_security_rule_registry_unknown "
                f"rule_name={rule_name} "
                f"rule_type={rule_cfg.rule_type} "
                f"known={REGISTRY.known_rule_types()}",
                flush=True,
            )
            continue
        zone = camera_config.get_zone(rule_cfg.zone)
        factory = REGISTRY.get_factory(rule_cfg.rule_type)
        rules.append(factory(rule_cfg, zone, cooldown))
    return rules
