"""C2 post-Savant sink metadata annotation builder tests."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"


def test_metadata_with_person_boxes_emits_person_annotation(tmp_path: Path) -> None:
    builder = _activate_builder()
    metadata = [_frame(objects=[_person_object()])]

    rows, summary = builder.build_annotations_from_metadata(metadata)

    assert len(rows) == 1
    person = rows[0]["objects"][0]
    assert person["object_type"] == "person"
    assert person["namespace"] == "yolo26_pose"
    assert person["track_id"] == "41"
    assert person["bbox"]["source"] == "track_box"
    assert person["bbox"]["format"] == "xyxy"
    assert person["bbox"]["coordinate_space"] == "pixel"
    assert person["bbox"]["xyxy"] == [80.0, 110.0, 120.0, 210.0]
    assert summary["production_ready"] is True
    assert summary["annotation_source"] == "post_savant_sink_metadata"


def test_keypoints_float_vector_emits_named_coco17_keypoints(tmp_path: Path) -> None:
    builder = _activate_builder()
    metadata = [_frame(objects=[_person_object(keypoints=_keypoints())])]

    rows, summary = builder.build_annotations_from_metadata(metadata)

    pose = rows[0]["objects"][0]["pose"]
    assert pose["format"] == "coco17"
    assert pose["keypoints_format"] == "coco17"
    assert pose["coordinate_space"] == "pixel"
    assert pose["keypoint_count"] == 17
    assert pose["keypoints"][0] == {
        "index": 0,
        "name": "nose",
        "x": 10.0,
        "y": 20.0,
        "confidence": 0.5,
    }
    assert pose["keypoints"][-1]["name"] == "right_ankle"
    assert summary["keypoints_count"] == 17


def test_face_bbox_and_landmarks_emit_face_observation_only_annotation(tmp_path: Path) -> None:
    builder = _activate_builder()
    metadata = [_frame(objects=[_face_object()])]

    rows, summary = builder.build_annotations_from_metadata(metadata)

    face = rows[0]["objects"][0]
    assert face["object_type"] == "face"
    assert face["namespace"] == "yolov8_face"
    assert face["track_id"] == "41"
    assert face["label"]["kind"] == "unknown_face"
    assert face["bbox"]["source"] == "detection_box"
    assert face["bbox"]["xyxy"] == [85.0, 95.0, 115.0, 125.0]
    assert face["landmarks"]["format"] == "5_point"
    assert len(face["landmarks"]["points"]) == 5
    assert face["identity"]["source_observation_id"] is None
    assert face["identity"]["visual_evidence_status"] == "observation_only"
    assert summary["object_counts"] == {"person": 0, "face": 1, "known_face": 0}
    assert summary["face_landmarks_count"] == 5


def test_face_bbox_path_is_viewer_compatible_without_known_face_identity(tmp_path: Path) -> None:
    builder = _activate_builder()
    metadata = [_frame(objects=[_face_object()])]

    rows, summary = builder.build_annotations_from_metadata(metadata)

    face = rows[0]["objects"][0]
    assert face["object_type"] == "face"
    assert face["bbox"]["format"] == "xyxy"
    assert isinstance(face["bbox"]["xyxy"], list)
    assert len(face["bbox"]["xyxy"]) == 4
    assert face["bbox"]["coordinate_space"] == "pixel"
    assert face["landmarks"]["points"][0] == [90.0, 100.0]
    assert face["identity"]["visual_evidence_status"] == "observation_only"
    assert face["identity"]["match_status"] == "not_searched"
    assert face["label"]["kind"] == "unknown_face"
    assert face.get("annotation_role") != "watchlist_trigger_face"
    assert summary["object_counts"]["known_face"] == 0


def test_no_objects_is_not_production_ready(tmp_path: Path) -> None:
    builder = _activate_builder()

    rows, summary = builder.build_annotations_from_metadata([_frame(objects=[])])

    assert len(rows) == 1
    assert rows[0]["objects"] == []
    assert rows[0]["production_ready"] is False
    assert rows[0]["displayable"] is False
    assert summary["production_ready"] is False
    assert summary["annotation_status"] == "no_post_savant_objects"
    assert summary["visual_binding_status"] == "unverified"


def test_missing_frame_uuid_does_not_fail_and_records_limitation(tmp_path: Path) -> None:
    builder = _activate_builder()
    frame = _frame(objects=[_person_object()])
    frame.pop("uuid")

    rows, summary = builder.build_annotations_from_metadata([frame])

    assert rows[0]["frame_uuid"] is None
    assert "frame_uuid_missing_in_native_metadata" in summary["limitations"]
    assert summary["frame_identity_method"] == "post_savant_metadata_frame_pts"
    assert summary["frame_identity_confidence"] == "medium"


def test_missing_source_observation_id_does_not_mark_known_face_or_watchlist(tmp_path: Path) -> None:
    builder = _activate_builder()
    metadata = [_frame(objects=[_face_object()])]

    rows, summary = builder.build_annotations_from_metadata(metadata)

    face = rows[0]["objects"][0]
    assert face["identity"]["source_observation_id"] is None
    assert face.get("annotation_role") != "watchlist_trigger_face"
    assert face["label"]["kind"] == "unknown_face"
    assert summary["object_counts"]["known_face"] == 0
    assert summary["trigger_face_row_exists"] is False
    assert summary["trigger_face_row_passed_freshness_guard"] is False
    assert "source_observation_id_missing_in_native_metadata" in summary["limitations"]
    assert "watchlist_trigger_identity_binding_not_verified_in_c2_1" in summary["limitations"]


def test_output_jsonl_has_one_row_per_frame(tmp_path: Path) -> None:
    builder = _activate_builder()
    metadata = [
        _frame(pts=1000, uuid="frame-1", objects=[_person_object()]),
        _frame(pts=2_001_000, uuid="frame-2", objects=[_face_object()]),
        _frame(pts=3_001_000, uuid="frame-3", objects=[]),
    ]

    rows, summary = builder.build_annotations_from_metadata(metadata)

    assert [row["frame_index"] for row in rows] == [0, 1, 2]
    assert [row["clip_frame_index"] for row in rows] == [0, 1, 2]
    assert [row["time_offset_ms"] for row in rows] == [0, 2, 3]
    assert summary["frame_count"] == 3


def test_max_frames_trims_rows_without_breaking_face_overlay_schema(tmp_path: Path) -> None:
    builder = _activate_builder()
    metadata = [
        _frame(pts=1000 + index, uuid=f"frame-{index}", objects=[_person_object(), _face_object()])
        for index in range(4)
    ]

    rows, summary = builder.build_annotations_from_metadata(metadata, max_frames=2)

    assert len(rows) == 2
    assert summary["original_metadata_frame_count"] == 4
    assert summary["sidecar_frame_count"] == 2
    assert summary["sidecar_trimmed"] is True
    face = next(obj for obj in rows[-1]["objects"] if obj["object_type"] == "face")
    assert face["bbox"]["format"] == "xyxy"
    assert face["bbox"]["coordinate_space"] == "pixel"
    assert face["landmarks"]["points"][0] == [90.0, 100.0]
    assert face["label"]["kind"] == "unknown_face"


def test_summary_counts_match_jsonl_contents(tmp_path: Path) -> None:
    builder = _activate_builder()
    metadata_path = tmp_path / "metadata.json"
    output_jsonl = tmp_path / "annotations.frame_cache.identity.jsonl"
    summary_path = tmp_path / "summary.json"
    metadata_rows = [
        _frame(pts=1000, uuid="frame-1", objects=[_person_object(), _face_object()]),
        _frame(pts=2_001_000, uuid="frame-2", objects=[_person_object(track_id=42)]),
    ]
    metadata_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in metadata_rows),
        encoding="utf-8",
    )

    result = builder.build_post_savant_annotation_sidecar(metadata_path, output_jsonl, summary_path)
    rows = _read_jsonl(output_jsonl)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    sibling_summary = json.loads((tmp_path / "summary.frame_cache.identity.json").read_text(encoding="utf-8"))

    person_count = sum(1 for row in rows for obj in row["objects"] if obj["object_type"] == "person")
    face_count = sum(1 for row in rows for obj in row["objects"] if obj["object_type"] == "face")
    assert result.summary == summary == sibling_summary
    assert len(rows) == summary["frame_count"] == 2
    assert person_count == summary["object_counts"]["person"] == 2
    assert face_count == summary["object_counts"]["face"] == 1
    assert summary["frames_with_objects_count"] == 2
    assert summary["timeline_domain"] == "final_canonical_clip"
    assert summary["sidecar_type"] == "production"
    assert summary["legacy_fallback_allowed"] is False
    assert summary["legacy_used_for_visual_binding"] is False
    assert summary["embedding_vectors_in_output"] == 0
    assert summary["image_bytes_in_output"] == 0


def _activate_builder() -> Any:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if str(MEDIA_WORKER_ROOT) in sys.path:
        sys.path.remove(str(MEDIA_WORKER_ROOT))
    sys.path.insert(0, str(MEDIA_WORKER_ROOT))
    return importlib.import_module("app.post_savant_metadata_annotation_builder")


def _frame(
    *,
    pts: int = 1_000_000,
    uuid: str = "frame-1",
    objects: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "source_id": "c2-poc",
        "uuid": uuid,
        "pts": pts,
        "width": 1920,
        "height": 1080,
        "objects": objects,
    }


def _person_object(*, track_id: int = 41, keypoints: list[float] | None = None) -> dict[str, Any]:
    return {
        "namespace": "yolo26_pose",
        "label": "person",
        "id": 123,
        "confidence": 0.82,
        "track_id": track_id,
        "detection_box": _box(100.0, 160.0, 30.0, 80.0),
        "track_box": _box(100.0, 160.0, 40.0, 100.0),
        "attributes": [
            {
                "namespace": "yolo26_pose",
                "name": "keypoints",
                "values": [{"confidence": 1.0, "value": {"FloatVector": keypoints or _keypoints()}}],
            }
        ],
    }


def _face_object() -> dict[str, Any]:
    return {
        "namespace": "yolov8_face",
        "label": "face",
        "id": 456,
        "confidence": 0.91,
        "detection_box": _box(100.0, 110.0, 30.0, 30.0),
        "attributes": [
            {
                "namespace": "yolov8_face",
                "name": "landmarks",
                "values": [
                    {
                        "confidence": 1.0,
                        "value": {"FloatVector": [90.0, 100.0, 110.0, 100.0, 100.0, 110.0, 92.0, 120.0, 108.0, 120.0]},
                    }
                ],
            },
            {
                "namespace": "face_person_associator",
                "name": "person_track_id",
                "values": [{"confidence": 1.0, "value": {"Integer": 41}}],
            },
        ],
    }


def _box(xc: float, yc: float, width: float, height: float) -> dict[str, float]:
    return {"xc": xc, "yc": yc, "width": width, "height": height, "angle": 0.0}


def _keypoints() -> list[float]:
    values = []
    for index in range(17):
        values.extend([float(10 + index), float(20 + index), 0.5])
    return values


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
