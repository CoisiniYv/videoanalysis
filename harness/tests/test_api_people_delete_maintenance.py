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

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.routers.maintenance import _repo as maintenance_repo_dep  # noqa: E402
from harness.tests.test_storage_maintenance_evidence_preview import FakeRepo  # noqa: E402


class PeopleMaintenanceRepo(FakeRepo):
    def __init__(self) -> None:
        super().__init__()
        self.persons = {
            10: {
                "id": 10,
                "name": "Reese",
                "external_person_id": "demo:reese",
                "is_active": True,
                "created_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
            }
        }
        self.gallery = {
            99: {
                "id": 99,
                "person_id": 10,
                "source_image_path": "/tmp/reese.jpg",
                "is_active": True,
                "is_primary": True,
                "payload": {},
                "created_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
            }
        }

    def find_people_for_delete(self, **kwargs: Any) -> list[dict[str, Any]]:
        ids = kwargs.get("person_ids") or []
        return [self.persons[i] for i in ids if i in self.persons]

    def list_gallery_for_people(self, person_ids: list[int]) -> list[dict[str, Any]]:
        return [row for row in self.gallery.values() if row["person_id"] in person_ids]

    def find_gallery_for_delete(self, **kwargs: Any) -> list[dict[str, Any]]:
        ids = kwargs.get("gallery_embedding_ids") or []
        return [self.gallery[i] for i in ids if i in self.gallery]

    def get_person(self, person_id: int) -> dict[str, Any] | None:
        return self.persons.get(person_id)

    def get_gallery(self, gallery_id: int) -> dict[str, Any] | None:
        return self.gallery.get(gallery_id)

    def soft_delete_person(self, *, person_id: int, operator: str, reason: str) -> None:
        self.persons[person_id]["is_active"] = False

    def soft_delete_gallery(self, *, gallery_id: int, operator: str, reason: str) -> None:
        self.gallery[gallery_id]["is_active"] = False
        self.gallery[gallery_id]["is_primary"] = False


@pytest.fixture
def repo() -> PeopleMaintenanceRepo:
    return PeopleMaintenanceRepo()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: PeopleMaintenanceRepo):
    app.dependency_overrides.clear()
    media = tmp_path / "media"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("FACE_UPLOAD_ROOT", str(media / "face_uploads"))
    monkeypatch.setenv("FACE_REGISTRATION_ROOT", str(media / "face_registration"))
    monkeypatch.setenv("STORAGE_MAINTENANCE_CONFIRM_SECRET", "test-secret")
    monkeypatch.setenv("STORAGE_MAINTENANCE_PREVIEW_ENABLED", "true")
    monkeypatch.setenv("STORAGE_MAINTENANCE_EXECUTE_ENABLED", "true")

    def _override():
        yield repo

    app.dependency_overrides[maintenance_repo_dep] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_people_delete_preview_and_execute_soft_deletes_person_and_gallery(
    client: TestClient,
    repo: PeopleMaintenanceRepo,
) -> None:
    preview = client.post(
        "/api/v1/maintenance/people/delete-preview",
        json={"person_ids": [10], "include_gallery": True},
    ).json()["data"]
    assert preview["candidate_count"] == 2

    resp = client.post(
        "/api/v1/maintenance/people/delete",
        json={
            "preview_id": preview["preview_id"],
            "confirm_token": preview["confirm_token"],
            "candidate_hash": preview["candidate_hash"],
            "reason": "test",
        },
    )
    assert resp.status_code == 200, resp.text
    assert repo.persons[10]["is_active"] is False
    assert repo.gallery[99]["is_active"] is False
    assert repo.gallery[99]["is_primary"] is False


def test_gallery_delete_preview_requires_selector(client: TestClient) -> None:
    preview = client.post(
        "/api/v1/maintenance/people/gallery-delete-preview",
        json={},
    ).json()["data"]
    assert preview["candidate_count"] == 0


def test_gallery_delete_execute_soft_deletes_only_gallery(
    client: TestClient,
    repo: PeopleMaintenanceRepo,
) -> None:
    preview = client.post(
        "/api/v1/maintenance/people/gallery-delete-preview",
        json={"gallery_embedding_ids": [99]},
    ).json()["data"]
    resp = client.post(
        "/api/v1/maintenance/people/gallery-delete",
        json={
            "preview_id": preview["preview_id"],
            "confirm_token": preview["confirm_token"],
            "candidate_hash": preview["candidate_hash"],
            "reason": "test",
        },
    )
    assert resp.status_code == 200, resp.text
    assert repo.persons[10]["is_active"] is True
    assert repo.gallery[99]["is_active"] is False
    assert repo.gallery[99]["is_primary"] is False
