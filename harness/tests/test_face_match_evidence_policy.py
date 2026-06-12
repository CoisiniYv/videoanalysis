"""Face match evidence-policy defaults."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _activate_face_service():
    service_root = str(ROOT / "services" / "face-worker")
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    service_roots = {str(path) for path in (ROOT / "services").glob("*")}
    sys.path[:] = [path for path in sys.path if path not in service_roots]
    sys.path.insert(0, service_root)
    return importlib.import_module("app.face_match_event_service")


def test_watchlist_hit_default_evidence_policy_is_5_plus_5() -> None:
    service = _activate_face_service()

    event = service.build_watchlist_hit_event(
        observation={
            "source_observation_id": "face:primary_rtsp:1:1000",
            "camera_id": "cam1",
            "source_id": "primary_rtsp",
            "track_id": "1",
            "timestamp_ms": 1_000,
            "quality": 0.9,
            "face_confidence": 0.8,
            "payload": {"media": {"frame_uuid": "frame-1"}},
        },
        gallery_match={
            "id": 10,
            "person_id": 20,
            "external_person_id": "person-20",
            "person_name": "Ada",
            "similarity": 0.91,
        },
        threshold=0.5,
    )

    assert event["evidence_policy"]["pre_seconds"] == 5
    assert event["evidence_policy"]["post_seconds"] == 5
    assert event["payload"]["media"]["pre_seconds"] == 5
    assert event["payload"]["media"]["post_seconds"] == 5


def test_watchlist_hit_event_time_uses_ntp_timestamp_not_frame_pts() -> None:
    service = _activate_face_service()

    event = service.build_watchlist_hit_event(
        observation={
            "source_observation_id": "face:primary_rtsp:1:1000",
            "camera_id": "cam1",
            "source_id": "primary_rtsp",
            "track_id": "1",
            "timestamp_ms": 1_000,
            "quality": 0.9,
            "face_confidence": 0.8,
            "payload": {
                "media": {
                    "frame_uuid": "frame-1",
                    "frame_pts": 1_000_000_000,
                    "ntp_timestamp": 1_781_191_630_112_867_000,
                }
            },
        },
        gallery_match={
            "id": 10,
            "person_id": 20,
            "external_person_id": "person-20",
            "person_name": "Ada",
            "similarity": 0.91,
        },
        threshold=0.5,
    )

    assert event["event_ts_ms"] == 1_781_191_630_112
    assert event["start_ts_ms"] == 1_781_191_630_112
    assert event["end_ts_ms"] == 1_781_191_630_112
    assert event["payload"]["observation"]["timestamp_ms"] == 1_000
