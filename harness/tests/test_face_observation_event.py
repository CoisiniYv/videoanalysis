"""Tests for FaceObservation event schema — pure Python, no Redis, no GPU."""

import sys
from pathlib import Path

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_security")
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import pytest

from custom.models.face_events import (
    FaceObservationEventDraft,
    build_face_source_observation_id,
    FACE_OBSERVATION_SCHEMA_VERSION,
)
from custom.models.faces import FaceObservationDraft


def _draft(**kwargs):
    defaults = dict(
        source_id="cam1",
        camera_id="cam1",
        track_id=42,
        timestamp_ms=1710000000000,
        person_bbox=[100.0, 200.0, 80.0, 180.0],
        face_bbox=[115.0, 215.0, 42.0, 42.0],
        landmarks=[[120.0, 220.0], [130.0, 225.0], [140.0, 230.0], [150.0, 235.0], [160.0, 240.0]],
        quality=0.82,
        model_name="scrfd_2.5g",
    )
    defaults.update(kwargs)
    return FaceObservationDraft(**defaults)


class TestBuildSourceObservationId:
    def test_stable_id_with_track(self):
        id1 = build_face_source_observation_id("cam1", 42, 1710000000000)
        id2 = build_face_source_observation_id("cam1", 42, 1710000000000)
        assert id1 == id2
        assert id1 == "face:cam1:42:1710000000000"

    def test_id_without_track(self):
        oid = build_face_source_observation_id("cam1", 0, 1710000000000)
        assert "no_track" in oid
        assert oid == "face:cam1:no_track:1710000000000"

    def test_id_stable_without_track(self):
        id1 = build_face_source_observation_id("cam1", 0, 1710000000000)
        id2 = build_face_source_observation_id("cam1", 0, 1710000000000)
        assert id1 == id2

    def test_different_source_differs(self):
        id1 = build_face_source_observation_id("cam1", 42, 1000)
        id2 = build_face_source_observation_id("cam2", 42, 1000)
        assert id1 != id2


class TestEventToDict:
    def test_all_fields_present(self):
        draft = _draft()
        draft.source_observation_id = build_face_source_observation_id(
            draft.source_id, draft.track_id, draft.timestamp_ms
        )
        event = FaceObservationEventDraft.from_draft(draft, producer="savant-gpu0")
        d = event.to_dict()
        assert d["schema_version"] == FACE_OBSERVATION_SCHEMA_VERSION
        assert d["source_observation_id"] == "face:cam1:42:1710000000000"
        assert d["producer"] == "savant-gpu0"
        assert d["message_type"] == "face_observation"
        assert d["camera_id"] == "cam1"
        assert d["source_id"] == "cam1"
        assert d["track_id"] == "42"
        assert d["person_track_id"] == "42"
        assert d["face_track_id"] is None
        assert d["track_id_semantics"] == "person_track_id"
        assert d["timestamp_ms"] == 1710000000000
        assert d["person_bbox"] == [100.0, 200.0, 80.0, 180.0]
        assert d["face_bbox"] == [115.0, 215.0, 42.0, 42.0]
        assert d["landmarks"] is not None
        assert len(d["landmarks"]) == 5
        assert d["quality"] == 0.82
        assert d["model_name"] == "scrfd_2.5g"

    def test_constructor_accepts_person_track_id_contract(self):
        event = FaceObservationEventDraft(
            source_observation_id="face:cam1:42:1000",
            camera_id="cam1",
            source_id="src1",
            track_id=42,
            person_track_id="42",
            face_track_id="face-7",
            track_id_semantics="person_track_id",
            timestamp_ms=1000,
            embedding=[0.01] * 512,
            embedding_dim=512,
        )

        d = event.to_dict()

        assert d["track_id"] == "42"
        assert d["person_track_id"] == "42"
        assert d["face_track_id"] == "face-7"
        assert d["track_id_semantics"] == "person_track_id"
        assert d["embedding_dim"] == 512

    def test_person_track_id_backfills_wire_track_id(self):
        event = FaceObservationEventDraft(
            source_observation_id="face:cam1:42:1000",
            camera_id="cam1",
            source_id="src1",
            track_id=0,
            person_track_id="42",
            timestamp_ms=1000,
        )

        d = event.to_dict()

        assert d["track_id"] == "42"
        assert d["person_track_id"] == "42"

    def test_from_draft_preserves_explicit_track_identity_fields(self):
        draft = _draft()
        draft.source_observation_id = "face:cam1:42:1000"
        draft.person_track_id = "42"
        draft.face_track_id = "face-7"
        draft.track_id_semantics = "person_track_id"

        event = FaceObservationEventDraft.from_draft(draft, producer="gpu0")
        d = event.to_dict()

        assert d["track_id"] == "42"
        assert d["person_track_id"] == "42"
        assert d["face_track_id"] == "face-7"
        assert d["track_id_semantics"] == "person_track_id"

    def test_wire_track_id_is_person_track_compatibility_alias_not_face_track(self):
        event = FaceObservationEventDraft(
            source_observation_id="face:cam1:42:1000",
            camera_id="cam1",
            source_id="src1",
            track_id=999,
            person_track_id="42",
            face_track_id="face-7",
            track_id_semantics="person_track_id",
            timestamp_ms=1000,
        )

        d = event.to_dict()

        assert d["track_id"] == "42"
        assert d["person_track_id"] == "42"
        assert d["face_track_id"] == "face-7"
        assert d["track_id"] != d["face_track_id"]
        assert d["track_id_semantics"] == "person_track_id"

    def test_quality_preserved(self):
        draft = _draft(quality=0.91)
        draft.source_observation_id = "face:cam1:1:1000"
        event = FaceObservationEventDraft.from_draft(draft, producer="gpu0")
        d = event.to_dict()
        assert d["quality"] == 0.91

    def test_camera_source_transparent(self):
        draft = _draft(source_id="srcA", camera_id="camB")
        draft.source_observation_id = "face:srcA:1:1000"
        event = FaceObservationEventDraft.from_draft(draft, producer="gpu0")
        d = event.to_dict()
        assert d["camera_id"] == "camB"
        assert d["source_id"] == "srcA"


class TestSlashPathsAreReferences:
    def test_snapshot_crop_path_null_by_default(self):
        draft = _draft()
        draft.source_observation_id = "face:cam1:1:1000"
        event = FaceObservationEventDraft.from_draft(draft, producer="gpu0")
        d = event.to_dict()
        assert d["snapshot_path"] is None
        assert d["crop_path"] is None

    def test_snapshot_crop_path_are_strings(self):
        draft = _draft()
        draft.source_observation_id = "face:cam1:1:1000"
        draft.snapshot_path = "/data/snapshots/face_cam1_1000.jpg"
        draft.crop_path = "/data/crops/face_cam1_1000_crop.jpg"
        event = FaceObservationEventDraft.from_draft(draft, producer="gpu0")
        d = event.to_dict()
        assert isinstance(d["snapshot_path"], str)
        assert isinstance(d["crop_path"], str)
        assert d["snapshot_path"] == "/data/snapshots/face_cam1_1000.jpg"


class TestNoImageBytes:
    def test_event_dict_has_no_image_bytes_field(self):
        draft = _draft()
        draft.source_observation_id = "face:cam1:1:1000"
        event = FaceObservationEventDraft.from_draft(draft, producer="gpu0")
        d = event.to_dict()
        forbidden = {"image", "frame_bytes", "crop_bytes", "jpeg", "png", "image_data"}
        assert forbidden.isdisjoint(set(d.keys()))

    def test_event_has_no_image_attributes(self):
        draft = _draft()
        draft.source_observation_id = "face:cam1:1:1000"
        event = FaceObservationEventDraft.from_draft(draft, producer="gpu0")
        forbidden = {"image", "frame_bytes", "crop_bytes"}
        for attr in forbidden:
            assert not hasattr(event, attr)


class TestEventToJson:
    def test_to_json_serializable(self):
        import json

        draft = _draft()
        draft.source_observation_id = "face:cam1:1:1000"
        event = FaceObservationEventDraft.from_draft(draft, producer="gpu0")
        raw = event.to_json()
        parsed = json.loads(raw)
        assert parsed["quality"] == 0.82

    def test_no_track_serialized_as_null(self):
        draft = _draft(track_id=0)
        draft.source_observation_id = "face:cam1:no_track:1000"
        event = FaceObservationEventDraft.from_draft(draft, producer="gpu0")
        d = event.to_dict()
        assert d["track_id"] is None
        assert d["person_track_id"] is None
