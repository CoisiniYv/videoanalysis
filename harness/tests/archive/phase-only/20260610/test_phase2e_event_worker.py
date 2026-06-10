"""Tests for Phase 2E event-worker: config, consumer, repository, worker logic."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import fakeredis
import pytest
from unittest.mock import patch, MagicMock

# Add event-worker to path
EW_DIR = str(Path(__file__).resolve().parents[2] / "services" / "event-worker")
if EW_DIR not in sys.path:
    sys.path.insert(0, EW_DIR)

from app.config import Config, load_config
from app.redis_consumer import RedisStreamConsumer
from app.repository import EventRepository
from app.worker import (
    _apply_default_evidence_policy,
    _handle_event,
    _parse_event,
    _process_batch,
    _requires_evidence,
)


# ===========================================================================
# Fake in-memory repository for unit tests
# ===========================================================================


import uuid


class FakeEventRepository:
    """In-memory repository matching EventRepository interface."""

    def __init__(self):
        self._events: dict[str, dict] = {}

    def insert_event(self, event: dict) -> str | None:
        sid = event.get("source_event_id", "")
        if sid in self._events:
            return None
        self._events[sid] = dict(event)
        return str(uuid.uuid4())

    def count_by_source_event_id(self, source_event_id: str) -> int:
        return 1 if source_event_id in self._events else 0

    def event_exists(self, source_event_id: str) -> bool:
        return source_event_id in self._events


# ===========================================================================
# Helpers
# ===========================================================================


def _build_security_event_dict(**overrides) -> dict:
    d = {
        "schema_version": "1.0",
        "source_event_id": "savant_phase2c:cam_01:3:intrusion:1000",
        "producer": "savant_phase2c",
        "gpu_id": 0,
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "source_id": "src_01",
        "track_id": 3,
        "person_id": 0,
        "start_ts_ms": 1000,
        "end_ts_ms": 2500,
        "event_ts_ms": 2000,
        "frame_id": 42,
        "frame_uuid": None,
        "keyframe_uuid": None,
        "confidence": 0.85,
        "severity": "medium",
        "zone": "full_frame",
        "rule_name": "debug_intrusion",
        "description": "Track 3 intruded",
        "snapshot_required": False,
        "clip_required": False,
        "payload": {
            "zone_id": "full_frame",
            "inside_ms": 1500,
            "bbox": {"x": 100, "y": 200, "width": 300, "height": 400},
            "rule": "debug_intrusion",
            "media": {
                "snapshot_required": False,
                "clip_required": False,
                "snapshot_status": "not_implemented",
                "clip_status": "not_implemented",
                "recording_strategy": "reserved",
                "pre_seconds": 5,
                "post_seconds": 5,
                "source_id": "src_01",
                "event_ts_ms": 2000,
                "frame_uuid": None,
                "keyframe_uuid": None,
            },
        },
    }
    d.update(overrides)
    return d


def _stream_fields(event: dict) -> dict[bytes, bytes]:
    """Build Redis stream fields dict as returned by xreadgroup (bytes keys)."""
    return {
        b"type": b"security_event",
        b"source_event_id": event["source_event_id"].encode(),
        b"event_type": event["event_type"].encode(),
        b"camera_id": event["camera_id"].encode(),
        b"track_id": str(event["track_id"]).encode(),
        b"start_ts_ms": str(event["start_ts_ms"]).encode(),
        b"end_ts_ms": str(event["end_ts_ms"]).encode(),
        b"severity": event.get("severity", "medium").encode(),
        b"data": json.dumps(event, ensure_ascii=False).encode(),
    }


# ===========================================================================
# 29. Config loads defaults
# ===========================================================================


def test_config_defaults(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("EVENT_STREAM", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CONSUMER_GROUP", raising=False)
    monkeypatch.delenv("CONSUMER_NAME", raising=False)

    cfg = load_config()
    assert cfg.redis_url == "redis://redis:6379/0"
    assert cfg.event_stream == "security.events"
    assert cfg.consumer_group == "event-workers"
    assert cfg.consumer_name == "event-worker-1"
    assert cfg.poll_timeout_ms == 5000
    assert cfg.batch_size == 10


# ===========================================================================
# 30. Config loads from env vars
# ===========================================================================


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://test:6379/1")
    monkeypatch.setenv("EVENT_STREAM", "test.events")
    monkeypatch.setenv("CONSUMER_GROUP", "test-group")
    monkeypatch.setenv("CONSUMER_NAME", "test-consumer")
    monkeypatch.setenv("POLL_TIMEOUT_MS", "2000")
    monkeypatch.setenv("EVENT_BATCH_SIZE", "5")

    cfg = load_config()
    assert cfg.redis_url == "redis://test:6379/1"
    assert cfg.event_stream == "test.events"
    assert cfg.consumer_group == "test-group"
    assert cfg.consumer_name == "test-consumer"
    assert cfg.poll_timeout_ms == 2000
    assert cfg.batch_size == 5


# ===========================================================================
# 31. RedisStreamConsumer creates group and reads messages
# ===========================================================================


def test_consumer_ensure_group_and_read():
    fake = fakeredis.FakeRedis(decode_responses=False)
    consumer = RedisStreamConsumer(fake, "test.stream", "test-group", "worker-1")
    consumer.ensure_group()

    # Add a test message
    fields = {b"type": b"security_event", b"data": b'{"test":true}'}
    fake.xadd("test.stream", fields)

    msgs = consumer.read_new(count=10, block_ms=100)
    assert len(msgs) == 1
    msg_id, data = msgs[0]
    assert data[b"type"] == b"security_event"
    assert msg_id  # non-empty message id

    # ACK
    assert consumer.ack(msg_id) is True


# ===========================================================================
# 32. RedisStreamConsumer ACK removes from pending
# ===========================================================================


def test_consumer_ack_removes_pending():
    fake = fakeredis.FakeRedis(decode_responses=False)
    consumer = RedisStreamConsumer(fake, "test.stream", "test-group", "worker-1")
    consumer.ensure_group()

    fake.xadd("test.stream", {b"data": b'{"e":1}'})

    msgs = consumer.read_new(count=1, block_ms=100)
    assert len(msgs) == 1
    msg_id, _ = msgs[0]

    # Before ACK, message is pending
    pending_before = fake.xpending_range("test.stream", "test-group", "-", "+", count=10)
    assert len(pending_before) >= 1

    consumer.ack(msg_id)

    # After ACK, pending is cleared
    pending_info = fake.xpending("test.stream", "test-group")
    # xpending returns dict with 'pending' key in redis-py >= 5
    if isinstance(pending_info, dict):
        assert pending_info.get("pending", 0) == 0


# ===========================================================================
# 33. _parse_event extracts valid SecurityEvent from stream fields
# ===========================================================================


def test_parse_event_valid():
    event = _build_security_event_dict()
    fields = _stream_fields(event)
    parsed = _parse_event(fields)
    assert parsed is not None
    assert parsed["source_event_id"] == event["source_event_id"]
    assert parsed["event_type"] == "intrusion"
    assert parsed["camera_id"] == "cam_01"
    assert parsed["track_id"] == 3


# ===========================================================================
# 34. _parse_event returns None for missing data field
# ===========================================================================


def test_parse_event_missing_data():
    fields = {b"type": b"security_event"}
    assert _parse_event(fields) is None


# ===========================================================================
# 35. _parse_event returns None for invalid JSON
# ===========================================================================


def test_parse_event_invalid_json():
    fields = {b"data": b"not-json!!!"}
    assert _parse_event(fields) is None


# ===========================================================================
# 36. _parse_event returns None for missing required fields
# ===========================================================================


def test_parse_event_missing_source_event_id():
    event = _build_security_event_dict(source_event_id="")
    fields = _stream_fields(event)
    assert _parse_event(fields) is None


# ===========================================================================
# 37. FakeEventRepository inserts and detects duplicates
# ===========================================================================


def test_fake_repo_insert_and_detect_duplicate():
    repo = FakeEventRepository()
    event = _build_security_event_dict()
    assert repo.insert_event(event) is not None
    assert repo.insert_event(event) is None  # duplicate

    assert repo.count_by_source_event_id(event["source_event_id"]) == 1
    assert repo.event_exists(event["source_event_id"]) is True
    assert repo.event_exists("nonexistent") is False


# ===========================================================================
# 38. _handle_event inserts and ACKs
# ===========================================================================


def test_handle_event_inserts_and_acks():
    repo = FakeEventRepository()
    fake = fakeredis.FakeRedis(decode_responses=False)
    consumer = RedisStreamConsumer(fake, "test.stream", "test-group", "w1")
    consumer.ensure_group()

    event = _build_security_event_dict()
    fields = _stream_fields(event)
    msg_id = fake.xadd("test.stream", fields)

    # Read as if through consumer group
    msgs = consumer.read_new(count=1, block_ms=100)
    assert len(msgs) == 1
    msg_id_read, data = msgs[0]

    parsed = _parse_event(data)
    assert parsed is not None
    new, event_id = _handle_event(parsed, msg_id_read, repo, consumer)
    assert new is True  # newly inserted
    assert event_id is not None


# ===========================================================================
# 39. Duplicate event is detected (idempotent)
# ===========================================================================


def test_handle_event_duplicate_is_idempotent():
    repo = FakeEventRepository()
    fake = fakeredis.FakeRedis(decode_responses=False)
    consumer = RedisStreamConsumer(fake, "test.stream", "test-group", "w2")
    consumer.ensure_group()

    event = _build_security_event_dict()
    fields = _stream_fields(event)

    # First insert
    msg_id_1 = fake.xadd("test.stream", fields)
    msgs = consumer.read_new(count=1, block_ms=100)
    assert len(msgs) == 1
    parsed = _parse_event(msgs[0][1])
    new1, _ = _handle_event(parsed, msgs[0][0], repo, consumer)
    assert new1 is True

    # Second insert — same source_event_id
    msg_id_2 = fake.xadd("test.stream", fields)
    msgs2 = consumer.read_new(count=1, block_ms=100)
    assert len(msgs2) == 1
    parsed2 = _parse_event(msgs2[0][1])
    new2, _ = _handle_event(parsed2, msgs2[0][0], repo, consumer)
    assert new2 is False  # duplicate

    # Only one row in repo
    assert repo.count_by_source_event_id(event["source_event_id"]) == 1


# ===========================================================================
# 40. media fields preserved in event processing
# ===========================================================================


def test_media_fields_preserved():
    repo = FakeEventRepository()
    event = _build_security_event_dict()
    repo.insert_event(event)

    stored = repo._events[event["source_event_id"]]
    media = stored["payload"]["media"]
    assert media["snapshot_status"] == "not_implemented"
    assert media["clip_status"] == "not_implemented"
    assert media["recording_strategy"] == "reserved"


def test_r3_1a_intrusion_defaults_require_evidence():
    event = _build_security_event_dict(
        snapshot_required=False,
        clip_required=False,
        evidence_policy={},
    )
    assert _requires_evidence(event) is False

    _apply_default_evidence_policy(event)

    assert event["snapshot_required"] is True
    assert event["clip_required"] is True
    assert event["evidence_policy"]["snapshot_required"] is True
    assert event["evidence_policy"]["clip_required"] is True
    assert event["evidence_policy"]["pre_seconds"] == 5
    assert event["evidence_policy"]["post_seconds"] == 10
    assert event["payload"]["media"]["snapshot_required"] is True
    assert event["payload"]["media"]["clip_required"] is True
    assert _requires_evidence(event) is True


# ===========================================================================
# 41. _process_batch handles multiple events
# ===========================================================================


def test_process_batch():
    repo = FakeEventRepository()
    fake = fakeredis.FakeRedis(decode_responses=False)
    consumer = RedisStreamConsumer(fake, "test.stream", "test-group", "w3")
    consumer.ensure_group()

    msgs = []
    for i in range(5):
        event = _build_security_event_dict(
            track_id=i + 1,
            start_ts_ms=1000 * (i + 1),
            source_event_id=f"savant:cam_01:{i+1}:intrusion:{1000*(i+1)}",
        )
        fields = _stream_fields(event)
        mid = fake.xadd("test.stream", fields)
        msgs.append((mid, fields))

    # Read them through the consumer group
    all_read = []
    batch = consumer.read_new(count=10, block_ms=100)
    all_read.extend(batch)

    inserted, duplicates = _process_batch(all_read, repo, consumer)
    assert inserted == 5
    assert duplicates == 0
    assert len(repo._events) == 5


# ===========================================================================
# 42. SecurityEvent schema v1.0 unchanged in event-worker processing
# ===========================================================================


def test_schema_version_unchanged_through_worker():
    event = _build_security_event_dict()
    assert event["schema_version"] == "1.0"

    repo = FakeEventRepository()
    repo.insert_event(event)

    stored = repo._events[event["source_event_id"]]
    assert stored["schema_version"] == "1.0"
    assert "zone" in stored
    assert "rule_name" in stored
    assert "description" in stored


# ===========================================================================
# Phase 2E.1 — track_id TEXT, keyframe_uuid persistence
# ===========================================================================


# 43. String track_id like "t_889" stored correctly


def test_track_id_string_preserved():
    repo = FakeEventRepository()
    event = _build_security_event_dict(track_id="t_889")
    repo.insert_event(event)
    stored = repo._events[event["source_event_id"]]
    assert stored["track_id"] == "t_889"


# 44. Numeric track_id stored as text


def test_track_id_numeric_stored_as_text():
    repo = FakeEventRepository()
    event = _build_security_event_dict(track_id=42)
    repo.insert_event(event)
    stored = repo._events[event["source_event_id"]]
    assert stored["track_id"] == 42  # json preserves numeric type
    assert str(stored["track_id"]) == "42"


# 45. Null/empty track_id does not crash


def test_track_id_empty_safe():
    repo = FakeEventRepository()
    for val in (0, "", "none"):
        sid = f"savant:cam_01:{val}:intrusion:1000"
        event = _build_security_event_dict(source_event_id=sid, track_id=val)
        assert repo.insert_event(event) is not None


# 46. keyframe_uuid stored when present


def test_keyframe_uuid_present():
    repo = FakeEventRepository()
    event = _build_security_event_dict(
        keyframe_uuid="kf-abc123",
        source_event_id="savant:cam_01:3:intrusion:1000",
    )
    repo.insert_event(event)
    stored = repo._events[event["source_event_id"]]
    assert stored["keyframe_uuid"] == "kf-abc123"


# 47. missing keyframe_uuid remains None


def test_keyframe_uuid_missing():
    repo = FakeEventRepository()
    event = _build_security_event_dict(keyframe_uuid=None)
    repo.insert_event(event)
    stored = repo._events[event["source_event_id"]]
    assert stored["keyframe_uuid"] is None


# 48. frame_uuid stored when present


def test_frame_uuid_present():
    repo = FakeEventRepository()
    event = _build_security_event_dict(
        frame_uuid="frm-xyz789",
        source_event_id="savant:cam_01:5:intrusion:2000",
    )
    repo.insert_event(event)
    stored = repo._events[event["source_event_id"]]
    assert stored["frame_uuid"] == "frm-xyz789"


# 49. Duplicate detection still works with string track_id


def test_duplicate_with_string_track_id():
    repo = FakeEventRepository()
    event = _build_security_event_dict(
        track_id="t_999",
        source_event_id="savant:cam_01:t_999:intrusion:5000",
    )
    assert repo.insert_event(event) is not None
    assert repo.insert_event(event) is None
    assert repo.count_by_source_event_id(event["source_event_id"]) == 1
