"""LoiteringRule - pure-Python dwell and low-motion behavior detection."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

from custom.geometry.polygon import point_in_polygon
from custom.models.events import SecurityEvent
from custom.models.pose import PersonPoseObservation
from custom.models.tracks import TrackState
from custom.rules.base import BehaviorRule
from custom.rules.registry import register_rule


def _float_value(value: Any, default: float) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_value(value: Any, default: int) -> int:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    return _float_value(os.getenv(name), default)


def _env_int(name: str, default: int) -> int:
    return _int_value(os.getenv(name), default)


def _config_alias(config: dict[str, Any], primary: str, *aliases: str) -> Any:
    if primary in config:
        return config[primary]
    for alias in aliases:
        if alias in config:
            return config[alias]
    return None


@dataclass(frozen=True)
class LoiteringConfig:
    min_duration_ms: int = 60000
    max_avg_speed_px_s: float = 20.0
    max_displacement_px: float = 0.0
    min_observation_count: int = 2
    cooldown_s: int = 60

    @classmethod
    def from_rule_config(
        cls,
        config: dict[str, Any],
        fallback_cooldown_s: int,
    ) -> "LoiteringConfig":
        defaults = cls(cooldown_s=fallback_cooldown_s or cls.cooldown_s)
        cfg = dict(config or {})
        duration_raw = _config_alias(cfg, "min_duration_ms", "min_loiter_ms", "min_duration_s")
        if "min_duration_s" in cfg and "min_duration_ms" not in cfg and "min_loiter_ms" not in cfg:
            min_duration_ms = int(_float_value(duration_raw, defaults.min_duration_ms / 1000.0) * 1000)
        else:
            min_duration_ms = _int_value(duration_raw, defaults.min_duration_ms)
        return cls(
            min_duration_ms=_env_int("LOITERING_MIN_DURATION_MS", min_duration_ms),
            max_avg_speed_px_s=_env_float(
                "LOITERING_MAX_AVG_SPEED_PX_S",
                _float_value(cfg.get("max_avg_speed_px_s"), defaults.max_avg_speed_px_s),
            ),
            max_displacement_px=_env_float(
                "LOITERING_MAX_DISPLACEMENT_PX",
                _float_value(cfg.get("max_displacement_px"), defaults.max_displacement_px),
            ),
            min_observation_count=_env_int(
                "LOITERING_MIN_OBSERVATION_COUNT",
                _int_value(cfg.get("min_observation_count"), defaults.min_observation_count),
            ),
            cooldown_s=_env_int(
                "LOITERING_COOLDOWN_S",
                _int_value(cfg.get("cooldown_s"), defaults.cooldown_s),
            ),
        )


def _avg_speed_px_s(observations: list[PersonPoseObservation]) -> float:
    if len(observations) < 2:
        return 0.0
    total_dist = 0.0
    total_time_ms = 0
    for prev, curr in zip(observations, observations[1:]):
        dt_ms = curr.timestamp_ms - prev.timestamp_ms
        if dt_ms <= 0:
            continue
        px, py = prev.bbox.foot_point
        cx, cy = curr.bbox.foot_point
        total_dist += math.hypot(cx - px, cy - py)
        total_time_ms += dt_ms
    if total_time_ms <= 0:
        return 0.0
    return total_dist / total_time_ms * 1000.0


def _displacement_px(observations: list[PersonPoseObservation]) -> float:
    if len(observations) < 2:
        return 0.0
    sx, sy = observations[0].bbox.foot_point
    ex, ey = observations[-1].bbox.foot_point
    return math.hypot(ex - sx, ey - sy)


@register_rule("loitering")
class LoiteringRule(BehaviorRule):
    """Fires when one track stays in a zone with low average motion."""

    def __init__(self, rule_config, zone, cooldown) -> None:
        super().__init__(rule_config, zone, cooldown)
        self.params = LoiteringConfig.from_rule_config(
            rule_config.config,
            fallback_cooldown_s=rule_config.cooldown_s,
        )

    def evaluate(self, track: TrackState) -> SecurityEvent | None:
        if not self.config.enabled or not track.observations:
            return None
        current_obs = track.observations[-1]
        if self.zone.polygon and not point_in_polygon(current_obs.bbox.foot_point, self.zone.polygon):
            return None

        first_inside_idx = self._first_continuous_inside_index(track.observations)
        inside_observations = track.observations[first_inside_idx:]
        if len(inside_observations) < self.params.min_observation_count:
            return None
        start_ts_ms = inside_observations[0].timestamp_ms
        duration_ms = current_obs.timestamp_ms - start_ts_ms
        if duration_ms < self.params.min_duration_ms:
            return None

        avg_speed = _avg_speed_px_s(inside_observations)
        if avg_speed > self.params.max_avg_speed_px_s:
            return None
        displacement = _displacement_px(inside_observations)
        if self.params.max_displacement_px > 0 and displacement > self.params.max_displacement_px:
            return None

        now_ms = track.last_seen_ms
        cooldown_key = self.cooldown_key(track)
        if not self.cooldown.can_emit(cooldown_key, now_ms):
            return None
        self.cooldown.record_emit(cooldown_key, self.params.cooldown_s, now_ms)

        return SecurityEvent(
            event_type="loitering",
            camera_id=current_obs.camera_id,
            source_id=current_obs.source_id,
            track_id=track.track_id,
            start_ts_ms=start_ts_ms,
            end_ts_ms=now_ms,
            confidence=current_obs.confidence,
            severity=self.config.severity,
            zone=self.zone.name,
            rule_name=self.config.name,
            description=(
                f"Track {track.track_id} loitering in zone '{self.zone.name}': "
                f"{duration_ms}ms at {avg_speed:.1f}px/s"
            ),
            snapshot_required=self.config.snapshot_required,
            clip_required=self.config.clip_required,
            payload={
                "algorithm_id": self.config.algorithm_id or "behavior.loitering",
                "rule_id": self.config.name,
                "camera_id": current_obs.camera_id,
                "zone_id": self.zone.name,
                "track_id": track.track_id,
                "duration_ms": duration_ms,
                "avg_speed_px_s": avg_speed,
                "displacement_px": displacement,
                "min_duration_ms": self.params.min_duration_ms,
                "max_avg_speed_px_s": self.params.max_avg_speed_px_s,
                "max_displacement_px": self.params.max_displacement_px,
                "observation_count": len(inside_observations),
            },
        )

    def _first_continuous_inside_index(
        self,
        observations: list[PersonPoseObservation],
    ) -> int:
        if not observations:
            return 0
        if not self.zone.polygon:
            return 0
        first_idx = len(observations) - 1
        for idx in range(len(observations) - 2, -1, -1):
            if point_in_polygon(observations[idx].bbox.foot_point, self.zone.polygon):
                first_idx = idx
            else:
                break
        return first_idx
