"""Tests for Phase 3B media-worker idempotency."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

MW_DIR = str(Path(__file__).resolve().parents[2] / "services" / "media-worker")
if MW_DIR not in sys.path:
    sys.path.insert(0, MW_DIR)

from app.worker import (
    _extract_event_id,
    _extract_uuid,
    _is_already_ready,
    _process_sink_output,
)


# ===========================================================================
# UUID extraction
# ===========================================================================


def test_extract_uuid_from_source_id():
    result = _extract_uuid("replay-event-019e5910-6c0e-7451-bab0-ba019495b968")
    assert result == "019e5910-6c0e-7451-bab0-ba019495b968"


def test_extract_uuid_from_resulting_stream_id():
    result = _extract_uuid("replay-event-abc12345-def6-7890-abcd-ef1234567890-00000000")
    assert result == "abc12345-def6-7890-abcd-ef1234567890"


def test_extract_uuid_returns_none_when_no_uuid():
    assert _extract_uuid("no-uuid-here") is None
    assert _extract_uuid("") is None


def test_extract_event_id_from_labels():
    meta = {"labels": {"event_id": "ev-labels-001"}}
    assert _extract_event_id(meta) == "ev-labels-001"


def test_extract_event_id_from_top_level():
    meta = {"event_id": "ev-top-001"}
    assert _extract_event_id(meta) == "ev-top-001"


def test_extract_event_id_from_source_id_uuid():
    meta = {"source_id": "replay-event-bbbb1111-cccc-2222-dddd-333333333333"}
    result = _extract_event_id(meta)
    assert result == "bbbb1111-cccc-2222-dddd-333333333333"


def test_extract_event_id_from_dirname():
    meta = {"_meta_dir": "/media/replay-sink-output/replay-event-ffff1111-eeee-4444-dddd-555555555555-00000000"}
    result = _extract_event_id(meta)
    assert result == "ffff1111-eeee-4444-dddd-555555555555"


# ===========================================================================
# _is_already_ready
# ===========================================================================


def test_is_already_ready_true():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ("ready",)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    assert _is_already_ready(mock_conn, "ev-001") is True


def test_is_already_ready_false():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ("replay_job_created",)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    assert _is_already_ready(mock_conn, "ev-002") is False


def test_is_already_ready_none_row():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    assert _is_already_ready(mock_conn, "ev-003") is False


# ===========================================================================
# Duplicate processing idempotency
# ===========================================================================


def test_processed_dirs_prevents_reprocessing():
    """After processing a dir, it should be in processed_dirs and skipped."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    processed: set[str] = set()

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a minimal sink output structure
        meta_dir = os.path.join(tmpdir, "replay-event-uuid1-00000000")
        os.makedirs(meta_dir, exist_ok=True)

        # Write NDJSON metadata with event_id label
        meta = {
            "source_id": "test_source",
            "job_id": "job-001",
            "resulting_stream_id": "replay-event-uuid1",
            "labels": {"event_id": "ev-dedup-001"},
        }
        with open(os.path.join(meta_dir, "metadata.json"), "w") as f:
            f.write(json.dumps(meta) + "\n")

        # Write a dummy video file
        with open(os.path.join(meta_dir, "video.mkv"), "w") as f:
            f.write("dummy")

        # First pass: should process
        updated = _process_sink_output(mock_conn, tmpdir, processed)
        assert updated == 1
        assert meta_dir in processed

        # Second pass: should skip because dir is in processed_dirs
        updated2 = _process_sink_output(mock_conn, tmpdir, processed)
        assert updated2 == 0


def test_already_ready_events_skipped():
    """When event is already ready, processing skips it."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ("ready",)  # already ready
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    processed: set[str] = set()

    with tempfile.TemporaryDirectory() as tmpdir:
        meta_dir = os.path.join(tmpdir, "replay-event-uuid2-00000000")
        os.makedirs(meta_dir, exist_ok=True)

        meta = {
            "source_id": "test_source",
            "labels": {"event_id": "ev-ready-001"},
        }
        with open(os.path.join(meta_dir, "metadata.json"), "w") as f:
            f.write(json.dumps(meta) + "\n")

        with open(os.path.join(meta_dir, "video.mkv"), "w") as f:
            f.write("dummy")

        updated = _process_sink_output(mock_conn, tmpdir, processed)
        # Should be 0 because is_already_ready returned True
        assert updated == 0
        # But dir should still be marked as processed
        assert meta_dir in processed
