"""Tests for Phase 3A API media URL fields."""

from __future__ import annotations

import sys
from pathlib import Path

API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from app.schemas.events import EventResponse


def test_clip_url_populated_when_clip_path_exists():
    row = {
        "id": "ev-001",
        "source_event_id": "savant:cam:t:intrusion:1000",
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "track_id": "t_1",
        "clip_path": "replay-sink-output/uuid/video.mkv",
        "snapshot_path": None,
        "payload": {"media": {
            "snapshot_status": "not_implemented",
            "clip_status": "ready",
            "recording_strategy": "savant_replay",
        }},
        "recording_strategy": "reserved",
        "media_status": "not_implemented",
    }
    resp = EventResponse.from_db_row(row, media_base_url="/media")
    assert resp.clip_url == "/media/replay-sink-output/uuid/video.mkv"
    assert resp.snapshot_url is None
    assert resp.clip_path == "replay-sink-output/uuid/video.mkv"


def test_both_urls_null_when_paths_null():
    row = {
        "id": "ev-002",
        "source_event_id": "savant:cam:t:intrusion:2000",
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "track_id": "t_2",
        "clip_path": None,
        "snapshot_path": None,
        "payload": {"media": {}},
        "recording_strategy": "reserved",
        "media_status": "not_implemented",
    }
    resp = EventResponse.from_db_row(row)
    assert resp.clip_url is None
    assert resp.snapshot_url is None


def test_media_fields_preserved_with_urls():
    row = {
        "id": "ev-003",
        "source_event_id": "savant:cam:t:intrusion:3000",
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "track_id": "t_3",
        "clip_path": "/media/clips/clip.mkv",
        "snapshot_path": None,
        "payload": {"media": {
            "snapshot_status": "not_implemented",
            "clip_status": "ready",
            "recording_strategy": "savant_replay",
            "replay_job_id": "job-001",
        }},
        "recording_strategy": "savant_replay",
        "media_status": "ready",
    }
    resp = EventResponse.from_db_row(row)
    assert resp.media["snapshot_status"] == "not_implemented"
    assert resp.media["clip_status"] == "ready"
    assert resp.media["recording_strategy"] == "savant_replay"
    assert resp.media["replay_job_id"] == "job-001"
    assert resp.clip_url is not None


def test_media_fallback_still_works_with_clip_path():
    """When payload.media is missing, fallback should still generate URLs."""
    row = {
        "id": "ev-004",
        "source_event_id": "savant:cam:t:intrusion:4000",
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "track_id": "t_4",
        "clip_path": "/media/clips/test.mkv",
        "snapshot_path": None,
        "payload": None,
        "recording_strategy": "reserved",
        "media_status": "not_implemented",
    }
    resp = EventResponse.from_db_row(row)
    assert resp.clip_url == "/media//media/clips/test.mkv"
    assert resp.media["snapshot_status"] == "not_implemented"
    assert resp.media["clip_status"] == "not_implemented"
