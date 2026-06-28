"""Tests for midterm camera / zone / rule configuration endpoints.

The tests use the same pattern as ``test_api_events.py``: a
FakeCameraRepository injected through ``app.dependency_overrides`` so
no PostgreSQL is required. The fake mirrors the SQL constraints
(UNIQUE on camera id, on (camera_id, zone_name), on (camera_id, rule_type))
and ON DELETE CASCADE semantics.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR in sys.path:
    sys.path.remove(API_DIR)
sys.path.insert(0, API_DIR)

# Reset any modules pulled in by sibling runtime tests so we get the
# services/api copy of ``app.*``.
for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from fastapi.testclient import TestClient

import app.routers.cameras as cameras_router_module
from app.main import app
from app.routers.cameras import _repo as cameras_repo_dep


NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# FakeCameraRepository
# ---------------------------------------------------------------------------


class FakeCameraRepository:
    """In-memory mirror of CameraRepository honouring the midterm SQL constraints."""

    def __init__(self) -> None:
        self.cameras: Dict[str, Dict[str, Any]] = {}
        self.zones: List[Dict[str, Any]] = []
        self.rules: List[Dict[str, Any]] = []
        self._zone_id_seq = 0
        self._rule_id_seq = 0

    # -- camera ---------------------------------------------------------

    def create_camera(self, *, camera_id, source_id, name, rtsp_url,
                      site_id, location, gpu_id, enabled) -> Dict[str, Any]:
        if camera_id in self.cameras:
            raise _UniqueViolation(f"cameras.id={camera_id}")
        if any(c["source_id"] == source_id for c in self.cameras.values()):
            raise _UniqueViolation(f"cameras.source_id={source_id}")
        row = {
            "id": camera_id,
            "source_id": source_id,
            "name": name,
            "rtsp_url": rtsp_url,
            "site_id": site_id,
            "location": location,
            "gpu_id": gpu_id,
            "enabled": enabled,
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.cameras[camera_id] = row
        return row

    def get_camera(self, camera_id: str) -> Optional[Dict[str, Any]]:
        return self.cameras.get(camera_id)

    def list_cameras(self, *, enabled: Optional[bool] = None) -> List[Dict[str, Any]]:
        rows = [c for c in self.cameras.values()
                if enabled is None or c["enabled"] is enabled]
        return sorted(rows, key=lambda r: r["id"])

    # -- zone -----------------------------------------------------------

    def create_zone(self, *, camera_id, zone_name, zone_type, points,
                    payload, zone_id=None, coordinate_space="pixel",
                    enabled=True) -> Dict[str, Any]:
        for z in self.zones:
            if z["camera_id"] == camera_id and z["zone_name"] == zone_name:
                raise _UniqueViolation(
                    f"camera_zones.(camera_id={camera_id}, zone_name={zone_name})"
                )
        self._zone_id_seq += 1
        row = {
            "id": self._zone_id_seq,
            "camera_id": camera_id,
            "zone_id": zone_id or zone_name,
            "zone_name": zone_name,
            "zone_type": zone_type,
            "coordinate_space": coordinate_space,
            "points": list(points),
            "enabled": enabled,
            "payload": dict(payload or {}),
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.zones.append(row)
        return row

    def get_zone(self, camera_id: str, zone_id: str) -> Dict[str, Any] | None:
        return next(
            (
                z
                for z in self.zones
                if z["camera_id"] == camera_id
                and zone_id in {str(z.get("zone_id")), str(z.get("zone_name"))}
            ),
            None,
        )

    def update_zone(
        self,
        *,
        camera_id,
        zone_id,
        new_zone_id=None,
        zone_name=None,
        zone_type=None,
        coordinate_space=None,
        points=None,
        enabled=None,
        payload=None,
    ) -> Dict[str, Any] | None:
        row = self.get_zone(camera_id, zone_id)
        if row is None:
            return None
        old_ids = {str(row.get("zone_id")), str(row.get("zone_name")), str(zone_id)}
        row.update(
            {
                "zone_id": new_zone_id or row.get("zone_id") or row["zone_name"],
                "zone_name": zone_name or row["zone_name"],
                "zone_type": zone_type or row["zone_type"],
                "coordinate_space": coordinate_space or row.get("coordinate_space", "pixel"),
                "points": list(points if points is not None else row["points"]),
                "enabled": row.get("enabled", True) if enabled is None else enabled,
                "payload": dict(payload if payload is not None else row.get("payload", {})),
                "updated_at": NOW,
            }
        )
        for old_id in old_ids:
            if old_id and old_id != str(row["zone_id"]):
                self.rebind_zone_references(
                    camera_id=camera_id,
                    old_zone_id=old_id,
                    new_zone_id=row["zone_id"],
                )
        return row

    def list_zones(self, camera_id: str) -> List[Dict[str, Any]]:
        return sorted(
            (z for z in self.zones if z["camera_id"] == camera_id),
            key=lambda z: z["zone_name"],
        )

    def get_zone_names(self, camera_id: str) -> List[str]:
        names: list[str] = []
        for z in self.zones:
            if z["camera_id"] != camera_id:
                continue
            for value in (z.get("zone_id"), z.get("zone_name")):
                if value and str(value) not in names:
                    names.append(str(value))
        return names

    def list_zones_for_cameras(self, camera_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        out = {str(cid): [] for cid in camera_ids}
        for z in self.zones:
            if str(z["camera_id"]) in out:
                out[str(z["camera_id"])].append(z)
        for cid in out:
            out[cid].sort(key=lambda z: z["zone_name"])
        return out

    # -- rule -----------------------------------------------------------

    def create_rule(
        self,
        *,
        camera_id,
        rule_type,
        enabled,
        config,
        rule_id=None,
        algorithm_id=None,
    ) -> Dict[str, Any]:
        for r in self.rules:
            if r["camera_id"] == camera_id and r["rule_type"] == rule_type:
                raise _UniqueViolation(
                    f"camera_rules.(camera_id={camera_id}, rule_type={rule_type})"
                )
        self._rule_id_seq += 1
        row = {
            "id": self._rule_id_seq,
            "camera_id": camera_id,
            "rule_id": rule_id or rule_type,
            "algorithm_id": algorithm_id or rule_type,
            "rule_type": rule_type,
            "enabled": enabled,
            "zone_id": config.get("zone_id") or config.get("zone"),
            "line_id": config.get("line_id"),
            "config": dict(config or {}),
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.rules.append(row)
        return row

    def rebind_zone_references(self, *, camera_id: str, old_zone_id: str, new_zone_id: str):
        rows = []
        for row in self.rules:
            config = dict(row.get("config") or {})
            if row["camera_id"] != camera_id:
                continue
            if not (
                row.get("zone_id") == old_zone_id
                or config.get("zone_id") == old_zone_id
                or config.get("zone") == old_zone_id
            ):
                continue
            row["zone_id"] = new_zone_id
            config["zone_id"] = new_zone_id
            config["zone"] = new_zone_id
            row["config"] = config
            row["updated_at"] = NOW
            rows.append(row)
        return rows

    def bind_final_roi_zone(self, *, camera_id: str, zone_id: str):
        rows = []
        for row in self.rules:
            if row["camera_id"] != camera_id:
                continue
            if row.get("algorithm_id") in {"face.watchlist", "face.observation", "face.live_search"}:
                continue
            if row.get("rule_type") in {"face.watchlist", "face.observation", "face.live_search"}:
                continue
            config = dict(row.get("config") or {})
            if row.get("rule_type") in {"wall_climb", "wall_climb_suspicious"}:
                continue
            row["zone_id"] = zone_id
            config["zone_id"] = zone_id
            config["zone"] = zone_id
            row["config"] = config
            row["updated_at"] = NOW
            rows.append(row)
        return rows

    def list_rules(self, camera_id: str) -> List[Dict[str, Any]]:
        return sorted(
            (r for r in self.rules if r["camera_id"] == camera_id),
            key=lambda r: r["rule_type"],
        )

    def list_rules_for_cameras(self, camera_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        out = {str(cid): [] for cid in camera_ids}
        for r in self.rules:
            if str(r["camera_id"]) in out:
                out[str(r["camera_id"])].append(r)
        for cid in out:
            out[cid].sort(key=lambda r: r["rule_type"])
        return out


class _UniqueViolation(Exception):
    """Stand-in for psycopg.errors.UniqueViolation in the fake repo."""


# ---------------------------------------------------------------------------
# Test client fixture — one repo shared across requests within a single test
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_camera(client, **overrides):
    body = {
        "id": "cam_001",
        "source_id": "primary_rtsp",
        "name": "Test Camera",
        "location": "Test Area",
        "rtsp_url": "rtsp://example.local/stream",
        "site_id": "site_a",
        "gpu_id": 0,
        "enabled": True,
    }
    body.update(overrides)
    return client.post("/api/v1/cameras", json=body)


def _create_zone(client, camera_id="cam_001", **overrides):
    body = {
        "zone_name": "perimeter",
        "zone_type": "polygon",
        "points": [[100, 300], [900, 300], [900, 700], [100, 700]],
    }
    body.update(overrides)
    return client.post(f"/api/v1/cameras/{camera_id}/zones", json=body)


def _create_intrusion_rule(client, camera_id="cam_001", zone="perimeter", **overrides):
    body = {
        "rule_type": "intrusion",
        "enabled": True,
        "config": {
            "zone": zone,
            "min_inside_ms": 1000,
            "cooldown_s": 30,
            "severity": "medium",
            "snapshot_required": True,
            "clip_required": True,
        },
    }
    if "config" in overrides:
        body["config"] = {**body["config"], **overrides.pop("config")}
    body.update(overrides)
    return client.post(f"/api/v1/cameras/{camera_id}/rules", json=body)


# ===========================================================================
# 1. create camera
# ===========================================================================


def test_create_camera(client):
    resp = _create_camera(client)
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["error"] is None
    cam = body["data"]
    assert cam["id"] == "cam_001"
    assert cam["source_id"] == "primary_rtsp"
    assert cam["rtsp_url"] == "rtsp://example.local/stream"
    assert cam["enabled"] is True
    assert cam["gpu_id"] == 0


# ===========================================================================
# 2. GET camera list sees the camera
# ===========================================================================


def test_list_cameras(client):
    _create_camera(client)
    resp = client.get("/api/v1/cameras")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["cameras"][0]["id"] == "cam_001"


# ===========================================================================
# 3. GET camera detail returns expected fields
# ===========================================================================


def test_get_camera_detail(client):
    _create_camera(client)
    resp = client.get("/api/v1/cameras/cam_001")
    assert resp.status_code == 200
    cam = resp.json()["data"]
    for field in ("source_id", "name", "location", "rtsp_url", "enabled", "gpu_id"):
        assert field in cam, f"missing {field}"
    assert cam["name"] == "Test Camera"
    assert cam["location"] == "Test Area"


# ===========================================================================
# 4. create polygon zone
# ===========================================================================


def test_create_polygon_zone(client):
    _create_camera(client)
    resp = _create_zone(client)
    assert resp.status_code == 200, resp.json()
    zone = resp.json()["data"]
    assert zone["zone_name"] == "perimeter"
    assert zone["zone_type"] == "polygon"
    assert zone["points"] == [[100, 300], [900, 300], [900, 700], [100, 700]]


# ===========================================================================
# 5. GET zones returns the zone
# ===========================================================================


def test_list_zones(client):
    _create_camera(client)
    _create_zone(client)
    resp = client.get("/api/v1/cameras/cam_001/zones")
    assert resp.status_code == 200
    zones = resp.json()["data"]["zones"]
    assert len(zones) == 1
    assert zones[0]["zone_name"] == "perimeter"


# ===========================================================================
# 6. create intrusion rule
# ===========================================================================


def test_create_intrusion_rule(client):
    _create_camera(client)
    _create_zone(client)
    resp = _create_intrusion_rule(client)
    assert resp.status_code == 200, resp.json()
    rule = resp.json()["data"]
    assert rule["rule_type"] == "intrusion"
    assert rule["enabled"] is True
    cfg = rule["config"]
    assert cfg["zone"] == "perimeter"
    assert cfg["min_inside_ms"] == 1000
    assert cfg["cooldown_s"] == 30
    assert cfg["severity"] == "medium"
    assert cfg["snapshot_required"] is True
    assert cfg["clip_required"] is True


def test_roi_save_can_bind_zone_rules(client, repo):
    _create_camera(client)
    _create_zone(client)
    _create_intrusion_rule(client)

    resp = client.post(
        "/api/v1/cameras/cam_001/zones?bind_rules=true",
        json={
            "zone_id": "roi_final",
            "zone_name": "roi_final",
            "zone_type": "polygon",
            "points": [[10, 10], [100, 10], [100, 100], [10, 100]],
        },
    )

    assert resp.status_code == 200, resp.json()
    rule = repo.list_rules("cam_001")[0]
    assert rule["zone_id"] == "roi_final"
    assert rule["config"]["zone_id"] == "roi_final"
    assert rule["config"]["zone"] == "roi_final"


def test_roi_save_does_not_bind_face_or_line_rules(client, repo):
    _create_camera(client)
    _create_zone(client)
    repo.rules.extend(
        [
            {
                "id": 100,
                "camera_id": "cam_001",
                "rule_id": "rule_watchlist",
                "algorithm_id": "face.watchlist",
                "rule_type": "face.watchlist",
                "enabled": True,
                "zone_id": None,
                "line_id": None,
                "config": {"threshold": 0.75},
                "created_at": NOW,
                "updated_at": NOW,
            },
            {
                "id": 101,
                "camera_id": "cam_001",
                "rule_id": "rule_wall_climb",
                "algorithm_id": "behavior.wall_climb_suspicious",
                "rule_type": "wall_climb",
                "enabled": True,
                "zone_id": None,
                "line_id": "tripwire_01",
                "config": {"line_id": "tripwire_01", "cooldown_s": 60},
                "created_at": NOW,
                "updated_at": NOW,
            },
        ]
    )

    resp = client.post(
        "/api/v1/cameras/cam_001/zones?bind_rules=true",
        json={
            "zone_id": "roi_final",
            "zone_name": "roi_final",
            "zone_type": "polygon",
            "points": [[10, 10], [100, 10], [100, 100], [10, 100]],
        },
    )

    assert resp.status_code == 200, resp.json()
    rules = {rule["rule_id"]: rule for rule in repo.list_rules("cam_001")}
    assert rules["rule_watchlist"]["zone_id"] is None
    assert "zone_id" not in rules["rule_watchlist"]["config"]
    assert rules["rule_wall_climb"]["line_id"] == "tripwire_01"
    assert rules["rule_wall_climb"]["zone_id"] is None
    assert "zone_id" not in rules["rule_wall_climb"]["config"]


def test_zone_rename_rebinds_existing_rule_references(client, repo):
    _create_camera(client)
    _create_zone(client, zone_id="perimeter", zone_name="perimeter")
    _create_intrusion_rule(client, zone="perimeter")

    resp = client.put(
        "/api/v1/cameras/cam_001/zones/perimeter",
        json={
            "zone_id": "roi_final",
            "zone_name": "roi_final",
            "zone_type": "polygon",
            "points": [[10, 10], [100, 10], [100, 100], [10, 100]],
        },
    )

    assert resp.status_code == 200, resp.json()
    rule = repo.list_rules("cam_001")[0]
    assert rule["zone_id"] == "roi_final"
    assert rule["config"]["zone"] == "roi_final"


# ===========================================================================
# 7. GET rules returns the rule
# ===========================================================================


def test_list_rules(client):
    _create_camera(client)
    _create_zone(client)
    _create_intrusion_rule(client)
    resp = client.get("/api/v1/cameras/cam_001/rules")
    assert resp.status_code == 200
    rules = resp.json()["data"]["rules"]
    assert len(rules) == 1
    assert rules[0]["rule_type"] == "intrusion"


# ===========================================================================
# 8. intrusion rule cannot reference an unknown zone
# ===========================================================================


def test_intrusion_rule_rejects_unknown_zone(client):
    _create_camera(client)
    _create_zone(client)
    resp = _create_intrusion_rule(client, config={"zone": "does_not_exist"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["data"] is None
    assert "unknown zone_name" in body["error"]["message"]


# ===========================================================================
# 9. duplicate zone_name on the same camera returns 409
# ===========================================================================


def test_duplicate_zone_name_returns_409(client):
    _create_camera(client)
    _create_zone(client)
    resp = _create_zone(client)  # same zone_name "perimeter"
    assert resp.status_code == 409
    body = resp.json()
    assert body["data"] is None
    assert "already exists" in body["error"]["message"]


# ===========================================================================
# 10. duplicate rule_type on the same camera returns 409 (midterm single-rule)
# ===========================================================================


def test_duplicate_rule_type_returns_409(client):
    _create_camera(client)
    _create_zone(client)
    _create_intrusion_rule(client)
    resp = _create_intrusion_rule(client)
    assert resp.status_code == 409
    body = resp.json()
    assert body["data"] is None
    assert "intrusion" in body["error"]["message"]


# ===========================================================================
# 11. creating zone / rule on missing camera returns 404
# ===========================================================================


def test_create_zone_on_missing_camera_404(client):
    resp = _create_zone(client, camera_id="ghost_cam")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == 404


def test_create_rule_on_missing_camera_404(client):
    resp = _create_intrusion_rule(client, camera_id="ghost_cam")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == 404


def test_get_missing_camera_404(client):
    resp = client.get("/api/v1/cameras/does_not_exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == 404


def test_runtime_supervisor_status_route_is_under_cameras_prefix(client, monkeypatch):
    monkeypatch.setattr(
        cameras_router_module,
        "get_savant_supervisor_snapshot",
        lambda: {
            "enabled": True,
            "savant_module_status": "running",
            "source_adapters": ["video-analytics-midterm-source-adapter"],
        },
    )

    resp = client.get("/api/v1/cameras/runtime/supervisor")

    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["data"]["savant_module_status"] == "running"


def test_runtime_sources_apply_route_is_under_cameras_prefix(client, monkeypatch):
    _create_camera(client)
    captured: dict[str, Any] = {}

    def fake_sync(*, export_doc, cameras):
        captured["export_doc"] = export_doc
        captured["cameras"] = cameras
        return {
            "runtime_action": "source_converge",
            "dynamic_sources_started": ["test_source"],
        }

    monkeypatch.setattr(cameras_router_module, "sync_camera_runtime_config_and_sources", fake_sync)

    resp = client.post("/api/v1/cameras/runtime/sources/apply")

    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["data"]["runtime_action"] == "source_converge"
    assert "cameras" in captured["export_doc"]
    assert captured["cameras"][0]["id"] == "cam_001"


def test_runtime_config_sync_route_only_syncs_module_config(client, monkeypatch):
    _create_camera(client)
    captured: dict[str, Any] = {}

    def fake_sync(*, export_doc):
        captured["export_doc"] = export_doc
        return {
            "runtime_action": "module_config_sync",
            "module_config_synced": True,
            "containers_restarted": [],
            "source_containers_touched": [],
        }

    def fail_source_sync(*args, **kwargs):
        raise AssertionError("config sync must not converge source containers")

    monkeypatch.setattr(cameras_router_module, "sync_camera_runtime_module_config", fake_sync)
    monkeypatch.setattr(cameras_router_module, "sync_camera_runtime_config_and_sources", fail_source_sync)

    resp = client.post("/api/v1/cameras/runtime/config/sync")

    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["data"]["runtime_action"] == "module_config_sync"
    assert body["data"]["containers_restarted"] == []
    assert body["data"]["source_containers_touched"] == []
    assert "cameras" in captured["export_doc"]


def test_runtime_restart_returns_409_when_evidence_guard_blocks(client, monkeypatch):
    _create_camera(client)

    def fake_restart(*, export_doc, cameras, force=False):
        raise cameras_router_module.RuntimeApplyBlockedError(
            "runtime restart blocked because evidence tasks are still active",
            details={
                "ok": False,
                "blocked": True,
                "active_count": 2,
                "tasks": [{"task_id": "task-1", "blocking_state": "replaying"}],
            },
            status_code=409,
        )

    monkeypatch.setattr(cameras_router_module, "restart_camera_runtime", fake_restart)

    resp = client.post("/api/v1/cameras/runtime/restart")

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == 409
    assert body["error"]["details"]["active_count"] == 2
    assert body["error"]["details"]["tasks"][0]["blocking_state"] == "replaying"


def test_runtime_restart_force_query_is_passed_to_service(client, monkeypatch):
    _create_camera(client)
    captured: dict[str, Any] = {}

    def fake_restart(*, export_doc, cameras, force=False):
        captured["force"] = force
        return {
            "runtime_action": "restart",
            "evidence_restart_guard": {"forced": force, "active_count": 1},
        }

    monkeypatch.setattr(cameras_router_module, "restart_camera_runtime", fake_restart)

    resp = client.post("/api/v1/cameras/runtime/restart?force=true")

    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert captured["force"] is True
    assert body["data"]["evidence_restart_guard"]["forced"] is True


# ===========================================================================
# Bonus guards on validation
# ===========================================================================


def test_polygon_zone_requires_three_points(client):
    _create_camera(client)
    resp = _create_zone(client, points=[[0, 0], [1, 1]])
    assert resp.status_code == 400
    assert "3 to 10" in resp.json()["error"]["message"]


def test_line_zone_requires_two_points(client):
    _create_camera(client)
    resp = _create_zone(
        client,
        zone_name="line_a",
        zone_type="line",
        points=[[0, 0], [1, 1], [2, 2]],
    )
    assert resp.status_code == 400
    assert "exactly 2 points" in resp.json()["error"]["message"]


def test_intrusion_min_inside_ms_must_be_positive(client):
    _create_camera(client)
    _create_zone(client)
    resp = _create_intrusion_rule(client, config={"min_inside_ms": 0})
    assert resp.status_code == 400
    assert "min_inside_ms" in resp.json()["error"]["message"]


def test_camera_id_uniqueness(client):
    _create_camera(client)
    resp = _create_camera(client)
    assert resp.status_code == 409
