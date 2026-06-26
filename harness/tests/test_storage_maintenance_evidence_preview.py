from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.schemas.maintenance import EvidenceDeletePreviewRequest  # noqa: E402
from app.services.storage_maintenance import (  # noqa: E402
    MaintenanceSettings,
    StorageMaintenanceService,
    candidate_hash_from_rows,
)


NOW = datetime(2026, 6, 10, 4, 0, 0, tzinfo=timezone.utc)


class FakeRepo:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.items: dict[str, list[dict[str, Any]]] = {}
        self.next_job = 1
        self.next_item = 1
        self.records: dict[str, dict[str, Any]] = {}
        self.task_status_updates: list[str] = []
        self.bundle_status_updates: list[str] = []

    def create_job(self, **kwargs: Any) -> dict[str, Any]:
        job_id = f"00000000-0000-4000-8000-{self.next_job:012d}"
        self.next_job += 1
        row = {"id": job_id, "created_at": NOW, **kwargs}
        self.jobs[job_id] = row
        return row

    def add_items(self, job_id: str, items: list[dict[str, Any]]) -> None:
        rows = []
        for item in items:
            row = dict(item)
            row["id"] = self.next_item
            self.next_item += 1
            row["job_id"] = job_id
            rows.append(row)
        self.items[job_id] = rows

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get(job_id)

    def update_job(self, job_id: str, **kwargs: Any) -> None:
        job = self.jobs[job_id]
        job.update({k: v for k, v in kwargs.items() if v is not None})

    def list_items(self, job_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        rows = list(self.items.get(job_id, []))
        status = kwargs.get("status")
        if status:
            rows = [row for row in rows if row["status"] == status]
        limit = kwargs.get("limit")
        offset = kwargs.get("offset", 0)
        if limit is not None:
            rows = rows[offset : offset + limit]
        return [dict(row) for row in rows]

    def count_items(self, job_id: str, **kwargs: Any) -> int:
        return len(self.list_items(job_id, **kwargs))

    def item_status_counts(self, job_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.items[job_id]:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts

    def update_item(self, item_id: int, **kwargs: Any) -> None:
        for rows in self.items.values():
            for row in rows:
                if row["id"] == item_id:
                    row.update({k: v for k, v in kwargs.items() if v is not None})
                    return

    def get_evidence_record(self, target_id: str) -> dict[str, Any] | None:
        return self.records.get(target_id)

    def update_event_media_deleted(self, **kwargs: Any) -> None:
        record = next(row for row in self.records.values() if str(row["event_id"]) == kwargs["event_id"])
        record["media_status"] = kwargs["media_status"]

    def update_evidence_bundle_media_deleted(self, **kwargs: Any) -> None:
        self.bundle_status_updates.append(
            f"{kwargs['event_id']}:{kwargs['job_id']}:{kwargs.get('media_status', 'media_deleted')}"
        )

    def mark_evidence_tasks_deleted_metadata(self, *, event_id: str, job_id: str) -> None:
        self.task_status_updates.append(f"{event_id}:{job_id}:error_message_only")

    def gallery_image_references(self) -> list[dict[str, Any]]:
        return []


def _settings(tmp_path: Path, *, guard: int = 0) -> MaintenanceSettings:
    media = tmp_path / "media"
    return MaintenanceSettings(
        media_root=media,
        evidence_root=media / "evidence",
        face_upload_root=media / "face_uploads",
        face_registration_root=media / "face_registration",
        artifacts_root=tmp_path / "artifacts",
        active_write_guard_seconds=guard,
        confirm_secret="test-secret",
    )


def _bundle(
    root: Path,
    event_id: str,
    *,
    event_type: str = "watchlist_hit",
    camera_id: str = "cam-1",
    start_ts: str | None = "2026-06-01T00:00:00+00:00",
) -> Path:
    bundle = root / event_id
    bundle.mkdir(parents=True)
    event = {
        "event_id": event_id,
        "event_type": event_type,
        "camera_id": camera_id,
        "source_id": "primary_rtsp",
    }
    if start_ts is not None:
        event["start_ts"] = start_ts
    (bundle / "metadata.json").write_text(
        json.dumps(
            {
                "event": event,
                "status": {"clip_status": "ready"},
            }
        ),
        encoding="utf-8",
    )
    (bundle / "raw_clip.mp4").write_bytes(b"video")
    return bundle


def test_preview_scans_only_flat_evidence_root_and_freezes_items(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = FakeRepo()
    _bundle(settings.evidence_root, "event-1")
    nested = settings.evidence_root / "events" / "legacy-event"
    nested.mkdir(parents=True)
    (nested / "raw_clip.mp4").write_bytes(b"legacy")

    service = StorageMaintenanceService(repo, settings)
    preview = service.create_evidence_delete_preview(EvidenceDeletePreviewRequest())

    assert preview["candidate_count"] == 1
    assert preview["deletable_count"] == 1
    item = repo.items[preview["preview_id"]][0]
    assert item["target_id"] == "event-1"
    assert item["relative_path"] == "event-1"
    assert repo.jobs[preview["preview_id"]]["candidate_hash"] == candidate_hash_from_rows(repo.items[preview["preview_id"]])


def test_explicit_events_directory_is_not_treated_as_bundle(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = FakeRepo()
    legacy = settings.evidence_root / "events"
    legacy.mkdir(parents=True)
    (legacy / "legacy-event").mkdir()

    service = StorageMaintenanceService(repo, settings)
    preview = service.create_evidence_delete_preview(EvidenceDeletePreviewRequest(event_ids=["events"]))

    assert preview["candidate_count"] == 1
    assert preview["deletable_count"] == 0
    assert preview["skipped"][0]["reason"] == "path_unsafe:reserved_legacy_events_root"
    assert legacy.exists()


def test_preview_skips_pending_task_and_does_not_delete_files(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = FakeRepo()
    _bundle(settings.evidence_root, "event-1")
    repo.records["event-1"] = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "source_event_id": "event-1",
        "media_status": "ready",
        "evidence_task_status": "pending",
        "task_id": "task-1",
    }

    service = StorageMaintenanceService(repo, settings)
    preview = service.create_evidence_delete_preview(EvidenceDeletePreviewRequest(event_ids=["event-1"]))

    assert preview["deletable_count"] == 0
    assert preview["skipped"][0]["reason"] == "task_pending"
    assert (settings.evidence_root / "event-1").exists()


def test_time_range_preview_uses_event_time_not_bundle_mtime(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = FakeRepo()
    _bundle(settings.evidence_root, "event-old", start_ts="2026-06-01T00:00:00+00:00")
    _bundle(settings.evidence_root, "event-new", start_ts="2026-06-10T00:00:00+00:00")

    service = StorageMaintenanceService(repo, settings)
    preview = service.create_evidence_delete_preview(
        EvidenceDeletePreviewRequest(
            time_from=datetime(2026, 6, 9, tzinfo=timezone.utc),
            time_to=datetime(2026, 6, 11, tzinfo=timezone.utc),
        )
    )

    assert preview["candidate_count"] == 1
    assert repo.items[preview["preview_id"]][0]["target_id"] == "event-new"
    assert repo.items[preview["preview_id"]][0]["item_payload"]["time_source"] == "metadata.event.start_ts"


def test_time_range_preview_skips_bundle_without_event_time(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = FakeRepo()
    _bundle(settings.evidence_root, "event-missing-time", start_ts=None)

    service = StorageMaintenanceService(repo, settings)
    preview = service.create_evidence_delete_preview(
        EvidenceDeletePreviewRequest(
            time_from=datetime(2026, 6, 9, tzinfo=timezone.utc),
            time_to=datetime(2026, 6, 11, tzinfo=timezone.utc),
        )
    )

    assert preview["candidate_count"] == 1
    assert preview["deletable_count"] == 0
    assert preview["skipped"][0]["target_id"] == "event-missing-time"
    assert preview["skipped"][0]["reason"] == "missing_event_time_for_range"


def test_preview_can_include_stale_pending_task_when_requested(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = FakeRepo()
    _bundle(settings.evidence_root, "event-1")
    repo.records["event-1"] = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "source_event_id": "event-1",
        "media_status": "generated_unverified",
        "evidence_task_status": "pending",
        "task_id": "task-1",
    }

    service = StorageMaintenanceService(repo, settings)
    preview = service.create_evidence_delete_preview(
        EvidenceDeletePreviewRequest(
            event_ids=["event-1"],
            allow_stale_pending_tasks=True,
        )
    )

    assert preview["deletable_count"] == 1
    assert preview["skipped_count"] == 0


def test_execute_allows_stale_pending_task_only_when_preview_requested_it(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = FakeRepo()
    _bundle(settings.evidence_root, "event-1")
    repo.records["event-1"] = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "source_event_id": "event-1",
        "media_status": "generated_unverified",
        "evidence_task_status": "pending",
        "task_id": "task-1",
    }

    service = StorageMaintenanceService(repo, settings)
    preview = service.create_evidence_delete_preview(
        EvidenceDeletePreviewRequest(
            event_ids=["event-1"],
            allow_stale_pending_tasks=True,
        )
    )
    result = service.execute_job(
        preview_id=preview["preview_id"],
        confirm_token=preview["confirm_token"],
        delete_mode="trash",
        reason="test",
        operator="operator",
        requested_candidate_hash=preview["candidate_hash"],
    )

    assert result["status"] == "completed"
    assert not (settings.evidence_root / "event-1").exists()
    assert repo.records["event-1"]["media_status"] == "media_deleted"
    assert repo.records["event-1"]["evidence_task_status"] == "pending"
    assert repo.task_status_updates == [
        "11111111-1111-4111-8111-111111111111:" + preview["preview_id"] + ":error_message_only"
    ]


def test_execute_uses_frozen_preview_and_does_not_rescan_new_bundle(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = FakeRepo()
    _bundle(settings.evidence_root, "event-1")
    repo.records["event-1"] = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "source_event_id": "event-1",
        "media_status": "ready",
        "evidence_task_status": "ready",
        "task_id": "task-1",
    }
    service = StorageMaintenanceService(repo, settings)
    preview = service.create_evidence_delete_preview(EvidenceDeletePreviewRequest(event_ids=["event-1"]))
    _bundle(settings.evidence_root, "event-2")

    result = service.execute_job(
        preview_id=preview["preview_id"],
        confirm_token=preview["confirm_token"],
        delete_mode="trash",
        reason="test",
        operator="operator",
        requested_candidate_hash=preview["candidate_hash"],
    )

    assert result["status"] == "completed"
    assert not (settings.evidence_root / "event-1").exists()
    assert (settings.evidence_root / "event-2").exists()
    assert repo.records["event-1"]["media_status"] == "media_deleted"
    assert repo.task_status_updates == [
        "11111111-1111-4111-8111-111111111111:" + preview["preview_id"] + ":error_message_only"
    ]


def test_completed_item_is_idempotent_on_repeated_execute(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = FakeRepo()
    _bundle(settings.evidence_root, "event-1")
    repo.records["event-1"] = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "source_event_id": "event-1",
        "media_status": "ready",
        "evidence_task_status": "ready",
        "task_id": "task-1",
    }
    service = StorageMaintenanceService(repo, settings)
    preview = service.create_evidence_delete_preview(EvidenceDeletePreviewRequest(event_ids=["event-1"]))

    first = service.execute_job(
        preview_id=preview["preview_id"],
        confirm_token=preview["confirm_token"],
        delete_mode="trash",
        reason="test",
        operator="operator",
        requested_candidate_hash=preview["candidate_hash"],
    )
    second = service.execute_job(
        preview_id=preview["preview_id"],
        confirm_token=preview["confirm_token"],
        delete_mode="trash",
        reason="test",
        operator="operator",
        requested_candidate_hash=preview["candidate_hash"],
    )

    assert first["status_counts"]["completed"] == 1
    assert second["status_counts"]["already_completed"] == 1
    assert len(list((settings.media_root / ".trash" / "evidence" / preview["preview_id"]).iterdir())) == 1


def test_execute_rechecks_db_state_and_skips_active_task(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = FakeRepo()
    _bundle(settings.evidence_root, "event-1")
    repo.records["event-1"] = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "source_event_id": "event-1",
        "media_status": "ready",
        "evidence_task_status": "ready",
        "task_id": "task-1",
    }
    service = StorageMaintenanceService(repo, settings)
    preview = service.create_evidence_delete_preview(EvidenceDeletePreviewRequest(event_ids=["event-1"]))
    repo.records["event-1"]["evidence_task_status"] = "processing"

    result = service.execute_job(
        preview_id=preview["preview_id"],
        confirm_token=preview["confirm_token"],
        delete_mode="trash",
        reason="test",
        operator="operator",
        requested_candidate_hash=preview["candidate_hash"],
    )

    assert result["status_counts"]["skipped"] == 1
    assert repo.items[preview["preview_id"]][0]["skip_reason"] == "db_state_changed"
    assert (settings.evidence_root / "event-1").exists()


def test_execute_skips_stale_size_mtime(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = FakeRepo()
    bundle = _bundle(settings.evidence_root, "event-1")
    service = StorageMaintenanceService(repo, settings)
    preview = service.create_evidence_delete_preview(EvidenceDeletePreviewRequest(event_ids=["event-1"]))
    (bundle / "extra.txt").write_text("changed", encoding="utf-8")
    os.utime(bundle, None)

    result = service.execute_job(
        preview_id=preview["preview_id"],
        confirm_token=preview["confirm_token"],
        delete_mode="trash",
        reason="test",
        operator="operator",
        requested_candidate_hash=preview["candidate_hash"],
    )

    assert result["status_counts"]["skipped"] == 1
    assert repo.items[preview["preview_id"]][0]["skip_reason"] == "stale_preview_item"
    assert bundle.exists()


def test_execute_rejects_expired_preview_and_candidate_hash_mismatch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = FakeRepo()
    _bundle(settings.evidence_root, "event-1")
    service = StorageMaintenanceService(repo, settings)
    preview = service.create_evidence_delete_preview(EvidenceDeletePreviewRequest(event_ids=["event-1"]))

    with pytest.raises(Exception, match="candidate_hash mismatch"):
        service.execute_job(
            preview_id=preview["preview_id"],
            confirm_token=preview["confirm_token"],
            delete_mode="trash",
            reason="test",
            operator="operator",
            requested_candidate_hash="bad",
        )

    repo.jobs[preview["preview_id"]]["preview_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(Exception, match="preview expired"):
        service.execute_job(
            preview_id=preview["preview_id"],
            confirm_token=preview["confirm_token"],
            delete_mode="trash",
            reason="test",
            operator="operator",
            requested_candidate_hash=preview["candidate_hash"],
        )
