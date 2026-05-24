"""Tests for Phase 3B source_id mapping contract."""

from __future__ import annotations

import os
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
        "clip_required": True,
        "payload": {"media": {
            "clip_required": True,
            "recording_strategy": "savant_replay",
            "source_id": "phase3b",
        }},
    }
    d.update(overrides)
    return d


# ===========================================================================
# source_id resolution with explicit default
# ===========================================================================


def test_source_id_from_media_payload():
    """payload.media.source_id takes highest priority."""
    event = _make_event()
    event["payload"]["media"]["source_id"] = "camera_front"
    assert _resolve_source_id(event, "fallback") == "camera_front"


def test_source_id_from_event_field():
    """event.source_id used when media.source_id is absent."""
    event = _make_event()
    del event["payload"]["media"]
    event["source_id"] = "cam_01"
    assert _resolve_source_id(event, "fallback") == "cam_01"


def test_source_id_from_config_default():
    """Config default used when no other source available."""
    event = _make_event()
    del event["payload"]["media"]
    event["source_id"] = "0"
    assert _resolve_source_id(event, "phase3b") == "phase3b"


def test_source_id_empty_when_no_default():
    """Returns empty string when no default is configured."""
    event = _make_event()
    del event["payload"]["media"]
    event["source_id"] = "0"
    assert _resolve_source_id(event, "") == ""


def test_source_id_media_ignores_nullish():
    """Null-ish media source_id values are skipped."""
    for bad_val in ("", "0", "None", "null"):
        event = _make_event()
        event["payload"]["media"]["source_id"] = bad_val
        event["source_id"] = "event_source"
        assert _resolve_source_id(event, "fallback") == "event_source"


def test_source_id_event_ignores_zero():
    """event.source_id='0' falls through to default."""
    event = _make_event()
    del event["payload"]["media"]
    event["source_id"] = "0"
    assert _resolve_source_id(event, "configured_source") == "configured_source"


# ===========================================================================
# RecordRequestPublisher uses configured source_id (not hidden hardcoded)
# ===========================================================================


def test_publisher_uses_configured_default():
    fake = fakeredis.FakeRedis(decode_responses=False)
    pub = RecordRequestPublisher(fake, "security.record_requests", default_replay_source_id="env_source")
    event = _make_event()
    del event["payload"]["media"]
    event["source_id"] = "0"

    pub.publish(event, event_id="uuid-1")

    _, fields = fake.xrange("security.record_requests", "-", "+")[0]
    import json
    record = json.loads(fields[b"data"])
    assert record["source_id"] == "env_source"


def test_publisher_skips_when_no_source_id_resolvable():
    """When source_id can't be resolved and no default, publish returns None."""
    fake = fakeredis.FakeRedis(decode_responses=False)
    pub = RecordRequestPublisher(fake, "security.record_requests", default_replay_source_id="")
    event = _make_event()
    del event["payload"]["media"]
    event["source_id"] = "0"

    result = pub.publish(event, event_id="uuid-1")
    assert result is None
    assert len(fake.xrange("security.record_requests", "-", "+")) == 0


# ===========================================================================
# Source ID consistency: same logical camera = same replay source_id
# ===========================================================================


def test_same_source_produces_consistent_source_id():
    """Two events from same camera produce same source_id via configured default."""
    fake = fakeredis.FakeRedis(decode_responses=False)
    pub = RecordRequestPublisher(fake, "security.record_requests", default_replay_source_id="camera_zone_a")

    event1 = _make_event(source_event_id="ev-1")
    event2 = _make_event(source_event_id="ev-2")
    del event1["payload"]["media"]
    del event2["payload"]["media"]
    event1["source_id"] = "0"
    event2["source_id"] = "0"

    pub.publish(event1, event_id="uuid-1")
    pub.publish(event2, event_id="uuid-2")

    results = fake.xrange("security.record_requests", "-", "+")
    import json
    sid1 = json.loads(results[0][1][b"data"])["source_id"]
    sid2 = json.loads(results[1][1][b"data"])["source_id"]
    assert sid1 == "camera_zone_a"
    assert sid2 == "camera_zone_a"


def test_source_id_from_media_overrides_default():
    """When payload.media.source_id is set, it takes priority over default."""
    fake = fakeredis.FakeRedis(decode_responses=False)
    pub = RecordRequestPublisher(fake, "security.record_requests", default_replay_source_id="default_src")
    event = _make_event()
    event["payload"]["media"]["source_id"] = "explicit_src"

    pub.publish(event, event_id="uuid-1")

    import json
    results = fake.xrange("security.record_requests", "-", "+")
    record = json.loads(results[0][1][b"data"])
    assert record["source_id"] == "explicit_src"
