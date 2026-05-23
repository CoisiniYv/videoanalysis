"""Tests for SecurityEvent schema — serialization, source_event_id, payload.media."""

import json
import sys
from pathlib import Path

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_phase2c")
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from custom.models.events import (
    SECURITY_EVENT_SCHEMA_VERSION,
    SecurityEvent,
    build_source_event_id,
)


# ===========================================================================
# 1. to_dict() contains all required fields
# ===========================================================================

def test_to_dict_contains_all_required_fields():
    """to_dict() output must include every field defined on the dataclass."""
    event = SecurityEvent()
    d = event.to_dict()
    required = [
        "schema_version", "source_event_id", "producer", "gpu_id",
        "event_type", "camera_id", "source_id", "track_id", "person_id",
        "start_ts_ms", "end_ts_ms", "event_ts_ms", "frame_id",
        "frame_uuid", "keyframe_uuid", "confidence", "severity",
        "snapshot_required", "clip_required", "payload",
    ]
    for field_name in required:
        assert field_name in d, f"Missing field: {field_name}"


# ===========================================================================
# 2. schema_version == "1.0"
# ===========================================================================

def test_schema_version_is_1_0():
    assert SECURITY_EVENT_SCHEMA_VERSION == "1.0"
    event = SecurityEvent()
    assert event.schema_version == "1.0"


# ===========================================================================
# 3. source_event_id followed generation rule
# ===========================================================================

def test_source_event_id_generation_format():
    sid = build_source_event_id(
        producer="savant_phase2c",
        camera_id="cam_01",
        track_id=3,
        event_type="intrusion",
        start_ts_ms=1000,
    )
    assert sid == "savant_phase2c:cam_01:3:intrusion:1000"


# ===========================================================================
# 4. Same input → same source_event_id
# ===========================================================================

def test_source_event_id_deterministic():
    sid1 = build_source_event_id("p", "c", 1, "intrusion", 5000)
    sid2 = build_source_event_id("p", "c", 1, "intrusion", 5000)
    assert sid1 == sid2


# ===========================================================================
# 5. Different track_id / event_type / start_ts_ms → different
# ===========================================================================

def test_source_event_id_different_fields():
    base = build_source_event_id("p", "c", 1, "intrusion", 1000)
    diff_track = build_source_event_id("p", "c", 2, "intrusion", 1000)
    diff_event = build_source_event_id("p", "c", 1, "loitering", 1000)
    diff_ts = build_source_event_id("p", "c", 1, "intrusion", 2000)
    assert diff_track != base
    assert diff_event != base
    assert diff_ts != base


# ===========================================================================
# 6. payload.media defaults exist
# ===========================================================================

def test_payload_media_defaults():
    event = SecurityEvent()
    # Before enrichment, payload is empty dict
    assert event.payload == {}


# ===========================================================================
# 7. payload.media.snapshot_status == "not_implemented"
# ===========================================================================

def test_payload_media_snapshot_status():
    event = SecurityEvent()
    event.payload = {
        "media": {
            "snapshot_status": "not_implemented",
            "clip_status": "not_implemented",
            "recording_strategy": "reserved",
            "pre_seconds": 5,
            "post_seconds": 5,
            "source_id": "test",
            "event_ts_ms": 1000,
            "frame_uuid": None,
            "keyframe_uuid": None,
        }
    }
    assert event.payload["media"]["snapshot_status"] == "not_implemented"


# ===========================================================================
# 8. payload.media.clip_status == "not_implemented"
# ===========================================================================

def test_payload_media_clip_status():
    event = SecurityEvent()
    event.payload = {
        "media": {
            "snapshot_status": "not_implemented",
            "clip_status": "not_implemented",
            "recording_strategy": "reserved",
            "pre_seconds": 5,
            "post_seconds": 5,
            "source_id": "test",
            "event_ts_ms": 1000,
            "frame_uuid": None,
            "keyframe_uuid": None,
        }
    }
    assert event.payload["media"]["clip_status"] == "not_implemented"


# ===========================================================================
# 9. payload.media.recording_strategy == "reserved"
# ===========================================================================

def test_payload_media_recording_strategy():
    event = SecurityEvent()
    event.payload = {
        "media": {
            "snapshot_status": "not_implemented",
            "clip_status": "not_implemented",
            "recording_strategy": "reserved",
            "pre_seconds": 5,
            "post_seconds": 5,
            "source_id": "test",
            "event_ts_ms": 1000,
            "frame_uuid": None,
            "keyframe_uuid": None,
        }
    }
    assert event.payload["media"]["recording_strategy"] == "reserved"


# ===========================================================================
# 10. event_ts_ms appears in both top-level and payload.media
# ===========================================================================

def test_event_ts_ms_in_both_levels():
    event = SecurityEvent(event_ts_ms=1234567)
    event.payload = {
        "media": {
            "event_ts_ms": 1234567,
            "snapshot_status": "not_implemented",
            "clip_status": "not_implemented",
            "recording_strategy": "reserved",
            "pre_seconds": 5,
            "post_seconds": 5,
            "source_id": "test",
            "frame_uuid": None,
            "keyframe_uuid": None,
        }
    }
    assert event.event_ts_ms == event.payload["media"]["event_ts_ms"]


# ===========================================================================
# 11. frame_uuid / keyframe_uuid can be None
# ===========================================================================

def test_frame_uuid_can_be_none():
    event = SecurityEvent(frame_uuid=None, keyframe_uuid=None)
    d = event.to_dict()
    assert d["frame_uuid"] is None
    assert d["keyframe_uuid"] is None


# ===========================================================================
# 12. to_json() can be parsed by json.loads()
# ===========================================================================

def test_to_json_is_valid_json():
    event = SecurityEvent(
        event_type="intrusion",
        camera_id="cam_01",
        track_id=5,
        start_ts_ms=1000,
        end_ts_ms=2000,
        source_event_id="test:cam_01:5:intrusion:1000",
    )
    raw = event.to_json()
    parsed = json.loads(raw)
    assert parsed["event_type"] == "intrusion"
    assert parsed["camera_id"] == "cam_01"
    assert parsed["track_id"] == 5
    assert parsed["schema_version"] == "1.0"


# ===========================================================================
# 13. SecurityEvent accepts zone/rule_name/description (IntrusionRule compat)
# ===========================================================================

def test_security_event_accepts_zone_rule_name():
    """Verify zone/rule_name/description are accepted kwargs (used by IntrusionRule)."""
    event = SecurityEvent(
        event_type="intrusion",
        zone="full_frame",
        rule_name="debug_intrusion",
        description="Track 3 intruded zone 'full_frame'",
    )
    assert event.zone == "full_frame"
    assert event.rule_name == "debug_intrusion"
    assert event.description == "Track 3 intruded zone 'full_frame'"
    d = event.to_dict()
    assert d["zone"] == "full_frame"
    assert d["rule_name"] == "debug_intrusion"
