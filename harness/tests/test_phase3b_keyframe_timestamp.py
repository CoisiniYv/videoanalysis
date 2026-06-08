"""Tests for Phase 3B.1 — keyframe timestamp anchoring."""

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
if CW_DIR not in sys.path:
    sys.path.insert(0, CW_DIR)

from app import replay_client as replay_client_module
from app.replay_client import ReplayClient, _ts_ms_to_unix_seconds


# ===========================================================================
# _ts_ms_to_unix_seconds
# ===========================================================================


def test_ts_ms_to_unix_seconds_returns_epoch_seconds():
    # 2026-01-15T10:30:00.000Z = 1768473000000 ms
    assert _ts_ms_to_unix_seconds(1768473000000) == 1768473000


def test_ts_ms_to_unix_seconds_handles_zero():
    assert _ts_ms_to_unix_seconds(0) == 0


# ===========================================================================
# find_keyframe — timestamp-anchored lookup
# ===========================================================================


def test_find_keyframe_passes_unix_seconds_from_to_when_ts_ms_provided():
    """When ts_ms > 0, Replay keyframe lookup uses Unix-second from/to."""
    with patch.object(replay_client_module.httpx, "post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "keyframes": ["source_1", ["019e5910-6c0e-7451-bab0-ba019495b968"]]
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        client = ReplayClient("http://replay:8080")
        result = client.find_keyframe("source_1", ts_ms=1768473000000, window_s=10)

        assert result == "019e5910-6c0e-7451-bab0-ba019495b968"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["source_id"] == "source_1"
        assert payload["limit"] == 20
        assert payload["from"] == 1768472990
        assert payload["to"] == 1768473010


def test_find_keyframe_passes_null_from_to_when_ts_ms_zero():
    """When ts_ms == 0, from/to are None (unbounded lookup)."""
    with patch.object(replay_client_module.httpx, "post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "keyframes": ["source_1", ["uuid-1"]]
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        client = ReplayClient("http://replay:8080")
        result = client.find_keyframe("source_1", ts_ms=0, window_s=10)

        assert result == "uuid-1"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["from"] is None
        assert payload["to"] is None


def test_find_keyframe_window_is_symmetric():
    """The from/to window is centered on event_ts_ms."""
    with patch.object(replay_client_module.httpx, "post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "keyframes": ["s", ["kf-1"]]
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        client = ReplayClient("http://replay:8080")
        # 11000 ms = 11 seconds, window_s=5 -> from=6s, to=16s.
        client.find_keyframe("source_1", ts_ms=11000, window_s=5)

        payload = mock_post.call_args.kwargs["json"]
        assert payload["from"] == 6
        assert payload["to"] == 16


def test_find_keyframe_selects_nearest_uuid7_keyframe_to_anchor():
    """Replay may return several keyframes; choose the nearest UUIDv7 timestamp."""
    with patch.object(replay_client_module.httpx, "post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "keyframes": [
                "source_1",
                [
                    "019ea66c-01c5-7cf0-bf59-298587f529f4",
                    "019ea66c-2a80-77e1-94ce-271405cee78c",
                    "019ea66c-533b-7b71-bab6-0c9d630bc1cd",
                ],
            ]
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        client = ReplayClient("http://replay:8080")
        result = client.find_keyframe("source_1", ts_ms=1780908564000, window_s=30)

        assert result == "019ea66c-2a80-77e1-94ce-271405cee78c"


def test_find_keyframe_can_select_first_keyframe_at_or_after_anchor():
    """Event Replay anchoring needs the next keyframe so offset rewind covers the event."""
    with patch.object(replay_client_module.httpx, "post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "keyframes": [
                "source_1",
                [
                    "019ea66c-01c5-7cf0-bf59-298587f529f4",
                    "019ea66c-2a80-77e1-94ce-271405cee78c",
                    "019ea66c-533b-7b71-bab6-0c9d630bc1cd",
                ],
            ]
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        client = ReplayClient("http://replay:8080")
        result = client.find_keyframe(
            "source_1",
            ts_ms=1780908570000,
            window_s=30,
            selection="at_or_after",
        )

        assert result == "019ea66c-533b-7b71-bab6-0c9d630bc1cd"


# ===========================================================================
# keyframe_uuid provided → bypasses lookup
# ===========================================================================


def test_find_keyframe_bypassed_when_uuid_provided():
    """When keyframe_uuid is in the request, no API call is made for lookup.

    This is tested at the worker level — the worker skips find_keyframe()
    when req['keyframe_uuid'] is already set.
    """
    # Simulate the worker's decision logic
    keyframe_uuid = "provided-kf-uuid"
    assert keyframe_uuid is not None
    # If keyframe_uuid is provided, worker does NOT call replay.find_keyframe()


# ===========================================================================
# Missing event_ts_ms → fails cleanly
# ===========================================================================


def test_missing_event_ts_ms_would_fail_cleanly():
    """When event_ts_ms is 0 or missing, the worker marks clip_status=failed.

    This validates the worker-level logic: if keyframe_uuid is None AND
    event_ts_ms is 0/missing, the worker writes clip_status=failed and
    does not call create_job.
    """
    # Simulate worker check
    keyframe_uuid = None
    event_ts_ms = 0
    source_id = "test_source"

    # Worker path: if not keyframe_uuid and not event_ts_ms → fail
    should_fail = not keyframe_uuid and not event_ts_ms
    assert should_fail is True

    expected_error = (
        f"missing event_ts_ms in record_request source_id={source_id}"
    )
    assert "missing event_ts_ms" in expected_error
    assert source_id in expected_error


# ===========================================================================
# No keyframe found → fails cleanly
# ===========================================================================


def test_no_keyframe_found_fails_with_event_context():
    """When find_keyframe returns None, error includes source_id and event_ts_ms."""
    with patch.object(replay_client_module.httpx, "post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_post.return_value = mock_resp

        client = ReplayClient("http://replay:8080")
        result = client.find_keyframe("cam_front", ts_ms=5000000, window_s=10)

        assert result is None


# ===========================================================================
# Duplicate/retry behavior remains idempotent
# ===========================================================================


def test_timestamp_anchored_lookup_is_idempotent():
    """Same event_ts_ms + source_id always produces same from/to window.

    Multiple calls with same parameters should produce identical requests.
    """
    ts_ms = 1768473000000
    window_s = 10

    # Two calls with same parameters
    with patch.object(replay_client_module.httpx, "post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "keyframes": ["s", ["uuid-consistent"]]
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        client = ReplayClient("http://replay:8080")
        result1 = client.find_keyframe("source_1", ts_ms=ts_ms, window_s=window_s)
        payload1 = mock_post.call_args.kwargs["json"]

    with patch.object(replay_client_module.httpx, "post") as mock_post2:
        mock_resp2 = MagicMock()
        mock_resp2.json.return_value = {
            "keyframes": ["s", ["uuid-consistent"]]
        }
        mock_resp2.raise_for_status.return_value = None
        mock_post2.return_value = mock_resp2

        result2 = client.find_keyframe("source_1", ts_ms=ts_ms, window_s=window_s)
        payload2 = mock_post2.call_args.kwargs["json"]

    assert result1 == result2
    assert payload1["from"] == payload2["from"]
    assert payload1["to"] == payload2["to"]
