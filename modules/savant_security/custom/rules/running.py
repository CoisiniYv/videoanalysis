"""RunningRule - pure-Python track speed behavior detection."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

from custom.geometry.polygon import point_in_polygon
from custom.models.events import SecurityEvent
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


@dataclass(frozen=True)
class RunningConfig:
    min_speed_px_s: float = 250.0
    min_normalized_speed: float = 0.0
    min_duration_ms: int = 500
    velocity_window_ms: int = 700
    cooldown_s: int = 20

    @classmethod
    def from_rule_config(
        cls,
        config: dict[str, Any],
        fallback_cooldown_s: int,
    ) -> "RunningConfig":
        defaults = cls(cooldown_s=fallback_cooldown_s or cls.cooldown_s)
        cfg = dict(config or {})
        return cls(
            min_speed_px_s=_env_float(
                "RUNNING_MIN_SPEED_PX_S",
                _float_value(cfg.get("min_speed_px_s"), defaults.min_speed_px_s),
            ),
            min_normalized_speed=_env_float(
                "RUNNING_MIN_NORMALIZED_SPEED",
                _float_value(
                    cfg.get("min_normalized_speed", cfg.get("min_norm_speed")),
                    defaults.min_normalized_speed,
                ),
            ),
            min_duration_ms=_env_int(
                "RUNNING_MIN_DURATION_MS",
                _int_value(cfg.get("min_duration_ms"), defaults.min_duration_ms),
            ),
            velocity_window_ms=_env_int(
                "RUNNING_VELOCITY_WINDOW_MS",
                _int_value(cfg.get("velocity_window_ms"), defaults.velocity_window_ms),
            ),
            cooldown_s=_env_int(
                "RUNNING_COOLDOWN_S",
                _int_value(cfg.get("cooldown_s"), defaults.cooldown_s),
            ),
        )


@dataclass(frozen=True)
class RunningKinematics:
    speed_px_s: float
    normalized_speed: float
    window_ms: int


def track_running_kinematics(track: TrackState, window_ms: int) -> RunningKinematics | None:
    observations = track.observations
    if len(observations) < 2:
        return None
    last = observations[-1]
    cutoff = last.timestamp_ms - max(int(window_ms), 1)
    start = observations[0]
    for obs in observations:
        if obs.timestamp_ms >= cutoff:
            start = obs
            break
    dt_ms = last.timestamp_ms - start.timestamp_ms
    if dt_ms <= 0:
        return None
    sx, sy = start.bbox.foot_point
    lx, ly = last.bbox.foot_point
    speed = math.hypot(lx - sx, ly - sy) / dt_ms * 1000.0
    normalized_speed = speed / last.bbox.height if last.bbox.height > 0 else 0.0
    return RunningKinematics(
        speed_px_s=speed,
        normalized_speed=normalized_speed,
        window_ms=dt_ms,
    )


@register_rule("running")
class RunningRule(BehaviorRule):
    """Fires when one track sustains high foot-point speed."""

    def __init__(self, rule_config, zone, cooldown) -> None:
        super().__init__(rule_config, zone, cooldown)
        self.params = RunningConfig.from_rule_config(
            rule_config.config,
            fallback_cooldown_s=rule_config.cooldown_s,
        )
        self._running_since: dict[tuple[str, int], int] = {}

    def evaluate(self, track: TrackState) -> SecurityEvent | None:
        if not self.config.enabled or not track.observations:
            return None
        current_obs = track.observations[-1]
        track_key = (current_obs.camera_id, track.track_id)
        if self.zone.polygon and not point_in_polygon(current_obs.bbox.foot_point, self.zone.polygon):
            self._running_since.pop(track_key, None)
            return None

        kin = track_running_kinematics(track, self.params.velocity_window_ms)
        if kin is None or not self._passes_speed_thresholds(kin):
            self._running_since.pop(track_key, None)
            return None

        now_ms = track.last_seen_ms
        started = self._running_since.setdefault(track_key, now_ms)
        duration_ms = now_ms - started
        if duration_ms < self.params.min_duration_ms:
            return None

        cooldown_key = self.cooldown_key(track)
        if not self.cooldown.can_emit(cooldown_key, now_ms):
            return None
        self.cooldown.record_emit(cooldown_key, self.params.cooldown_s, now_ms)

        return SecurityEvent(
            event_type="running",
            camera_id=current_obs.camera_id,
            source_id=current_obs.source_id,
            track_id=track.track_id,
            start_ts_ms=started,
            end_ts_ms=now_ms,
            confidence=current_obs.confidence,
            severity=self.config.severity,
            zone=self.zone.name,
            rule_name=self.config.name,
            description=(
                f"Track {track.track_id} running in zone '{self.zone.name}': "
                f"{kin.speed_px_s:.1f}px/s for {duration_ms}ms"
            ),
            snapshot_required=self.config.snapshot_required,
            clip_required=self.config.clip_required,
            payload={
                "algorithm_id": self.config.algorithm_id or "behavior.running",
                "rule_id": self.config.name,
                "camera_id": current_obs.camera_id,
                "zone_id": self.zone.name,
                "track_id": track.track_id,
                "duration_ms": duration_ms,
                "speed_px_s": kin.speed_px_s,
                "normalized_speed": kin.normalized_speed,
                "velocity_window_ms": self.params.velocity_window_ms,
                "kinematic_window_ms": kin.window_ms,
                "min_speed_px_s": self.params.min_speed_px_s,
                "min_normalized_speed": self.params.min_normalized_speed,
            },
        )

    def _passes_speed_thresholds(self, kin: RunningKinematics) -> bool:
        if kin.speed_px_s < self.params.min_speed_px_s:
            return False
        if (
            self.params.min_normalized_speed > 0
            and kin.normalized_speed < self.params.min_normalized_speed
        ):
            return False
        return True
