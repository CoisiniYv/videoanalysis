"""R3.3A1 unified frame anchor propagation checks."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SAVANT_ROOT = ROOT / "modules" / "savant_security"
FACE_WORKER_ROOT = ROOT / "services" / "face-worker"
HELPER = SAVANT_ROOT / "custom" / "services" / "frame_anchor_metadata.py"
BEHAVIOR_RULES = SAVANT_ROOT / "custom" / "pyfuncs" / "behavior_rules.py"
FACE_EXPORTER = SAVANT_ROOT / "custom" / "pyfuncs" / "face_observation_exporter.py"
FACE_REPOSITORY = FACE_WORKER_ROOT / "app" / "repository.py"
FACE_MATCH_SERVICE = FACE_WORKER_ROOT / "app" / "face_match_event_service.py"
DOC = ROOT / "docs" / "r3_3a1_unified_frame_anchor_propagation.md"


def _load_helper():
    spec = importlib.util.spec_from_file_location("r3_3a1_frame_anchor_metadata", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class FakePydsFrameMeta:
    buf_pts = 1228900000
    ntp_timestamp = 1780136225288893000
    frame_num = 7


class FakeVideoFrame:
    uuid = "019e7863-296d-7c40-9422-23add71cc39a"
    previous_keyframe_uuid = "019e7863-keyframe-prev"
    keyframe_uuid = None
    keyframe = False
    pts = 1228900000
    dts = 1144900000
    duration = 41708333
    time_base = (1, 1000000000)
    source_id = "source-a"


class FakeFrameMeta:
    source_id = "source-a"
    frame_num = 7
    pts = 1228900000
    duration = 41708333
    video_frame = FakeVideoFrame()
    frame_meta = FakePydsFrameMeta()


def test_extract_frame_anchor_metadata_prefers_nested_video_frame() -> None:
    helper = _load_helper()
    anchor = helper.extract_frame_anchor_metadata(FakeFrameMeta())

    assert anchor["frame_uuid"] == FakeVideoFrame.uuid
    assert anchor["previous_keyframe_uuid"] == FakeVideoFrame.previous_keyframe_uuid
    assert anchor["keyframe_uuid"] == FakeVideoFrame.previous_keyframe_uuid
    assert anchor["keyframe_pts"] is None
    assert anchor["frame_pts"] == FakeVideoFrame.pts
    assert anchor["frame_dts"] == FakeVideoFrame.dts
    assert anchor["duration"] == FakeVideoFrame.duration
    assert anchor["frame_num"] == FakeFrameMeta.frame_num
    assert anchor["ntp_timestamp"] == FakePydsFrameMeta.ntp_timestamp
    assert anchor["time_base"] == "1/1000000000"
    assert anchor["source_id"] == "source-a"
    assert anchor["metadata_source"] == "video_frame"


def test_extract_frame_anchor_metadata_missing_values_do_not_raise() -> None:
    helper = _load_helper()

    class EmptyFrame:
        pass

    anchor = helper.extract_frame_anchor_metadata(EmptyFrame())
    assert set(anchor) == {
        "frame_uuid",
        "keyframe_uuid",
        "previous_keyframe_uuid",
        "keyframe_pts",
        "frame_pts",
        "frame_dts",
        "duration",
        "frame_num",
        "ntp_timestamp",
        "time_base",
        "source_id",
        "metadata_source",
    }
    assert anchor["frame_uuid"] is None
    assert anchor["previous_keyframe_uuid"] is None


def test_extract_frame_anchor_metadata_marks_explicit_keyframe() -> None:
    helper = _load_helper()

    class KeyVideoFrame:
        uuid = "019e7863-1000-7000-8000-000000000000"
        previous_keyframe_uuid = None
        keyframe_uuid = None
        keyframe = True
        pts = 4_000_000_000
        dts = 4_000_000_000
        duration = 41_666_667
        time_base = (1, 1000000000)
        source_id = "source-a"

    class KeyFrameMeta:
        source_id = "source-a"
        video_frame = KeyVideoFrame()

    anchor = helper.extract_frame_anchor_metadata(KeyFrameMeta())

    assert anchor["frame_uuid"] == KeyVideoFrame.uuid
    assert anchor["keyframe_uuid"] == KeyVideoFrame.uuid
    assert anchor["keyframe_pts"] == KeyVideoFrame.pts


def test_extract_frame_anchor_metadata_does_not_guess_unknown_keyframe() -> None:
    helper = _load_helper()

    class UnknownKeyVideoFrame:
        uuid = "019e7863-2000-7000-8000-000000000000"
        previous_keyframe_uuid = None
        keyframe_uuid = None
        keyframe = None
        pts = 5_000_000_000
        time_base = (1, 1000000000)
        source_id = "source-a"

    class UnknownKeyFrameMeta:
        source_id = "source-a"
        video_frame = UnknownKeyVideoFrame()

    anchor = helper.extract_frame_anchor_metadata(UnknownKeyFrameMeta())

    assert anchor["frame_uuid"] == UnknownKeyVideoFrame.uuid
    assert anchor["keyframe_uuid"] is None
    assert anchor["keyframe_pts"] is None


def test_behavior_rules_uses_anchor_for_security_event_and_payload_media() -> None:
    text = _text(BEHAVIOR_RULES)
    assert "extract_frame_anchor_metadata(frame_meta)" in text
    assert 'event.frame_uuid = frame_anchor.get("frame_uuid")' in text
    assert 'event.keyframe_uuid = frame_anchor.get("keyframe_uuid")' in text
    for key in (
        "previous_keyframe_uuid",
        "frame_pts",
        "frame_dts",
        "duration",
        "frame_num",
        "ntp_timestamp",
        "time_base",
        "metadata_source",
    ):
        assert f'"{key}": frame_anchor.get("{key}")' in text


def test_security_event_json_allows_frame_uuid_and_null_keyframe_uuid() -> None:
    if str(SAVANT_ROOT) not in sys.path:
        sys.path.insert(0, str(SAVANT_ROOT))
    from custom.models.events import SecurityEvent

    event = SecurityEvent(
        event_type="intrusion",
        source_event_id="sid",
        camera_id="cam",
        frame_uuid="frame-uuid-1",
        keyframe_uuid=None,
    )
    event.payload = {
        "media": {
            "frame_uuid": event.frame_uuid,
            "keyframe_uuid": event.keyframe_uuid,
            "previous_keyframe_uuid": None,
        }
    }
    data = json.loads(event.to_json())
    assert data["frame_uuid"] == "frame-uuid-1"
    assert data["keyframe_uuid"] is None
    assert data["payload"]["media"]["frame_uuid"] == "frame-uuid-1"


def test_face_observation_exporter_adds_anchor_to_payload_media() -> None:
    text = _text(FACE_EXPORTER)
    assert "extract_frame_anchor_metadata(frame_meta)" in text
    assert "frame_anchor" in text
    assert 'obs.payload.setdefault("media", {})' in text
    for key in (
        "frame_uuid",
        "keyframe_uuid",
        "previous_keyframe_uuid",
        "frame_pts",
        "frame_num",
        "ntp_timestamp",
    ):
        assert f'"{key}"' in text
    assert "image_bytes" not in text


def test_face_worker_repository_preserves_unknown_payload_media_fields() -> None:
    text = _text(FACE_REPOSITORY)
    assert "payload_copy = dict(payload)" in text
    assert 'payload_copy.pop("camera_config_resolved", False)' in text
    assert "json.dumps(payload_copy" in text
    assert "embedding" not in text.split("payload = data.get", 1)[1].split("params =", 1)[0]


def test_face_match_event_inherits_anchor_from_observation_payload() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if str(FACE_WORKER_ROOT) not in sys.path:
        sys.path.insert(0, str(FACE_WORKER_ROOT))
    from app.face_match_event_service import build_watchlist_hit_event

    observation = {
        "source_observation_id": "face:src:1:1000",
        "camera_id": "cam",
        "source_id": "src",
        "track_id": "1",
        "timestamp_ms": 1000,
        "face_bbox": [10, 20, 30, 40],
        "landmarks": [],
        "quality": 0.9,
        "face_confidence": 0.8,
        "person_bbox": None,
        "payload": {
            "media": {
                "frame_uuid": "frame-uuid-face",
                "keyframe_uuid": None,
                "previous_keyframe_uuid": "prev-kf",
                "frame_pts": 123,
                "frame_num": 9,
                "ntp_timestamp": 456,
            }
        },
    }
    gallery_match = {
        "person_id": 7,
        "id": 11,
        "similarity": 0.91,
        "person_name": "Reese",
        "external_person_id": "demo:reese",
    }

    event = build_watchlist_hit_event(
        observation=observation,
        gallery_match=gallery_match,
        threshold=0.35,
    )
    media = event["payload"]["media"]
    assert event["event_ts_ms"] == 1000
    assert event["frame_uuid"] == "frame-uuid-face"
    assert event["keyframe_uuid"] is None
    assert media["frame_uuid"] == "frame-uuid-face"
    assert media["previous_keyframe_uuid"] == "prev-kf"
    assert media["frame_pts"] == 123
    assert media["frame_num"] == 9
    assert media["ntp_timestamp"] == 456


def test_r3_3a1_documentation_declares_boundaries_and_topology() -> None:
    assert DOC.exists()
    text = _text(DOC)
    for phrase in (
        "One RTSP frame",
        "YOLO26-pose full-frame",
        "YOLOv8-Face full-frame primary",
        "not secondary ROI inference",
        "Frame UUID is available",
        "No Replay clip",
        "No exact snapshot",
        "No production exact visual evidence yet",
        "Replay UUID anchor",
    ):
        assert phrase in text
