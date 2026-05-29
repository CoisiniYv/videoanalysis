"""R3 EvidenceTask and event-worker contract checks."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_DIR = str(ROOT / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.schemas.algorithms import EvidenceTask


def test_evidence_task_schema_has_required_fields():
    fields = set(EvidenceTask.model_fields)
    expected = {
        "task_id",
        "event_id",
        "source_event_id",
        "camera_id",
        "source_id",
        "event_type",
        "event_ts_ms",
        "snapshot_required",
        "clip_required",
        "pre_seconds",
        "post_seconds",
        "status",
        "snapshot_path",
        "clip_path",
        "error_message",
        "created_at",
        "updated_at",
    }
    assert expected <= fields


def test_evidence_task_accepts_snapshot_and_clip_policy():
    task = EvidenceTask(
        task_id="task-1",
        event_id=123,
        source_event_id="savant:cam:t:intrusion:1000",
        camera_id="cam_001",
        source_id="source_001",
        event_type="intrusion",
        event_ts_ms=1000,
        snapshot_required=True,
        clip_required=True,
        pre_seconds=5,
        post_seconds=10,
    )
    assert task.snapshot_required is True
    assert task.clip_required is True
    assert task.status == "pending"


def test_event_worker_uses_one_evidence_path_for_all_event_types():
    worker_source = (ROOT / "services" / "event-worker" / "app" / "worker.py").read_text()
    assert "create_evidence_task" in worker_source
    assert "snapshot_required" in worker_source
    assert "clip_required" in worker_source
    assert "if event.get(\"event_type\")" not in worker_source
    assert "elif event.get(\"event_type\")" not in worker_source


def test_database_migration_declares_evidence_tasks():
    migration = (
        ROOT / "db" / "migrations" / "008_r3_unified_event_evidence_algorithm_rules.sql"
    ).read_text()
    assert "CREATE TABLE IF NOT EXISTS evidence_tasks" in migration
    assert "snapshot_required" in migration
    assert "clip_required" in migration
    assert "source_event_id" in migration
