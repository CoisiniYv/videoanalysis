"""Bundle → per-source runtime adapter (Phase C1.2).

Maps a ``CameraConfigBundle`` (loaded from cameras.generated.yml in the
C1 export schema) into the per-source runtime structures the Savant
pyfunc consumes: a ``TrackStateStore`` per source, the rule list from
the registry, and the original ``CameraEntry`` for downstream
enrichment.

This module is intentionally pure-Python:

- no Savant / DeepStream imports,
- no PostgreSQL,
- no FastAPI / HTTP.

Tests can import and exercise it without GPU or network. The Savant
pyfunc in ``custom/pyfuncs/behavior_rules.py`` is the only consumer
that needs ``savant.deepstream.pyfunc``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from custom.models.camera_config import CameraConfig, RuleConfig, ZoneConfig
from custom.models.tracks import TrackStateStore
from custom.rules import BehaviorRule, build_rules
from custom.services.camera_config import CameraConfigBundle, CameraEntry
from custom.services.cooldown import CooldownTracker


@dataclass
class SourceRuntime:
    """Per-source bundle of state + rules for ``BehaviorRulesPyFunc``."""

    source_id: str
    camera_id: str
    camera_entry: CameraEntry
    rules: List[BehaviorRule] = field(default_factory=list)
    store: TrackStateStore = field(default_factory=TrackStateStore)


def camera_entry_to_legacy_config(cam: CameraEntry) -> CameraConfig:
    """Translate a C1.1 ``CameraEntry`` into the legacy ``CameraConfig``
    shape that ``build_rules()`` already understands.

    Disabled rule entries are skipped here so the resulting
    ``CameraConfig.rules`` only contains rules the pyfunc should
    actually evaluate.
    """
    zones: Dict[str, ZoneConfig] = {}
    for zone_name, zone_entry in cam.zones.items():
        polygon = [(float(p[0]), float(p[1])) for p in zone_entry.points]
        zones[zone_name] = ZoneConfig(name=zone_name, polygon=polygon)

    rules: Dict[str, RuleConfig] = {}
    for rule_type, rule_entry in cam.rules.items():
        if not rule_entry.enabled:
            continue
        cfg = rule_entry.config
        rules[rule_type] = RuleConfig(
            name=rule_type,
            rule_type=rule_type,
            zone=str(cfg.get("zone", "")),
            enabled=True,
            min_inside_ms=int(cfg.get("min_inside_ms", 1000)),
            cooldown_s=int(cfg.get("cooldown_s", 30)),
            severity=str(cfg.get("severity", "medium")),
            snapshot_required=bool(cfg.get("snapshot_required", True)),
            clip_required=bool(cfg.get("clip_required", True)),
        )

    return CameraConfig(camera_id=cam.camera_id, zones=zones, rules=rules)


def build_per_source_runtime(
    bundle: CameraConfigBundle,
    cooldown: CooldownTracker,
    *,
    observation_window_s: float = 5.0,
    track_timeout_s: float = 10.0,
) -> Dict[str, SourceRuntime]:
    """Build the ``source_id → SourceRuntime`` map the pyfunc routes by.

    Disabled cameras are skipped — they produce no events. The cooldown
    tracker is shared across all sources because the rule cooldown key
    is already ``camera_id:track_id:zone`` (camera-scoped).
    """
    out: Dict[str, SourceRuntime] = {}
    for cam in bundle.iter_enabled_cameras():
        legacy = camera_entry_to_legacy_config(cam)
        rules = build_rules(legacy, cooldown)
        if not rules:
            # The camera has zero enabled rules. Skip — keeps logs and
            # tests cleaner than a runtime entry that always no-ops.
            continue
        out[cam.source_id] = SourceRuntime(
            source_id=cam.source_id,
            camera_id=cam.camera_id,
            camera_entry=cam,
            rules=rules,
            store=TrackStateStore(
                observation_window_s=observation_window_s,
                track_timeout_s=track_timeout_s,
            ),
        )
    return out
