"""Camera configuration loader for the savant_security mainline.

Reads a ``cameras.yml`` file (written in the C1 export schema) into pure
Python dataclasses. Validates intrusion-rule references and parameter
shapes at load time so the runtime can fail fast on a bad config rather
than silently dropping events.

This module is intentionally narrow:

- It does not import Savant or DeepStream.
- It does not connect to PostgreSQL or call the FastAPI service.
- It does not write any file or talk to a network.

The expected file shape mirrors GET /api/v1/cameras/config/export from
Phase C1::

    cameras:
      cam_001:
        enabled: true
        source_id: phase3h
        name: Test Camera
        rtsp_url: rtsp://...
        gpu_id: 0
        location: Test Area      # optional
        site_id: site_a          # optional
        zones:
          perimeter:
            type: polygon
            points: [[100, 300], ...]
            payload: {...}       # optional
        rules:
          intrusion:
            enabled: true
            zone: perimeter
            min_inside_ms: 1000
            cooldown_s: 30
            severity: medium
            snapshot_required: true
            clip_required: true

The historical phase3h_zmq ``cameras.yml`` (zones.<name>.polygon /
rules.<name>.rule_type) is **not** the format this loader expects. The
phase3h_zmq runtime keeps its own loader; this one is for the future
savant_security entrypoint (C1.2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml


ALLOWED_ZONE_TYPES = ("polygon", "line", "direction_line")
ALLOWED_SEVERITIES = ("low", "medium", "high")

# Polygon ROI vertex bounds. Must match
# services/api/app/schemas/cameras.py — config the API accepts must
# also load at runtime, and vice versa.
POLYGON_MIN_POINTS = 3
POLYGON_MAX_POINTS = 10


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ZoneEntry:
    name: str
    type: str
    points: List[List[float]] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RuleEntry:
    rule_type: str
    enabled: bool
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CameraEntry:
    camera_id: str
    source_id: str
    name: str
    rtsp_url: str
    enabled: bool = True
    gpu_id: int = 0
    location: Optional[str] = None
    site_id: Optional[str] = None
    zones: Dict[str, ZoneEntry] = field(default_factory=dict)
    rules: Dict[str, RuleEntry] = field(default_factory=dict)


@dataclass
class CameraConfigBundle:
    """Loaded camera configuration, indexed for the runtime's needs."""

    cameras: Dict[str, CameraEntry] = field(default_factory=dict)
    _by_source_id: Dict[str, str] = field(default_factory=dict)

    def get_camera(self, camera_id: str) -> Optional[CameraEntry]:
        return self.cameras.get(camera_id)

    def get_by_source_id(self, source_id: str) -> Optional[CameraEntry]:
        camera_id = self._by_source_id.get(source_id)
        if camera_id is None:
            return None
        return self.cameras.get(camera_id)

    def get_zone(self, camera_id: str, zone_name: str) -> Optional[ZoneEntry]:
        cam = self.cameras.get(camera_id)
        if cam is None:
            return None
        return cam.zones.get(zone_name)

    def get_rule(self, camera_id: str, rule_type: str) -> Optional[RuleEntry]:
        cam = self.cameras.get(camera_id)
        if cam is None:
            return None
        return cam.rules.get(rule_type)

    def iter_enabled_cameras(self) -> Iterable[CameraEntry]:
        for cam_id in sorted(self.cameras):
            cam = self.cameras[cam_id]
            if cam.enabled:
                yield cam


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class CameraConfigError(ValueError):
    """Raised when a cameras.yml fails validation."""


def load_camera_config(path: str) -> CameraConfigBundle:
    """Read *path*, parse it, validate the C1 schema, and return a bundle.

    Raises ``CameraConfigError`` on any structural or semantic violation
    (unknown zone reference, missing required field, bad type, ...).
    Raises ``FileNotFoundError`` if *path* does not exist.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"cameras.yml not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CameraConfigError(
            f"top-level YAML must be a mapping, got {type(raw).__name__}"
        )

    cameras_map = raw.get("cameras", {})
    if not isinstance(cameras_map, dict):
        raise CameraConfigError(
            f"'cameras' must be a mapping, got {type(cameras_map).__name__}"
        )

    bundle = CameraConfigBundle()

    for camera_id, cam_raw in cameras_map.items():
        if not isinstance(cam_raw, dict):
            raise CameraConfigError(
                f"cameras.{camera_id} must be a mapping, got "
                f"{type(cam_raw).__name__}"
            )
        cam = _parse_camera(camera_id, cam_raw)

        if cam.source_id in bundle._by_source_id:
            other = bundle._by_source_id[cam.source_id]
            raise CameraConfigError(
                f"duplicate source_id {cam.source_id!r} on cameras "
                f"{other!r} and {camera_id!r}"
            )

        bundle.cameras[camera_id] = cam
        bundle._by_source_id[cam.source_id] = camera_id

    return bundle


def _parse_camera(camera_id: str, raw: Dict[str, Any]) -> CameraEntry:
    if not camera_id:
        raise CameraConfigError("camera_id must be a non-empty string")

    source_id = _require_str(raw, "source_id", camera_id)
    name = _require_str(raw, "name", camera_id)
    rtsp_url = _require_str(raw, "rtsp_url", camera_id)

    enabled = bool(raw.get("enabled", True))
    gpu_id = raw.get("gpu_id", 0)
    if not isinstance(gpu_id, int) or isinstance(gpu_id, bool):
        raise CameraConfigError(
            f"cameras.{camera_id}.gpu_id must be an integer"
        )

    location = raw.get("location")
    if location is not None and not isinstance(location, str):
        raise CameraConfigError(
            f"cameras.{camera_id}.location must be a string when provided"
        )
    site_id = raw.get("site_id")
    if site_id is not None and not isinstance(site_id, str):
        raise CameraConfigError(
            f"cameras.{camera_id}.site_id must be a string when provided"
        )

    zones = _parse_zones(camera_id, raw.get("zones", {}))
    rules = _parse_rules(camera_id, raw.get("rules", {}), zones)

    return CameraEntry(
        camera_id=camera_id,
        source_id=source_id,
        name=name,
        rtsp_url=rtsp_url,
        enabled=enabled,
        gpu_id=gpu_id,
        location=location,
        site_id=site_id,
        zones=zones,
        rules=rules,
    )


def _parse_zones(camera_id: str, zones_raw: Any) -> Dict[str, ZoneEntry]:
    if not isinstance(zones_raw, dict):
        raise CameraConfigError(
            f"cameras.{camera_id}.zones must be a mapping when provided"
        )
    out: Dict[str, ZoneEntry] = {}
    for zone_name, z_raw in zones_raw.items():
        if not zone_name:
            raise CameraConfigError(
                f"cameras.{camera_id} has an empty zone name"
            )
        if not isinstance(z_raw, dict):
            raise CameraConfigError(
                f"cameras.{camera_id}.zones.{zone_name} must be a mapping"
            )
        ztype = z_raw.get("type")
        if ztype not in ALLOWED_ZONE_TYPES:
            raise CameraConfigError(
                f"cameras.{camera_id}.zones.{zone_name}.type must be one of "
                f"{list(ALLOWED_ZONE_TYPES)}, got {ztype!r}"
            )
        points_raw = z_raw.get("points", [])
        if not isinstance(points_raw, list):
            raise CameraConfigError(
                f"cameras.{camera_id}.zones.{zone_name}.points must be a list"
            )
        points: List[List[float]] = []
        for i, pt in enumerate(points_raw):
            if not isinstance(pt, list) or len(pt) != 2:
                raise CameraConfigError(
                    f"cameras.{camera_id}.zones.{zone_name}.points[{i}] "
                    f"must be [x, y]"
                )
            for j, coord in enumerate(pt):
                if not isinstance(coord, (int, float)) or isinstance(coord, bool):
                    raise CameraConfigError(
                        f"cameras.{camera_id}.zones.{zone_name}.points"
                        f"[{i}][{j}] must be a number"
                    )
            points.append([float(pt[0]), float(pt[1])])

        if ztype == "polygon":
            if len(points) < POLYGON_MIN_POINTS or len(points) > POLYGON_MAX_POINTS:
                raise CameraConfigError(
                    f"cameras.{camera_id}.zones.{zone_name}.points polygon "
                    f"requires {POLYGON_MIN_POINTS} to {POLYGON_MAX_POINTS} "
                    f"points (got {len(points)})"
                )
        if ztype in ("line", "direction_line") and len(points) != 2:
            raise CameraConfigError(
                f"cameras.{camera_id}.zones.{zone_name}.points {ztype} "
                f"requires exactly 2 points (got {len(points)})"
            )

        payload = z_raw.get("payload", {})
        if not isinstance(payload, dict):
            raise CameraConfigError(
                f"cameras.{camera_id}.zones.{zone_name}.payload must be a "
                f"mapping when provided"
            )

        out[zone_name] = ZoneEntry(
            name=zone_name, type=ztype, points=points, payload=dict(payload)
        )
    return out


def _parse_rules(
    camera_id: str,
    rules_raw: Any,
    zones: Dict[str, ZoneEntry],
) -> Dict[str, RuleEntry]:
    if not isinstance(rules_raw, dict):
        raise CameraConfigError(
            f"cameras.{camera_id}.rules must be a mapping when provided"
        )
    out: Dict[str, RuleEntry] = {}
    for rule_type, r_raw in rules_raw.items():
        if not rule_type:
            raise CameraConfigError(
                f"cameras.{camera_id} has an empty rule_type"
            )
        if not isinstance(r_raw, dict):
            raise CameraConfigError(
                f"cameras.{camera_id}.rules.{rule_type} must be a mapping"
            )
        enabled = bool(r_raw.get("enabled", True))
        # Strip the "enabled" key from the inline config the rule sees.
        config = {k: v for k, v in r_raw.items() if k != "enabled"}

        if rule_type == "intrusion":
            _validate_intrusion(camera_id, config, zones)

        out[rule_type] = RuleEntry(
            rule_type=rule_type, enabled=enabled, config=config
        )
    return out


def _validate_intrusion(
    camera_id: str,
    config: Dict[str, Any],
    zones: Dict[str, ZoneEntry],
) -> None:
    zone = config.get("zone")
    if not isinstance(zone, str) or not zone:
        raise CameraConfigError(
            f"cameras.{camera_id}.rules.intrusion.zone must be a non-empty string"
        )
    if zone not in zones:
        raise CameraConfigError(
            f"cameras.{camera_id}.rules.intrusion.zone references unknown "
            f"zone_name: {zone!r}"
        )

    min_inside_ms = config.get("min_inside_ms")
    if not isinstance(min_inside_ms, int) or isinstance(min_inside_ms, bool):
        raise CameraConfigError(
            f"cameras.{camera_id}.rules.intrusion.min_inside_ms must be an integer"
        )
    if min_inside_ms <= 0:
        raise CameraConfigError(
            f"cameras.{camera_id}.rules.intrusion.min_inside_ms must be > 0"
        )

    cooldown_s = config.get("cooldown_s")
    if not isinstance(cooldown_s, int) or isinstance(cooldown_s, bool):
        raise CameraConfigError(
            f"cameras.{camera_id}.rules.intrusion.cooldown_s must be an integer"
        )
    if cooldown_s < 0:
        raise CameraConfigError(
            f"cameras.{camera_id}.rules.intrusion.cooldown_s must be >= 0"
        )

    severity = config.get("severity", "medium")
    if severity not in ALLOWED_SEVERITIES:
        raise CameraConfigError(
            f"cameras.{camera_id}.rules.intrusion.severity must be one of "
            f"{list(ALLOWED_SEVERITIES)}, got {severity!r}"
        )

    for bool_field in ("snapshot_required", "clip_required"):
        if bool_field in config and not isinstance(config[bool_field], bool):
            raise CameraConfigError(
                f"cameras.{camera_id}.rules.intrusion.{bool_field} must be "
                f"a boolean when provided"
            )


def _require_str(raw: Dict[str, Any], key: str, camera_id: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise CameraConfigError(
            f"cameras.{camera_id}.{key} must be a non-empty string"
        )
    return value
