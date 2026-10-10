"""FallRule — pure-Python single-track fall detection."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

from custom.geometry.polygon import point_in_polygon
from custom.models.events import SecurityEvent
from custom.models.pose import (
    BBox,
    Keypoint,
    PersonPoseObservation,
    visible_keypoint_count,
)
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


def _bool_value(value: Any, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    return _float_value(os.getenv(name), default)


def _env_int(name: str, default: int) -> int:
    return _int_value(os.getenv(name), default)


def _env_bool(name: str, default: bool) -> bool:
    return _bool_value(os.getenv(name), default)


def _config_alias(config: dict[str, Any], primary: str, *aliases: str) -> Any:
    if primary in config:
        return config[primary]
    for alias in aliases:
        if alias in config:
            return config[alias]
    return None


@dataclass(frozen=True)
class FallConfig:
    min_down_ms: int = 1500
    cooldown_s: int = 60
    require_transition: bool = True
    transition_window_ms: int = 1500
    lying_aspect_ratio: float = 0.85
    upright_aspect_ratio: float = 0.55
    torso_horizontal_deg: float = 45.0
    head_hip_collapse_ratio: float = 0.22
    min_visible_keypoints: int = 0
    keypoint_threshold: float = 0.25
    joint_order_votes: bool = True
    keypoint_box_aspect_ratio: float = 0.90
    keypoint_box_min_points: int = 6

    @classmethod
    def from_rule_config(cls, config: dict[str, Any], fallback_cooldown_s: int) -> "FallConfig":
        defaults = cls(cooldown_s=fallback_cooldown_s or cls.cooldown_s)
        cfg = dict(config or {})
        min_down_ms = _int_value(
            _config_alias(cfg, "min_down_ms", "min_on_ground_ms"),
            defaults.min_down_ms,
        )
        transition_window_ms = _int_value(
            _config_alias(cfg, "transition_window_ms", "fall_transition_window_ms"),
            defaults.transition_window_ms,
        )
        return cls(
            min_down_ms=_env_int(
                "FALL_MIN_DOWN_MS",
                _env_int("FALL_MIN_ON_GROUND_MS", min_down_ms),
            ),
            cooldown_s=_env_int(
                "FALL_COOLDOWN_S",
                _int_value(cfg.get("cooldown_s"), defaults.cooldown_s),
            ),
            require_transition=_env_bool(
                "FALL_REQUIRE_TRANSITION",
                _bool_value(cfg.get("require_transition"), defaults.require_transition),
            ),
            transition_window_ms=_env_int(
                "FALL_TRANSITION_WINDOW_MS",
                transition_window_ms,
            ),
            lying_aspect_ratio=_env_float(
                "FALL_LYING_ASPECT_RATIO",
                _float_value(cfg.get("lying_aspect_ratio"), defaults.lying_aspect_ratio),
            ),
            upright_aspect_ratio=_env_float(
                "FALL_UPRIGHT_ASPECT_RATIO",
                _float_value(cfg.get("upright_aspect_ratio"), defaults.upright_aspect_ratio),
            ),
            torso_horizontal_deg=_env_float(
                "FALL_TORSO_HORIZONTAL_DEG",
                _float_value(cfg.get("torso_horizontal_deg"), defaults.torso_horizontal_deg),
            ),
            head_hip_collapse_ratio=_env_float(
                "FALL_HEAD_HIP_COLLAPSE_RATIO",
                _float_value(
                    cfg.get("head_hip_collapse_ratio"),
                    defaults.head_hip_collapse_ratio,
                ),
            ),
            min_visible_keypoints=_env_int(
                "FALL_MIN_VISIBLE_KEYPOINTS",
                _int_value(cfg.get("min_visible_keypoints"), defaults.min_visible_keypoints),
            ),
            keypoint_threshold=_env_float(
                "FALL_KEYPOINT_THRESHOLD",
                _float_value(cfg.get("keypoint_threshold"), defaults.keypoint_threshold),
            ),
            joint_order_votes=_env_bool(
                "FALL_JOINT_ORDER_VOTES",
                _bool_value(cfg.get("joint_order_votes"), defaults.joint_order_votes),
            ),
            keypoint_box_aspect_ratio=_env_float(
                "FALL_KEYPOINT_BOX_ASPECT_RATIO",
                _float_value(
                    cfg.get("keypoint_box_aspect_ratio"),
                    defaults.keypoint_box_aspect_ratio,
                ),
            ),
            keypoint_box_min_points=_env_int(
                "FALL_KEYPOINT_BOX_MIN_POINTS",
                _int_value(
                    cfg.get("keypoint_box_min_points"),
                    defaults.keypoint_box_min_points,
                ),
            ),
        )


@dataclass(frozen=True)
class PostureSignals:
    aspect_ratio: float | None
    torso_angle_deg: float | None
    head_hip_ratio: float | None
    available_votes: int
    is_lying: bool
    is_upright: bool
    pose_quality_mode: str
    shoulders_below_ankles: bool | None = None
    shoulders_below_knees: bool | None = None
    keypoint_box_aspect_ratio: float | None = None
    legs_stacked_upright: bool = False
    lying_weight: int = 0
    total_weight: int = 0

    def payload(self, *, is_upright_before: bool) -> dict[str, Any]:
        return {
            "aspect_ratio": self.aspect_ratio,
            "torso_angle_deg": self.torso_angle_deg,
            "head_hip_ratio": self.head_hip_ratio,
            "keypoint_box_aspect_ratio": self.keypoint_box_aspect_ratio,
            "shoulders_below_ankles": self.shoulders_below_ankles,
            "shoulders_below_knees": self.shoulders_below_knees,
            "legs_stacked_upright": self.legs_stacked_upright,
            "available_votes": self.available_votes,
            "lying_weight": self.lying_weight,
            "total_weight": self.total_weight,
            "is_lying": self.is_lying,
            "is_upright_before": is_upright_before,
        }


def _aspect_ratio(bbox: BBox) -> float | None:
    if bbox.height <= 0:
        return None
    return bbox.width / bbox.height


def _keypoints_by_name(keypoints: list[Keypoint], threshold: float) -> dict[str, Keypoint]:
    return {kp.name: kp for kp in keypoints if kp.confidence >= threshold}


def _midpoint(a: Keypoint | None, b: Keypoint | None) -> tuple[float, float] | None:
    points = [p for p in (a, b) if p is not None]
    if not points:
        return None
    return (
        sum(point.x for point in points) / len(points),
        sum(point.y for point in points) / len(points),
    )


def torso_angle_from_vertical_deg(
    keypoints: list[Keypoint],
    threshold: float,
) -> float | None:
    kp = _keypoints_by_name(keypoints, threshold)
    shoulder = _midpoint(kp.get("left_shoulder"), kp.get("right_shoulder"))
    hip = _midpoint(kp.get("left_hip"), kp.get("right_hip"))
    if shoulder is None or hip is None:
        return None
    dx = hip[0] - shoulder[0]
    dy = hip[1] - shoulder[1]
    if dx == 0.0 and dy == 0.0:
        return None
    return math.degrees(math.atan2(abs(dx), abs(dy)))


def head_above_hip_ratio(
    keypoints: list[Keypoint],
    bbox: BBox,
    threshold: float,
) -> float | None:
    kp = _keypoints_by_name(keypoints, threshold)
    nose = kp.get("nose")
    hip = _midpoint(kp.get("left_hip"), kp.get("right_hip"))
    if nose is None or hip is None or bbox.height <= 0:
        return None
    return (hip[1] - nose.y) / bbox.height


# Joint-order cues compare image heights of body parts (y grows downward).
# Only keypoints at or above the confidence threshold take part: pose models
# return a guessed position with a near-zero score for occluded joints, and
# those guesses must not decide whether someone is lying.


def shoulders_below_ankles(kp: dict[str, Keypoint]) -> bool | None:
    """Highest shoulder at or below ankle height: no upright posture does this."""

    left, right = kp.get("left_shoulder"), kp.get("right_shoulder")
    ankle = _midpoint(kp.get("left_ankle"), kp.get("right_ankle"))
    if left is None or right is None or ankle is None:
        return None
    return min(left.y, right.y) >= ankle[1]


def shoulders_below_knees(kp: dict[str, Keypoint]) -> bool | None:
    """Lower shoulder below the higher knee."""

    left, right = kp.get("left_shoulder"), kp.get("right_shoulder")
    knees = [k for k in (kp.get("left_knee"), kp.get("right_knee")) if k is not None]
    if left is None or right is None or not knees:
        return None
    return max(left.y, right.y) > min(knee.y for knee in knees)


def keypoint_box_aspect_ratio(kp: dict[str, Keypoint], min_points: int) -> float | None:
    """Width/height of the box around confident keypoints.

    Detector boxes carry padding and clip at the frame edge; the keypoint box
    follows the body itself. Needs enough points that a few joints spread
    sideways (raised arms) cannot make a short box look wide.
    """

    if len(kp) < max(2, min_points):
        return None
    xs = [point.x for point in kp.values()]
    ys = [point.y for point in kp.values()]
    height = max(ys) - min(ys)
    if height <= 0:
        return None
    return (max(xs) - min(xs)) / height


def legs_stacked_upright(kp: dict[str, Keypoint]) -> bool:
    """Shoulders above hips above knees above ankles, each by a clear margin.

    A high camera foreshortens a standing person so the detector box is no
    longer narrow; the stacked joints still show they are upright. Sitting
    (knee at hip height) and squatting (hip at or below the knee) fail the
    margin, so neither is mistaken for standing.
    """

    shoulder = _midpoint(kp.get("left_shoulder"), kp.get("right_shoulder"))
    hip = _midpoint(kp.get("left_hip"), kp.get("right_hip"))
    knee = _midpoint(kp.get("left_knee"), kp.get("right_knee"))
    ankle = _midpoint(kp.get("left_ankle"), kp.get("right_ankle"))
    if shoulder is None or hip is None or knee is None or ankle is None:
        return False
    body = ankle[1] - shoulder[1]
    if body <= 0:
        return False
    margin = 0.15 * body
    return (
        hip[1] - shoulder[1] >= margin
        and knee[1] - hip[1] >= margin
        and ankle[1] - knee[1] >= margin
    )


def posture_signals(obs: PersonPoseObservation, cfg: FallConfig) -> PostureSignals:
    aspect = _aspect_ratio(obs.bbox)
    torso = torso_angle_from_vertical_deg(obs.keypoints, cfg.keypoint_threshold)
    head_hip = head_above_hip_ratio(obs.keypoints, obs.bbox, cfg.keypoint_threshold)
    kp = _keypoints_by_name(obs.keypoints, cfg.keypoint_threshold)

    below_ankles: bool | None = None
    below_knees: bool | None = None
    kp_aspect: float | None = None
    if cfg.joint_order_votes:
        below_ankles = shoulders_below_ankles(kp)
        below_knees = shoulders_below_knees(kp)
        kp_aspect = keypoint_box_aspect_ratio(kp, cfg.keypoint_box_min_points)

    # (says lying, weight). Shoulders at or below ankle height weighs double:
    # standing, sitting, squatting and bending all keep the shoulders well
    # above the feet, so it is the strongest single cue in the set.
    votes: list[tuple[bool, int]] = []
    if aspect is not None:
        votes.append((aspect >= cfg.lying_aspect_ratio, 1))
    if torso is not None:
        votes.append((torso >= cfg.torso_horizontal_deg, 1))
    if head_hip is not None:
        votes.append((head_hip < cfg.head_hip_collapse_ratio, 1))
    if kp_aspect is not None:
        votes.append((kp_aspect >= cfg.keypoint_box_aspect_ratio, 1))
    if below_knees is not None:
        votes.append((below_knees, 1))
    if below_ankles is not None:
        votes.append((below_ankles, 2))

    total_weight = sum(weight for _says, weight in votes)
    lying_weight = sum(weight for says, weight in votes if says)
    # Up to the three original shape votes a tie still counts as lying, as it
    # always has. Once joint-order votes join, a tie no longer does: one
    # ambiguous cue must not tip a crouch or a lean into a fall.
    if total_weight <= 3:
        is_lying = total_weight > 0 and lying_weight * 2 >= total_weight
    else:
        is_lying = lying_weight * 2 > total_weight

    pose_quality_mode = "keypoints" if torso is not None or head_hip is not None else "bbox_only"
    stacked = cfg.joint_order_votes and legs_stacked_upright(kp)
    torso_upright = torso is None or torso < cfg.torso_horizontal_deg
    is_upright = torso_upright and (
        (aspect is not None and aspect <= cfg.upright_aspect_ratio) or stacked
    )
    return PostureSignals(
        aspect_ratio=aspect,
        torso_angle_deg=torso,
        head_hip_ratio=head_hip,
        available_votes=len(votes),
        is_lying=is_lying,
        is_upright=is_upright,
        pose_quality_mode=pose_quality_mode,
        shoulders_below_ankles=below_ankles,
        shoulders_below_knees=below_knees,
        keypoint_box_aspect_ratio=kp_aspect,
        legs_stacked_upright=stacked,
        lying_weight=lying_weight,
        total_weight=total_weight,
    )


@register_rule("fall")
class FallRule(BehaviorRule):
    """Fires after an upright-to-lying transition persists past min_down_ms."""

    def __init__(self, rule_config, zone, cooldown) -> None:
        super().__init__(rule_config, zone, cooldown)
        self.params = FallConfig.from_rule_config(
            rule_config.config,
            fallback_cooldown_s=rule_config.cooldown_s,
        )

    def evaluate(self, track: TrackState) -> SecurityEvent | None:
        if not self.config.enabled or not track.observations:
            return None

        current_obs = track.observations[-1]
        if self.zone.polygon and not point_in_polygon(
            current_obs.bbox.foot_point,
            self.zone.polygon,
        ):
            return None

        current_visible = visible_keypoint_count(
            current_obs.keypoints,
            self.params.keypoint_threshold,
        )
        if current_visible < self.params.min_visible_keypoints:
            return None

        current = posture_signals(current_obs, self.params)
        if not current.is_lying:
            return None

        first_lying_idx = self._first_continuous_lying_index(track.observations)
        on_ground_ms = current_obs.timestamp_ms - track.observations[first_lying_idx].timestamp_ms
        if on_ground_ms < self.params.min_down_ms:
            return None

        is_upright_before = self._had_upright_before(track.observations, first_lying_idx)
        if self.params.require_transition and not is_upright_before:
            return None

        now_ms = track.last_seen_ms
        cooldown_key = self.cooldown_key(track)
        if not self.cooldown.can_emit(cooldown_key, now_ms):
            return None
        self.cooldown.record_emit(cooldown_key, self.params.cooldown_s, now_ms)

        start_ts_ms = track.observations[first_lying_idx].timestamp_ms
        return SecurityEvent(
            event_type="fall",
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
                f"Track {track.track_id} fall in zone '{self.zone.name}': "
                f"lying {on_ground_ms}ms"
            ),
            snapshot_required=self.config.snapshot_required,
            clip_required=self.config.clip_required,
            payload={
                "algorithm_id": self.config.algorithm_id or "behavior.fall",
                "rule_id": self.config.name,
                "camera_id": current_obs.camera_id,
                "zone_id": self.zone.name,
                "track_id": track.track_id,
                "on_ground_ms": on_ground_ms,
                "require_transition": self.params.require_transition,
                "transition_window_ms": self.params.transition_window_ms,
                "pose_quality_mode": current.pose_quality_mode,
                "visible_keypoint_count": current_visible,
                "posture": current.payload(is_upright_before=is_upright_before),
            },
        )

    def _first_continuous_lying_index(
        self,
        observations: list[PersonPoseObservation],
    ) -> int:
        first_idx = len(observations) - 1
        for idx in range(len(observations) - 2, -1, -1):
            if posture_signals(observations[idx], self.params).is_lying:
                first_idx = idx
            else:
                break
        return first_idx

    def _had_upright_before(
        self,
        observations: list[PersonPoseObservation],
        first_lying_idx: int,
    ) -> bool:
        lying_start_ts = observations[first_lying_idx].timestamp_ms
        for idx in range(first_lying_idx - 1, -1, -1):
            obs = observations[idx]
            if lying_start_ts - obs.timestamp_ms > self.params.transition_window_ms:
                break
            if posture_signals(obs, self.params).is_upright:
                return True
        return False
