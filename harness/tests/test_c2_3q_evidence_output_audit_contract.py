"""C2.3Q evidence output correctness audit contract tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "scripts" / "tools" / "audit_c2_evidence_output_correctness.py"


def test_audit_summary_schema_and_csv_outputs(tmp_path: Path) -> None:
    audit = _load_audit_module()
    bundle_dir = _make_bundle(tmp_path, frame_count=2)
    output_dir = tmp_path / "audit"

    result = audit.audit_bundle(
        bundle_dir=bundle_dir,
        output_dir=output_dir,
        sample_frames=[0, 1],
        video_frame_count_reader=lambda _path: 2,
        frame_loader=_blank_frame,
        audit_time=_fixed_time(audit),
    )

    summary = json.loads(result.audit_summary_path.read_text(encoding="utf-8"))
    assert summary["result_marker"] == audit.RESULT_PASS
    assert summary["timeline"]["video_frame_count"] == 2
    assert summary["timeline"]["metadata_frame_count"] == 2
    assert summary["timeline"]["sidecar_frame_count"] == 2
    assert summary["summary_flags"]["annotation_source_kind"] == "production_sidecar"
    assert summary["identity_semantics"]["known_face_zero_expected_before_c2_4"] is True
    assert summary["identity_semantics"]["recognition_claim_allowed"] is False
    assert (output_dir / "index.html").is_file()
    assert (output_dir / "report.md").is_file()
    assert (output_dir / "contact_sheet.jpg").is_file()
    frame_header = (output_dir / "frame_table.csv").read_text(encoding="utf-8").splitlines()[0]
    object_header = (output_dir / "object_table.csv").read_text(encoding="utf-8").splitlines()[0]
    for field in audit.FRAME_TABLE_FIELDS:
        assert field in frame_header
    for field in audit.OBJECT_TABLE_FIELDS:
        assert field in object_header


def test_known_face_zero_does_not_fail_without_identity_binding(tmp_path: Path) -> None:
    audit = _load_audit_module()
    bundle_dir = _make_bundle(tmp_path, frame_count=1, objects=[_face_object()])

    result = audit.audit_bundle(
        bundle_dir=bundle_dir,
        output_dir=tmp_path / "audit",
        sample_frames=[0],
        video_frame_count_reader=lambda _path: 1,
        frame_loader=_blank_frame,
    )

    summary = result.summary
    assert summary["object_counts"]["known_face"] == 0
    assert summary["identity_semantics"]["identity_binding_connected"] is False
    assert summary["result_marker"] == audit.RESULT_PASS


def test_fallback_used_causes_fail(tmp_path: Path) -> None:
    audit = _load_audit_module()
    bundle_dir = _make_bundle(tmp_path, frame_count=1, summary_overrides={"fallback_used": True})

    result = audit.audit_bundle(
        bundle_dir=bundle_dir,
        output_dir=tmp_path / "audit",
        sample_frames=[0],
        video_frame_count_reader=lambda _path: 1,
        frame_loader=_blank_frame,
    )

    assert result.summary["result_marker"] == audit.RESULT_FAIL
    assert _codes(result.summary["failures"]) == {"fallback_used"}


def test_legacy_visual_binding_causes_fail(tmp_path: Path) -> None:
    audit = _load_audit_module()
    bundle_dir = _make_bundle(tmp_path, frame_count=1, summary_overrides={"legacy_used_for_visual_binding": True})

    result = audit.audit_bundle(
        bundle_dir=bundle_dir,
        output_dir=tmp_path / "audit",
        sample_frames=[0],
        video_frame_count_reader=lambda _path: 1,
        frame_loader=_blank_frame,
    )

    assert result.summary["result_marker"] == audit.RESULT_FAIL
    assert _codes(result.summary["failures"]) == {"legacy_used_for_visual_binding"}


def test_negative_width_bbox_is_failure(tmp_path: Path) -> None:
    audit = _load_audit_module()
    bad = _person_object()
    bad["bbox"]["xyxy"] = [80, 20, 40, 100]
    bundle_dir = _make_bundle(tmp_path, frame_count=1, objects=[bad])

    result = audit.audit_bundle(
        bundle_dir=bundle_dir,
        output_dir=tmp_path / "audit",
        sample_frames=[0],
        video_frame_count_reader=lambda _path: 1,
        frame_loader=_blank_frame,
    )

    assert result.summary["result_marker"] == audit.RESULT_FAIL
    assert "bbox_negative_or_zero_size" in _codes(result.summary["failures"])


def test_empty_frame_is_no_objects_not_fail(tmp_path: Path) -> None:
    audit = _load_audit_module()
    bundle_dir = _make_bundle(tmp_path, frame_count=1, objects=[])

    result = audit.audit_bundle(
        bundle_dir=bundle_dir,
        output_dir=tmp_path / "audit",
        sample_frames=[0],
        video_frame_count_reader=lambda _path: 1,
        frame_loader=_blank_frame,
    )

    assert result.summary["result_marker"] == audit.RESULT_PARTIAL
    assert result.summary["sample_results"][0]["audit_status"] == "no_objects"
    assert "all_sampled_frames_no_objects" in _codes(result.summary["warnings"])
    assert result.summary["failures"] == []


def _load_audit_module() -> Any:
    spec = importlib.util.spec_from_file_location("audit_c2_evidence_output_correctness", AUDIT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixed_time(audit: Any) -> Any:
    return audit.datetime(2026, 6, 7, 0, 0, tzinfo=audit.timezone.utc)


def _make_bundle(
    tmp_path: Path,
    *,
    frame_count: int,
    objects: list[dict[str, Any]] | None = None,
    summary_overrides: dict[str, Any] | None = None,
) -> Path:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "raw_clip.mov").write_bytes(b"not a real video when tests inject frame readers")
    metadata = [{"frame_num": index, "frame_pts": index * 1000, "objects": []} for index in range(frame_count)]
    (bundle_dir / "sink_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    rows = [_sidecar_row(index, objects if objects is not None else [_person_object(), _face_object()]) for index in range(frame_count)]
    with (bundle_dir / "annotations.frame_cache.identity.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    summary = {
        "annotation_source": "post_savant_sink_metadata",
        "production_ready": True,
        "sidecar_type": "production",
        "legacy_used_for_visual_binding": False,
        "fallback_used": False,
        "original_metadata_frame_count": frame_count,
        "decoded_video_frame_count": frame_count,
        "sidecar_frame_count": frame_count,
        "frame_count": frame_count,
        "trim_occurred": False,
        "timeline_reconciliation_status": "frame_counts_match",
        "object_counts": {"person": 1 if objects is None else sum(1 for obj in objects if obj["object_type"] == "person"), "face": 1 if objects is None else sum(1 for obj in objects if obj["object_type"] == "face"), "known_face": 0},
    }
    summary.update(summary_overrides or {})
    (bundle_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return bundle_dir


def _sidecar_row(index: int, objects: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "frame_index": index,
        "frame_pts": index * 1000,
        "source_id": "c2-test",
        "annotation_source": "post_savant_sink_metadata",
        "width": 320,
        "height": 240,
        "objects": objects,
    }


def _person_object() -> dict[str, Any]:
    return {
        "object_type": "person",
        "track_id": "7",
        "label": {"kind": "person"},
        "bbox": {"format": "xyxy", "xyxy": [40, 20, 160, 220], "confidence": 0.9},
        "pose": {
            "format": "coco17",
            "keypoints": [{"x": 80 + index, "y": 60 + index, "confidence": 0.8} for index in range(17)],
        },
    }


def _face_object() -> dict[str, Any]:
    return {
        "object_type": "face",
        "track_id": "7",
        "label": {"kind": "unknown_face"},
        "bbox": {"format": "xyxy", "xyxy": [70, 35, 120, 85], "confidence": 0.8},
        "landmarks": {"format": "5_point", "points": [[80, 50], [105, 50], [92, 62], [84, 75], [102, 75]]},
        "identity": {"source_observation_id": None, "visual_evidence_status": "observation_only"},
    }


def _blank_frame(_path: Path, _frame_index: int) -> Any:
    import numpy as np

    return np.zeros((240, 320, 3), dtype=np.uint8)


def _codes(issues: list[dict[str, Any]]) -> set[str]:
    return {str(issue["code"]) for issue in issues}
