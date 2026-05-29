"""Pydantic request/response models for camera configuration endpoints (Phase C1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator


ALLOWED_ZONE_TYPES = ("polygon", "line", "direction_line")
ALLOWED_SEVERITIES = ("low", "medium", "high")

# Polygon ROI vertex bounds. 3 keeps it a valid polygon; 10 keeps the
# error surface manageable without ruling out reasonable site shapes.
POLYGON_MIN_POINTS = 3
POLYGON_MAX_POINTS = 10


def _iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------


class CameraCreate(BaseModel):
    id: str
    source_id: str
    name: str
    rtsp_url: str
    site_id: Optional[str] = None
    location: Optional[str] = None
    gpu_id: int = 0
    enabled: bool = True

    @field_validator("id", "source_id", "name", "rtsp_url")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be non-empty")
        return v


class CameraResponse(BaseModel):
    id: str
    source_id: str
    name: str
    rtsp_url: str
    site_id: Optional[str] = None
    location: Optional[str] = None
    gpu_id: int = 0
    enabled: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_db_row(cls, row: Dict[str, Any]) -> "CameraResponse":
        return cls(
            id=row["id"],
            source_id=row["source_id"],
            name=row["name"],
            rtsp_url=row["rtsp_url"],
            site_id=row.get("site_id"),
            location=row.get("location"),
            gpu_id=int(row.get("gpu_id", 0)),
            enabled=bool(row.get("enabled", True)),
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


# ---------------------------------------------------------------------------
# Zone
# ---------------------------------------------------------------------------


class ZoneCreate(BaseModel):
    zone_name: str
    zone_type: str
    points: List[List[float]]
    payload: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("zone_name")
    @classmethod
    def _zname(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("zone_name must be non-empty")
        return v

    @field_validator("zone_type")
    @classmethod
    def _ztype(cls, v: str) -> str:
        if v not in ALLOWED_ZONE_TYPES:
            raise ValueError(
                f"zone_type must be one of {list(ALLOWED_ZONE_TYPES)}, got {v!r}"
            )
        return v

    @field_validator("points")
    @classmethod
    def _points_shape(cls, v: List[List[float]]) -> List[List[float]]:
        if not isinstance(v, list):
            raise ValueError("points must be a list")
        for i, p in enumerate(v):
            if not isinstance(p, list) or len(p) != 2:
                raise ValueError(f"points[{i}] must be [x, y] numeric pair")
            for j, coord in enumerate(p):
                if not isinstance(coord, (int, float)) or isinstance(coord, bool):
                    raise ValueError(
                        f"points[{i}][{j}] must be a number (got {type(coord).__name__})"
                    )
        return v

    def validate_for_zone_type(self) -> None:
        """Shape check that depends on both zone_type and points length."""
        if self.zone_type == "polygon":
            n = len(self.points)
            if n < POLYGON_MIN_POINTS or n > POLYGON_MAX_POINTS:
                raise ValueError(
                    f"polygon zone requires {POLYGON_MIN_POINTS} to "
                    f"{POLYGON_MAX_POINTS} points (got {n})"
                )
        if self.zone_type in ("line", "direction_line") and len(self.points) != 2:
            raise ValueError(
                f"{self.zone_type} zone requires exactly 2 points "
                f"(got {len(self.points)})"
            )


class ZoneResponse(BaseModel):
    id: int
    camera_id: str
    zone_name: str
    zone_type: str
    points: List[List[float]]
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_db_row(cls, row: Dict[str, Any]) -> "ZoneResponse":
        points = row.get("points") or []
        if isinstance(points, str):
            import json
            points = json.loads(points)
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        return cls(
            id=int(row["id"]),
            camera_id=row["camera_id"],
            zone_name=row["zone_name"],
            zone_type=row["zone_type"],
            points=[list(p) for p in points],
            payload=payload,
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


# ---------------------------------------------------------------------------
# Rule
# ---------------------------------------------------------------------------


class RuleCreate(BaseModel):
    rule_type: str
    enabled: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("rule_type")
    @classmethod
    def _rtype(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("rule_type must be non-empty")
        return v


class RuleResponse(BaseModel):
    id: int
    camera_id: str
    rule_type: str
    enabled: bool
    config: Dict[str, Any]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_db_row(cls, row: Dict[str, Any]) -> "RuleResponse":
        config = row.get("config") or {}
        if isinstance(config, str):
            import json
            config = json.loads(config)
        return cls(
            id=int(row["id"]),
            camera_id=row["camera_id"],
            rule_type=row["rule_type"],
            enabled=bool(row.get("enabled", True)),
            config=config,
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


def validate_intrusion_config(
    config: Dict[str, Any],
    existing_zone_names: List[str],
) -> Tuple[bool, str]:
    """Validate the ``config`` payload of an intrusion rule.

    Returns ``(ok, error_message)``. ``error_message`` is empty when ok.
    C1 strict-validates only intrusion. Other rule types are accepted
    as-is with no shape constraint.
    """
    zone = config.get("zone")
    if not zone or not isinstance(zone, str):
        return False, "config.zone is required and must be a string"
    if zone not in existing_zone_names:
        return (
            False,
            f"config.zone references unknown zone_name: {zone!r}",
        )

    min_inside_ms = config.get("min_inside_ms")
    if min_inside_ms is None or not isinstance(min_inside_ms, int) or isinstance(min_inside_ms, bool):
        return False, "config.min_inside_ms must be an integer"
    if min_inside_ms <= 0:
        return False, "config.min_inside_ms must be a positive integer"

    cooldown_s = config.get("cooldown_s")
    if cooldown_s is None or not isinstance(cooldown_s, int) or isinstance(cooldown_s, bool):
        return False, "config.cooldown_s must be an integer"
    if cooldown_s < 0:
        return False, "config.cooldown_s must be >= 0"

    severity = config.get("severity", "medium")
    if severity not in ALLOWED_SEVERITIES:
        return (
            False,
            f"config.severity must be one of {list(ALLOWED_SEVERITIES)}, got {severity!r}",
        )

    for bool_field in ("snapshot_required", "clip_required"):
        if bool_field in config and not isinstance(config[bool_field], bool):
            return False, f"config.{bool_field} must be a boolean if provided"

    return True, ""


def apply_intrusion_defaults(config: Dict[str, Any]) -> Dict[str, Any]:
    """Fill in intrusion rule defaults that were not supplied by the caller."""
    out = dict(config)
    out.setdefault("severity", "medium")
    out.setdefault("snapshot_required", True)
    out.setdefault("clip_required", True)
    return out


# ---------------------------------------------------------------------------
# Full config (single camera)
# ---------------------------------------------------------------------------


class CameraConfigResponse(BaseModel):
    """Aggregate view of one camera with its zones and rules."""

    camera: CameraResponse
    zones: List[ZoneResponse]
    rules: List[RuleResponse]


# ---------------------------------------------------------------------------
# Helpers for cameras.yml-compatible export
# ---------------------------------------------------------------------------


def build_export_doc(
    cameras: List[Dict[str, Any]],
    zones_by_camera: Dict[str, List[Dict[str, Any]]],
    rules_by_camera: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Build a cameras.yml-compatible dict from raw DB rows."""
    root: Dict[str, Any] = {"cameras": {}}
    for cam in cameras:
        cam_id = cam["id"]
        cam_zones = zones_by_camera.get(cam_id, [])
        cam_rules = rules_by_camera.get(cam_id, [])

        zones_dict: Dict[str, Any] = {}
        for z in cam_zones:
            pts = z.get("points") or []
            if isinstance(pts, str):
                import json
                pts = json.loads(pts)
            zones_dict[z["zone_name"]] = {
                "type": z["zone_type"],
                "points": [list(p) for p in pts],
            }
            payload = z.get("payload") or {}
            if isinstance(payload, str):
                import json
                payload = json.loads(payload)
            if payload:
                zones_dict[z["zone_name"]]["payload"] = payload

        rules_dict: Dict[str, Any] = {}
        for r in cam_rules:
            cfg = r.get("config") or {}
            if isinstance(cfg, str):
                import json
                cfg = json.loads(cfg)
            rules_dict[r["rule_type"]] = {
                "enabled": bool(r.get("enabled", True)),
                **cfg,
            }
            if r.get("zone_id"):
                rules_dict[r["rule_type"]]["zone_id"] = r["zone_id"]
            if r.get("line_id"):
                rules_dict[r["rule_type"]]["line_id"] = r["line_id"]
            evidence_policy = r.get("evidence_policy") or {}
            if isinstance(evidence_policy, str):
                import json
                evidence_policy = json.loads(evidence_policy)
            if evidence_policy:
                rules_dict[r["rule_type"]]["evidence_policy"] = evidence_policy

        cam_doc: Dict[str, Any] = {
            "enabled": bool(cam.get("enabled", True)),
            "source_id": cam["source_id"],
            "name": cam["name"],
            "rtsp_url": cam["rtsp_url"],
            "gpu_id": int(cam.get("gpu_id", 0)),
        }
        if cam.get("location"):
            cam_doc["location"] = cam["location"]
        if cam.get("site_id"):
            cam_doc["site_id"] = cam["site_id"]
        if zones_dict:
            cam_doc["zones"] = zones_dict
        if rules_dict:
            cam_doc["rules"] = rules_dict

        root["cameras"][cam_id] = cam_doc

    return root
