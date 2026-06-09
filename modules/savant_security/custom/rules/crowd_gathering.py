"""Crowd gathering frame-level behavior rule."""

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
class CrowdConfig:
    min_person_count: int = 5
    exit_person_count: int = 3
    min_duration_ms: int = 2000
    eps_px: float = 180.0
    require_in_zone: bool = True
    cooldown_s: int = 60

    @classmethod
    def from_rule_config(
        cls,
        config: dict[str, Any],
        fallback_cooldown_s: int,
    ) -> "CrowdConfig":
        defaults = cls(cooldown_s=fallback_cooldown_s or cls.cooldown_s)
        cfg = dict(config or {})
        min_duration_raw = _config_alias(cfg, "min_duration_s", "min_duration_ms")
        min_duration_ms = defaults.min_duration_ms
        if "min_duration_ms" in cfg:
            min_duration_ms = _int_value(min_duration_raw, defaults.min_duration_ms)
        elif min_duration_raw is not None:
            min_duration_ms = int(_float_value(min_duration_raw, 2.0) * 1000)
        return cls(
            min_person_count=_env_int(
                "CROWD_MIN_PERSON_COUNT",
                _int_value(
                    _config_alias(cfg, "min_person_count", "count_high"),
                    defaults.min_person_count,
                ),
            ),
            exit_person_count=_env_int(
                "CROWD_EXIT_PERSON_COUNT",
                _int_value(
                    _config_alias(cfg, "exit_person_count", "count_low"),
                    defaults.exit_person_count,
                ),
            ),
            min_duration_ms=_env_int(
                "CROWD_MIN_DURATION_MS",
                min_duration_ms,
            ),
            eps_px=_env_float(
                "CROWD_EPS_PX",
                _float_value(cfg.get("eps_px"), defaults.eps_px),
            ),
            require_in_zone=_env_bool(
                "CROWD_REQUIRE_IN_ZONE",
                _bool_value(cfg.get("require_in_zone"), defaults.require_in_zone),
            ),
            cooldown_s=_env_int(
                "CROWD_COOLDOWN_S",
                _int_value(cfg.get("cooldown_s"), defaults.cooldown_s),
            ),
        )


@dataclass(frozen=True)
class CrowdCluster:
    members: frozenset[int]
    centroid: tuple[float, float]


def _cluster_by_proximity(
    points: list[tuple[int, tuple[float, float]]],
    eps_px: float,
) -> list[CrowdCluster]:
    visited: set[int] = set()
    clusters: list[CrowdCluster] = []
    eps2 = eps_px * eps_px
    for start in range(len(points)):
        if start in visited:
            continue
        stack = [start]
        visited.add(start)
        component: list[int] = []
        while stack:
            idx = stack.pop()
            component.append(idx)
            x0, y0 = points[idx][1]
            for other_idx, (_, (x1, y1)) in enumerate(points):
                if other_idx in visited:
                    continue
                if (x0 - x1) ** 2 + (y0 - y1) ** 2 <= eps2:
                    visited.add(other_idx)
                    stack.append(other_idx)
        member_ids = frozenset(points[idx][0] for idx in component)
        centroid = (
            sum(points[idx][1][0] for idx in component) / len(component),
            sum(points[idx][1][1] for idx in component) / len(component),
        )
        clusters.append(CrowdCluster(members=member_ids, centroid=centroid))
    return clusters


def find_crowd_clusters(
    tracks: list[TrackState],
    cfg: CrowdConfig,
    zone_polygon: list[tuple[float, float]] | None = None,
) -> list[CrowdCluster]:
    points: list[tuple[int, tuple[float, float]]] = []
    for track in tracks:
        if not track.observations:
            continue
        foot_point = track.observations[-1].bbox.foot_point
        if cfg.require_in_zone and zone_polygon:
            if not point_in_polygon(foot_point, zone_polygon):
                continue
        points.append((track.track_id, foot_point))
    return _cluster_by_proximity(points, cfg.eps_px)


@register_rule("crowd_gathering")
class CrowdGatheringRule(FrameBehaviorRule):
    """Fires for sustained dense clusters in a zone."""

    def __init__(self, rule_config, zone, cooldown) -> None:
        super().__init__(rule_config, zone, cooldown)
        self.params = CrowdConfig.from_rule_config(
            rule_config.config,
            fallback_cooldown_s=rule_config.cooldown_s,
        )
        self._clusters: dict[int, dict[str, Any]] = {}
        self._next_cluster_id = 1

    def evaluate_frame(
        self,
        frame_tracks: list[TrackState],
        frame_ts_ms: int,
    ) -> list[SecurityEvent]:
        if not self.config.enabled or not frame_tracks:
            if not frame_tracks:
                self._clusters.clear()
            return []
        frame_ts_ms = frame_ts_ms or max((track.last_seen_ms for track in frame_tracks), default=0)
        zone_polygon = self.zone.polygon if self.zone and self.zone.polygon else None
        clusters = find_crowd_clusters(frame_tracks, self.params, zone_polygon)
        camera_id, source_id = self._frame_identity(frame_tracks)

        events: list[SecurityEvent] = []
        matched_ids: set[int] = set()
        for cluster in clusters:
            cluster_id = self._match_cluster(cluster.members, matched_ids)
            if cluster_id is None:
                cluster_id = self._next_cluster_id
                self._next_cluster_id += 1
                self._clusters[cluster_id] = {
                    "members": set(),
                    "is_crowd": False,
                    "since_ts": frame_ts_ms,
                }
            matched_ids.add(cluster_id)
            state = self._clusters[cluster_id]
            state["members"] = set(cluster.members)
            count = len(cluster.members)

            if not state["is_crowd"] and count >= self.params.min_person_count:
                state["is_crowd"] = True
                state["since_ts"] = frame_ts_ms
            elif state["is_crowd"] and count <= self.params.exit_person_count:
                state["is_crowd"] = False
                state["since_ts"] = frame_ts_ms

            if not state["is_crowd"]:
                continue
            duration_ms = frame_ts_ms - int(state["since_ts"])
            if duration_ms < self.params.min_duration_ms:
                continue

            cooldown_key = f"{camera_id}:crowd_gathering:{self.zone.name}:{cluster_id}"
            if not self.cooldown.can_emit(cooldown_key, frame_ts_ms):
                continue
            self.cooldown.record_emit(cooldown_key, self.params.cooldown_s, frame_ts_ms)
            members = sorted(cluster.members)
            events.append(
                SecurityEvent(
                    event_type="crowd_gathering",
                    camera_id=camera_id,
                    source_id=source_id,
                    track_id=0,
                    start_ts_ms=int(state["since_ts"]),
                    end_ts_ms=frame_ts_ms,
                    confidence=min(1.0, count / max(self.params.min_person_count, 1)),
                    severity=self.config.severity,
                    zone=self.zone.name,
                    rule_name=self.config.name,
                    description=(
                        f"Crowd gathering in zone '{self.zone.name}': "
                        f"{count} people for {duration_ms}ms"
                    ),
                    snapshot_required=self.config.snapshot_required,
                    clip_required=self.config.clip_required,
                    payload={
                        "algorithm_id": self.config.algorithm_id
                        or "behavior.crowd_gathering",
                        "rule_id": self.config.name,
                        "camera_id": camera_id,
                        "zone_id": self.zone.name,
                        "cluster_id": cluster_id,
                        "member_track_ids": members,
                        "person_count": count,
                        "duration_ms": duration_ms,
                        "centroid": {
                            "x": cluster.centroid[0],
                            "y": cluster.centroid[1],
                        },
                        "eps_px": self.params.eps_px,
                    },
                )
            )

        for cluster_id in list(self._clusters):
            if cluster_id not in matched_ids:
                del self._clusters[cluster_id]
        return events

    def _match_cluster(
        self,
        members: frozenset[int],
        already_matched: set[int],
    ) -> int | None:
        best_id = None
        best_overlap = 0
        for cluster_id, state in self._clusters.items():
            if cluster_id in already_matched:
                continue
            overlap = len(set(members) & set(state["members"]))
            if overlap > best_overlap:
                best_id = cluster_id
                best_overlap = overlap
        return best_id if best_overlap > 0 else None

    @staticmethod
    def _frame_identity(frame_tracks: list[TrackState]) -> tuple[str, str]:
        for track in frame_tracks:
            if track.observations:
                obs = track.observations[-1]
                return obs.camera_id, obs.source_id
        return "", ""


def distance_px(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
