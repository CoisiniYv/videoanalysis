"""Tests for Phase 3B API media fields enhancement."""

from __future__ import annotations

import sys
from pathlib import Path

API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from app.schemas.events import EventResponse


# ===========================================================================
# clip_url / snapshot_url (existing behavior preserved)
# ===========================================================================


def test_clip_url_and_snapshot_url():
    row = {
        "id": "ev-001",
        "source_event_id": "savant:cam:t:intrusion:1000",
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "track_id": "t_1",
        "clip_path": "/media/replay-sink-output/uuid/video.mkv",
        "snapshot_path": None,
        "payload": {"media": {
            "snapshot_status": "not_implemented",
            "clip_status": "ready",
            "recording_strategy": "savant_replay",
        }},
    }
    resp = EventResponse.from_db_row(row, media_base_url="/media")
    assert resp.clip_url == "/media/replay-sink-output/uuid/video.mkv"
    assert resp.snapshot_url is None


# ===========================================================================
# New media fields exposed
# ===========================================================================


def test_media_replay_job_id_exposed():
    row = {
        "id": "ev-010",
        "source_event_id": "savant:cam:t:intrusion:10000",
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "track_id": "t_10",
        "clip_path": "/media/clips/clip.mkv",
        "snapshot_path": None,
        "payload": {"media": {
            "clip_status": "ready",
            "snapshot_status": "not_implemented",
            "recording_strategy": "savant_replay",
            "replay_job_id": "job-xyz-123",
            "sink_output_path": "/media/replay-sink-output/dir-001",
        }},
    }
    resp = EventResponse.from_db_row(row)
    assert resp.media["replay_job_id"] == "job-xyz-123"
    assert resp.media["sink_output_path"] == "/media/replay-sink-output/dir-001"
    assert resp.media["clip_status"] == "ready"
    assert resp.media["snapshot_status"] == "not_implemented"
    assert resp.media["recording_strategy"] == "savant_replay"


def test_media_error_message_exposed():
    row = {
        "id": "ev-011",
        "source_event_id": "savant:cam:t:intrusion:11000",
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "track_id": "t_11",
        "clip_path": None,
        "snapshot_path": None,
        "payload": {"media": {
            "clip_status": "failed",
            "snapshot_status": "not_implemented",
            "recording_strategy": "savant_replay",
            "error_message": "no keyframe found for source_id=phase3b",
        }},
    }
    resp = EventResponse.from_db_row(row)
    assert resp.media["clip_status"] == "failed"
    assert resp.media["error_message"] == "no keyframe found for source_id=phase3b"
    assert resp.clip_url is None


def test_media_fields_default_to_none_when_missing():
    """When payload.media is missing certain fields, they appear as None from fallback."""
    row = {
        "id": "ev-012",
        "source_event_id": "savant:cam:t:intrusion:12000",
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "track_id": "t_12",
        "clip_path": None,
        "snapshot_path": None,
        "payload": {"media": {
            "clip_status": "pending",
        }},
    }
    resp = EventResponse.from_db_row(row)
    assert resp.media["replay_job_id"] is None
    assert resp.media["sink_output_path"] is None
    assert resp.media["error_message"] is None
    # But explicitly set fields are preserved
    assert resp.media["clip_status"] == "pending"


def test_failed_status_preserved_in_response():
    """Failed media status is fully reflected in API response."""
    row = {
        "id": "ev-013",
        "source_event_id": "savant:cam:t:intrusion:13000",
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "track_id": "t_13",
        "clip_path": None,
        "snapshot_path": None,
        "payload": {"media": {
            "clip_status": "failed",
            "snapshot_status": "not_implemented",
            "recording_strategy": "savant_replay",
            "replay_job_id": "job-failed-001",
            "error_message": "Replay job creation returned None",
        }},
    }
    resp = EventResponse.from_db_row(row)
    assert resp.media["clip_status"] == "failed"
    assert resp.media["replay_job_id"] == "job-failed-001"
    assert resp.media["error_message"] == "Replay job creation returned None"
    assert resp.clip_url is None
