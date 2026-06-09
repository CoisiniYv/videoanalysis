"""Chasing frame-level behavior rule."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

from custom.geometry.polygon import point_in_polygon
from custom.models.events import SecurityEvent
from custom.models.tracks import TrackState
from custom.rules.base import FrameBehaviorRule
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
class ChasingConfig:
    min_speed_px_s: float = 120.0
    max_distance_px: float = 220.0
    min_cos_alignment: float = 0.80
    speed_ratio_tolerance: float = 0.60
    behind_cos_min: float = 0.50
    velocity_window_ms: int = 700
    min_pair_duration_ms: int = 1500
    cooldown_s: int = 30

    @classmethod
    def from_rule_config(
        cls,
        config: dict[str, Any],
        fallback_cooldown_s: int,
    ) -> "ChasingConfig":
        defaults = cls(cooldown_s=fallback_cooldown_s or cls.cooldown_s)
        cfg = dict(config or {})
        duration_raw = _config_alias(cfg, "min_pair_duration_s", "min_duration_ms")
        if "min_duration_ms" in cfg:
            min_pair_duration_ms = _int_value(duration_raw, defaults.min_pair_duration_ms)
        elif duration_raw is not None:
            min_pair_duration_ms = int(_float_value(duration_raw, 1.5) * 1000)
        else:
            min_pair_duration_ms = defaults.min_pair_duration_ms
        return cls(
            min_speed_px_s=_env_float(
                "CHASE_MIN_SPEED_PX_S",
                _float_value(cfg.get("min_speed_px_s"), defaults.min_speed_px_s),
            ),
            max_distance_px=_env_float(
                "CHASE_MAX_DISTANCE_PX",
                _env_float(
                    "CHASE_MAX_PAIR_DISTANCE_PX",
                    _float_value(
                        _config_alias(cfg, "max_distance_px", "max_pair_distance_px"),
                        defaults.max_distance_px,
                    ),
                ),
            ),
            min_cos_alignment=_env_float(
                "CHASE_MIN_COS_ALIGNMENT",
                _float_value(cfg.get("min_cos_alignment"), defaults.min_cos_alignment),
            ),
            speed_ratio_tolerance=_env_float(
                "CHASE_SPEED_RATIO_TOLERANCE",
                _float_value(
                    cfg.get("speed_ratio_tolerance"),
                    defaults.speed_ratio_tolerance,
                ),
            ),
            behind_cos_min=_env_float(
                "CHASE_BEHIND_COS_MIN",
                _float_value(cfg.get("behind_cos_min"), defaults.behind_cos_min),
            ),
            velocity_window_ms=_env_int(
                "CHASE_VELOCITY_WINDOW_MS",
                _int_value(cfg.get("velocity_window_ms"), defaults.velocity_window_ms),
            ),
            min_pair_duration_ms=_env_int(
                "CHASE_MIN_PAIR_DURATION_MS",
                _env_int("CHASE_MIN_DURATION_MS", min_pair_duration_ms),
            ),
            cooldown_s=_env_int(
                "CHASE_COOLDOWN_S",
                _int_value(cfg.get("cooldown_s"), defaults.cooldown_s),
            ),
        )


@dataclass(frozen=True)
class Kinematics:
    track_id: int
    position: tuple[float, float]
    velocity: tuple[float, float]
    speed_px_s: float


@dataclass(frozen=True)
class ChasePair:
    leader_track_id: int
    follower_track_id: int
    distance_px: float
    alignment: float
    leader_speed_px_s: float
    follower_speed_px_s: float


def track_kinematics(track: TrackState, window_ms: int) -> Kinematics | None:
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
    x0, y0 = start.bbox.foot_point
    x1, y1 = last.bbox.foot_point
    vx = (x1 - x0) / dt_ms * 1000.0
    vy = (y1 - y0) / dt_ms * 1000.0
    speed = math.hypot(vx, vy)
    return Kinematics(
        track_id=track.track_id,
        position=(x1, y1),
        velocity=(vx, vy),
        speed_px_s=speed,
    )


def _cos(a: tuple[float, float], b: tuple[float, float]) -> float:
    norm_a = math.hypot(a[0], a[1])
    norm_b = math.hypot(b[0], b[1])
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return (a[0] * b[0] + a[1] * b[1]) / (norm_a * norm_b)


def detect_chase_pairs(
    tracks: list[TrackState],
    cfg: ChasingConfig,
) -> list[ChasePair]:
    kinematics = [
        kin
        for kin in (track_kinematics(track, cfg.velocity_window_ms) for track in tracks)
        if kin is not None and kin.speed_px_s >= cfg.min_speed_px_s
    ]
    pairs: list[ChasePair] = []
    for i in range(len(kinematics)):
        for j in range(i + 1, len(kinematics)):
            left = kinematics[i]
            right = kinematics[j]
            distance = math.hypot(
                left.position[0] - right.position[0],
                left.position[1] - right.position[1],
            )
            if distance > cfg.max_distance_px:
                continue
            alignment = _cos(left.velocity, right.velocity)
            if alignment < cfg.min_cos_alignment:
                continue
            faster = max(left.speed_px_s, right.speed_px_s)
            slower = min(left.speed_px_s, right.speed_px_s)
            if faster > 0.0 and (faster - slower) / faster > cfg.speed_ratio_tolerance:
                continue
            heading = (
                (left.velocity[0] + right.velocity[0]) / 2.0,
                (left.velocity[1] + right.velocity[1]) / 2.0,
            )
            left_to_right = (
                right.position[0] - left.position[0],
                right.position[1] - left.position[1],
            )
            behind_alignment = _cos(left_to_right, heading)
            if abs(behind_alignment) < cfg.behind_cos_min:
                continue
            if behind_alignment > 0:
                leader = right
                follower = left
            else:
                leader = left
                follower = right
            pairs.append(
                ChasePair(
                    leader_track_id=leader.track_id,
                    follower_track_id=follower.track_id,
                    distance_px=distance,
                    alignment=alignment,
                    leader_speed_px_s=leader.speed_px_s,
                    follower_speed_px_s=follower.speed_px_s,
                )
            )
    return pairs


@register_rule("chasing")
class ChasingRule(FrameBehaviorRule):
    """Fires when a leader/follower chase pair persists."""

    def __init__(self, rule_config, zone, cooldown) -> None:
        super().__init__(rule_config, zone, cooldown)
        self.params = ChasingConfig.from_rule_config(
            rule_config.config,
            fallback_cooldown_s=rule_config.cooldown_s,
        )
        self._pair_since: dict[tuple[int, int], int] = {}

    def evaluate_frame(
        self,
        frame_tracks: list[TrackState],
        frame_ts_ms: int,
    ) -> list[SecurityEvent]:
        if not self.config.enabled or not frame_tracks:
            if not frame_tracks:
                self._pair_since.clear()
            return []
        frame_ts_ms = frame_ts_ms or max((track.last_seen_ms for track in frame_tracks), default=0)
        zone_tracks = self._zone_tracks(frame_tracks)
        pairs = detect_chase_pairs(zone_tracks, self.params)
        camera_id, source_id = self._frame_identity(zone_tracks or frame_tracks)
        active_pairs: set[tuple[int, int]] = set()
        events: list[SecurityEvent] = []

        for pair in pairs:
            pair_key = (pair.leader_track_id, pair.follower_track_id)
            active_pairs.add(pair_key)
            started = self._pair_since.setdefault(pair_key, frame_ts_ms)
            duration_ms = frame_ts_ms - started
            if duration_ms < self.params.min_pair_duration_ms:
                continue
            cooldown_key = (
                f"{camera_id}:chasing:{self.zone.name}:"
                f"{pair.leader_track_id}:{pair.follower_track_id}"
            )
            if not self.cooldown.can_emit(cooldown_key, frame_ts_ms):
                continue
            self.cooldown.record_emit(cooldown_key, self.params.cooldown_s, frame_ts_ms)
            events.append(
                SecurityEvent(
                    event_type="chasing",
                    camera_id=camera_id,
                    source_id=source_id,
                    track_id=pair.follower_track_id,
                    start_ts_ms=started,
                    end_ts_ms=frame_ts_ms,
                    confidence=min(1.0, max(pair.alignment, 0.0)),
                    severity=self.config.severity,
                    zone=self.zone.name,
                    rule_name=self.config.name,
                    description=(
                        f"Track {pair.follower_track_id} chasing "
                        f"{pair.leader_track_id} in zone '{self.zone.name}'"
                    ),
                    snapshot_required=self.config.snapshot_required,
                    clip_required=self.config.clip_required,
                    payload={
                        "algorithm_id": self.config.algorithm_id or "behavior.chasing",
                        "rule_id": self.config.name,
                        "camera_id": camera_id,
                        "zone_id": self.zone.name,
                        "leader_track_id": pair.leader_track_id,
                        "follower_track_id": pair.follower_track_id,
                        "distance_px": pair.distance_px,
                        "alignment": pair.alignment,
                        "leader_speed_px_s": pair.leader_speed_px_s,
                        "follower_speed_px_s": pair.follower_speed_px_s,
                        "duration_ms": duration_ms,
                    },
                )
            )

        for pair_key in list(self._pair_since):
            if pair_key not in active_pairs:
                del self._pair_since[pair_key]
        return events

    def _zone_tracks(self, frame_tracks: list[TrackState]) -> list[TrackState]:
        if not self.zone.polygon:
            return list(frame_tracks)
        out: list[TrackState] = []
        for track in frame_tracks:
            if not track.observations:
                continue
            if point_in_polygon(track.observations[-1].bbox.foot_point, self.zone.polygon):
                out.append(track)
        return out

    @staticmethod
    def _frame_identity(frame_tracks: list[TrackState]) -> tuple[str, str]:
        for track in frame_tracks:
            if track.observations:
                obs = track.observations[-1]
                return obs.camera_id, obs.source_id
        return "", ""
