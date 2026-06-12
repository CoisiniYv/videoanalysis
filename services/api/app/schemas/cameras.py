"""Pydantic request/response models for camera configuration endpoints (camera config)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.algorithm_ids import (
    RULE_ALGORITHM_IDS,
    normalize_algorithm_id,
    runtime_rule_type_for_algorithm_id,
)


ALLOWED_ZONE_TYPES = ("polygon", "line", "direction_line")
ALLOWED_SEVERITIES = ("low", "medium", "high")
ALLOWED_ALGORITHM_IDS = RULE_ALGORITHM_IDS
OBSERVATION_ALGORITHM_IDS = ("face.observation",)
ALERT_ALGORITHM_IDS = tuple(
    algorithm_id
    for algorithm_id in ALLOWED_ALGORITHM_IDS
    if algorithm_id not in OBSERVATION_ALGORITHM_IDS
)

DEFAULT_ALERT_POLICY: Dict[str, Any] = {
    "global_alert_cooldown_s": 30,
    "cooldown_scope": "algorithm",
    "store_suppressed_events": True,
    "suppress_record_request": True,
    "critical_bypass": False,
}
DEFAULT_EVIDENCE_POLICY: Dict[str, Any] = {
    "snapshot_required": True,
    "clip_required": True,
    "pre_seconds": 5,
    "post_seconds": 5,
}

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


def _export_text(value: Any) -> str:
    return str(value)


def _export_yaml_safe(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {_export_text(k): _export_yaml_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_export_yaml_safe(v) for v in value]
    return value


def _export_rows_for(
    rows_by_camera: Dict[Any, List[Dict[str, Any]]],
    camera_id: Any,
) -> List[Dict[str, Any]]:
    rows = rows_by_camera.get(camera_id)
    if rows is not None:
        return rows
    return rows_by_camera.get(str(camera_id), [])


def _effective_evidence_policy(
    config: Dict[str, Any], evidence_policy: Dict[str, Any]
) -> Dict[str, Any]:
    return {
        "snapshot_required": config.get(
            "snapshot_required", DEFAULT_EVIDENCE_POLICY["snapshot_required"]
        ),
        "clip_required": config.get(
            "clip_required", DEFAULT_EVIDENCE_POLICY["clip_required"]
        ),
        "pre_seconds": config.get("pre_seconds", DEFAULT_EVIDENCE_POLICY["pre_seconds"]),
        "post_seconds": config.get("post_seconds", DEFAULT_EVIDENCE_POLICY["post_seconds"]),
        **evidence_policy,
    }


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
    input_type: str = "rtsp"
    rtsp_transport: str = "tcp"
    fps_policy: Dict[str, Any] = Field(default_factory=dict)
    alert_policy: Dict[str, Any] = Field(default_factory=lambda: dict(DEFAULT_ALERT_POLICY))

    @field_validator("id", "source_id", "name", "rtsp_url")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be non-empty")
        return v

    @field_validator("input_type")
    @classmethod
    def _input_type(cls, v: str) -> str:
        if v != "rtsp":
            raise ValueError("input_type currently supports only 'rtsp'")
        return v

    @field_validator("rtsp_transport")
    @classmethod
    def _rtsp_transport(cls, v: str) -> str:
        if v not in ("tcp", "udp"):
            raise ValueError("rtsp_transport must be 'tcp' or 'udp'")
        return v


class CameraUpdate(BaseModel):
    name: Optional[str] = None
    source_id: Optional[str] = None
    rtsp_url: Optional[str] = None
    site_id: Optional[str] = None
    location: Optional[str] = None
    gpu_id: Optional[int] = None
    enabled: Optional[bool] = None
    input_type: Optional[str] = None
    rtsp_transport: Optional[str] = None
    fps_policy: Optional[Dict[str, Any]] = None
    alert_policy: Optional[Dict[str, Any]] = None

    @field_validator("input_type")
    @classmethod
    def _input_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v != "rtsp":
            raise ValueError("input_type currently supports only 'rtsp'")
        return v

    @field_validator("rtsp_transport")
    @classmethod
    def _rtsp_transport(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("tcp", "udp"):
            raise ValueError("rtsp_transport must be 'tcp' or 'udp'")
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
    input_type: str = "rtsp"
    rtsp_transport: str = "tcp"
    fps_policy: Dict[str, Any] = Field(default_factory=dict)
    alert_policy: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_db_row(cls, row: Dict[str, Any]) -> "CameraResponse":
        return cls(
            id=str(row["id"]),
            source_id=row["source_id"],
            name=row["name"],
            rtsp_url=row["rtsp_url"],
            site_id=row.get("site_id"),
            location=row.get("location"),
            gpu_id=int(row.get("gpu_id", 0)),
            enabled=bool(row.get("enabled", True)),
            input_type=row.get("input_type") or "rtsp",
            rtsp_transport=row.get("rtsp_transport") or "tcp",
            fps_policy=_json_dict(row.get("fps_policy")),
            alert_policy=_json_dict(row.get("alert_policy")),
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


# ---------------------------------------------------------------------------
# Zone
# ---------------------------------------------------------------------------


class ZoneCreate(BaseModel):
    zone_id: Optional[str] = None
    zone_name: Optional[str] = None
    zone_type: str
    coordinate_space: str = "pixel"
    points: List[List[float]]
    enabled: bool = True
    payload: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_ids(self) -> "ZoneCreate":
        if not self.zone_id and not self.zone_name:
            raise ValueError("zone_id or zone_name must be provided")
        if not self.zone_id:
            self.zone_id = self.zone_name
        if not self.zone_name:
            self.zone_name = self.zone_id
        return self

    @field_validator("zone_id", "zone_name")
    @classmethod
    def _zname(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and (not v or not v.strip()):
            raise ValueError("zone_id/zone_name must be non-empty")
        return v

    @field_validator("zone_type")
    @classmethod
    def _ztype(cls, v: str) -> str:
        if v not in ALLOWED_ZONE_TYPES:
            raise ValueError(
                f"zone_type must be one of {list(ALLOWED_ZONE_TYPES)}, got {v!r}"
            )
        return v

    @field_validator("coordinate_space")
    @classmethod
    def _coordinate_space(cls, v: str) -> str:
        if v not in ("pixel", "normalized"):
            raise ValueError("coordinate_space must be 'pixel' or 'normalized'")
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
    id: str | int
    camera_id: str
    zone_id: str
    zone_name: str
    zone_type: str
    coordinate_space: str = "pixel"
    points: List[List[float]]
    enabled: bool = True
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_db_row(cls, row: Dict[str, Any]) -> "ZoneResponse":
        points = row.get("points") or []
        if isinstance(points, str):
            import json
            points = json.loads(points)
        return cls(
            id=str(row["id"]),
            camera_id=str(row["camera_id"]),
            zone_id=row.get("zone_id") or row["zone_name"],
            zone_name=row["zone_name"],
            zone_type=row["zone_type"],
            coordinate_space=row.get("coordinate_space") or "pixel",
            points=[list(p) for p in points],
            enabled=bool(row.get("enabled", True)),
            payload=_json_dict(row.get("payload")),
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


# ---------------------------------------------------------------------------
# Rule
# ---------------------------------------------------------------------------


class RuleCreate(BaseModel):
    rule_id: Optional[str] = None
    algorithm_id: Optional[str] = None
    rule_type: Optional[str] = None
    enabled: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_rule(self) -> "RuleCreate":
        if not self.algorithm_id and not self.rule_type:
            raise ValueError("algorithm_id or rule_type must be provided")
        if self.algorithm_id:
            self.algorithm_id = normalize_algorithm_id(self.algorithm_id)
        if self.rule_type and not self.algorithm_id:
            self.algorithm_id = normalize_algorithm_id(self.rule_type)
        if self.algorithm_id and self.algorithm_id not in ALLOWED_ALGORITHM_IDS:
            raise ValueError(f"algorithm_id must be one of {list(ALLOWED_ALGORITHM_IDS)}")
        if not self.rule_type:
            self.rule_type = runtime_rule_type_for_algorithm_id(self.algorithm_id)
        if not self.rule_id:
            safe = str(self.algorithm_id).replace(".", "_")
            self.rule_id = f"rule_{safe}"
        return self

    @field_validator("rule_id", "algorithm_id", "rule_type")
    @classmethod
    def _rtype(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and (not v or not v.strip()):
            raise ValueError("rule_id/algorithm_id/rule_type must be non-empty")
        return v


class RuleUpdate(BaseModel):
    rule_id: Optional[str] = None
    algorithm_id: Optional[str] = None
    rule_type: Optional[str] = None
    enabled: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None

    @field_validator("algorithm_id")
    @classmethod
    def _algorithm_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        normalized = normalize_algorithm_id(v)
        if normalized not in ALLOWED_ALGORITHM_IDS:
            raise ValueError(f"algorithm_id must be one of {list(ALLOWED_ALGORITHM_IDS)}")
        return normalized


class RuleResponse(BaseModel):
    id: str | int
    camera_id: str
    rule_id: str
    algorithm_id: str
    rule_type: str
    enabled: bool
    config: Dict[str, Any]
    rule_category: str = "alert"
    is_alert_rule: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_db_row(cls, row: Dict[str, Any]) -> "RuleResponse":
        config = _json_dict(row.get("config"))
        algorithm_id = normalize_algorithm_id(row.get("algorithm_id") or row.get("rule_type", ""))
        return cls(
            id=str(row["id"]),
            camera_id=str(row["camera_id"]),
            rule_id=row.get("rule_id") or f"rule_{row['id']}",
            algorithm_id=algorithm_id,
            rule_type=row["rule_type"],
            enabled=bool(row.get("enabled", True)),
            config=config,
            rule_category=rule_category(algorithm_id),
            is_alert_rule=is_alert_rule(algorithm_id),
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


class AlertPolicy(BaseModel):
    global_alert_cooldown_s: int = Field(default=30, ge=0)
    cooldown_scope: str = Field(default="algorithm", pattern="^(algorithm|event_type|global)$")
    store_suppressed_events: bool = True
    suppress_record_request: bool = True
    critical_bypass: bool = False


def _json_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json

        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return dict(value)


def is_alert_rule(algorithm_id: str) -> bool:
    algorithm_id = normalize_algorithm_id(algorithm_id)
    return algorithm_id in ALERT_ALGORITHM_IDS


def rule_category(algorithm_id: str) -> str:
    algorithm_id = normalize_algorithm_id(algorithm_id)
    if algorithm_id in OBSERVATION_ALGORITHM_IDS:
        return "observation"
    if algorithm_id in ALERT_ALGORITHM_IDS:
        return "alert"
    return "legacy"


def validate_intrusion_config(
    config: Dict[str, Any],
    existing_zone_names: List[str],
) -> Tuple[bool, str]:
    """Validate the ``config`` payload of an intrusion rule.

    Returns ``(ok, error_message)``. ``error_message`` is empty when ok.
    midterm strict-validates only intrusion. Other rule types are accepted
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
    out.setdefault("min_inside_ms", 1000)
    out.setdefault("cooldown_s", 30)
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
    alert_policy: Dict[str, Any] = Field(default_factory=dict)


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
        raw_cam_id = cam["id"]
        cam_id = _export_text(raw_cam_id)
        cam_zones = _export_rows_for(zones_by_camera, raw_cam_id)
        cam_rules = _export_rows_for(rules_by_camera, raw_cam_id)

        zones_dict: Dict[str, Any] = {}
        for z in cam_zones:
            pts = z.get("points") or []
            if isinstance(pts, str):
                import json
                pts = json.loads(pts)
            zone_id = _export_text(z.get("zone_id") or z["zone_name"])
            zones_dict[zone_id] = {
                "zone_id": zone_id,
                "zone_type": _export_text(z["zone_type"]),
                "type": _export_text(z["zone_type"]),
                "coordinate_space": _export_text(z.get("coordinate_space") or "pixel"),
                "points": _export_yaml_safe([list(p) for p in pts]),
                "enabled": bool(z.get("enabled", True)),
            }
            payload = z.get("payload") or {}
            if isinstance(payload, str):
                import json
                payload = json.loads(payload)
            if payload:
                zones_dict[zone_id]["payload"] = _export_yaml_safe(payload)

        rules_dict: Dict[str, Any] = {}
        for r in cam_rules:
            cfg = r.get("config") or {}
            if isinstance(cfg, str):
                import json
                cfg = json.loads(cfg)
            algorithm_id = normalize_algorithm_id(r.get("algorithm_id") or r["rule_type"])
            rule_id = _export_text(r.get("rule_id") or f"rule_{algorithm_id.replace('.', '_')}")
            cfg = _export_yaml_safe(cfg)
            rules_dict[rule_id] = {
                "rule_id": rule_id,
                "algorithm_id": algorithm_id,
                "rule_type": _export_text(r["rule_type"]),
                "enabled": bool(r.get("enabled", True)),
                "config": cfg,
            }
            if r.get("zone_id"):
                zone_id_ref = _export_text(r["zone_id"])
                rules_dict[rule_id]["zone_id"] = zone_id_ref
                rules_dict[rule_id]["config"].setdefault("zone_id", zone_id_ref)
                rules_dict[rule_id]["config"].setdefault("zone", zone_id_ref)
            if r.get("line_id"):
                line_id_ref = _export_text(r["line_id"])
                rules_dict[rule_id]["line_id"] = line_id_ref
                rules_dict[rule_id]["config"].setdefault("line_id", line_id_ref)
            evidence_policy = r.get("evidence_policy") or {}
            if isinstance(evidence_policy, str):
                import json
                evidence_policy = json.loads(evidence_policy)
            rules_dict[rule_id]["evidence_policy"] = _export_yaml_safe(
                _effective_evidence_policy(cfg, evidence_policy)
            )

        cam_doc: Dict[str, Any] = {
            "enabled": bool(cam.get("enabled", True)),
            "source_id": _export_text(cam["source_id"]),
            "name": _export_text(cam["name"]),
            "rtsp_url": _export_text(cam["rtsp_url"]),
            "gpu_id": int(cam.get("gpu_id", 0)),
            "input_type": _export_text(cam.get("input_type", "rtsp")),
            "rtsp_transport": _export_text(cam.get("rtsp_transport", "tcp")),
        }
        fps_policy = cam.get("fps_policy") or {}
        if isinstance(fps_policy, str):
            import json
            fps_policy = json.loads(fps_policy)
        if fps_policy:
            cam_doc["fps_policy"] = _export_yaml_safe(fps_policy)
        alert_policy = cam.get("alert_policy") or {}
        if isinstance(alert_policy, str):
            import json
            alert_policy = json.loads(alert_policy)
        if alert_policy:
            cam_doc["alert_policy"] = _export_yaml_safe(alert_policy)
        if cam.get("location"):
            cam_doc["location"] = _export_text(cam["location"])
        if cam.get("site_id"):
            cam_doc["site_id"] = _export_text(cam["site_id"])
        if zones_dict:
            cam_doc["zones"] = zones_dict
        if rules_dict:
            cam_doc["rules"] = rules_dict

        root["cameras"][cam_id] = cam_doc

    return root
