"""C1G.1 camera / ROI / algorithm configuration API contract tests."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from fastapi.testclient import TestClient

from app.main import app
from app.routers.cameras import _repo as cameras_repo_dep


NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


class FakeCameraRepository:
    def __init__(self) -> None:
        self.cameras: dict[str, dict[str, Any]] = {}
        self.zones: list[dict[str, Any]] = []
        self.rules: list[dict[str, Any]] = []
        self._zone_pk = 0
        self._rule_pk = 0

    def create_camera(self, **kwargs: Any) -> dict[str, Any]:
        row = {
            "id": kwargs["camera_id"],
            "name": kwargs["name"],
            "source_id": kwargs["source_id"],
            "rtsp_url": kwargs["rtsp_url"],
            "site_id": kwargs.get("site_id"),
            "location": kwargs.get("location"),
            "gpu_id": kwargs.get("gpu_id", 0),
            "enabled": kwargs.get("enabled", True),
            "input_type": kwargs.get("input_type", "rtsp"),
            "rtsp_transport": kwargs.get("rtsp_transport", "tcp"),
            "fps_policy": kwargs.get("fps_policy") or {},
            "alert_policy": kwargs.get("alert_policy") or {},
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.cameras[row["id"]] = row
        return row

    def update_camera(self, camera_id: str, **kwargs: Any) -> dict[str, Any] | None:
        row = self.cameras.get(camera_id)
        if row is None:
            return None
        for key, value in kwargs.items():
            if value is not None:
                row[key] = value
        row["updated_at"] = NOW
        return row

    def set_camera_enabled(self, camera_id: str, enabled: bool) -> dict[str, Any] | None:
        row = self.cameras.get(camera_id)
        if row is None:
            return None
        row["enabled"] = enabled
        return row

    def set_alert_policy(self, camera_id: str, alert_policy: dict[str, Any]) -> dict[str, Any] | None:
        row = self.cameras.get(camera_id)
        if row is None:
            return None
        row["alert_policy"] = alert_policy
        return row

    def get_camera(self, camera_id: str) -> dict[str, Any] | None:
        return self.cameras.get(camera_id)

    def list_cameras(self, *, enabled: bool | None = None) -> list[dict[str, Any]]:
        rows = list(self.cameras.values())
        if enabled is not None:
            rows = [row for row in rows if row["enabled"] is enabled]
        return sorted(rows, key=lambda row: row["id"])

    def create_zone(self, **kwargs: Any) -> dict[str, Any]:
        self._zone_pk += 1
        row = {
            "id": self._zone_pk,
            "camera_id": kwargs["camera_id"],
            "zone_id": kwargs.get("zone_id") or kwargs["zone_name"],
            "zone_name": kwargs["zone_name"],
            "zone_type": kwargs["zone_type"],
            "coordinate_space": kwargs.get("coordinate_space", "pixel"),
            "points": kwargs["points"],
            "enabled": kwargs.get("enabled", True),
            "payload": kwargs.get("payload") or {},
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.zones.append(row)
        return row

    def get_zone(self, camera_id: str, zone_id: str) -> dict[str, Any] | None:
        return next(
            (
                zone
                for zone in self.zones
                if zone["camera_id"] == camera_id
                and zone["zone_id"] in (zone_id, zone.get("zone_name"))
            ),
            None,
        )

    def update_zone(self, **kwargs: Any) -> dict[str, Any] | None:
        row = self.get_zone(kwargs["camera_id"], kwargs["zone_id"])
        if row is None:
            return None
        row.update(
            {
                "zone_id": kwargs.get("new_zone_id") or row["zone_id"],
                "zone_name": kwargs.get("zone_name") or row["zone_name"],
                "zone_type": kwargs.get("zone_type") or row["zone_type"],
                "coordinate_space": kwargs.get("coordinate_space") or row["coordinate_space"],
                "points": kwargs.get("points") or row["points"],
                "enabled": row["enabled"] if kwargs.get("enabled") is None else kwargs["enabled"],
                "payload": kwargs.get("payload") if kwargs.get("payload") is not None else row["payload"],
                "updated_at": NOW,
            }
        )
        return row

    def delete_zone(self, camera_id: str, zone_id: str) -> bool:
        before = len(self.zones)
        self.zones = [
            zone
            for zone in self.zones
            if not (zone["camera_id"] == camera_id and zone["zone_id"] == zone_id)
        ]
        return len(self.zones) < before

    def list_zones(self, camera_id: str) -> list[dict[str, Any]]:
        return [zone for zone in self.zones if zone["camera_id"] == camera_id]

    def get_zone_names(self, camera_id: str) -> list[str]:
        return [zone["zone_name"] for zone in self.list_zones(camera_id)]

    def list_zones_for_cameras(self, camera_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return {camera_id: self.list_zones(camera_id) for camera_id in camera_ids}

    def create_rule(self, **kwargs: Any) -> dict[str, Any]:
        self._rule_pk += 1
        row = {
            "id": self._rule_pk,
            "camera_id": kwargs["camera_id"],
            "rule_id": kwargs.get("rule_id") or kwargs["rule_type"],
            "algorithm_id": kwargs.get("algorithm_id") or kwargs["rule_type"],
            "rule_type": kwargs["rule_type"],
            "enabled": kwargs.get("enabled", True),
            "config": kwargs.get("config") or {},
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.rules.append(row)
        return row

    def get_rule(self, camera_id: str, rule_id: str) -> dict[str, Any] | None:
        return next(
            (
                rule
                for rule in self.rules
                if rule["camera_id"] == camera_id
                and rule["rule_id"] in (rule_id, str(rule["id"]))
            ),
            None,
        )

    def update_rule(self, **kwargs: Any) -> dict[str, Any] | None:
        row = self.get_rule(kwargs["camera_id"], kwargs["rule_id"])
        if row is None:
            return None
        if kwargs.get("new_rule_id"):
            row["rule_id"] = kwargs["new_rule_id"]
        if kwargs.get("algorithm_id"):
            row["algorithm_id"] = kwargs["algorithm_id"]
        if kwargs.get("rule_type"):
            row["rule_type"] = kwargs["rule_type"]
        if kwargs.get("enabled") is not None:
            row["enabled"] = kwargs["enabled"]
        if kwargs.get("config") is not None:
            row["config"] = kwargs["config"]
        row["updated_at"] = NOW
        return row

    def delete_rule(self, camera_id: str, rule_id: str) -> bool:
        before = len(self.rules)
        self.rules = [
            rule
            for rule in self.rules
            if not (rule["camera_id"] == camera_id and rule["rule_id"] == rule_id)
        ]
        return len(self.rules) < before

    def set_rule_enabled(self, camera_id: str, rule_id: str, enabled: bool) -> dict[str, Any] | None:
        row = self.get_rule(camera_id, rule_id)
        if row is None:
            return None
        row["enabled"] = enabled
        return row

    def list_rules(self, camera_id: str) -> list[dict[str, Any]]:
        return [rule for rule in self.rules if rule["camera_id"] == camera_id]

    def list_rules_for_cameras(self, camera_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return {camera_id: self.list_rules(camera_id) for camera_id in camera_ids}


@pytest.fixture
def repo() -> FakeCameraRepository:
    return FakeCameraRepository()


@pytest.fixture
def client(repo: FakeCameraRepository):
    app.dependency_overrides.clear()

    def _override():
        yield repo

    app.dependency_overrides[cameras_repo_dep] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _camera() -> dict[str, Any]:
    return {
        "id": "cam_c1g1_test",
        "name": "C1G1 Test Camera",
        "source_id": "c1e_rtsp_replay",
        "rtsp_url": "rtsp://10.37.57.112:8554/live/1080movie",
        "site_id": "test_site",
        "location": "test_location",
        "gpu_id": 0,
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


def _create_camera(client: TestClient) -> dict[str, Any]:
    resp = client.post("/api/v1/cameras", json=_camera())
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_create_update_enable_disable_camera(client: TestClient):
    camera = _create_camera(client)
    assert camera["id"] == "cam_c1g1_test"
    assert camera["rtsp_transport"] == "tcp"
    assert camera["alert_policy"]["global_alert_cooldown_s"] == 30

    updated = {**_camera(), "name": "Updated", "location": "new_location"}
    resp = client.put("/api/v1/cameras/cam_c1g1_test", json=updated)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["name"] == "Updated"

    resp = client.post("/api/v1/cameras/cam_c1g1_test/disable")
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False

    resp = client.post("/api/v1/cameras/cam_c1g1_test/enable")
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is True


def test_create_polygon_and_line_zones(client: TestClient):
    _create_camera(client)
    polygon = {
        "zone_id": "perimeter",
        "zone_name": "周界区域",
        "zone_type": "polygon",
        "coordinate_space": "pixel",
        "points": [[100, 300], [900, 300], [900, 700], [100, 700]],
        "enabled": True,
    }
    resp = client.post("/api/v1/cameras/cam_c1g1_test/zones", json=polygon)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["zone_id"] == "perimeter"

    line = {
        "zone_id": "tripwire",
        "zone_type": "line",
        "points": [[0, 0], [100, 100]],
    }
    resp = client.post("/api/v1/cameras/cam_c1g1_test/zones", json=line)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["zone_type"] == "line"


def test_algorithm_rules_full_config_and_semantics(client: TestClient):
    _create_camera(client)
    client.post(
        "/api/v1/cameras/cam_c1g1_test/zones",
        json={
            "zone_id": "perimeter",
            "zone_type": "polygon",
            "points": [[0, 0], [1, 0], [1, 1]],
        },
    )
    rules = [
        {
            "rule_id": "rule_face_observation",
            "algorithm_id": "face.observation",
            "enabled": True,
            "config": {"min_quality": 0.6, "retention_days": 30},
        },
        {
            "rule_id": "rule_watchlist",
            "algorithm_id": "face.watchlist",
            "enabled": True,
            "config": {"threshold": 0.75, "cooldown_s": 60},
        },
        {
            "rule_id": "rule_intrusion",
            "algorithm_id": "behavior.intrusion",
            "enabled": True,
            "config": {"zone_id": "perimeter", "min_inside_ms": 1000},
        },
    ]
    created = []
    for rule in rules:
        resp = client.post("/api/v1/cameras/cam_c1g1_test/rules", json=rule)
        assert resp.status_code == 200, resp.text
        created.append(resp.json()["data"])

    face_observation = next(r for r in created if r["algorithm_id"] == "face.observation")
    assert face_observation["rule_category"] == "observation"
    assert face_observation["is_alert_rule"] is False

    watchlist = next(r for r in created if r["algorithm_id"] == "face.watchlist")
    assert watchlist["rule_category"] == "alert"
    assert watchlist["is_alert_rule"] is True

    resp = client.post("/api/v1/cameras/cam_c1g1_test/rules/rule_watchlist/disable")
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False

    resp = client.post("/api/v1/cameras/cam_c1g1_test/rules/rule_watchlist/enable")
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is True

    resp = client.get("/api/v1/cameras/cam_c1g1_test/config")
    assert resp.status_code == 200
    config = resp.json()["data"]
    assert config["camera"]["id"] == "cam_c1g1_test"
    assert len(config["zones"]) == 1
    assert {rule["rule_id"] for rule in config["rules"]} == {
        "rule_face_observation",
        "rule_watchlist",
        "rule_intrusion",
    }


def test_alert_policy_endpoint_saves_global_cooldown(client: TestClient):
    _create_camera(client)
    policy = {
        "global_alert_cooldown_s": 30,
        "store_suppressed_events": True,
        "suppress_record_request": True,
        "critical_bypass": False,
    }
    resp = client.put("/api/v1/cameras/cam_c1g1_test/alert-policy", json=policy)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["global_alert_cooldown_s"] == 30

    resp = client.get("/api/v1/cameras/cam_c1g1_test/alert-policy")
    assert resp.status_code == 200
    assert resp.json()["data"]["global_alert_cooldown_s"] == 30


def test_invalid_algorithm_zone_type_and_config_are_rejected(client: TestClient):
    _create_camera(client)
    resp = client.post(
        "/api/v1/cameras/cam_c1g1_test/rules",
        json={"rule_id": "bad", "algorithm_id": "face.everyone_alert", "config": {}},
    )
    assert resp.status_code == 422

    resp = client.post(
        "/api/v1/cameras/cam_c1g1_test/zones",
        json={"zone_id": "bad", "zone_type": "circle", "points": [[0, 0], [1, 1]]},
    )
    assert resp.status_code == 422

    resp = client.post(
        "/api/v1/cameras/cam_c1g1_test/rules",
        json={"rule_id": "bad_config", "algorithm_id": "face.watchlist", "config": "not-json"},
    )
    assert resp.status_code == 422
