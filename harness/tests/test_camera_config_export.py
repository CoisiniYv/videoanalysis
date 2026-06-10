"""Tests for camera config camera config GET and YAML export endpoints."""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from fastapi.testclient import TestClient

from app.main import app
from app.routers.cameras import _repo as cameras_repo_dep
from app.schemas.cameras import build_export_doc

# Reuse the FakeCameraRepository from the sibling test module
SIBLING_DIR = str(Path(__file__).resolve().parent)
if SIBLING_DIR not in sys.path:
    sys.path.insert(0, SIBLING_DIR)
from test_api_cameras import FakeCameraRepository  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> FakeCameraRepository:
    return FakeCameraRepository()


@pytest.fixture
def client(repo):
    app.dependency_overrides.clear()

    def _override():
        yield repo

    app.dependency_overrides[cameras_repo_dep] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _seed_cam_001(client):
    """Create cam_001 + perimeter zone + intrusion rule via the API."""
    cam_resp = client.post("/api/v1/cameras", json={
        "id": "cam_001",
        "source_id": "primary_rtsp",
        "name": "Test Camera",
        "location": "Test Area",
        "rtsp_url": "rtsp://example.local/stream",
        "site_id": "site_a",
        "gpu_id": 0,
        "enabled": True,
    })
    assert cam_resp.status_code == 200, cam_resp.json()
    zone_resp = client.post("/api/v1/cameras/cam_001/zones", json={
        "zone_name": "perimeter",
        "zone_type": "polygon",
        "points": [[100, 300], [900, 300], [900, 700], [100, 700]],
    })
    assert zone_resp.status_code == 200, zone_resp.json()
    rule_resp = client.post("/api/v1/cameras/cam_001/rules", json={
        "rule_type": "intrusion",
        "enabled": True,
        "config": {
            "zone": "perimeter",
            "min_inside_ms": 1000,
            "cooldown_s": 30,
            "severity": "medium",
            "snapshot_required": True,
            "clip_required": True,
        },
    })
    assert rule_resp.status_code == 200, rule_resp.json()


def _seed_disabled_cam(client, cam_id="cam_off", source_id="off_source"):
    resp = client.post("/api/v1/cameras", json={
        "id": cam_id,
        "source_id": source_id,
        "name": "Disabled Camera",
        "rtsp_url": "rtsp://example.local/off",
        "gpu_id": 0,
        "enabled": False,
    })
    assert resp.status_code == 200, resp.json()


# ===========================================================================
# 1. Aggregate GET /api/v1/cameras/{id}/config
# ===========================================================================


def test_camera_config_returns_full_doc(client):
    _seed_cam_001(client)
    resp = client.get("/api/v1/cameras/cam_001/config")
    assert resp.status_code == 200, resp.json()
    data = resp.json()["data"]
    assert data["camera"]["id"] == "cam_001"
    assert len(data["zones"]) == 1
    assert data["zones"][0]["zone_name"] == "perimeter"
    assert len(data["rules"]) == 1
    assert data["rules"][0]["rule_type"] == "intrusion"


# ===========================================================================
# 2. GET /api/v1/cameras/config/export returns YAML
# ===========================================================================


def test_export_returns_yaml_content_type(client):
    _seed_cam_001(client)
    resp = client.get("/api/v1/cameras/config/export")
    assert resp.status_code == 200
    ctype = resp.headers["content-type"]
    assert ctype.startswith("text/yaml") or ctype.startswith("application/x-yaml")


# ===========================================================================
# 3. yaml.safe_load parses the response body
# ===========================================================================


def test_export_is_parseable_yaml(client):
    _seed_cam_001(client)
    resp = client.get("/api/v1/cameras/config/export")
    doc = yaml.safe_load(resp.text)
    assert isinstance(doc, dict)
    assert "cameras" in doc


# ===========================================================================
# 4. Export structure matches cameras.yml convention
# ===========================================================================


def test_export_cam_001_shape(client):
    _seed_cam_001(client)
    resp = client.get("/api/v1/cameras/config/export")
    doc = yaml.safe_load(resp.text)

    assert list(doc["cameras"].keys()) == ["cam_001"]
    cam = doc["cameras"]["cam_001"]

    assert cam["enabled"] is True
    assert cam["source_id"] == "primary_rtsp"
    assert cam["name"] == "Test Camera"
    assert cam["rtsp_url"] == "rtsp://example.local/stream"
    assert cam["gpu_id"] == 0
    assert cam["location"] == "Test Area"

    zones = cam["zones"]
    assert "perimeter" in zones
    perim = zones["perimeter"]
    assert perim["type"] == "polygon"
    assert perim["points"] == [[100, 300], [900, 300], [900, 700], [100, 700]]

    rules = cam["rules"]
    assert "rule_behavior_intrusion" in rules
    intr = rules["rule_behavior_intrusion"]
    assert intr["rule_id"] == "rule_behavior_intrusion"
    assert intr["algorithm_id"] == "behavior.intrusion"
    assert intr["rule_type"] == "intrusion"
    assert intr["enabled"] is True
    assert intr["config"]["zone"] == "perimeter"
    assert intr["config"]["min_inside_ms"] == 1000
    assert intr["config"]["cooldown_s"] == 30
    assert intr["config"]["severity"] == "medium"
    assert intr["config"]["snapshot_required"] is True
    assert intr["config"]["clip_required"] is True
    assert intr["evidence_policy"] == {
        "snapshot_required": True,
        "clip_required": True,
        "pre_seconds": 5,
        "post_seconds": 5,
    }


# ===========================================================================
# 5. disabled cameras default excluded
# ===========================================================================


def test_export_excludes_disabled_by_default(client):
    _seed_cam_001(client)
    _seed_disabled_cam(client)
    resp = client.get("/api/v1/cameras/config/export")
    doc = yaml.safe_load(resp.text)
    assert set(doc["cameras"].keys()) == {"cam_001"}


# ===========================================================================
# 6. include_disabled=true keeps enabled=false entries
# ===========================================================================


def test_export_include_disabled_keeps_enabled_false(client):
    _seed_cam_001(client)
    _seed_disabled_cam(client)
    resp = client.get("/api/v1/cameras/config/export?include_disabled=true")
    doc = yaml.safe_load(resp.text)
    assert set(doc["cameras"].keys()) == {"cam_001", "cam_off"}
    assert doc["cameras"]["cam_off"]["enabled"] is False
    assert doc["cameras"]["cam_001"]["enabled"] is True


# ===========================================================================
# 7. export omits database internal fields
# ===========================================================================


def test_export_omits_internal_fields(client):
    _seed_cam_001(client)
    resp = client.get("/api/v1/cameras/config/export")
    text = resp.text
    for forbidden in ("created_at", "updated_at"):
        assert forbidden not in text, f"export must not include {forbidden}"
    doc = yaml.safe_load(text)
    cam = doc["cameras"]["cam_001"]
    # No DB row id, no auto-increment zone/rule ids
    assert "id" not in cam
    for zone_body in cam["zones"].values():
        assert "id" not in zone_body
        assert "camera_id" not in zone_body
    for rule_body in cam["rules"].values():
        assert "id" not in rule_body
        assert "camera_id" not in rule_body


# ===========================================================================
# 8. empty export still yields parseable YAML
# ===========================================================================


def test_export_with_no_cameras_is_valid_yaml(client):
    resp = client.get("/api/v1/cameras/config/export")
    doc = yaml.safe_load(resp.text)
    assert doc == {"cameras": {}}


def test_export_doc_serializes_uuid_backed_database_rows() -> None:
    camera_id = uuid.UUID("00000000-0000-4000-8000-000000000901")
    zone_id = uuid.UUID("00000000-0000-4000-8000-000000000902")

    doc = build_export_doc(
        cameras=[
            {
                "id": camera_id,
                "source_id": "uuid_camera_source",
                "name": "UUID Camera",
                "rtsp_url": "rtsp://example.local/uuid-camera",
                "gpu_id": 0,
                "enabled": True,
                "input_type": "rtsp",
                "rtsp_transport": "tcp",
                "fps_policy": {"max_fps": "8/1"},
                "alert_policy": {"global_alert_cooldown_s": 30},
                "site_id": "uuid_site",
                "location": "uuid_location",
            }
        ],
        zones_by_camera={
            camera_id: [
                {
                    "id": zone_id,
                    "camera_id": camera_id,
                    "zone_id": zone_id,
                    "zone_name": "full_frame",
                    "zone_type": "polygon",
                    "coordinate_space": "pixel",
                    "points": [[0, 0], [1920, 0], [1920, 1080], [0, 1080]],
                    "enabled": True,
                    "payload": {"camera_ref": camera_id},
                }
            ]
        },
        rules_by_camera={
            camera_id: [
                {
                    "id": uuid.UUID("00000000-0000-4000-8000-000000000903"),
                    "camera_id": camera_id,
                    "rule_id": "uuid_intrusion",
                    "algorithm_id": "behavior.intrusion",
                    "rule_type": "intrusion",
                    "enabled": True,
                    "zone_id": zone_id,
                    "line_id": None,
                    "config": {"min_inside_ms": 1000},
                    "evidence_policy": {"camera_ref": camera_id},
                }
            ]
        },
    )

    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    parsed = yaml.safe_load(text)
    cam = parsed["cameras"][str(camera_id)]
    zone = cam["zones"][str(zone_id)]
    rule = cam["rules"]["uuid_intrusion"]

    assert zone["zone_id"] == str(zone_id)
    assert zone["payload"]["camera_ref"] == str(camera_id)
    assert rule["zone_id"] == str(zone_id)
    assert rule["config"]["zone"] == str(zone_id)
    assert rule["evidence_policy"]["camera_ref"] == str(camera_id)
