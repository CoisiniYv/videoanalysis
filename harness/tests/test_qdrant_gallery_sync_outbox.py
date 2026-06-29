"""Static and SQL-contract tests for Qdrant gallery sync outbox."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db" / "migrations" / "021_qdrant_gallery_sync_outbox.sql"
SYNC_SCRIPT = ROOT / "services" / "face-worker" / "sync_qdrant_gallery.py"


def test_outbox_migration_is_additive_and_triggered():
    text = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS gallery_vector_sync_outbox" in text
    assert "DROP TABLE" not in text
    assert "TRUNCATE" not in text
    assert "FOR EACH ROW" in text
    assert "AFTER INSERT OR UPDATE OR DELETE" in text
    assert "AFTER UPDATE OF is_active, name, external_person_id" in text
    assert "gallery_vector_sync_enqueue" in text
    assert "person_gallery_embeddings_" in text
    assert "persons_update" in text


def test_sync_script_supports_required_modes_and_skip_locked_claim():
    text = SYNC_SCRIPT.read_text(encoding="utf-8")
    helper = (ROOT / "services" / "face-worker" / "app" / "gallery_sync_outbox.py").read_text(
        encoding="utf-8"
    )

    for mode in ("bootstrap", "drain-outbox", "reconcile", "rebuild", "status"):
        assert f'"{mode}"' in text
    assert "FOR UPDATE SKIP LOCKED" in helper
    assert "status IN ('pending', 'retry')" in helper
    assert "poisoned" in helper
    assert "HnswConfigDiff" in text
    assert "OptimizersConfigDiff" in text
    assert "indexing_threshold" in text
    assert "full_scan_threshold" in text
    assert "update_collection" in text


def test_outbox_status_summary_uses_valid_aggregate_filters():
    helper = (ROOT / "services" / "face-worker" / "app" / "gallery_sync_outbox.py").read_text(
        encoding="utf-8"
    )

    assert "count(*) FILTER (WHERE status IN ('pending', 'retry', 'processing'))" in helper
    assert "min(created_at)\n                            FILTER" in helper
    assert "max(processed_at)\n                            FILTER" in helper
    assert "EXTRACT(EPOCH FROM (now() - min(created_at)))\n                    FILTER" not in helper
