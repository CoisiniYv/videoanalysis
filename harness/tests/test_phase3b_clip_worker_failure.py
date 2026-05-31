"""Tests for Phase 3B clip-worker failure status handling."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

CW_DIR = str(Path(__file__).resolve().parents[2] / "services" / "clip-worker")
for name in list(sys.modules):
    if name == "app" or name.startswith("app."):
        del sys.modules[name]
if CW_DIR in sys.path:
    sys.path.remove(CW_DIR)
sys.path.insert(0, CW_DIR)

from app import replay_client as replay_client_module
from app.replay_client import ReplayClient
from app.repository import update_clip_status


# ===========================================================================
# ReplayClient failure paths
# ===========================================================================


def test_replay_client_find_keyframe_returns_none_on_error():
    with patch.object(
        replay_client_module.httpx, "post", side_effect=Exception("network error")
    ):
        client = ReplayClient("http://replay:8080")
        result = client.find_keyframe("source_1", ts_ms=5000)
        assert result is None


def test_replay_client_create_job_returns_none_on_error():
    with patch.object(
        replay_client_module.httpx, "put", side_effect=Exception("timeout")
    ):
        client = ReplayClient("http://replay:8080")
        result = client.create_job(
            source_id="source_1",
            keyframe_uuid="kf-abc",
            pre_seconds=5,
            post_seconds=5,
            sink_endpoint="dealer+connect:tcp://sink:6666",
            labels={"event_id": "ev-001"},
        )
        assert result is None


def test_replay_client_find_keyframe_404_no_crash():
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    with patch.object(replay_client_module.httpx, "post", return_value=mock_resp):
        client = ReplayClient("http://replay:8080")
        result = client.find_keyframe("source_1")
        assert result is None


# ===========================================================================
# update_clip_status failure handling
# ===========================================================================


def test_update_clip_status_success():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    result = update_clip_status(mock_conn, "ev-001", "replay_job_created", replay_job_id="job-abc")
    assert result is True


def test_update_clip_status_with_error_message():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    result = update_clip_status(
        mock_conn, "ev-002", "failed",
        error_message="no keyframe found for source_id=test"
    )
    assert result is True


def test_update_clip_status_empty_event_id():
    mock_conn = MagicMock()
    result = update_clip_status(mock_conn, "", "failed")
    assert result is False


def test_update_clip_status_db_error():
    mock_conn = MagicMock()
    mock_conn.cursor.side_effect = Exception("connection lost")

    result = update_clip_status(mock_conn, "ev-003", "failed", error_message="db error test")
    assert result is False
