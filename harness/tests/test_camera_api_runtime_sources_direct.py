from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR in sys.path:
    sys.path.remove(API_DIR)
sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

import app.routers.cameras as cameras_router  # noqa: E402
from app.schemas.cameras import CameraCreate, CameraUpdate  # noqa: E402


NOW = datetime(2026, 6, 12, 8, 0, 0, tzinfo=timezone.utc)


class FakeRepo:
    def __init__(self) -> None:
        self.cameras: dict[str, dict[str, Any]] = {}
        self.activity_source_ids: set[str] = set()

    def get_camera(self, camera_id: str) -> dict[str, Any] | None:
        return self.cameras.get(camera_id)

    def list_cameras(self, *, enabled: bool | None = None) -> list[dict[str, Any]]:
        rows = list(self.cameras.values())
        if enabled is not None:
            rows = [row for row in rows if row["enabled"] is enabled]
        return sorted(rows, key=lambda row: row["id"])

    def list_zones_for_cameras(self, camera_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return {camera_id: [] for camera_id in camera_ids}

    def list_rules_for_cameras(self, camera_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return {camera_id: [] for camera_id in camera_ids}

    def create_camera(self, **kwargs: Any) -> dict[str, Any]:
        row = {
            "id": kwargs["camera_id"],
            "source_id": kwargs["source_id"],
            "name": kwargs["name"],
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

    def update_camera(self, camera_id: str, **updates: Any) -> dict[str, Any] | None:
        row = self.cameras.get(camera_id)
        if row is None:
            return None
        next_source_id = updates.get("source_id", row["source_id"])
        if next_source_id != row["source_id"] and self.source_id_has_activity(row["source_id"]):
            raise ValueError(
                "source_id cannot be changed after events, evidence, or observations exist"
            )
        for key, value in updates.items():
            if value is not None:
                row[key] = value
        row["updated_at"] = NOW
        return row

    def set_camera_enabled(self, camera_id: str, enabled: bool) -> dict[str, Any] | None:
        row = self.cameras.get(camera_id)
        if row is None:
            return None
        row["enabled"] = enabled
        row["updated_at"] = NOW
        return row

    def source_id_has_activity(self, source_id: str) -> bool:
        return source_id in self.activity_source_ids


def _create_body(source_id: str = "source_cam_001") -> CameraCreate:
    return CameraCreate(
        id="cam_001",
        source_id=source_id,
        name="Gate Camera",
        rtsp_url="rtsp://example.local/stream",
        enabled=True,
    )


def test_camera_create_update_enable_disable_sync_runtime_config_and_sources(
    monkeypatch,
) -> None:
    repo = FakeRepo()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_sync(*, export_doc, cameras):
        calls.append(([row["source_id"] for row in cameras], export_doc))
        return {
            "runtime_action": "source_converge",
            "sources_total": len(cameras),
            "module_config_synced": True,
        }

    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "true")
    monkeypatch.setattr(cameras_router, "sync_camera_runtime_config_and_sources", fake_sync)

    created = cameras_router.cameras_create(_create_body(), repo=repo, request_id="req")
    updated = cameras_router.cameras_update(
        "cam_001",
        CameraUpdate(name="Gate Camera Updated", rtsp_url="rtsp://example.local/updated"),
        repo=repo,
        request_id="req",
    )
    disabled = cameras_router.cameras_disable("cam_001", repo=repo, request_id="req")
    enabled = cameras_router.cameras_enable("cam_001", repo=repo, request_id="req")

    for response in (created, updated, disabled, enabled):
        assert response["error"] is None
        assert response["data"]["runtime_source_apply"]["ok"] is True
        assert response["data"]["runtime_source_apply"]["result"]["runtime_action"] == (
            "source_converge"
        )
        assert response["data"]["runtime_source_apply"]["result"]["module_config_synced"] is True
    assert [source_ids for source_ids, _doc in calls] == [
        ["source_cam_001"],
        ["source_cam_001"],
        ["source_cam_001"],
        ["source_cam_001"],
    ]
    assert calls[2][1]["cameras"]["cam_001"]["enabled"] is False
    assert calls[3][1]["cameras"]["cam_001"]["enabled"] is True


def test_camera_crud_does_not_call_source_convergence_when_runtime_disabled(
    monkeypatch,
) -> None:
    repo = FakeRepo()

    def fail_sync(*, export_doc, cameras):
        raise AssertionError(f"unexpected convergence call: {cameras!r}")

    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "false")
    monkeypatch.setattr(cameras_router, "sync_camera_runtime_config_and_sources", fail_sync)

    response = cameras_router.cameras_create(_create_body(), repo=repo, request_id="req")

    assert response["error"] is None
    assert response["data"]["runtime_source_apply"] == {
        "ok": False,
        "skipped": "camera runtime control is disabled",
    }


@pytest.mark.parametrize(
    "source_id",
    [
        "source/one",
        "source one",
        "source:one",
        "source$one",
        ".",
        "..",
        "x" * 97,
    ],
)
def test_source_id_validation_rejects_runtime_unsafe_values(source_id: str) -> None:
    with pytest.raises(ValidationError):
        _create_body(source_id=source_id)


def test_source_id_update_is_blocked_after_activity(monkeypatch) -> None:
    repo = FakeRepo()
    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "false")
    cameras_router.cameras_create(_create_body(source_id="source_locked"), repo=repo, request_id="req")
    repo.activity_source_ids.add("source_locked")

    response = cameras_router.cameras_update(
        "cam_001",
        CameraUpdate(source_id="source_new"),
        repo=repo,
        request_id="req",
    )

    assert response.status_code == 409
    payload = json.loads(response.body)
    assert "source_id cannot be changed" in payload["error"]["message"]
