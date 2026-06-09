"""R3 unified SecurityEvent contract checks."""

from __future__ import annotations

import sys
from dataclasses import fields
from pathlib import Path


MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_security")
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

for _mod in [m for m in list(sys.modules) if m == "custom" or m.startswith("custom.")]:
    sys.modules.pop(_mod, None)

from custom.models.events import SecurityEvent, SECURITY_EVENT_TYPES

ROOT = Path(__file__).resolve().parents[2]


def test_security_event_has_r3_unified_fields():
    names = {f.name for f in fields(SecurityEvent)}
    expected = {
        "event_type",
        "source_event_id",
        "camera_id",
        "source_id",
        "track_id",
        "person_id",
        "algorithm_type",
        "algorithm_version",
        "severity",
        "confidence",
        "start_ts_ms",
        "end_ts_ms",
        "snapshot_required",
        "clip_required",
        "evidence_policy",
        "payload",
    }
    assert expected <= names


def test_security_event_types_are_unified():
    assert SECURITY_EVENT_TYPES == (
        "intrusion",
        "loitering",
        "crowd_gathering",
        "running",
        "chasing",
        "fall",
        "wall_climb_suspicious",
        "face_observed",
        "watchlist_hit",
        "live_search_hit",
    )


def test_face_events_map_to_face_intelligence_algorithm():
    event = SecurityEvent(
        event_type="watchlist_hit",
        source_event_id="face:cam:track:watchlist_hit:1000",
        camera_id="cam_001",
        source_id="source_001",
        track_id="3",
        person_id=123,
        start_ts_ms=1000,
        snapshot_required=True,
        clip_required=True,
        evidence_policy={"pre_seconds": 5, "post_seconds": 10},
        payload={"match_score": 0.91},
    )
    data = event.to_dict()
    assert data["algorithm_type"] == "face_intelligence"
    assert data["snapshot_required"] is True
    assert data["clip_required"] is True
    assert data["payload"]["match_score"] == 0.91


def test_payload_allows_algorithm_private_fields():
    event = SecurityEvent(
        event_type="fall",
        payload={"pose_angle_deg": 72, "private_detector_state": {"a": 1}},
    )
    assert event.to_dict()["algorithm_type"] == "behavior.fall"
    assert event.to_dict()["payload"]["private_detector_state"] == {"a": 1}


def test_r3_architecture_doc_declares_scope_limits():
    doc = (ROOT / "docs" / "r3_unified_event_and_evidence_architecture.md").read_text()
    assert "does not implement new concrete algorithm logic" in doc
    assert "not a performance test phase" in doc
    assert "Performance testing must wait" in doc
    assert "YOLOv8-Face full-frame primary" in doc
