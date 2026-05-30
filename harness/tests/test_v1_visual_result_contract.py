from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "debug" / "generate_visual_result.py"
SMOKE = ROOT / "scripts" / "smoke" / "check_v1_visual_result_output.sh"
DOC = ROOT / "docs" / "v1_visual_result_output_mvp.md"


def load_visual_module():
    spec = importlib.util.spec_from_file_location("generate_visual_result", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def write_test_media(tmp_path: Path) -> tuple[Path, Path]:
    frame = tmp_path / "frame.jpg"
    image = np.zeros((240, 320, 3), dtype=np.uint8)
    image[:] = (34, 48, 62)
    cv2.rectangle(image, (110, 40), (210, 220), (80, 180, 255), -1)
    assert cv2.imwrite(str(frame), image)

    clip = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(
        str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 12.0, (320, 240)
    )
    assert writer.isOpened()
    for _ in range(8):
        writer.write(image)
    writer.release()
    assert clip.exists() and clip.stat().st_size > 0
    return frame, clip


def run_renderer(
    tmp_path: Path,
    event: dict,
    output_root: Path | None = None,
    result_type: str = "auto",
    camera_config: str = "",
    person_bbox_format: str = "auto",
    face_bbox_format: str = "cxcywh",
    source_frame_uuid: str | None = None,
    frame_trace_root: Path | None = None,
    explicit_frame_uuid_index_root: Path | None = None,
    source_mp4: Path | None = None,
    source_frame: str | None = None,
    source_clip: str | None = None,
) -> dict:
    module = load_visual_module()
    frame, clip = write_test_media(tmp_path)
    event_json = tmp_path / "event.json"
    event_json.write_text(json.dumps(event), encoding="utf-8")
    args = SimpleNamespace(
        event_id=None,
        event_json=str(event_json),
        a2a_summary_json=None,
        database_url=None,
        source_frame=str(source_frame or frame),
        source_frame_uuid=source_frame_uuid,
        source_clip=str(source_clip or clip),
        source_mp4=str(source_mp4 or (tmp_path / "missing.mp4")),
        frame_trace_root=str(frame_trace_root or (tmp_path / "missing_trace")),
        runtime_frame_dump_root=str(tmp_path / "runtime_frame_dump"),
        explicit_frame_uuid_index_root=str(
            explicit_frame_uuid_index_root or (tmp_path / "missing_explicit_index")
        ),
        output_root=str(output_root or (tmp_path / "visual_results")),
        run_id="test_run",
        mode="both",
        result_type=result_type,
        camera_config=camera_config,
        person_bbox_format=person_bbox_format,
        face_bbox_format=face_bbox_format,
    )
    return module.generate_visual_result(args)


def frame_uuid_for(event: dict) -> str | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    return event.get("frame_uuid") or media.get("frame_uuid")


def run_renderer_matched(tmp_path: Path, event: dict, **kwargs) -> dict:
    frame, _clip = write_test_media(tmp_path)
    frame_uuid = frame_uuid_for(event)
    source_id = event.get("source_id") or event.get("payload", {}).get("media", {}).get("source_id") or ""
    index_root = tmp_path / "explicit_index"
    index_dir = index_root / source_id
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / f"{frame_uuid}.json").write_text(
        json.dumps(
            {
                "frame_uuid": frame_uuid,
                "source_id": source_id,
                "image_path": str(frame),
                "proof_source": "unit_test_explicit_index",
            }
        ),
        encoding="utf-8",
    )
    return run_renderer(
        tmp_path,
        event,
        source_frame=str(frame),
        explicit_frame_uuid_index_root=index_root,
        **kwargs,
    )


def test_metadata_schema_required_fields(tmp_path):
    metadata = run_renderer_matched(
        tmp_path,
        {
            "event_id": "event-1",
            "source_event_id": "source-event-1",
            "event_type": "intrusion",
            "camera_id": "cam-1",
            "source_id": "source-1",
            "track_id": "7",
            "event_ts_ms": 123,
            "frame_uuid": "frame-uuid",
            "keyframe_uuid": "keyframe-uuid",
            "previous_keyframe_uuid": "prev-keyframe-uuid",
            "payload": {"bbox": {"x": 100, "y": 40, "width": 80, "height": 160}},
        },
    )
    assert metadata["schema_version"] == "1.0"
    assert metadata["phase"] == "V1"
    assert metadata["visual_result_type"] == "debug_mvp"
    assert metadata["result_type"] == "behavior_intrusion"
    assert metadata["debug_only"] is True
    assert metadata["not_production_evidence"] is True
    assert metadata["source"]["event_id"] == "event-1"
    assert metadata["source"]["frame_uuid"] == "frame-uuid"
    assert Path(metadata["media"]["metadata_path"]).exists()
    assert Path(metadata["media"]["report_path"]).exists()
    assert Path(metadata["media"]["index_path"]).exists()
    assert Path(metadata["media"]["raw_snapshot_path"]).exists()
    assert Path(metadata["media"]["annotated_snapshot_path"]).exists()


def test_missing_bbox_does_not_crash_and_records_limitation(tmp_path):
    metadata = run_renderer_matched(
        tmp_path,
        {
            "event_id": "event-no-bbox",
            "event_type": "intrusion",
            "camera_id": "cam-1",
            "source_id": "source-1",
            "event_ts_ms": 123,
            "frame_uuid": "frame-no-bbox",
        },
    )
    assert metadata["annotations"]["person_bbox_status"] == "missing_required"
    assert metadata["required_annotation_status"] == "fail"
    assert any("person bbox unavailable" in item for item in metadata["limitations"])
    assert metadata["media"]["snapshot_annotation_status"] == "generated"


def test_person_bbox_sets_generated_status(tmp_path):
    metadata = run_renderer_matched(
        tmp_path,
        {
            "event_id": "event-person",
            "event_type": "intrusion",
            "frame_uuid": "frame-person",
            "payload": {"bbox": {"x": 100, "y": 20, "width": 90, "height": 180}},
        },
    )
    assert metadata["annotations"]["person_bbox_status"] == "generated"


def test_roi_polygon_sets_generated_status(tmp_path):
    metadata = run_renderer_matched(
        tmp_path,
        {
            "event_id": "event-roi",
            "event_type": "intrusion",
            "frame_uuid": "frame-roi",
            "payload": {
                "bbox": {"x": 100, "y": 20, "width": 90, "height": 180},
                "roi_polygon": [[10, 10], [300, 10], [300, 230], [10, 230]],
            },
        },
    )
    assert metadata["annotations"]["roi_status"] == "generated"


def test_source_extraction_limitation_and_not_production_label(tmp_path):
    metadata = run_renderer_matched(
        tmp_path,
        {
            "event_id": "event-limit",
            "event_type": "intrusion",
            "frame_uuid": "frame-limit",
            "payload": {"bbox": {"x": 100, "y": 20, "width": 90, "height": 180}},
        },
    )
    limitations = "\n".join(metadata["limitations"])
    assert "source extraction only; not Replay evidence" in limitations
    assert "not production evidence" in limitations
    assert metadata["visual_result_type"] != "production_evidence"
    assert metadata["not_production_evidence"] is True


def test_output_root_refuses_source_tree_dirs(tmp_path):
    metadata = run_renderer(
        tmp_path,
        {"event_id": "event-path", "event_type": "intrusion"},
        output_root=ROOT / "scripts" / "debug" / "visual_results",
    )
    output_root = Path(metadata["media"]["output_root"]).resolve()
    assert metadata["media"]["fallback_output_root"] is True
    assert "tmp/visual_results" in str(output_root)
    forbidden = ("docs", "harness", "scripts", "modules", "services")
    for key, value in metadata["media"].items():
        if key.endswith("_path") and value:
            rel = Path(value).resolve().relative_to(ROOT)
            assert rel.parts[0] not in forbidden


def test_face_bbox_list_is_supported(tmp_path):
    metadata = run_renderer_matched(
        tmp_path,
        {
            "source_observation_id": "face:source:7:123",
            "message_type": "face_observation",
            "camera_id": "cam-1",
            "source_id": "source-1",
            "track_id": "7",
            "timestamp_ms": 123,
            "face_bbox": [160, 120, 60, 80],
            "quality": 0.87,
            "payload": {"media": {"frame_uuid": "face-frame"}},
        },
    )
    assert metadata["annotations"]["face_bbox_status"] == "generated"
    assert metadata["source"]["event_ts_ms"] == 123
    assert metadata["source"]["frame_uuid"] == "face-frame"
    assert metadata["bbox"]["face_bbox_xyxy"] == [130, 80, 190, 160]


def test_track_id_falls_back_from_source_event_id(tmp_path):
    metadata = run_renderer_matched(
        tmp_path,
        {
            "event_id": "event-track-fallback",
            "source_event_id": "savant_security:cam-1:19:intrusion:123",
            "event_type": "intrusion",
            "camera_id": "cam-1",
            "source_id": "source-1",
            "track_id": None,
            "frame_uuid": "frame-1",
            "payload": {
                "bbox": {"x": 10, "y": 20, "width": 30, "height": 40},
                "roi_polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
            },
        },
        result_type="behavior_intrusion",
    )
    assert metadata["source"]["track_id"] == "19"
    assert metadata["source"]["track_id_source"] == "source_event_id_fallback"
    assert any("track_id recovered" in item for item in metadata["limitations"])


def test_intrusion_missing_roi_is_required_failure(tmp_path):
    metadata = run_renderer_matched(
        tmp_path,
        {
            "event_id": "event-no-roi",
            "source_event_id": "savant_security:cam-1:3:intrusion:123",
            "event_type": "intrusion",
            "camera_id": "cam-1",
            "source_id": "source-1",
            "frame_uuid": "frame-1",
            "payload": {"bbox": {"x": 10, "y": 20, "width": 30, "height": 40}},
        },
        result_type="behavior_intrusion",
    )
    assert metadata["annotations"]["roi_status"] == "missing_required"
    assert metadata["required_annotation_status"] == "fail"
    assert "intrusion ROI polygon is required" in metadata["required_annotation_failures"]


def test_bbox_xywh_to_xyxy():
    module = load_visual_module()
    result = module.parse_bbox_to_xyxy({"x": 10, "y": 20, "width": 30, "height": 40}, 320, 240, "xywh")
    assert result.xyxy == (10, 20, 40, 60)
    assert result.bbox_format == "xywh"


def test_bbox_xyxy_to_xyxy():
    module = load_visual_module()
    result = module.parse_bbox_to_xyxy([10, 20, 40, 60], 320, 240, "xyxy")
    assert result.xyxy == (10, 20, 40, 60)
    assert result.bbox_format == "xyxy"


def test_bbox_cxcywh_to_xyxy():
    module = load_visual_module()
    result = module.parse_bbox_to_xyxy([100, 80, 40, 20], 320, 240, "cxcywh")
    assert result.xyxy == (80, 70, 120, 90)
    assert result.bbox_format == "cxcywh"


def test_bbox_normalized_xywh_to_pixel_xyxy():
    module = load_visual_module()
    result = module.parse_bbox_to_xyxy([0.25, 0.25, 0.5, 0.5], 320, 240, "xywh")
    assert result.xyxy == (80, 60, 240, 180)
    assert result.normalized is True


def test_bbox_out_of_bounds_clamps_and_records_status():
    module = load_visual_module()
    result = module.parse_bbox_to_xyxy([-10, -20, 400, 300], 320, 240, "xyxy")
    assert result.xyxy == (0, 0, 319, 239)
    assert result.clamped is True


def test_face_observation_cxcywh_conversion(tmp_path):
    metadata = run_renderer_matched(
        tmp_path,
        {
            "source_observation_id": "face:source:7:123",
            "message_type": "face_observation",
            "track_id": "7",
            "timestamp_ms": 123,
            "face_bbox": [160, 120, 60, 80],
            "payload": {"media": {"frame_uuid": "face-frame"}},
        },
        result_type="face_observation",
        face_bbox_format="cxcywh",
    )
    assert metadata["bbox"]["face_bbox_format"] == "cxcywh"
    assert metadata["bbox"]["face_bbox_xyxy"] == [130, 80, 190, 160]
    assert metadata["required_annotation_status"] == "pass"


def test_missing_frame_uuid_image_blocks_visual_rendering(tmp_path):
    metadata = run_renderer(
        tmp_path,
        {
            "event_id": "event-blocked",
            "event_type": "intrusion",
            "frame_uuid": "frame-blocked",
            "payload": {
                "bbox": {"x": 10, "y": 20, "width": 30, "height": 40},
                "roi_polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
            },
        },
        result_type="behavior_intrusion",
    )
    assert metadata["diagnosis"]["frame_alignment_status"] == "blocked"
    assert metadata["media"]["snapshot_annotation_status"] == "blocked"
    assert metadata["media"]["annotated_snapshot_path"] is None
    assert "frame_uuid-aligned image is required" in metadata["required_annotation_failures"]


def test_image_frame_uuid_copied_from_record_without_proof_source_blocks(tmp_path):
    metadata = run_renderer(
        tmp_path,
        {
            "event_id": "event-self-proof",
            "event_type": "intrusion",
            "frame_uuid": "record-frame",
            "_image_frame_uuid": "record-frame",
            "payload": {
                "bbox": {"x": 10, "y": 20, "width": 30, "height": 40},
                "roi_polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
            },
        },
        result_type="behavior_intrusion",
        source_frame_uuid="record-frame",
    )
    assert metadata["diagnosis"]["frame_alignment_status"] == "blocked"
    assert metadata["diagnosis"]["image_frame_uuid_proof_source"] == "unknown"
    assert metadata["diagnosis"]["blocking_reason"] == "source_frame_without_independent_frame_uuid_proof"
    assert metadata["media"]["annotated_snapshot_path"] is None


def test_behavior_bbox_only_drawn_when_frame_alignment_matched(tmp_path):
    event = {
        "event_id": "event-matched",
        "event_type": "intrusion",
        "frame_uuid": "matched-frame",
        "payload": {
            "bbox": {"x": 10, "y": 20, "width": 30, "height": 40},
            "roi_polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
        },
    }
    metadata = run_renderer_matched(
        tmp_path,
        event,
        result_type="behavior_intrusion",
    )
    assert metadata["diagnosis"]["frame_alignment_status"] == "matched"
    assert metadata["diagnosis"]["image_frame_uuid_proof_source"] == "explicit_frame_uuid_index"
    assert metadata["annotations"]["person_bbox_status"] == "generated"
    assert metadata["media"]["annotated_snapshot_path"]
    assert metadata["diagnosis"]["bbox_drawn"] is True


def test_face_bbox_only_drawn_when_frame_alignment_matched(tmp_path):
    event = {
        "source_observation_id": "face:source:7:123",
        "message_type": "face_observation",
        "track_id": "7",
        "timestamp_ms": 123,
        "face_bbox": [160, 120, 60, 80],
        "payload": {"media": {"frame_uuid": "face-frame"}},
    }
    blocked = run_renderer(
        tmp_path,
        event,
        result_type="face_observation",
        face_bbox_format="cxcywh",
    )
    assert blocked["diagnosis"]["frame_alignment_status"] == "blocked"
    assert blocked["media"]["annotated_snapshot_path"] is None

    matched = run_renderer_matched(
        tmp_path,
        event,
        result_type="face_observation",
        face_bbox_format="cxcywh",
    )
    assert matched["diagnosis"]["frame_alignment_status"] == "matched"
    assert matched["diagnosis"]["image_frame_uuid_proof_source"] == "explicit_frame_uuid_index"
    assert matched["annotations"]["face_bbox_status"] == "generated"


def test_face_observation_marked_not_gallery_recognition(tmp_path):
    metadata = run_renderer_matched(
        tmp_path,
        {
            "source_observation_id": "face:source:7:123",
            "message_type": "face_observation",
            "track_id": "7",
            "timestamp_ms": 123,
            "face_bbox": [160, 120, 60, 80],
            "payload": {"media": {"frame_uuid": "face-frame"}},
        },
        result_type="face_observation",
    )
    assert metadata["diagnosis"]["is_gallery_recognition"] is False
    assert (
        metadata["diagnosis"]["recognition_semantics_status"]
        == "face_observation_only_not_gallery_match"
    )
    assert metadata["diagnosis"]["is_gallery_recognition"] is False


def test_different_frame_uuid_same_image_sha256_is_image_reuse_failure():
    module = load_visual_module()
    behavior = {
        "record_frame_uuid": "behavior-frame",
        "image_sha256": "same-sha",
    }
    face = {
        "record_frame_uuid": "face-frame",
        "image_sha256": "same-sha",
    }
    result = module.evaluate_cross_result_image_reuse(behavior, face)
    assert result["visual_resolver_status"] == "failed"
    assert result["reason"] == "different_records_reused_same_image"


def test_source_extraction_actual_frame_index_zero_blocks(tmp_path):
    module = load_visual_module()
    source_mp4 = tmp_path / "source.mp4"
    _frame, clip = write_test_media(tmp_path)
    source_mp4.write_bytes(clip.read_bytes())
    trace_root = tmp_path / "trace"
    trace_dir = trace_root / "source-1"
    trace_dir.mkdir(parents=True)
    (trace_dir / "trace.jsonl").write_text(
        json.dumps(
            {
                "source_id": "source-1",
                "frame_uuid": "frame-zero",
                "source_frame_index": 0,
                "frame_num": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    metadata = run_renderer(
        tmp_path,
        {
            "event_id": "event-zero",
            "event_type": "intrusion",
            "source_id": "source-1",
            "frame_uuid": "frame-zero",
            "payload": {
                "bbox": {"x": 10, "y": 20, "width": 30, "height": 40},
                "roi_polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
            },
        },
        result_type="behavior_intrusion",
        frame_trace_root=trace_root,
        source_mp4=source_mp4,
    )
    assert metadata["diagnosis"]["frame_alignment_status"] == "blocked"
    assert (
        metadata["diagnosis"]["blocking_reason"]
        == "source_extraction_returned_first_frame_or_unknown"
    )
    assert metadata["diagnosis"]["source_extraction"]["actual_frame_index"] == 0
    assert metadata["media"]["annotated_snapshot_path"] is None


def test_bbox_not_drawn_when_frame_proof_blocked(tmp_path):
    metadata = run_renderer(
        tmp_path,
        {
            "event_id": "event-blocked-box",
            "event_type": "intrusion",
            "frame_uuid": "blocked-frame",
            "payload": {
                "bbox": {"x": 10, "y": 20, "width": 30, "height": 40},
                "roi_polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
            },
        },
        result_type="behavior_intrusion",
    )
    assert metadata["diagnosis"]["frame_proof_status"] == "blocked"
    assert metadata["diagnosis"]["bbox_drawn"] is False
    assert metadata["media"]["snapshot_annotation_status"] == "blocked"


def test_matched_requires_trusted_proof_source(tmp_path):
    module = load_visual_module()
    frame, _clip = write_test_media(tmp_path)
    resolved = module.ResolvedImage(
        frame,
        width=320,
        height=240,
        frame_uuid="frame-1",
        resolution_status="matched",
        proof_source="unknown",
    )
    assert resolved.matched is False


def test_diagnosis_contains_raw_parsed_image_size_fields(tmp_path):
    metadata = run_renderer_matched(
        tmp_path,
        {
            "event_id": "event-diagnosis",
            "event_type": "intrusion",
            "frame_uuid": "frame-diagnosis",
            "payload": {
                "bbox": {"x": 10, "y": 20, "width": 30, "height": 40},
                "roi_polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
            },
        },
        result_type="behavior_intrusion",
    )
    diagnosis = metadata["diagnosis"]
    assert diagnosis["person_bbox_raw"] == {"x": 10, "y": 20, "width": 30, "height": 40}
    assert diagnosis["person_bbox_format"] == "xywh"
    assert diagnosis["person_bbox_xyxy"] == [10, 20, 40, 60]
    assert diagnosis["image_size"] == [320, 240]
    assert metadata["bbox"]["person_bbox_source_field"] == "bbox/person_bbox"


def test_smoke_and_docs_exist_and_document_boundaries():
    assert SCRIPT.exists()
    assert SMOKE.exists()
    assert DOC.exists()
    doc = DOC.read_text(encoding="utf-8")
    assert "debug/MVP visual output" in doc
    assert "not production evidence" in doc
    assert "source extraction only; not Replay evidence" in doc
    assert "raw_snapshot.jpg" in doc
    assert "annotated_snapshot.jpg" in doc
    assert "raw_clip.mp4" in doc
    assert "annotated_clip.mp4" in doc
    assert "metadata.json" in doc
    smoke = SMOKE.read_text(encoding="utf-8")
    assert "behavior_intrusion" in smoke
    assert "face_observation" in smoke
    assert "V1.4 BEHAVIOR_FRAME_PROOF_PASS" in smoke
    assert "V1.4 FACE_FRAME_PROOF_PASS" in smoke
    assert "V1.4 FRAME_PROOF_BLOCKED" in smoke
    assert "V1.4 IMAGE_REUSE_FAILED" in smoke
    assert "GALLERY_RECOGNITION_UNAVAILABLE" in smoke
