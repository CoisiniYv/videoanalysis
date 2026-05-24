"""Tests for Phase 3A RecordRequestPublisher."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import fakeredis
import pytest

EW_DIR = str(Path(__file__).resolve().parents[2] / "services" / "event-worker")
if EW_DIR not in sys.path:
    sys.path.insert(0, EW_DIR)

from app.record_request import RecordRequestPublisher, _resolve_source_id


def _make_event(**overrides) -> dict:
    d = {
        "source_event_id": "savant:cam_01:t_889:intrusion:1000",
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "source_id": "src_01",
        "track_id": "t_889",
        "event_ts_ms": 2000,
        "frame_uuid": "frm-001",
        "keyframe_uuid": "kf-001",
        "payload": {"media": {"snapshot_status": "not_implemented"}},
    }
    d.update(overrides)
    return d


def test_publish_writes_to_stream():
    fake = fakeredis.FakeRedis(decode_responses=False)
    pub = RecordRequestPublisher(fake, "security.record_requests")
    event = _make_event()
    msg_id = pub.publish(event, event_id="uuid-1234")
    assert msg_id

    results = fake.xrange("security.record_requests", "-", "+")
    assert len(results) == 1
    _, fields = results[0]
    assert fields[b"status"] == b"pending"
    assert fields[b"event_id"] == b"uuid-1234"


def test_publish_contains_required_fields():
    fake = fakeredis.FakeRedis(decode_responses=False)
    pub = RecordRequestPublisher(fake, "security.record_requests")
    event = _make_event()
    pub.publish(event, event_id="uuid-1")

    _, fields = fake.xrange("security.record_requests", "-", "+")[0]
    record = json.loads(fields[b"data"])

    for key in ("request_id", "event_id", "source_event_id", "camera_id",
                 "source_id", "event_ts_ms", "pre_seconds", "post_seconds",
                 "strategy", "status"):
        assert key in record, f"Missing field: {key}"
    assert record["strategy"] == "savant_replay"
    assert record["status"] == "pending"
    assert record["pre_seconds"] == 5
    assert record["post_seconds"] == 5


def test_publish_preserves_keyframe_uuid():
    fake = fakeredis.FakeRedis(decode_responses=False)
    pub = RecordRequestPublisher(fake, "security.record_requests")
    event = _make_event(keyframe_uuid="kf-abc")
    pub.publish(event, event_id="uuid-2")

    _, fields = fake.xrange("security.record_requests", "-", "+")[0]
    record = json.loads(fields[b"data"])
    assert record["keyframe_uuid"] == "kf-abc"


def test_publish_handles_missing_keyframe():
    fake = fakeredis.FakeRedis(decode_responses=False)
    pub = RecordRequestPublisher(fake, "security.record_requests")
    event = _make_event()
    del event["keyframe_uuid"]
    pub.publish(event, event_id="uuid-3")

    _, fields = fake.xrange("security.record_requests", "-", "+")[0]
    record = json.loads(fields[b"data"])
    assert record["keyframe_uuid"] is None


def test_publish_is_idempotent_in_stream():
    """Multiple publishes with same event create separate stream entries (Redis design)."""
    fake = fakeredis.FakeRedis(decode_responses=False)
    pub = RecordRequestPublisher(fake, "security.record_requests")
    event = _make_event()
    pub.publish(event, event_id="uuid-1")
    pub.publish(event, event_id="uuid-1")
    assert len(fake.xrange("security.record_requests", "-", "+")) == 2


# ===========================================================================
# source_id resolution tests
# ===========================================================================


def test_source_id_from_media(monkeypatch):
    monkeypatch.delenv("DEFAULT_REPLAY_SOURCE_ID", raising=False)
    event = _make_event()
    event["payload"] = {"media": {"source_id": "phase3a"}}
    assert _resolve_source_id(event) == "phase3a"


def test_source_id_from_event(monkeypatch):
    monkeypatch.delenv("DEFAULT_REPLAY_SOURCE_ID", raising=False)
    event = _make_event()
    event["source_id"] = "cam_01"
    del event["payload"]["media"]
    assert _resolve_source_id(event) == "cam_01"


def test_source_id_fallback_to_default(monkeypatch):
    monkeypatch.setenv("DEFAULT_REPLAY_SOURCE_ID", "phase3a")
    event = _make_event()
    event["source_id"] = "0"
    del event["payload"]["media"]
    assert _resolve_source_id(event) == "phase3a"


def test_source_id_media_takes_priority(monkeypatch):
    monkeypatch.setenv("DEFAULT_REPLAY_SOURCE_ID", "fallback")
    event = _make_event()
    event["source_id"] = "0"
    event["payload"] = {"media": {"source_id": "replay-source-1"}}
    assert _resolve_source_id(event) == "replay-source-1"
