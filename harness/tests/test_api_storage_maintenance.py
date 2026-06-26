from __future__ import annotations

import json
import os
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


def _bundle(root: Path, event_id: str) -> None:
    bundle = root / event_id
    bundle.mkdir(parents=True)
    (bundle / "metadata.json").write_text(
        json.dumps(
            {
                "event": {
                    "event_id": event_id,
                    "event_type": "watchlist_hit",
                    "camera_id": "cam-1",
                    "source_id": "primary_rtsp",
                    "start_ts": "2026-06-01T00:00:00+00:00",
                },
                "status": {"clip_status": "ready"},
            }
        ),
        encoding="utf-8",
    )
    (bundle / "raw_clip.mp4").write_bytes(b"video")


@pytest.fixture
def repo() -> FakeRepo:
    return FakeRepo()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: FakeRepo):
    app.dependency_overrides.clear()
    media = tmp_path / "media"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("FACE_UPLOAD_ROOT", str(media / "face_uploads"))
    monkeypatch.setenv("FACE_REGISTRATION_ROOT", str(media / "face_registration"))
    monkeypatch.setenv("STORAGE_MAINTENANCE_ACTIVE_WRITE_GUARD_SECONDS", "0")
    monkeypatch.setenv("STORAGE_MAINTENANCE_CONFIRM_SECRET", "test-secret")
    monkeypatch.setenv("STORAGE_MAINTENANCE_SUMMARY_ENABLED", "true")
    monkeypatch.setenv("STORAGE_MAINTENANCE_PREVIEW_ENABLED", "true")
    monkeypatch.setenv("STORAGE_MAINTENANCE_EXECUTE_ENABLED", "false")
    monkeypatch.setenv("STORAGE_MAINTENANCE_EXECUTE_CONTROL_ENABLED", "true")
    monkeypatch.delenv("STORAGE_MAINTENANCE_EXECUTE_CONTROL_PATH", raising=False)

    def _override():
        yield repo

    app.dependency_overrides[maintenance_repo_dep] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_storage_summary_and_preview_are_enabled_by_default(client: TestClient, tmp_path: Path) -> None:
    media = Path(os.environ["MEDIA_ROOT"])
    _bundle(media / "evidence", "event-1")

    resp = client.get("/api/v1/maintenance/storage/summary")
    assert resp.status_code == 200, resp.text
    summary = resp.json()["data"]
    assert summary["evidence"]["layout"] == "flat_bundle"
    assert summary["contract"]["entrypoint"] == "8090"

    resp = client.post("/api/v1/maintenance/evidence/delete-preview", json={"event_ids": ["event-1"]})
    assert resp.status_code == 200, resp.text
    preview = resp.json()["data"]
    assert preview["candidate_count"] == 1
    assert preview["candidate_hash"]
    assert preview["preview_expires_at"]
    assert "删除后不会自动重新生成证据" in preview["no_auto_regenerate_message"]


def test_execute_disabled_rejects_real_delete(client: TestClient, tmp_path: Path) -> None:
    media = Path(os.environ["MEDIA_ROOT"])
    _bundle(media / "evidence", "event-1")
    preview = client.post("/api/v1/maintenance/evidence/delete-preview", json={"event_ids": ["event-1"]}).json()["data"]

    resp = client.post(
        "/api/v1/maintenance/evidence/delete",
        json={
            "preview_id": preview["preview_id"],
            "confirm_token": preview["confirm_token"],
            "candidate_hash": preview["candidate_hash"],
            "delete_mode": "trash",
            "reason": "test",
        },
    )
    assert resp.status_code == 403
    assert "execute disabled" in resp.json()["error"]["message"]
    assert (media / "evidence" / "event-1").exists()


def test_execution_control_enables_delete_without_api_restart(
    client: TestClient,
    repo: FakeRepo,
) -> None:
    media = Path(os.environ["MEDIA_ROOT"])
    _bundle(media / "evidence", "event-1")
    repo.records["event-1"] = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "source_event_id": "event-1",
        "media_status": "ready",
        "evidence_task_status": "ready",
        "task_id": "task-1",
        "start_ts": datetime(2026, 6, 1, tzinfo=timezone.utc),
    }
    preview = client.post("/api/v1/maintenance/evidence/delete-preview", json={"event_ids": ["event-1"]}).json()["data"]

    disabled = client.post(
        "/api/v1/maintenance/evidence/delete",
        json={
            "preview_id": preview["preview_id"],
            "confirm_token": preview["confirm_token"],
            "candidate_hash": preview["candidate_hash"],
            "delete_mode": "trash",
            "reason": "blocked before control toggle",
        },
    )
    assert disabled.status_code == 403

    control = client.patch(
        "/api/v1/maintenance/execution-control",
        json={
            "enabled": True,
            "reason": "operator controlled delete test",
            "operator": "tester",
        },
    )
    assert control.status_code == 200, control.text
    state = control.json()["data"]
    assert state["effective_enabled"] is True
    assert state["source"] == "runtime_control"

    summary = client.get("/api/v1/maintenance/storage/summary").json()["data"]
    assert summary["contract"]["execute_enabled"] is True
    assert summary["contract"]["execute_control"]["updated_by"] == "tester"

    resp = client.post(
        "/api/v1/maintenance/evidence/delete",
        json={
            "preview_id": preview["preview_id"],
            "confirm_token": preview["confirm_token"],
            "candidate_hash": preview["candidate_hash"],
            "delete_mode": "trash",
            "reason": "controlled delete",
        },
    )
    assert resp.status_code == 200, resp.text
    assert not (media / "evidence" / "event-1").exists()
    assert repo.bundle_status_updates


def test_execute_enabled_deletes_temp_bundle_without_changing_task_status(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    repo: FakeRepo,
) -> None:
    media = Path(os.environ["MEDIA_ROOT"])
    _bundle(media / "evidence", "event-1")
    repo.records["event-1"] = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "source_event_id": "event-1",
        "media_status": "ready",
        "evidence_task_status": "ready",
        "task_id": "task-1",
        "start_ts": datetime(2026, 6, 1, tzinfo=timezone.utc),
    }
    preview = client.post("/api/v1/maintenance/evidence/delete-preview", json={"event_ids": ["event-1"]}).json()["data"]

    monkeypatch.setenv("STORAGE_MAINTENANCE_EXECUTE_ENABLED", "true")
    resp = client.post(
        "/api/v1/maintenance/evidence/delete",
        json={
            "preview_id": preview["preview_id"],
            "confirm_token": preview["confirm_token"],
            "candidate_hash": preview["candidate_hash"],
            "delete_mode": "trash",
            "reason": "temporary bundle test",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "completed"
    assert not (media / "evidence" / "event-1").exists()
    assert repo.records["event-1"]["media_status"] == "media_deleted"
    assert repo.records["event-1"]["evidence_task_status"] == "ready"
    assert repo.task_status_updates
    assert repo.bundle_status_updates


def test_job_detail_includes_no_auto_regenerate_message(client: TestClient) -> None:
    media = Path(os.environ["MEDIA_ROOT"])
    _bundle(media / "evidence", "event-1")
    preview = client.post("/api/v1/maintenance/evidence/delete-preview", json={"event_ids": ["event-1"]}).json()["data"]

    resp = client.get(f"/api/v1/maintenance/jobs/{preview['preview_id']}")
    assert resp.status_code == 200, resp.text
    detail = resp.json()["data"]
    assert detail["job"]["candidate_hash"] == preview["candidate_hash"]
    assert "删除后不会自动重新生成证据" in detail["job"]["no_auto_regenerate_message"]
