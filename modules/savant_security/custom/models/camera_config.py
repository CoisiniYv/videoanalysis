"""Camera / zone / rule configuration dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


@dataclass
class ZoneConfig:
    name: str = ""
    polygon: List[Tuple[float, float]] = field(default_factory=list)


@dataclass
class RuleConfig:
    name: str = ""
    rule_type: str = ""
    zone: str = ""
    enabled: bool = True
    min_inside_ms: int = 1000
    cooldown_s: int = 30
    severity: str = "medium"
    snapshot_required: bool = True
    clip_required: bool = True
    config: Dict[str, Any] = field(default_factory=dict)
    algorithm_id: str = ""


@dataclass
class CameraConfig:
    camera_id: str = ""
    zones: Dict[str, ZoneConfig] = field(default_factory=dict)
    rules: Dict[str, RuleConfig] = field(default_factory=dict)

    def get_zone(self, name: str) -> ZoneConfig:
        return self.zones.get(name, ZoneConfig())

    def get_rule(self, name: str) -> RuleConfig:
        return self.rules.get(name, RuleConfig())
