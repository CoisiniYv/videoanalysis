"""Tests for Phase 3A ReplayClient."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

CW_DIR = str(Path(__file__).resolve().parents[2] / "services" / "clip-worker")
if CW_DIR not in sys.path:
    sys.path.insert(0, CW_DIR)

from app.replay_client import ReplayClient


def test_replay_client_status():
    with patch("app.replay_client.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "running", "buffer_seconds": 60}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        client = ReplayClient("http://replay:8080")
        result = client.status()
        assert result == {"status": "running", "buffer_seconds": 60}
        mock_get.assert_called_once_with(
            "http://replay:8080/api/v1/status", timeout=30.0
        )


def test_replay_client_find_keyframe():
    with patch("app.replay_client.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"keyframe_uuid": "kf-abc123"}]
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        client = ReplayClient("http://replay:8080")
        result = client.find_keyframe("source_1", ts_ms=5000)

        assert result == "kf-abc123"
        call_args = mock_post.call_args
        payload = call_args.kwargs["json"]
        assert payload["source_id"] == "source_1"
        assert payload["limit"] == 1


def test_replay_client_find_keyframe_404():
    with patch("app.replay_client.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_post.return_value = mock_resp

        client = ReplayClient("http://replay:8080")
        result = client.find_keyframe("source_1", ts_ms=5000)
        assert result is None


def test_replay_client_create_job():
    with patch("app.replay_client.httpx.put") as mock_put:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"job_id": "job-001"}
        mock_resp.raise_for_status.return_value = None
        mock_put.return_value = mock_resp

        client = ReplayClient("http://replay:8080")
        result = client.create_job(
            source_id="source_1",
            keyframe_uuid="kf-abc",
            pre_seconds=5,
            post_seconds=5,
            sink_endpoint="pub+connect:tcp://sink:6666",
            labels={"event_id": "ev-001"},
        )
        assert result == "job-001"

        # Verify payload structure
        call_args = mock_put.call_args
        payload = call_args.kwargs["json"]
        assert payload["source_id"] == "source_1"
        assert payload["anchor_keyframe"] == "kf-abc"
        assert payload["offset"]["seconds"] == 5
        assert payload["stop_condition"]["frame_count"] == 300  # (5+5)*30fps
        assert payload["sink"]["url"] == "pub+connect:tcp://sink:6666"
        assert payload["configuration"]["stored_stream_id"] == "source_1"
        assert payload["configuration"]["labels"]["event_id"] == "ev-001"


def test_replay_client_status_error():
    with patch("app.replay_client.httpx.get", side_effect=Exception("timeout")):
        client = ReplayClient("http://replay:8080")
        result = client.status()
        assert result is None
