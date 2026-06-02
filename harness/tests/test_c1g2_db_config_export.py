"""C1G.2 DB config export contract tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR in sys.path:
    sys.path.remove(API_DIR)
sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.runtime_config_export import (
    ExportOptions,
    ExportValidationError,
    classify_algorithm_rule,
    export_runtime_config,
)


class FakeCameraRepository:
    def __init__(
        self,
        *,
        cameras: list[dict[str, Any]] | None = None,
        zones: list[dict[str, Any]] | None = None,
        rules: list[dict[str, Any]] | None = None,
    ) -> None:
        self.cameras = cameras if cameras is not None else [_camera()]
        self.zones = zones if zones is not None else [_zone()]
        self.rules = rules if rules is not None else _rules()

    def get_camera(self, camera_id: str) -> dict[str, Any] | None:
        return next((cam for cam in self.cameras if cam["id"] == camera_id), None)

    def list_cameras(self, *, enabled: bool | None = None) -> list[dict[str, Any]]:
        rows = list(self.cameras)
        if enabled is not None:
            rows = [row for row in rows if row.get("enabled", True) is enabled]
        return rows

    def list_zones_for_cameras(self, camera_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return {
            camera_id: [zone for zone in self.zones if zone["camera_id"] == camera_id]
            for camera_id in camera_ids
        }

    def list_rules_for_cameras(self, camera_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return {
            camera_id: [rule for rule in self.rules if rule["camera_id"] == camera_id]
            for camera_id in camera_ids
        }


def _camera(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "cam_c1e_rtsp_replay",
        "name": "C1E RTSP Replay Camera",
        "source_id": "c1e_rtsp_replay",
        "rtsp_url": "rtsp://user:secret@10.37.57.112:8554/live/1080movie",
        "enabled": True,
        "input_type": "rtsp",
        "rtsp_transport": "tcp",
        "fps_policy": {"max_fps": "8/1", "min_fps": "2/1"},
        "alert_policy": {
            "global_alert_cooldown_s": 30,
            "store_suppressed_events": True,
            "suppress_record_request": True,
            "critical_bypass": False,
        },
    }
    row.update(overrides)
    return row


def _zone(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 1,
        "camera_id": "cam_c1e_rtsp_replay",
        "zone_id": "perimeter",
        "zone_name": "周界区域",
        "zone_type": "polygon",
        "coordinate_space": "pixel",
        "points": [[100, 300], [900, 300], [900, 700], [100, 700]],
        "enabled": True,
        "payload": {},
    }
    row.update(overrides)
    return row


def _rules() -> list[dict[str, Any]]:
    return [
        {
            "id": 1,
            "camera_id": "cam_c1e_rtsp_replay",
            "rule_id": "rule_face_observation",
            "algorithm_id": "face.observation",
            "rule_type": "face.observation",
            "enabled": True,
            "config": {
                "min_face_confidence": 0.6,
                "min_face_size": 40,
                "min_quality": 0.6,
                "reid_min_interval_ms": 1000,
            },
        },
        {
            "id": 2,
            "camera_id": "cam_c1e_rtsp_replay",
            "rule_id": "rule_watchlist",
            "algorithm_id": "face.watchlist",
            "rule_type": "face.watchlist",
            "enabled": True,
            "config": {"threshold": 0.75, "cooldown_s": 60},
        },
        {
            "id": 3,
            "camera_id": "cam_c1e_rtsp_replay",
            "rule_id": "rule_intrusion",
            "algorithm_id": "behavior.intrusion",
            "rule_type": "behavior.intrusion",
            "enabled": True,
            "config": {"zone_id": "perimeter", "min_inside_ms": 1000, "cooldown_s": 30},
        },
        {
            "id": 4,
            "camera_id": "cam_c1e_rtsp_replay",
            "rule_id": "rule_disabled_watchlist",
            "algorithm_id": "face.watchlist",
            "rule_type": "face.watchlist",
            "enabled": False,
            "config": {"threshold": 0.8, "cooldown_s": 60},
        },
    ]


def _export(tmp_path: Path, repo: FakeCameraRepository, *, include_disabled: bool = False):
    return export_runtime_config(
        repo,
        ExportOptions(
            camera_id="cam_c1e_rtsp_replay",
            include_disabled=include_disabled,
            output_dir=tmp_path,
        ),
    )


def test_classify_algorithm_rule():
    assert classify_algorithm_rule("face.observation") == "observation"
    assert classify_algorithm_rule("face.watchlist") == "alert"
    assert classify_algorithm_rule("face.live_search") == "alert_config"
    assert classify_algorithm_rule("behavior.intrusion") == "alert"
    with pytest.raises(ValueError):
        classify_algorithm_rule("unknown")


def test_enabled_camera_zones_rules_export_yaml(tmp_path: Path):
    result = _export(tmp_path, FakeCameraRepository())
    doc = yaml.safe_load(result.cameras_generated_yml.read_text())
    cam = doc["cameras"]["cam_c1e_rtsp_replay"]
    assert cam["camera_id"] == "cam_c1e_rtsp_replay"
    assert cam["source_id"] == "c1e_rtsp_replay"
    assert cam["input"]["rtsp_transport"] == "tcp"
    assert cam["zones"]["perimeter"]["zone_type"] == "polygon"
    assert set(cam["rules"]) == {
        "rule_face_observation",
        "rule_watchlist",
        "rule_intrusion",
    }


def test_face_rule_kinds_and_intrusion_zone_success(tmp_path: Path):
    result = _export(tmp_path, FakeCameraRepository())
    cam = result.algorithm_runtime_config["cameras"][0]
    rules = {rule["algorithm_id"]: rule for rule in cam["rules"]}
    assert rules["face.observation"]["rule_kind"] == "observation"
    assert rules["face.watchlist"]["rule_kind"] == "alert"
    assert rules["behavior.intrusion"]["config"]["zone_id"] == "perimeter"


def test_intrusion_missing_zone_fails(tmp_path: Path):
    rules = _rules()
    rules[2] = {**rules[2], "config": {"zone_id": "missing", "min_inside_ms": 1000}}
    with pytest.raises(ExportValidationError) as exc:
        _export(tmp_path, FakeCameraRepository(rules=rules))
    assert "unknown zone_id" in json.dumps(exc.value.errors)


def test_unknown_algorithm_fails(tmp_path: Path):
    rules = _rules()
    rules[1] = {**rules[1], "algorithm_id": "face.everyone_alert"}
    with pytest.raises(ExportValidationError) as exc:
        _export(tmp_path, FakeCameraRepository(rules=rules))
    assert "unknown algorithm_id" in json.dumps(exc.value.errors)


def test_invalid_polygon_fails(tmp_path: Path):
    zones = [_zone(points=[[0, 0], [1, 1]])]
    with pytest.raises(ExportValidationError) as exc:
        _export(tmp_path, FakeCameraRepository(zones=zones))
    assert "polygon must have at least 3 points" in json.dumps(exc.value.errors)


def test_alert_policy_cooldown_exported(tmp_path: Path):
    result = _export(tmp_path, FakeCameraRepository())
    cam = result.algorithm_runtime_config["cameras"][0]
    assert cam["alert_policy"]["global_alert_cooldown_s"] == 30


def test_disabled_rule_filtered_by_default_and_included_with_flag(tmp_path: Path):
    default_result = _export(tmp_path / "default", FakeCameraRepository())
    default_rules = default_result.cameras_generated_doc["cameras"]["cam_c1e_rtsp_replay"]["rules"]
    assert "rule_disabled_watchlist" not in default_rules

    include_result = _export(
        tmp_path / "include", FakeCameraRepository(), include_disabled=True
    )
    include_rules = include_result.cameras_generated_doc["cameras"]["cam_c1e_rtsp_replay"]["rules"]
    assert "rule_disabled_watchlist" in include_rules
    assert include_rules["rule_disabled_watchlist"]["enabled"] is False


def test_summary_redacts_rtsp_password_but_runtime_yaml_keeps_it(tmp_path: Path):
    result = _export(tmp_path, FakeCameraRepository())
    summary_text = result.export_summary_json.read_text()
    yaml_text = result.cameras_generated_yml.read_text()
    assert "rtsp://***:***@10.37.57.112:8554/live/1080movie" in summary_text
    assert "user:secret" not in summary_text
    assert "rtsp://user:secret@10.37.57.112:8554/live/1080movie" in yaml_text


def test_validation_failed_does_not_write_final_outputs(tmp_path: Path):
    rules = _rules()
    rules[1] = {**rules[1], "algorithm_id": "bad.algorithm"}
    with pytest.raises(ExportValidationError):
        _export(tmp_path, FakeCameraRepository(rules=rules))
    assert not (tmp_path / "cameras.generated.yml").exists()
    assert not (tmp_path / "algorithm_runtime_config.json").exists()
    assert not (tmp_path / "export_summary.json").exists()
    assert not (tmp_path / "apply_plan.json").exists()


def test_apply_plan_marks_restart_future_not_executed(tmp_path: Path):
    result = _export(tmp_path, FakeCameraRepository())
    actions = result.apply_plan["actions"]
    future_restart = next(action for action in actions if action["action"] == "future_restart")
    assert future_restart["services"] == ["source-adapter", "savant-security"]
    assert future_restart["status"] == "not_executed"
    assert "C1G.3" in future_restart["reason"]
