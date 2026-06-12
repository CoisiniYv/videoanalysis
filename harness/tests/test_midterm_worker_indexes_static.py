from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db" / "migrations" / "014_worker_media_indexes.sql"
MEDIA_WORKER = ROOT / "services" / "media-worker" / "app" / "worker.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_media_worker_index_migration_targets_worker_query_shapes() -> None:
    migration = _text(MIGRATION)
    worker = _text(MEDIA_WORKER)

    assert "events_media_clip_status_with_clip_idx" in migration
    assert "events_media_snapshot_queue_idx" in migration
    assert "events_media_snapshot_not_required_idx" in migration
    assert "events_media_annotation_queue_idx" in migration

    assert "(payload -> 'media' ->> 'clip_status')" in migration
    assert "(payload -> 'media' ->> 'snapshot_status')" in migration
    assert "(payload -> 'media' ->> 'snapshot_required')" in migration
    assert "(payload -> 'media' ->> 'annotated_snapshot_status')" in migration

    assert "payload -> 'media' ->> 'clip_status'" in worker
    assert "payload -> 'media' ->> 'snapshot_status'" in worker
    assert "payload -> 'media' ->> 'snapshot_required'" in worker
    assert "payload -> 'media' ->> 'annotated_snapshot_status'" in worker


def test_media_worker_indexes_are_partial_and_additive() -> None:
    migration = _text(MIGRATION)

    assert "CREATE INDEX IF NOT EXISTS" in migration
    assert "ALTER TABLE" not in migration
    assert "DROP " not in migration
    assert "WHERE clip_path IS NOT NULL" in migration
    assert "AND clip_path <> ''" in migration
    assert "WHERE (payload -> 'media' ->> 'snapshot_status') = 'ready'" in migration
    assert "COMMENT ON INDEX events_media_snapshot_queue_idx" in migration
