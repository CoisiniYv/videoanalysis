"""Tests for RedisStreamEventExporter — stream write, field preservation, factory."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import fakeredis
import pytest

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_phase2c")
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from custom.models.events import SecurityEvent, build_source_event_id
from custom.services.event_exporter import (
    DryRunEventExporter,
    EventExporter,
    RedisStreamEventExporter,
    create_event_exporter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides):
    kwargs = {
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "source_id": "src_01",
        "track_id": 3,
        "person_id": 0,
        "start_ts_ms": 1000,
        "end_ts_ms": 2500,
        "event_ts_ms": 2000,
        "producer": "savant_phase2c",
        "gpu_id": 0,
        "zone": "full_frame",
        "rule_name": "debug_intrusion",
        "description": "Track 3 intruded",
        "source_event_id": "savant_phase2c:cam_01:3:intrusion:1000",
    }
    kwargs.update(overrides)
    event = SecurityEvent(**kwargs)
    event.payload = {
        "zone_id": event.zone,
        "inside_ms": 1500,
        "bbox": {"x": 100, "y": 200, "width": 300, "height": 400},
        "rule": event.rule_name,
        "media": {
            "snapshot_required": False,
            "clip_required": False,
            "snapshot_status": "not_implemented",
            "clip_status": "not_implemented",
            "recording_strategy": "reserved",
            "pre_seconds": 5,
            "post_seconds": 5,
            "source_id": event.source_id,
            "event_ts_ms": event.event_ts_ms,
            "frame_uuid": None,
            "keyframe_uuid": None,
        },
    }
    return event


_STREAM_SEQ = 0


def _unique_stream():
    global _STREAM_SEQ
    _STREAM_SEQ += 1
    return f"test.stream.{_STREAM_SEQ}"


def _create_exporter(stream=None):
    if stream is None:
        stream = _unique_stream()
    with patch("redis.Redis", fakeredis.FakeRedis):
        return RedisStreamEventExporter(
            redis_url="redis://localhost:6379/0",
            stream=stream,
        )


def _last_entry(exporter):
    """Return the latest entry in the exporter's stream, or None."""
    results = exporter._client.xrange(exporter._stream, "-", "+")
    return results[-1] if results else None


# ---------------------------------------------------------------------------
# 17. RedisStreamEventExporter implements EventExporter
# ---------------------------------------------------------------------------


def test_redis_exporter_is_event_exporter():
    exporter = _create_exporter()
    assert isinstance(exporter, EventExporter)


# ---------------------------------------------------------------------------
# 18. RedisStreamEventExporter.export() does not raise
# ---------------------------------------------------------------------------


def test_redis_export_no_exception():
    event = _make_event()
    exporter = _create_exporter()
    try:
        exporter.export(event)
    except Exception as exc:
        assert False, f"RedisStreamEventExporter.export() raised: {exc}"


# ---------------------------------------------------------------------------
# 19. Export writes event to stream — readable via XRANGE
# ---------------------------------------------------------------------------


def test_redis_export_writes_to_stream():
    event = _make_event()
    exporter = _create_exporter()
    exporter.export(event)

    entry = _last_entry(exporter)
    assert entry is not None

    _msg_id, fields = entry
    assert fields[b"type"] == b"security_event"
    assert fields[b"event_type"] == b"intrusion"
    assert fields[b"camera_id"] == b"cam_01"
    assert fields[b"track_id"] == b"3"


# ---------------------------------------------------------------------------
# 20. Export preserves source_event_id in stream fields
# ---------------------------------------------------------------------------


def test_redis_export_preserves_source_event_id():
    sid = "savant_phase2c:cam_01:3:intrusion:1000"
    event = _make_event(source_event_id=sid)
    exporter = _create_exporter()
    exporter.export(event)

    entry = _last_entry(exporter)
    assert entry is not None
    _msg_id, fields = entry
    assert fields[b"source_event_id"].decode() == sid


# ---------------------------------------------------------------------------
# 21. Export preserves full JSON in data field
# ---------------------------------------------------------------------------


def test_redis_export_data_field_contains_valid_json():
    event = _make_event()
    exporter = _create_exporter()
    exporter.export(event)

    entry = _last_entry(exporter)
    assert entry is not None
    _msg_id, fields = entry
    data = json.loads(fields[b"data"])
    assert data["schema_version"] == "1.0"
    assert data["event_type"] == "intrusion"
    assert data["camera_id"] == "cam_01"
    assert data["track_id"] == 3
    assert data["payload"]["media"]["snapshot_status"] == "not_implemented"
    assert data["payload"]["media"]["clip_status"] == "not_implemented"
    assert data["payload"]["media"]["recording_strategy"] == "reserved"


# ---------------------------------------------------------------------------
# 22. Custom stream name is used
# ---------------------------------------------------------------------------


def test_redis_export_custom_stream_name():
    event = _make_event()
    stream = _unique_stream()
    exporter = _create_exporter(stream=stream)
    exporter.export(event)

    results = exporter._client.xrange(stream, "-", "+")
    assert len(results) == 1


# ---------------------------------------------------------------------------
# 23. Multiple events appear in order
# ---------------------------------------------------------------------------


def test_redis_export_multiple_events():
    exporter = _create_exporter()
    for i in range(3):
        event = _make_event(
            track_id=i + 1,
            start_ts_ms=1000 * (i + 1),
            source_event_id=f"savant:cam_01:{i+1}:intrusion:{1000*(i+1)}",
        )
        exporter.export(event)

    results = exporter._client.xrange(exporter._stream, "-", "+")
    assert len(results) == 3
    track_ids = [fields[b"track_id"].decode() for _, fields in results]
    assert track_ids == ["1", "2", "3"]


# ---------------------------------------------------------------------------
# 24. RedisStreamEventExporter uses REDIS_URL / EVENT_STREAM env vars
# ---------------------------------------------------------------------------


def test_redis_exporter_env_vars(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://testhost:6380/1")
    monkeypatch.setenv("EVENT_STREAM", "test.stream")
    monkeypatch.setenv("EVENT_MAXLEN", "500")

    with patch("redis.Redis", fakeredis.FakeRedis):
        exporter = RedisStreamEventExporter()

    assert exporter._redis_url == "redis://testhost:6380/1"
    assert exporter._stream == "test.stream"
    assert exporter._maxlen == 500


# ---------------------------------------------------------------------------
# 25. create_event_exporter() returns DryRunEventExporter by default
# ---------------------------------------------------------------------------


def test_factory_defaults_to_dryrun(monkeypatch):
    monkeypatch.delenv("EVENT_EXPORTER", raising=False)
    exporter = create_event_exporter()
    assert isinstance(exporter, DryRunEventExporter)


# ---------------------------------------------------------------------------
# 26. create_event_exporter() returns RedisStreamEventExporter when
#     EVENT_EXPORTER=redis
# ---------------------------------------------------------------------------


def test_factory_redis(monkeypatch):
    monkeypatch.setenv("EVENT_EXPORTER", "redis")
    with patch("redis.Redis", fakeredis.FakeRedis):
        exporter = create_event_exporter()
    assert isinstance(exporter, RedisStreamEventExporter)


# ---------------------------------------------------------------------------
# 27. RedisStreamEventExporter does not change SecurityEvent schema
# ---------------------------------------------------------------------------


def test_redis_export_schema_version_unchanged():
    event = _make_event()
    exporter = _create_exporter()
    exporter.export(event)

    entry = _last_entry(exporter)
    assert entry is not None
    _msg_id, fields = entry
    data = json.loads(fields[b"data"])
    assert data["schema_version"] == "1.0"
    assert "zone" in data
    assert "rule_name" in data
    assert "description" in data


# ---------------------------------------------------------------------------
# 28. RedisStreamEventExporter preserves source_event_id format
#     {producer}:{camera_id}:{track_id}:{event_type}:{start_ts_ms}
# ---------------------------------------------------------------------------


def test_redis_export_source_event_id_format():
    sid = build_source_event_id(
        producer="savant_phase2c",
        camera_id="cam_01",
        track_id=3,
        event_type="intrusion",
        start_ts_ms=1000,
    )
    assert sid == "savant_phase2c:cam_01:3:intrusion:1000"

    event = _make_event(source_event_id=sid)
    exporter = _create_exporter()
    exporter.export(event)

    entry = _last_entry(exporter)
    assert entry is not None
    _msg_id, fields = entry
    assert fields[b"source_event_id"].decode() == sid
