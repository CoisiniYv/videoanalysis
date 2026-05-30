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


def run_renderer(tmp_path: Path, event: dict, output_root: Path | None = None) -> dict:
    module = load_visual_module()
    frame, clip = write_test_media(tmp_path)
    event_json = tmp_path / "event.json"
    event_json.write_text(json.dumps(event), encoding="utf-8")
    args = SimpleNamespace(
        event_id=None,
        event_json=str(event_json),
        a2a_summary_json=None,
        database_url=None,
        source_frame=str(frame),
        source_clip=str(clip),
        output_root=str(output_root or (tmp_path / "visual_results")),
        run_id="test_run",
        mode="both",
    )
    return module.generate_visual_result(args)


def test_metadata_schema_required_fields(tmp_path):
    metadata = run_renderer(
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
    assert metadata["debug_only"] is True
    assert metadata["not_production_evidence"] is True
    assert metadata["source"]["event_id"] == "event-1"
    assert metadata["source"]["frame_uuid"] == "frame-uuid"
    assert Path(metadata["media"]["metadata_path"]).exists()
    assert Path(metadata["media"]["report_path"]).exists()
    assert Path(metadata["media"]["raw_snapshot_path"]).exists()
    assert Path(metadata["media"]["annotated_snapshot_path"]).exists()


def test_missing_bbox_does_not_crash_and_records_limitation(tmp_path):
    metadata = run_renderer(
        tmp_path,
        {
            "event_id": "event-no-bbox",
            "event_type": "intrusion",
            "camera_id": "cam-1",
            "source_id": "source-1",
            "event_ts_ms": 123,
        },
    )
    assert metadata["annotations"]["person_bbox_status"] == "unavailable"
    assert any("person bbox unavailable" in item for item in metadata["limitations"])
    assert metadata["media"]["snapshot_annotation_status"] == "generated"


def test_person_bbox_sets_generated_status(tmp_path):
    metadata = run_renderer(
        tmp_path,
        {
            "event_id": "event-person",
            "event_type": "intrusion",
            "payload": {"bbox": {"x": 100, "y": 20, "width": 90, "height": 180}},
        },
    )
    assert metadata["annotations"]["person_bbox_status"] == "generated"


def test_roi_polygon_sets_generated_status(tmp_path):
    metadata = run_renderer(
        tmp_path,
        {
            "event_id": "event-roi",
            "event_type": "intrusion",
            "payload": {
                "bbox": {"x": 100, "y": 20, "width": 90, "height": 180},
                "roi_polygon": [[10, 10], [300, 10], [300, 230], [10, 230]],
            },
        },
    )
    assert metadata["annotations"]["roi_status"] == "generated"


def test_source_extraction_limitation_and_not_production_label(tmp_path):
    metadata = run_renderer(
        tmp_path,
        {
            "event_id": "event-limit",
            "event_type": "intrusion",
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
    metadata = run_renderer(
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
