"""Bundle → per-source runtime adapter (midterm camera config).

Maps a ``CameraConfigBundle`` (loaded from cameras.midterm.yml in the
midterm camera config schema) into the per-source runtime structures the Savant
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
from custom.models.events import SecurityEvent
from custom.models.pose import PersonPoseObservation
from custom.models.tracks import TrackState, TrackStateStore
from custom.rules import BehaviorRule, FrameBehaviorRule, build_rules
from custom.services.camera_config import CameraConfigBundle, CameraEntry
from custom.services.algorithm_activation import (
    behavior_rule_type_for_algorithm_id,
    is_behavior_algorithm_id,
)
from custom.services.cooldown import CooldownTracker


def _bool_config(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


@dataclass
class SourceRuntime:
    """Per-source bundle of state + rules for ``BehaviorRulesPyFunc``."""

    source_id: str
    camera_id: str
    camera_entry: CameraEntry
    runtime_epoch_id: str = ""
    rules: List[BehaviorRule | FrameBehaviorRule] = field(default_factory=list)
    single_track_rules: List[BehaviorRule] = field(default_factory=list)
    frame_rules: List[FrameBehaviorRule] = field(default_factory=list)
    store: TrackStateStore = field(default_factory=TrackStateStore)


@dataclass
class RuleEvaluation:
    """Rule event plus optional representative track for enrichment."""

    event: SecurityEvent
    track: TrackState | None = None


def camera_entry_to_legacy_config(cam: CameraEntry) -> CameraConfig:
    """Translate a midterm ``CameraEntry`` into the legacy ``CameraConfig``
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
    for rule_id, rule_entry in cam.rules.items():
        if not rule_entry.enabled:
            continue
        if not is_behavior_algorithm_id(rule_entry.algorithm_id):
            continue
        runtime_rule_type = behavior_rule_type_for_algorithm_id(rule_entry.algorithm_id)
        if runtime_rule_type is None:
            print(
                f"stage=savant_security_rule_algorithm_unknown "
                f"rule_id={rule_id} "
                f"algorithm_id={rule_entry.algorithm_id}",
                flush=True,
            )
            continue
        cfg = dict(rule_entry.config or {})
        if rule_entry.evidence_policy:
            policy = dict(rule_entry.evidence_policy)
            cfg["evidence_policy"] = policy
            cfg["snapshot_required"] = _bool_config(
                policy.get("snapshot_required"),
                _bool_config(cfg.get("snapshot_required"), True),
            )
            cfg["clip_required"] = _bool_config(
                policy.get("clip_required"),
                _bool_config(cfg.get("clip_required"), True),
            )
        zone_id = str(cfg.get("zone_id") or cfg.get("zone") or "")
        rules[rule_id] = RuleConfig(
            name=rule_id,
            rule_type=runtime_rule_type,
            zone=zone_id,
            enabled=True,
            min_inside_ms=int(cfg.get("min_inside_ms", 1000)),
            cooldown_s=int(cfg.get("cooldown_s", 30)),
            severity=str(cfg.get("severity", "medium")),
            snapshot_required=bool(cfg.get("snapshot_required", True)),
            clip_required=bool(cfg.get("clip_required", True)),
            config=dict(cfg),
            algorithm_id=rule_entry.algorithm_id,
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
        single_track_rules = [rule for rule in rules if isinstance(rule, BehaviorRule)]
        frame_rules = [rule for rule in rules if isinstance(rule, FrameBehaviorRule)]
        out[cam.source_id] = SourceRuntime(
            source_id=cam.source_id,
            camera_id=cam.camera_id,
            camera_entry=cam,
            runtime_epoch_id=cam.runtime_epoch_id or bundle.runtime_epoch_id,
            rules=rules,
            single_track_rules=single_track_rules,
            frame_rules=frame_rules,
            store=TrackStateStore(
                observation_window_s=observation_window_s,
                track_timeout_s=track_timeout_s,
            ),
        )
    return out


def evaluate_runtime_frame(
    runtime: SourceRuntime,
    observations: list[PersonPoseObservation],
    *,
    frame_ts_ms: int | None = None,
) -> list[RuleEvaluation]:
    """Update source state and evaluate configured rules for one frame.

    Single-track rules run once per active track. Frame-level rules run once
    per source frame and may return multiple events.
    """
    runtime.store.update(observations)
    active_tracks = runtime.store.active_tracks
    evaluations: list[RuleEvaluation] = []

    for track in active_tracks:
        for rule in runtime.single_track_rules:
            event = rule.evaluate(track)
            if event is not None:
                evaluations.append(RuleEvaluation(event=event, track=track))

    resolved_frame_ts_ms = (
        frame_ts_ms
        if frame_ts_ms is not None
        else max((obs.timestamp_ms for obs in observations), default=0)
    )
    if resolved_frame_ts_ms <= 0:
        resolved_frame_ts_ms = max(
            (track.last_seen_ms for track in active_tracks),
            default=0,
        )

    for rule in runtime.frame_rules:
        for event in rule.evaluate_frame(active_tracks, resolved_frame_ts_ms) or []:
            evaluations.append(
                RuleEvaluation(
                    event=event,
                    track=representative_track_for_event(event, active_tracks),
                )
            )
    return evaluations


def representative_track_for_event(
    event: SecurityEvent,
    active_tracks: list[TrackState],
) -> TrackState | None:
    try:
        event_track_id = int(getattr(event, "track_id", None) or 0)
    except (TypeError, ValueError):
        event_track_id = 0
    if event_track_id > 0:
        for track in active_tracks:
            if track.track_id == event_track_id:
                return track
    return None
