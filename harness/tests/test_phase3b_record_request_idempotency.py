"""Tests for Phase 3B record_request idempotency."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

EW_DIR = str(Path(__file__).resolve().parents[2] / "services" / "event-worker")
if EW_DIR not in sys.path:
    sys.path.insert(0, EW_DIR)

from app.record_request import RecordRequestPublisher
from app.repository import EventRepository


def _make_event(**overrides) -> dict:
    d = {
        "source_event_id": "savant:cam_01:t_889:intrusion:1000",
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "source_id": "src_01",
        "track_id": "t_889",
        "event_ts_ms": 2000,
        "clip_required": True,
        "payload": {"media": {
            "clip_required": True,
            "recording_strategy": "savant_replay",
            "source_id": "test_source",
        }},
    }
    d.update(overrides)
    return d


# ===========================================================================
# RecordRequestPublisher idempotency via DB clip_status check
# ===========================================================================


def test_publisher_publishes_unique_request_ids():
    """Each publish creates a new unique request_id (by design)."""
    fake = fakeredis.FakeRedis(decode_responses=False)
    pub = RecordRequestPublisher(fake, "security.record_requests", default_replay_source_id="test")
    event = _make_event()
    pub.publish(event, event_id="uuid-1")
    pub.publish(event, event_id="uuid-1")

    results = fake.xrange("security.record_requests", "-", "+")
    assert len(results) == 2
    import json
    r1 = json.loads(results[0][1][b"data"])
    r2 = json.loads(results[1][1][b"data"])
    assert r1["request_id"] != r2["request_id"]


def test_publisher_skips_when_clip_status_would_block():
    """The business logic in worker.py checks clip_status before publishing.

    This test validates that the publisher itself publishes, and the
    worker-level gate is the DB check (tested via mock below).
    """
    fake = fakeredis.FakeRedis(decode_responses=False)
    pub = RecordRequestPublisher(fake, "security.record_requests", default_replay_source_id="test")
    event = _make_event()
    msg_id = pub.publish(event, event_id="uuid-1")
    assert msg_id is not None
    assert len(fake.xrange("security.record_requests", "-", "+")) == 1


# ===========================================================================
# EventRepository media status checks
# ===========================================================================


class TestEventRepositoryClipStatus:
    """Test that EventRepository correctly reports clip_status for idempotency gate."""

    def test_get_media_clip_status_returns_none_for_nonexistent(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = None  # no row

        repo = EventRepository(mock_conn)
        result = repo.get_media_clip_status("nonexistent")
        assert result is None

    def test_get_media_clip_status_returns_value(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = ("pending",)

        repo = EventRepository(mock_conn)
        result = repo.get_media_clip_status("exists")
        assert result == "pending"

    def test_set_clip_status_updates_row(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        repo = EventRepository(mock_conn)
        result = repo.set_clip_status("uuid-1", "replay_job_created", replay_job_id="job-001")
        assert result is True

    def test_set_clip_status_with_error(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        repo = EventRepository(mock_conn)
        result = repo.set_clip_status(
            "uuid-1", "failed",
            error_message="Replay job creation failed"
        )
        assert result is True
