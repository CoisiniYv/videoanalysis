"""C2.14B runtime RTSP segment writer and event clip contract tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SMOKE_TOOL = ROOT / "scripts" / "tools" / "run_c2_14b_rtsp_segment_writer_smoke.py"
RING_TOOL = ROOT / "scripts" / "tools" / "manage_c2_14_rtsp_segment_ring.py"
CLIP_TOOL = ROOT / "scripts" / "tools" / "build_c2_14_event_clip_from_ring.py"


def test_pass_requires_rtsp_input() -> None:
    tool = _load(SMOKE_TOOL)

    result = tool.validate_writer_pass_requirements(
        input_type="file",
        source_id="c2_post_savant_fps_probe",
        expected_source_id="c2_post_savant_fps_probe",
        chunk_size=120,
        segment_count=1,
        indexed_rows=1,
        source_mismatch_count=0,
    )

    assert result["passed"] is False
    assert "input_not_rtsp" in result["failure_reasons"]


def test_pass_requires_compatible_segment_with_matching_source_id() -> None:
    tool = _load(SMOKE_TOOL)

    result = tool.validate_writer_pass_requirements(
        input_type="rtsp",
        source_id="c2_post_savant_fps_probe",
        expected_source_id="c2_post_savant_fps_probe",
        chunk_size=120,
        segment_count=0,
        indexed_rows=0,
        source_mismatch_count=0,
    )

    assert result["passed"] is False
    assert "no_compatible_segments" in result["failure_reasons"]
    assert "no_index_rows" in result["failure_reasons"]


def test_source_id_mismatch_segments_are_rejected(tmp_path: Path) -> None:
    ring_tool = _load(RING_TOOL)
    segment = tmp_path / "ring" / "c2_post_savant_fps_probe" / "segments" / "0000"
    segment.mkdir(parents=True)
    (segment / "video.mov").write_bytes(b"not-real-video")
    _write_metadata(segment / "metadata.json", source_id="wrong_source")

    result = ring_tool.index_existing(
        source_dir=tmp_path / "ring" / "c2_post_savant_fps_probe" / "segments",
        ring_root=tmp_path / "ring",
        source_id="c2_post_savant_fps_probe",
        completed_age_seconds=0,
    )

    assert result["compatible_segments_found"] == 0
    assert result["skipped"][0]["reason"] == "source_id_mismatch"


def test_chunk_size_zero_cannot_satisfy_runtime_writer_pass() -> None:
    tool = _load(SMOKE_TOOL)

    result = tool.validate_writer_pass_requirements(
        input_type="rtsp",
        source_id="c2_post_savant_fps_probe",
        expected_source_id="c2_post_savant_fps_probe",
        chunk_size=0,
        segment_count=1,
        indexed_rows=1,
        source_mismatch_count=0,
    )

    assert result["passed"] is False
    assert "chunk_size_not_ring_ready" in result["failure_reasons"]


def test_retention_dry_run_never_deletes_outside_ring_root(tmp_path: Path) -> None:
    ring_tool = _load(RING_TOOL)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    _write_index(tmp_path, [_index_row(segment_id="outside", segment_dir=outside)])

    plan = ring_tool.retention_plan(
        ring_root=tmp_path,
        source_id="c2_post_savant_fps_probe",
        ttl_seconds=1,
        max_bytes=1,
        min_keep_seconds=0,
        now=datetime.now(timezone.utc),
    )
    result = ring_tool.apply_retention_plan(plan, dry_run=True)

    assert result["delete_candidate_count"] == 0
    assert result["deleted_count"] == 0
    assert outside.exists()


def test_clip_builder_requires_event_time_within_segment_range(tmp_path: Path) -> None:
    ring_tool = _load(RING_TOOL)
    _write_index(tmp_path, [_index_row(first_frame_pts=0, last_frame_pts=1_000_000_000)])

    window = ring_tool.find_window(
        ring_root=tmp_path,
        source_id="c2_post_savant_fps_probe",
        center_pts=20_000_000_000,
        center_timestamp_ms=None,
        pre_seconds=1,
        post_seconds=1,
    )

    assert window["status"] == "partial"
    assert window["reason"] == "no_segment_overlap"


def test_clip_builder_cannot_pass_without_raw_clip() -> None:
    clip_tool = _load(CLIP_TOOL)

    acceptance = clip_tool.evaluate_clip_acceptance(
        raw_clip_exists=False,
        metadata_exists=True,
        sidecar_exists=True,
        selected_segment_window_covers_event=True,
        decoded_video_frame_count=10,
        sidecar_frame_count=10,
        video_integrity_pass=True,
        fallback_used=False,
        legacy_used_for_visual_binding=False,
        db_window_fallback_used=False,
        event_style_replay_job_passed=False,
    )

    assert acceptance["passed"] is False
    assert "raw_clip_missing" in acceptance["failure_reasons"]


def test_clip_builder_cannot_pass_without_video_integrity() -> None:
    clip_tool = _load(CLIP_TOOL)

    acceptance = clip_tool.evaluate_clip_acceptance(
        raw_clip_exists=True,
        metadata_exists=True,
        sidecar_exists=True,
        selected_segment_window_covers_event=True,
        decoded_video_frame_count=10,
        sidecar_frame_count=10,
        video_integrity_pass=False,
        fallback_used=False,
        legacy_used_for_visual_binding=False,
        db_window_fallback_used=False,
        event_style_replay_job_passed=False,
    )

    assert acceptance["passed"] is False
    assert "video_integrity_failed" in acceptance["failure_reasons"]


def test_event_style_replay_not_claimed() -> None:
    tool = _load(SMOKE_TOOL)

    result = tool.validate_clip_candidate(
        event={"event_type": "intrusion", "frame_pts": 5},
        window={"status": "pass"},
        clip_result={"summary": {"raw_clip": "raw.mov", "video_integrity_pass": True, "event_style_replay_job_passed": True, "db_window_fallback_used": False, "legacy_used_for_visual_binding": False}},
    )

    assert result["passed"] is False
    assert "event_style_replay_claimed" in result["failure_reasons"]


def test_db_fallback_legacy_fallback_cannot_satisfy_pass() -> None:
    tool = _load(SMOKE_TOOL)

    result = tool.validate_clip_candidate(
        event={"event_type": "intrusion", "frame_pts": 5},
        window={"status": "pass"},
        clip_result={"summary": {"raw_clip": "raw.mov", "video_integrity_pass": True, "event_style_replay_job_passed": False, "db_window_fallback_used": True, "legacy_used_for_visual_binding": True}},
    )

    assert result["passed"] is False
    assert "db_window_fallback_used" in result["failure_reasons"]
    assert "legacy_used_for_visual_binding" in result["failure_reasons"]


def test_unsafe_payload_scan_rejects_embedding_image_bytes() -> None:
    ring_tool = _load(RING_TOOL)

    scan = ring_tool.scan_for_unsafe_payload(
        {
            "event": {
                "embedding": [0.1] * 128,
                "image_base64": "abc",
            }
        }
    )

    assert scan["passed"] is False
    assert scan["payload_has_embedding"] is True
    assert scan["payload_has_image_bytes"] is True


def test_no_event_in_window_returns_partial_not_fake_event() -> None:
    tool = _load(SMOKE_TOOL)

    marker = tool.decide_marker(
        writer_validation={"passed": True},
        event_capture={"selected_event": None},
        window=None,
        clip_result=None,
        retention={"deleted_count": 0, "unsafe_deletion_target_count": 0},
        unsafe_scan={"passed": True},
    )

    assert marker == tool.MARKER_NO_EVENT


def test_event_near_boundary_returns_partial_if_requested_window_not_covered(tmp_path: Path) -> None:
    ring_tool = _load(RING_TOOL)
    _write_index(tmp_path, [_index_row(first_frame_pts=0, last_frame_pts=6_000_000_000)])

    window = ring_tool.find_window(
        ring_root=tmp_path,
        source_id="c2_post_savant_fps_probe",
        center_pts=1_000_000_000,
        center_timestamp_ms=None,
        pre_seconds=5,
        post_seconds=5,
    )

    assert window["status"] == "partial"
    assert window["reason"] == "segment_window_gap"


def test_single_segment_crop_requires_contiguous_selected_frames(tmp_path: Path) -> None:
    clip_tool = _load(CLIP_TOOL)

    result = clip_tool.crop_window_clip_from_segment(
        segment={"video_path": str(tmp_path / "video.mov")},
        selected_indices=[1, 2, 4],
        output_path=tmp_path / "raw_clip.mp4",
        requested_duration_s=10,
    )

    assert result["status"] == "failed"
    assert result["reason"] == "non_contiguous_frame_window"


def _load(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def _index_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "schema_version": "1.0-c2.14-segment-ring",
        "source_id": "c2_post_savant_fps_probe",
        "segment_id": "seg-a",
        "segment_dir": "/tmp/ring/c2_post_savant_fps_probe/segments/seg-a",
        "video_path": "/tmp/ring/c2_post_savant_fps_probe/segments/seg-a/video.mov",
        "metadata_path": "/tmp/ring/c2_post_savant_fps_probe/segments/seg-a/metadata.json",
        "first_frame_pts": 0,
        "last_frame_pts": 10_000_000_000,
        "first_timestamp_ms": 0,
        "last_timestamp_ms": 10_000,
        "first_frame_uuid": "frame-a",
        "last_frame_uuid": "frame-z",
        "frame_count": 10,
        "keyframe_count": 1,
        "size_bytes": 10,
        "created_at": _old_iso(),
        "expires_at": _old_iso(),
        "evidence_refs": [],
        "cleanup_eligible": True,
        "completed": True,
    }
    row.update(overrides)
    if isinstance(row.get("segment_dir"), Path):
        row["segment_dir"] = str(row["segment_dir"])
    return row


def _write_index(root: Path, rows: list[dict[str, Any]]) -> None:
    index = root / "c2_post_savant_fps_probe" / "segment_index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _write_metadata(path: Path, *, source_id: str) -> None:
    frames = [
        {
            "type": "VideoFrame",
            "source_id": source_id,
            "uuid": "frame-a",
            "pts": 1_000_000_000,
            "duration": 41_708_333,
            "keyframe": True,
            "objects": [],
        }
    ]
    path.write_text("".join(json.dumps(frame) + "\n" for frame in frames), encoding="utf-8")


def _old_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
