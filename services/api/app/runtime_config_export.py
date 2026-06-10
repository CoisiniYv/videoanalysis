"""Midterm DB-backed camera runtime config export.

This module converts rows from ``cameras`` / ``camera_zones`` /
``camera_rules`` into generated runtime config artifacts. It is deliberately
side-effect free until validation has completed, so failed exports do not leave
partial final files behind.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from app.algorithm_ids import (
    BEHAVIOR_ALGORITHM_IDS,
    FACE_RULE_ALGORITHM_IDS,
    RULE_ALGORITHM_IDS,
    behavior_rule_type_for_algorithm_id,
    normalize_algorithm_id,
)

SCHEMA_VERSION_RUNTIME = "midterm.runtime_config.v1"
SCHEMA_VERSION_SUMMARY = "midterm.export_summary.v1"
SCHEMA_VERSION_APPLY_PLAN = "midterm.apply_plan.v1"
DEFAULT_OUTPUT_DIR = "/data/video-analytics/artifacts/midterm/generated-config"

ALLOWED_ALGORITHM_IDS = set(RULE_ALGORITHM_IDS)

ALLOWED_ZONE_TYPES = {"polygon", "line", "direction_line"}
DEFAULT_EVIDENCE_POLICY = {
    "snapshot_required": True,
    "clip_required": True,
    "pre_seconds": 5,
    "post_seconds": 5,
}


class ExportValidationError(ValueError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__(json.dumps({"validation_errors": errors}, ensure_ascii=False))


@dataclass(frozen=True)
class ExportOptions:
    camera_id: str | None = None
    all_enabled: bool = False
    include_disabled: bool = False
    output_dir: Path = Path(DEFAULT_OUTPUT_DIR)
    redact_secrets: bool = True
    dry_run: bool = False


@dataclass(frozen=True)
class ExportResult:
    cameras_generated_yml: Path
    algorithm_runtime_config_json: Path
    export_summary_json: Path
    apply_plan_json: Path
    cameras_generated_doc: dict[str, Any]
    algorithm_runtime_config: dict[str, Any]
    export_summary: dict[str, Any]
    apply_plan: dict[str, Any]
    dry_run: bool = False

    def paths_dict(self) -> dict[str, str]:
        return {
            "cameras_generated_yml": str(self.cameras_generated_yml),
            "algorithm_runtime_config_json": str(self.algorithm_runtime_config_json),
            "export_summary_json": str(self.export_summary_json),
            "apply_plan_json": str(self.apply_plan_json),
        }


def classify_algorithm_rule(algorithm_id: str) -> str:
    algorithm_id = normalize_algorithm_id(algorithm_id)
    if algorithm_id == "face.observation":
        return "observation"
    if algorithm_id == "face.watchlist":
        return "alert"
    if algorithm_id == "face.live_search":
        return "alert_config"
    if algorithm_id in BEHAVIOR_ALGORITHM_IDS:
        return "alert"
    raise ValueError(f"unknown algorithm_id: {algorithm_id}")


def redact_rtsp_url(value: str) -> str:
    if not value:
        return value
    parts = urlsplit(value)
    if parts.scheme.lower() != "rtsp" or "@" not in parts.netloc:
        return value
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, f"***:***@{host}", parts.path, parts.query, parts.fragment))


def export_runtime_config(repo: Any, options: ExportOptions) -> ExportResult:
    generated_at = datetime.now(timezone.utc).isoformat()
    output_dir = Path(options.output_dir)
    paths = {
        "cameras": output_dir / "cameras.midterm.yml",
        "runtime": output_dir / "algorithm_runtime_config.json",
        "summary": output_dir / "export_summary.json",
        "apply_plan": output_dir / "apply_plan.json",
    }

    cameras = _load_cameras(repo, options)
    camera_ids = [row["id"] for row in cameras]
    zones_by_camera = repo.list_zones_for_cameras(camera_ids) if camera_ids else {}
    rules_by_camera = repo.list_rules_for_cameras(camera_ids) if camera_ids else {}

    normalized = _normalize_and_validate(
        cameras,
        zones_by_camera,
        rules_by_camera,
        include_disabled=options.include_disabled,
    )

    cameras_doc = _build_cameras_yml_doc(normalized)
    runtime_config = _build_algorithm_runtime_config(normalized, generated_at)
    summary = _build_summary(
        normalized,
        generated_at,
        output_dir,
        paths,
        include_disabled=options.include_disabled,
        redact_secrets=options.redact_secrets,
    )
    apply_plan = _build_apply_plan(generated_at, paths, output_dir)

    result = ExportResult(
        cameras_generated_yml=paths["cameras"],
        algorithm_runtime_config_json=paths["runtime"],
        export_summary_json=paths["summary"],
        apply_plan_json=paths["apply_plan"],
        cameras_generated_doc=cameras_doc,
        algorithm_runtime_config=runtime_config,
        export_summary=summary,
        apply_plan=apply_plan,
        dry_run=options.dry_run,
    )

    if not options.dry_run:
        _write_result(result)
    return result


def _load_cameras(repo: Any, options: ExportOptions) -> list[dict[str, Any]]:
    if options.camera_id:
        row = repo.get_camera(options.camera_id)
        if row is None:
            raise ExportValidationError(
                [{"path": "camera_id", "message": f"camera not found: {options.camera_id}"}]
            )
        if not options.include_disabled and not bool(row.get("enabled", True)):
            return []
        return [dict(row)]

    if not options.all_enabled:
        raise ExportValidationError(
            [{"path": "target", "message": "--camera-id or --all-enabled is required"}]
        )
    enabled_filter = None if options.include_disabled else True
    return [dict(row) for row in repo.list_cameras(enabled=enabled_filter)]


def _normalize_and_validate(
    cameras: list[dict[str, Any]],
    zones_by_camera: dict[str, list[dict[str, Any]]],
    rules_by_camera: dict[str, list[dict[str, Any]]],
    *,
    include_disabled: bool,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []

    for camera in cameras:
        cam_id = str(camera.get("id") or "")
        cam_path = f"cameras.{cam_id or '<missing>'}"
        _validate_camera(camera, cam_path, errors)

        zones = []
        for zone in zones_by_camera.get(cam_id, []):
            if not include_disabled and not bool(zone.get("enabled", True)):
                continue
            normalized_zone = _normalize_zone(zone)
            _validate_zone(normalized_zone, f"{cam_path}.zones.{normalized_zone.get('zone_id', '<missing>')}", errors)
            zones.append(normalized_zone)

        zone_ids = {zone["zone_id"] for zone in zones}
        polygon_zone_ids = {
            zone["zone_id"] for zone in zones if zone["zone_type"] == "polygon"
        }
        line_zone_ids = {
            zone["zone_id"]
            for zone in zones
            if zone["zone_type"] in ("line", "direction_line")
        }

        rules = []
        for rule in rules_by_camera.get(cam_id, []):
            if not include_disabled and not bool(rule.get("enabled", True)):
                continue
            normalized_rule = _normalize_rule(rule)
            _validate_rule(
                normalized_rule,
                zone_ids=zone_ids,
                polygon_zone_ids=polygon_zone_ids,
                line_zone_ids=line_zone_ids,
                path=f"{cam_path}.rules.{normalized_rule.get('rule_id', '<missing>')}",
                errors=errors,
            )
            rules.append(normalized_rule)

        normalized.append(
            {
                "camera_id": cam_id,
                "source_id": str(camera.get("source_id") or ""),
                "name": str(camera.get("name") or ""),
                "rtsp_url": str(camera.get("rtsp_url") or ""),
                "site_id": camera.get("site_id"),
                "location": camera.get("location"),
                "gpu_id": int(camera.get("gpu_id") or 0),
                "enabled": bool(camera.get("enabled", True)),
                "input_type": str(camera.get("input_type") or "rtsp"),
                "rtsp_transport": str(camera.get("rtsp_transport") or "tcp"),
                "fps_policy": _json_obj(camera.get("fps_policy")),
                "alert_policy": _normalize_alert_policy(camera.get("alert_policy")),
                "zones": sorted(zones, key=lambda item: item["zone_id"]),
                "rules": sorted(rules, key=lambda item: item["rule_id"]),
            }
        )

    if errors:
        raise ExportValidationError(errors)
    return normalized


def _validate_camera(camera: dict[str, Any], path: str, errors: list[dict[str, Any]]) -> None:
    if not str(camera.get("id") or ""):
        _error(errors, path, "camera_id must be non-empty")
    if not str(camera.get("source_id") or ""):
        _error(errors, path, "source_id must be non-empty")
    if str(camera.get("input_type") or "rtsp") != "rtsp":
        _error(errors, path, "input_type must be rtsp")
    if str(camera.get("rtsp_transport") or "tcp") not in ("tcp", "udp"):
        _error(errors, path, "rtsp_transport must be tcp or udp")
    if not isinstance(camera.get("enabled", True), bool):
        _error(errors, path, "enabled must be boolean")
    _validate_alert_policy(_normalize_alert_policy(camera.get("alert_policy")), f"{path}.alert_policy", errors)


def _normalize_zone(zone: dict[str, Any]) -> dict[str, Any]:
    zone_id = str(zone.get("zone_id") or zone.get("zone_name") or "")
    return {
        "zone_id": zone_id,
        "zone_name": str(zone.get("zone_name") or zone_id),
        "zone_type": str(zone.get("zone_type") or ""),
        "coordinate_space": str(zone.get("coordinate_space") or "pixel"),
        "points": _json_value(zone.get("points")) or [],
        "enabled": bool(zone.get("enabled", True)),
        "payload": _json_obj(zone.get("payload")),
    }


def _validate_zone(zone: dict[str, Any], path: str, errors: list[dict[str, Any]]) -> None:
    if not zone["zone_id"]:
        _error(errors, path, "zone_id must be non-empty")
    if zone["zone_type"] not in ALLOWED_ZONE_TYPES:
        _error(errors, path, f"zone_type must be one of {sorted(ALLOWED_ZONE_TYPES)}")
    points = zone.get("points")
    if not isinstance(points, list):
        _error(errors, path, "points must be a list")
        return
    for idx, point in enumerate(points):
        if (
            not isinstance(point, list)
            or len(point) != 2
            or not all(isinstance(coord, (int, float)) and not isinstance(coord, bool) for coord in point)
        ):
            _error(errors, f"{path}.points[{idx}]", "point must be [x, y] numeric pair")
    if zone["zone_type"] == "polygon" and len(points) < 3:
        _error(errors, path, "polygon must have at least 3 points")
    if zone["zone_type"] in ("line", "direction_line") and len(points) < 2:
        _error(errors, path, f"{zone['zone_type']} must have at least 2 points")


def _normalize_rule(rule: dict[str, Any]) -> dict[str, Any]:
    algorithm_id = normalize_algorithm_id(str(rule.get("algorithm_id") or rule.get("rule_type") or ""))
    rule_id = str(rule.get("rule_id") or f"rule_{algorithm_id.replace('.', '_')}" or rule.get("id") or "")
    config = _json_obj(rule.get("config"))
    return {
        "id": rule.get("id"),
        "rule_id": rule_id,
        "algorithm_id": algorithm_id,
        "rule_type": behavior_rule_type_for_algorithm_id(algorithm_id)
        or str(rule.get("rule_type") or algorithm_id),
        "enabled": bool(rule.get("enabled", True)),
        "config": config,
        "zone_id": str(rule.get("zone_id") or ""),
        "line_id": str(rule.get("line_id") or ""),
        "evidence_policy": _effective_evidence_policy(
            config, _json_obj(rule.get("evidence_policy"))
        ),
    }


def _validate_rule(
    rule: dict[str, Any],
    *,
    zone_ids: set[str],
    polygon_zone_ids: set[str],
    line_zone_ids: set[str],
    path: str,
    errors: list[dict[str, Any]],
) -> None:
    if not rule["rule_id"]:
        _error(errors, path, "rule_id must be non-empty")
    if not isinstance(rule.get("enabled"), bool):
        _error(errors, path, "enabled must be boolean")
    if not isinstance(rule.get("config"), dict):
        _error(errors, path, "config must be a JSON object")

    algorithm_id = rule["algorithm_id"]
    try:
        rule["rule_kind"] = classify_algorithm_rule(algorithm_id)
    except ValueError as exc:
        _error(errors, path, str(exc))
        return

    config = rule["config"]
    if algorithm_id in (
        "behavior.intrusion",
        "behavior.loitering",
        "behavior.crowd_gathering",
        "behavior.running",
        "behavior.chasing",
        "behavior.fall",
    ):
        zone_id = str(rule.get("zone_id") or config.get("zone_id") or config.get("zone") or "")
        if not zone_id:
            _error(errors, path, "config.zone_id is required")
        elif zone_id not in zone_ids:
            _error(errors, path, f"config.zone_id references unknown zone_id: {zone_id}")
        elif zone_id not in polygon_zone_ids:
            _error(errors, path, f"config.zone_id must reference a polygon zone: {zone_id}")
    if algorithm_id == "behavior.wall_climb_suspicious":
        line_id = str(rule.get("line_id") or config.get("line_id") or "")
        if not line_id:
            _error(errors, path, "config.line_id is required")
        elif line_id not in line_zone_ids:
            _error(errors, path, f"config.line_id references unknown line zone: {line_id}")
    if algorithm_id == "face.watchlist":
        threshold = config.get("threshold")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            _error(errors, path, "config.threshold must be numeric")
        elif threshold < 0 or threshold > 1:
            _error(errors, path, "config.threshold must be between 0 and 1")


def _normalize_alert_policy(value: Any) -> dict[str, Any]:
    policy = {
        "global_alert_cooldown_s": 0,
        "store_suppressed_events": True,
        "suppress_record_request": True,
        "critical_bypass": False,
    }
    policy.update(_json_obj(value))
    return policy


def _validate_alert_policy(policy: dict[str, Any], path: str, errors: list[dict[str, Any]]) -> None:
    cooldown = policy.get("global_alert_cooldown_s")
    if not isinstance(cooldown, int) or isinstance(cooldown, bool) or cooldown < 0:
        _error(errors, path, "global_alert_cooldown_s must be an integer >= 0")
    for field in ("store_suppressed_events", "suppress_record_request", "critical_bypass"):
        if not isinstance(policy.get(field), bool):
            _error(errors, path, f"{field} must be boolean")


def _build_cameras_yml_doc(cameras: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"cameras": {}}
    for camera in cameras:
        cam_doc: dict[str, Any] = {
            "camera_id": camera["camera_id"],
            "source_id": camera["source_id"],
            "name": camera["name"],
            "enabled": camera["enabled"],
            "input": {
                "type": camera["input_type"],
                "rtsp_url": camera["rtsp_url"],
                "rtsp_transport": camera["rtsp_transport"],
            },
            "fps_policy": camera["fps_policy"],
            "alert_policy": camera["alert_policy"],
            "zones": {},
            "rules": {},
        }
        if camera.get("site_id"):
            cam_doc["site_id"] = camera["site_id"]
        if camera.get("location"):
            cam_doc["location"] = camera["location"]
        for zone in camera["zones"]:
            cam_doc["zones"][zone["zone_id"]] = dict(zone)
        for rule in camera["rules"]:
            rule_config = dict(rule["config"])
            if rule.get("zone_id"):
                rule_config.setdefault("zone_id", rule["zone_id"])
                rule_config.setdefault("zone", rule["zone_id"])
            if rule.get("line_id"):
                rule_config.setdefault("line_id", rule["line_id"])
            cam_doc["rules"][rule["rule_id"]] = {
                "rule_id": rule["rule_id"],
                "algorithm_id": rule["algorithm_id"],
                "rule_type": rule["rule_type"],
                "enabled": rule["enabled"],
                "rule_kind": rule["rule_kind"],
                "config": rule_config,
            }
            if rule.get("zone_id"):
                cam_doc["rules"][rule["rule_id"]]["zone_id"] = rule["zone_id"]
            if rule.get("line_id"):
                cam_doc["rules"][rule["rule_id"]]["line_id"] = rule["line_id"]
            if rule.get("evidence_policy"):
                cam_doc["rules"][rule["rule_id"]]["evidence_policy"] = rule["evidence_policy"]
        out["cameras"][camera["camera_id"]] = cam_doc
    return out


def _build_algorithm_runtime_config(cameras: list[dict[str, Any]], generated_at: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION_RUNTIME,
        "generated_at": generated_at,
        "source": "postgres",
        "cameras": [
            {
                "camera_id": camera["camera_id"],
                "source_id": camera["source_id"],
                "enabled": camera["enabled"],
                "alert_policy": camera["alert_policy"],
                "zones": camera["zones"],
                "capabilities": _camera_capabilities(camera["rules"]),
                "rules": [
                    {
                        "rule_id": rule["rule_id"],
                        "algorithm_id": rule["algorithm_id"],
                        "rule_type": rule["rule_type"],
                        "rule_kind": rule["rule_kind"],
                        "enabled": rule["enabled"],
                        "config": rule["config"],
                        "zone_id": rule.get("zone_id") or None,
                        "line_id": rule.get("line_id") or None,
                        "evidence_policy": rule.get("evidence_policy") or {},
                    }
                    for rule in camera["rules"]
                ],
            }
            for camera in cameras
        ],
    }


def _build_summary(
    cameras: list[dict[str, Any]],
    generated_at: str,
    output_dir: Path,
    paths: dict[str, Path],
    *,
    include_disabled: bool,
    redact_secrets: bool,
) -> dict[str, Any]:
    def safe_url(url: str) -> str:
        return redact_rtsp_url(url) if redact_secrets else url

    return {
        "schema_version": SCHEMA_VERSION_SUMMARY,
        "generated_at": generated_at,
        "source": "postgres",
        "output_dir": str(output_dir),
        "include_disabled": include_disabled,
        "redact_secrets": redact_secrets,
        "camera_count": len(cameras),
        "files": {name: str(path) for name, path in paths.items()},
        "cameras": [
            {
                "camera_id": camera["camera_id"],
                "source_id": camera["source_id"],
                "enabled": camera["enabled"],
                "rtsp_url": safe_url(camera["rtsp_url"]),
                "zone_count": len(camera["zones"]),
                "rule_count": len(camera["rules"]),
                "rule_kinds": sorted({rule["rule_kind"] for rule in camera["rules"]}),
            }
            for camera in cameras
        ],
        "validation_errors": [],
    }


def _camera_capabilities(rules: list[dict[str, Any]]) -> dict[str, bool]:
    enabled_algorithm_ids = {
        rule["algorithm_id"] for rule in rules if bool(rule.get("enabled", True))
    }
    has_behavior = any(algorithm_id in BEHAVIOR_ALGORITHM_IDS for algorithm_id in enabled_algorithm_ids)
    return {
        "needs_person_bbox": has_behavior,
        "needs_pose_keypoints": bool(
            enabled_algorithm_ids
            & {"behavior.fall", "behavior.wall_climb_suspicious"}
        ),
        "needs_track_velocity": bool(
            enabled_algorithm_ids & {"behavior.running", "behavior.chasing"}
        ),
        "needs_multi_track_state": bool(
            enabled_algorithm_ids & {"behavior.crowd_gathering", "behavior.chasing"}
        ),
        "needs_face_detection": bool(enabled_algorithm_ids & set(FACE_RULE_ALGORITHM_IDS)),
        "needs_face_embedding": bool(
            enabled_algorithm_ids & {"face.watchlist", "face.live_search"}
        ),
        "needs_face_reid": bool(
            enabled_algorithm_ids & {"face.watchlist", "face.live_search"}
        ),
    }


def _build_apply_plan(generated_at: str, paths: dict[str, Path], output_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION_APPLY_PLAN,
        "generated_at": generated_at,
        "output_dir": str(output_dir),
        "actions": [
            {"action": "write_file", "path": str(paths["cameras"])},
            {"action": "write_file", "path": str(paths["runtime"])},
            {"action": "write_file", "path": str(paths["summary"])},
            {"action": "write_file", "path": str(paths["apply_plan"])},
            {
                "action": "future_mount_or_copy",
                "target": "modules/savant_security/config/cameras.midterm.yml",
                "status": "not_executed",
            },
            {
                "action": "future_restart",
                "services": ["source-adapter", "savant-security"],
                "status": "not_executed",
                "reason": "controlled runtime apply deferred to a midterm runtime apply step",
            },
        ],
    }


def _write_result(result: ExportResult) -> None:
    result.cameras_generated_yml.parent.mkdir(parents=True, exist_ok=True)
    cameras_text = yaml.safe_dump(
        result.cameras_generated_doc, sort_keys=False, allow_unicode=True
    )
    runtime_text = json.dumps(result.algorithm_runtime_config, indent=2, ensure_ascii=False) + "\n"
    summary_text = json.dumps(result.export_summary, indent=2, ensure_ascii=False) + "\n"
    apply_plan_text = json.dumps(result.apply_plan, indent=2, ensure_ascii=False) + "\n"

    for path, text in (
        (result.cameras_generated_yml, cameras_text),
        (result.algorithm_runtime_config_json, runtime_text),
        (result.export_summary_json, summary_text),
        (result.apply_plan_json, apply_plan_text),
    ):
        _atomic_write_text(path, text)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _json_obj(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(value, dict):
        return dict(value)
    return dict(value)


def _effective_evidence_policy(
    config: dict[str, Any], evidence_policy: dict[str, Any]
) -> dict[str, Any]:
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


def _error(errors: list[dict[str, Any]], path: str, message: str) -> None:
    errors.append({"path": path, "message": message})


def contains_secret(value: str) -> bool:
    return bool(re.search(r"rtsp://[^/\s:@]+:[^@\s/]+@", value))
