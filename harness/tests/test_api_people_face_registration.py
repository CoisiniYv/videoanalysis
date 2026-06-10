from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)
LIBS_DIR = str(Path(__file__).resolve().parents[2] / "libs")
if LIBS_DIR not in sys.path:
    sys.path.insert(0, LIBS_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from fastapi.testclient import TestClient

from app.main import app
from app.routers.people import _registrar as registrar_dep
from app.routers.people import _repo as people_repo_dep
from face_registration.image_face_registration import RegistrationResult


NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


class FakePeopleRepository:
    def __init__(self) -> None:
        self.people = {
            10: {
                "id": 10,
                "person_id": 10,
                "name": "Reese",
                "external_person_id": "demo:midterm:reese",
                "description": "registered",
                "is_active": True,
                "payload": {},
                "created_at": NOW,
                "updated_at": NOW,
                "active_gallery_count": 1,
                "primary_gallery_embedding_id": 99,
                "primary_source_image_path": "/data/video-analytics/media/face-registration/reese.jpg",
                "primary_registered_crop_path": None,
            }
        }
        self.gallery = {
            10: [
                {
                    "id": 99,
                    "person_id": 10,
                    "source_type": "manual_upload",
                    "source_image_path": "/data/video-analytics/media/face-registration/reese.jpg",
                    "source_observation_id": None,
                    "embedding_model": "adaface",
                    "model_version": None,
                    "embedding_dim": 512,
                    "embedding_norm": 1.0,
                    "quality": 0.91,
                    "face_bbox": [1, 2, 3, 4],
                    "landmarks": [[1, 1], [2, 2], [3, 3], [4, 4], [5, 5]],
                    "is_primary": True,
                    "is_active": True,
                    "payload": {},
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ]
        }

    def list_people(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        rows = list(self.people.values())
        return rows, len(rows)

    def get_person(self, person_id: int) -> dict[str, Any] | None:
        return self.people.get(person_id)

    def list_gallery(self, person_id: int) -> list[dict[str, Any]]:
        return self.gallery.get(person_id, [])


@dataclass
class CapturingRegistrar:
    last_request: Any = None

    def __call__(self, request):
        self.last_request = request
        return RegistrationResult(
            status="REGISTERED",
            mode="external_image",
            person_id=10,
            person_reused=False,
            external_person_id=request.external_person_id,
            name=request.name,
            gallery_embedding_id=99,
            is_primary=request.is_primary,
            face_bbox=[1.0, 2.0, 3.0, 4.0],
            landmarks=[[1.0, 1.0], [2.0, 2.0], [3.0, 3.0], [4.0, 4.0], [5.0, 5.0]],
            quality=0.91,
            embedding_dim=512,
            embedding_norm=1.0,
            source_image_path=request.image_path,
            registered_crop_path=None,
            real_embedding_used=True,
            dev_mock_used=False,
            fallback_used=False,
        )


@pytest.fixture
def registrar() -> CapturingRegistrar:
    return CapturingRegistrar()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registrar: CapturingRegistrar):
    app.dependency_overrides.clear()
    monkeypatch.setenv("FACE_UPLOAD_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("FACE_UPLOAD_MAX_BYTES", str(10 * 1024 * 1024))

    def _repo_override():
        yield FakePeopleRepository()

    app.dependency_overrides[people_repo_dep] = _repo_override
    app.dependency_overrides[registrar_dep] = lambda: registrar
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_people_list_and_detail(client: TestClient) -> None:
    resp = client.get("/api/v1/people")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error"] is None
    assert body["data"]["people"][0]["external_person_id"] == "demo:midterm:reese"
    assert body["data"]["people"][0]["primary_source_image_url"] == "/media/face-registration/reese.jpg"

    resp = client.get("/api/v1/people/10")
    assert resp.status_code == 200, resp.text
    detail = resp.json()["data"]
    assert detail["person"]["person_id"] == 10
    assert detail["gallery"][0]["gallery_embedding_id"] == 99
    assert detail["gallery"][0]["source_image_url"] == "/media/face-registration/reese.jpg"
    assert "embedding" not in detail["gallery"][0]


def test_register_face_upload_forces_real_manual_upload_path(
    client: TestClient,
    registrar: CapturingRegistrar,
) -> None:
    resp = client.post(
        "/api/v1/people/register-face",
        data={
            "external_person_id": "demo:midterm:reese",
            "name": "Reese",
            "is_primary": "true",
            "allow_multiple_faces": "true",
            "quality_threshold": "0.72",
        },
        files={"image": ("reese.jpg", b"fake-image-bytes", "image/jpeg")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "REGISTERED"
    assert data["source_type"] == "manual_upload"
    assert data["dev_mock_used"] is False

    request = registrar.last_request
    assert request is not None
    assert request.source_type == "manual_upload"
    assert request.dev_mock_embedding_fixture is None
    assert request.allow_multiple_faces is True
    assert request.quality_threshold == 0.72
    assert request.keep_crop is True
    assert Path(request.image_path).exists()


def test_register_face_rejects_invalid_quality_threshold(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/people/register-face",
        data={
            "external_person_id": "demo:x",
            "name": "Demo",
            "quality_threshold": "1.5",
        },
        files={"image": ("face.jpg", b"fake-image-bytes", "image/jpeg")},
    )
    assert resp.status_code == 400
    assert "quality_threshold" in resp.json()["error"]["message"]


def test_register_face_enforces_upload_size_limit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACE_UPLOAD_MAX_BYTES", "4")
    resp = client.post(
        "/api/v1/people/register-face",
        data={"external_person_id": "demo:x", "name": "Demo"},
        files={"image": ("face.jpg", b"too-large", "image/jpeg")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message"].startswith("upload_too_large")


def test_register_face_requires_external_person_id(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/people/register-face",
        data={"name": "No External Id"},
        files={"image": ("face.jpg", b"fake-image-bytes", "image/jpeg")},
    )
    assert resp.status_code == 422


def test_register_face_rejects_video_extension(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/people/register-face",
        data={"external_person_id": "demo:x", "name": "Demo"},
        files={"image": ("face.mp4", b"not-an-image", "video/mp4")},
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["registration_error_code"] == "IMAGE_FILE_TYPE_UNSUPPORTED"
