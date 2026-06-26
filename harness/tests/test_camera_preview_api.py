from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR in sys.path:
    sys.path.remove(API_DIR)
sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from fastapi.testclient import TestClient

import app.routers.cameras as cameras_router_module
from app.main import app
from app.routers.cameras import _repo as cameras_repo_dep
from app.services.camera_preview import CameraPreviewError, CameraPreviewFrame


class FakeCameraRepository:
    def __init__(self) -> None:
        self.cameras: dict[str, dict[str, Any]] = {
            "cam_001": {
                "id": "cam_001",
                "source_id": "source_cam_001",
                "name": "Lab",
                "rtsp_url": "rtsp://example.local/live",
                "enabled": True,
                "input_type": "rtsp",
                "rtsp_transport": "tcp",
            }
        }

    def get_camera(self, camera_id: str) -> dict[str, Any] | None:
        return self.cameras.get(camera_id)


@pytest.fixture()
def repo() -> FakeCameraRepository:
    return FakeCameraRepository()


@pytest.fixture()
def client(repo: FakeCameraRepository):
    app.dependency_overrides.clear()

    def _override():
        yield repo

    app.dependency_overrides[cameras_repo_dep] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_camera_preview_endpoint_returns_jpeg(client, monkeypatch):
    calls: list[dict[str, Any]] = []

    def fake_capture(rtsp_url: str, **kwargs: Any) -> CameraPreviewFrame:
        calls.append({"rtsp_url": rtsp_url, **kwargs})
        return CameraPreviewFrame(
            data=b"\xff\xd8fake-jpeg\xff\xd9",
            width=640,
            height=360,
            source_width=1920,
            source_height=1080,
        )

    monkeypatch.setattr(cameras_router_module, "capture_camera_preview_jpeg", fake_capture)

    response = client.get(
        "/api/v1/cameras/cam_001/preview.jpg?max_width=640&timeout_ms=900&quality=70"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-camera-preview-width"] == "640"
    assert response.headers["x-camera-preview-height"] == "360"
    assert response.headers["x-camera-source-width"] == "1920"
    assert response.headers["x-camera-source-height"] == "1080"
    assert response.content.startswith(b"\xff\xd8")
    assert calls == [
        {
            "rtsp_url": "rtsp://example.local/live",
            "rtsp_transport": "tcp",
            "timeout_ms": 900,
            "max_width": 640,
            "jpeg_quality": 70,
        }
    ]


def test_camera_preview_endpoint_returns_404_for_unknown_camera(client):
    response = client.get("/api/v1/cameras/missing/preview.jpg")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "camera not found: missing"


def test_camera_preview_endpoint_returns_503_when_capture_fails(client, monkeypatch):
    def fake_capture(_rtsp_url: str, **_kwargs: Any) -> CameraPreviewFrame:
        raise CameraPreviewError("failed to read camera frame")

    monkeypatch.setattr(cameras_router_module, "capture_camera_preview_jpeg", fake_capture)

    response = client.get("/api/v1/cameras/cam_001/preview.jpg")

    assert response.status_code == 503
    assert response.json()["error"]["message"] == (
        "camera preview unavailable: failed to read camera frame"
    )


def test_camera_preview_endpoint_rejects_non_rtsp_inputs(client, repo):
    repo.cameras["cam_001"]["input_type"] = "file"

    response = client.get("/api/v1/cameras/cam_001/preview.jpg")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == (
        "camera preview currently supports only rtsp inputs"
    )
