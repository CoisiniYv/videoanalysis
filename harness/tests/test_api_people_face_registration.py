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
from face_registration.image_face_registration import (
    ERROR_EXTERNAL_PERSON_ID_CONFLICT,
    RegistrationRequest,
    RegistrationResult,
    RegistrationError,
    _has_active_primary_gallery,
    _resolve_person,
)


NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


class FakePeopleRepository:
    trajectory_calls: list[dict[str, Any]] = []

    def __init__(self) -> None:
        self.last_trajectory_kwargs: dict[str, Any] = {}
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
        self.trajectory_rows = [
            {
                "event_id": "11111111-1111-1111-1111-111111111111",
                "source_event_id": "watchlist:1",
                "event_type": "watchlist_hit",
                "person_id": 10,
                "person_name": "Reese",
                "external_person_id": "demo:midterm:reese",
                "camera_id": "cam-lab",
                "source_id": "lab",
                "camera_name": "lab",
                "event_ts_ms": 1_000_000,
                "event_created_at": NOW,
                "similarity": 0.91,
                "source_observation_id": "face:lab:uuid:frame-1:0",
                "observation_timestamp_ms": 1_000_000,
                "face_bbox": [10, 20, 30, 40],
                "person_bbox": None,
                "face_crop_uri": "/data/video-analytics/media/faces/reese-crop.jpg",
                "full_frame_uri": "/data/video-analytics/media/faces/reese-frame.jpg",
                "annotated_frame_uri": "/data/video-analytics/media/faces/reese-annotated.jpg",
                "evidence_media_status": "image_ready",
                "evidence_summary": {"playback_kind": "image"},
                "trajectory_source": "watchlist_event",
            }
        ]

    def list_people(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        rows = list(self.people.values())
        return rows, len(rows)

    def get_person(self, person_id: int) -> dict[str, Any] | None:
        return self.people.get(person_id)

    def list_gallery(self, person_id: int) -> list[dict[str, Any]]:
        return self.gallery.get(person_id, [])

    def latest_location(self, person_id: int, **kwargs: Any) -> dict[str, Any] | None:
        rows = self.trajectory(person_id, **kwargs, limit=1, offset=0)
        return rows[0] if rows else None

    def trajectory(self, person_id: int, **kwargs: Any) -> list[dict[str, Any]]:
        self.last_trajectory_kwargs = dict(kwargs)
        type(self).trajectory_calls.append(dict(kwargs))
        rows = [row for row in self.trajectory_rows if row["person_id"] == person_id]
        min_similarity = float(kwargs.get("min_similarity") or 0.0)
        rows = [row for row in rows if float(row["similarity"] or 0.0) >= min_similarity]
        camera_id = kwargs.get("camera_id")
        if camera_id:
            rows = [row for row in rows if row["camera_id"] == camera_id]
        limit = int(kwargs.get("limit") or 50)
        offset = int(kwargs.get("offset") or 0)
        return rows[offset:offset + limit]


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
    FakePeopleRepository.trajectory_calls.clear()
    monkeypatch.setattr(
        "app.routers.people.cache_trajectory_thumbnails",
        lambda _person_id, _uris: {},
    )
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


def test_people_find_returns_image_trajectory_without_evidence_video(client: TestClient) -> None:
    resp = client.get("/api/v1/people/10/find?limit=10&min_similarity=0.7")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["mode"] == "person_lookup"
    assert data["latest_location"]["playback_kind"] == "image"
    assert data["latest_location"]["result_mode"] == "latest_camera_hit"
    assert data["latest_location"]["annotated_frame_url"] == "/media/faces/reese-annotated.jpg"
    assert data["results"][0]["trajectory_source"] == "watchlist_event"
    assert data["query"]["limit"] == 10
    assert (
        FakePeopleRepository.trajectory_calls[-1]["include_observation_search"] is True
    )


def test_people_trajectory_uses_persisted_hits_without_full_vector_search(
    client: TestClient,
) -> None:
    resp = client.get(
        "/api/v1/people/10/trajectory"
        "?limit=20&offset=0&min_similarity=0.6&camera_id=cam-lab"
        "&start_ts_ms=900000&end_ts_ms=1100000"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["mode"] == "persisted_trajectory"
    assert data["returned_count"] == 1
    assert data["has_more"] is False
    assert data["trajectory"][0]["trajectory_thumbnail_url"] == (
        "/media/faces/reese-crop.jpg"
    )
    call = FakePeopleRepository.trajectory_calls[-1]
    assert call["include_observation_search"] is False
    assert call["camera_id"] == "cam-lab"
    assert call["start_ts_ms"] == 900000
    assert call["end_ts_ms"] == 1100000
    assert call["limit"] == 21


def test_people_trajectory_reports_has_more_without_returning_extra_row(
    client: TestClient,
) -> None:
    class PagingPeopleRepository(FakePeopleRepository):
        def __init__(self) -> None:
            super().__init__()
            template = self.trajectory_rows[0]
            self.trajectory_rows = [
                {
                    **template,
                    "source_observation_id": f"face:lab:frame-{index}:0",
                    "event_ts_ms": 1_000_000 + index,
                }
                for index in range(21)
            ]

    def _repo_override():
        yield PagingPeopleRepository()

    app.dependency_overrides[people_repo_dep] = _repo_override
    resp = client.get("/api/v1/people/10/trajectory?limit=20&offset=0")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data["trajectory"]) == 20
    assert data["returned_count"] == 20
    assert data["has_more"] is True


def test_people_find_defaults_to_nonzero_similarity_threshold(client: TestClient) -> None:
    resp = client.get("/api/v1/people/10/find?limit=10")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["query"]["min_similarity"] == 0.6
    assert data["query"]["include_unregistered_sources"] is False


def test_people_find_can_explicitly_include_unregistered_sources(client: TestClient) -> None:
    resp = client.get("/api/v1/people/10/find?limit=10&include_unregistered_sources=true")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["query"]["include_unregistered_sources"] is True


def test_people_repository_prefers_small_persisted_trajectory_crop() -> None:
    source = (Path(API_DIR) / "app" / "repositories" / "people.py").read_text(
        encoding="utf-8"
    )
    assert "COALESCE(fo.crop_path, face_crop.uri) AS face_crop_uri" in source
    assert "WHERE %(include_observation_search)s::boolean" in source


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


def test_resolve_person_reports_person_id_external_id_conflict() -> None:
    class RepoStub:
        def get_by_id(self, person_id: int) -> dict[str, Any] | None:
            assert person_id == 10
            return {
                "id": 10,
                "external_person_id": "demo:midterm:reese",
                "name": "Reese",
            }

        def get_by_external_person_id(self, external_person_id: str) -> dict[str, Any] | None:
            raise AssertionError("not expected")

    repo = RepoStub()
    request = RegistrationRequest(
        image_path="/tmp/face.jpg",
        external_person_id="demo:midterm:finch",
        name="Finch",
        person_id=10,
        description=None,
        source_type="manual_upload",
        is_primary=False,
        quality_threshold=0.65,
        allow_multiple_faces=False,
        keep_crop=True,
        created_by="operator",
        dev_mock_embedding_fixture=None,
    )

    with pytest.raises(RegistrationError) as exc_info:
        _resolve_person(repo, request)

    exc = exc_info.value
    assert getattr(exc, "error_code", None) == ERROR_EXTERNAL_PERSON_ID_CONFLICT
    assert "请选中匹配的人员" in str(exc)


def test_has_active_primary_gallery_detects_missing_primary() -> None:
    class CursorStub:
        def __init__(self, row):
            self.row = row
            self.params = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql, params):
            self.params = params

        def fetchone(self):
            return self.row

    class ConnStub:
        def __init__(self, row):
            self.cursor_obj = CursorStub(row)

        def cursor(self):
            return self.cursor_obj

    missing = ConnStub(None)
    assert _has_active_primary_gallery(missing, person_id=8) is False
    assert missing.cursor_obj.params == {"person_id": 8}

    existing = ConnStub((1,))
    assert _has_active_primary_gallery(existing, person_id=8) is True
