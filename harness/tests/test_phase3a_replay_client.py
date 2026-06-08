"""Tests for Phase 3A ReplayClient."""

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


def test_replay_client_status():
    with patch.object(replay_client_module.httpx, "get") as mock_get:
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
    with patch.object(replay_client_module.httpx, "post") as mock_post:
        mock_resp = MagicMock()
        # Response: {"keyframes": ["source_id", ["uuid1"]]}
        mock_resp.json.return_value = {"keyframes": ["source_1", ["019e5910-6c0e-7451-bab0-ba019495b968"]]}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        client = ReplayClient("http://replay:8080")
        result = client.find_keyframe("source_1", ts_ms=5000)

        assert result == "019e5910-6c0e-7451-bab0-ba019495b968"
        call_args = mock_post.call_args
        payload = call_args.kwargs["json"]
        assert payload["source_id"] == "source_1"
        assert payload["limit"] == 20


def test_replay_client_find_keyframe_404():
    with patch.object(replay_client_module.httpx, "post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_post.return_value = mock_resp

        client = ReplayClient("http://replay:8080")
        result = client.find_keyframe("source_1", ts_ms=5000)
        assert result is None


def test_replay_client_create_job():
    with patch.object(replay_client_module.httpx, "put") as mock_put:
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
            sink_endpoint="dealer+connect:tcp://sink:6666",
            labels={"event_id": "ev-001"},
        )
        assert result == "job-001"

        # Verify payload structure
        call_args = mock_put.call_args
        payload = call_args.kwargs["json"]
        assert payload["anchor_keyframe"] == "kf-abc"
        assert payload["offset"]["seconds"] == 5.0
        assert payload["stop_condition"]["frame_count"] == 300
        assert payload["sink"]["url"] == "dealer+connect:tcp://sink:6666"
        assert payload["sink"]["options"]["send_hwm"] == 10000
        cfg = payload["configuration"]
        assert cfg["ts_sync"] is True
        assert cfg["min_duration"] == {"secs": 0, "nanos": 33333333}
        assert cfg["max_duration"] == {"secs": 0, "nanos": 33333333}
        assert cfg["stored_stream_id"] == "source_1"
        assert cfg["resulting_stream_id"] == "replay-event-ev-001"
        assert cfg["send_metadata_only"] is False
        assert cfg["labels"]["event_id"] == "ev-001"


def test_replay_client_status_error():
    with patch.object(
        replay_client_module.httpx, "get", side_effect=Exception("timeout")
    ):
        client = ReplayClient("http://replay:8080")
        result = client.status()
        assert result is None
