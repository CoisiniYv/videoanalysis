"""R3.1A behavior event evidence MVP contract checks."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_evidence_api_route_exists() -> None:
    router = _read("services/api/app/routers/events.py")
    assert '@router.get("/{event_id}/evidence")' in router
    assert "repo.list_evidence_tasks(event_id)" in router
    assert "EventEvidenceResponse.from_event_and_tasks" in router


def test_event_evidence_response_schema_has_media_fields() -> None:
    schema = _read("services/api/app/schemas/events.py")
    for field in (
        "media_status",
        "snapshot_path",
        "clip_path",
        "metadata_path",
        "snapshot_url",
        "clip_url",
        "metadata_url",
        "error_message",
    ):
        assert field in schema


def test_evidence_task_lifecycle_statuses_are_supported() -> None:
    repo = _read("services/event-worker/app/repository.py")
    for status in (
        '"pending"',
        '"processing"',
        '"ready"',
        '"failed"',
        '"not_implemented"',
    ):
        assert status in repo


def test_event_worker_creates_evidence_task_for_required_media() -> None:
    worker = _read("services/event-worker/app/worker.py")
    assert "_requires_evidence" in worker
    assert "snapshot_required" in worker
    assert "clip_required" in worker
    assert "create_evidence_task" in worker


def test_media_failure_does_not_block_event_insert_contract() -> None:
    worker = _read("services/event-worker/app/worker.py")
    assert "evidence_task creation failed" in worker
    assert "logger.exception" in worker
    assert "consumer.ack" in worker


def test_migration_adds_r3_1a_evidence_runtime_fields() -> None:
    migration = _read("db/migrations/009_r3_1a_evidence_lifecycle.sql")
    for field in (
        "metadata_path",
        "output_root",
        "storage_fallback_used",
        "retry_count",
        "claimed_by",
        "claimed_at",
    ):
        assert field in migration


def test_docs_declare_r3_1a_scope_limits() -> None:
    doc = _read("docs/r3_1a_behavior_event_evidence_mvp.md")
    assert "Intrusion is the first" in doc
    assert "does not implement new algorithm logic" in doc
    assert "does not run performance" in doc
    assert "does not change the Savant pipeline" in doc
    assert "not_implemented" in doc


def test_docs_declare_r3_1a_media_policy() -> None:
    doc = _read("docs/r3_1a_behavior_event_evidence_mvp.md")
    assert "raw_clip.mp4" in doc
    assert "canonical evidence video" in doc
    assert "annotated_clip.mp4" in doc
    assert "optional/on-demand" in doc
    assert "must not generate two video files per event" in doc
    assert "render dynamic overlays" in doc
    assert "Annotated video generation is deferred" in doc
