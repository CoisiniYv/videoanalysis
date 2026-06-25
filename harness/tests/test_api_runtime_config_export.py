"""Tests for DB-backed API runtime config export."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.routers.cameras import cameras_runtime_config_preview
from app.runtime_config_export import ExportOptions, export_runtime_config


class _Repo:
    def get_camera(self, camera_id: str) -> dict[str, Any] | None:
        return next((row for row in self.list_cameras() if row["id"] == camera_id), None)

    def list_cameras(self, *, enabled=None) -> list[dict[str, Any]]:
        row = {
            "id": "cam_001",
            "source_id": "primary_rtsp",
            "name": "Primary",
            "rtsp_url": "rtsp://example.local/stream",
            "gpu_id": 0,
            "enabled": True,
            "input_type": "rtsp",
            "rtsp_transport": "tcp",
            "fps_policy": {},
            "alert_policy": {},
        }
        return [row] if enabled in (None, True) else []

    def list_zones_for_cameras(self, camera_ids):
        return {
            "cam_001": [
                {
                    "zone_id": "full_frame",
                    "zone_name": "full_frame",
                    "zone_type": "polygon",
                    "coordinate_space": "pixel",
                    "points": [[0, 0], [1, 0], [1, 1], [0, 1]],
                    "enabled": True,
                    "payload": {},
                }
            ]
        }

    def list_rules_for_cameras(self, camera_ids):
        return {
            "cam_001": [
                {
                    "id": 1,
                    "rule_id": "intrusion_full_frame",
                    "algorithm_id": "behavior.intrusion",
                    "rule_type": "intrusion",
                    "enabled": True,
                    "zone_id": "full_frame",
                    "line_id": None,
                    "config": {
                        "zone": "full_frame",
                        "min_inside_ms": 1000,
                        "cooldown_s": 30,
                        "snapshot_required": False,
                        "clip_required": True,
                    },
                    "evidence_policy": {},
                }
            ]
        }


def test_runtime_export_fills_effective_evidence_policy(tmp_path: Path) -> None:
    result = export_runtime_config(
        _Repo(),
        ExportOptions(all_enabled=True, output_dir=tmp_path, dry_run=True),
    )

    generated_rule = result.cameras_generated_doc["cameras"]["cam_001"]["rules"][
        "intrusion_full_frame"
    ]
    runtime_rule = result.algorithm_runtime_config["cameras"][0]["rules"][0]
    expected = {
        "snapshot_required": False,
        "clip_required": True,
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    assert generated_rule["evidence_policy"] == expected
    assert runtime_rule["evidence_policy"] == expected
    assert runtime_rule["support_status"] == "production_ready"
    assert runtime_rule["runtime_apply_state"] == "applied"
    assert runtime_rule["runtime_consumed"] is True
    assert runtime_rule["runtime_skip_reason"] == ""


def test_camera_runtime_config_preview_endpoint_returns_selected_camera() -> None:
    body = cameras_runtime_config_preview(
        "cam_001",
        repo=_Repo(),
        request_id="req-1",
    )

    data = body["data"]
    assert data["camera_id"] == "cam_001"
    assert data["cameras_midterm_yml"]["source_id"] == "primary_rtsp"
    runtime_rule = data["algorithm_runtime_config"]["rules"][0]
    assert runtime_rule["rule_id"] == "intrusion_full_frame"
    assert runtime_rule["support_status"] == "production_ready"
    assert runtime_rule["runtime_apply_state"] == "applied"
