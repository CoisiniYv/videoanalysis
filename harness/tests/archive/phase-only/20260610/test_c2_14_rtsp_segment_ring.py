"""C2.14 RTSP segment ring and event clip-builder contract tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RING_TOOL = ROOT / "scripts" / "tools" / "manage_c2_14_rtsp_segment_ring.py"
CLIP_TOOL = ROOT / "scripts" / "tools" / "build_c2_14_event_clip_from_ring.py"


def test_segment_index_requires_source_id() -> None:
    tool = _load(RING_TOOL)
    row = _index_row(source_id="")

    failures = tool.validate_segment_index_row(row)

    assert "source_id_required" in failures


def test_segment_index_requires_video_path_and_metadata_path() -> None:
    tool = _load(RING_TOOL)
    row = _index_row(video_path="", metadata_path="")

    failures = tool.validate_segment_index_row(row)

    assert "video_path_required" in failures
    assert "metadata_path_required" in failures


def test_segment_index_requires_first_last_pts_or_timestamps() -> None:
    tool = _load(RING_TOOL)
    row = _index_row(first_frame_pts=None, last_frame_pts=None, first_timestamp_ms=None, last_timestamp_ms=None)

    failures = tool.validate_segment_index_row(row)

    assert "first_last_pts_or_timestamps_required" in failures


def test_find_window_returns_segments_covering_requested_window(tmp_path: Path) -> None:
    tool = _load(RING_TOOL)
    _write_index(tmp_path, [_index_row(segment_id="seg-a", first_frame_pts=0, last_frame_pts=12_000_000_000)])

    result = tool.find_window(
        ring_root=tmp_path,
        source_id="c2_post_savant_fps_probe",
        center_pts=6_000_000_000,
        center_timestamp_ms=None,
        pre_seconds=5,
        post_seconds=5,
    )

    assert result["status"] == "pass"
    assert result["window_covered"] is True
    assert result["selected_segments"][0]["segment_id"] == "seg-a"


def test_find_window_fails_clearly_if_no_coverage(tmp_path: Path) -> None:
    tool = _load(RING_TOOL)
    _write_index(tmp_path, [_index_row(segment_id="seg-a", first_frame_pts=0, last_frame_pts=1_000_000_000)])

    result = tool.find_window(
        ring_root=tmp_path,
        source_id="c2_post_savant_fps_probe",
        center_pts=20_000_000_000,
        center_timestamp_ms=None,
        pre_seconds=5,
        post_seconds=5,
    )

    assert result["status"] == "partial"
    assert result["reason"] == "no_segment_overlap"


def test_retention_never_deletes_outside_ring_root(tmp_path: Path) -> None:
    tool = _load(RING_TOOL)
    outside = tmp_path / "outside-segment"
    outside.mkdir()
    (outside / "keep.txt").write_text("do-not-delete", encoding="utf-8")
    _write_index(tmp_path, [_index_row(segment_id="outside", segment_dir=outside, created_at=_old_iso())])

    plan = tool.retention_plan(
        ring_root=tmp_path,
        source_id="c2_post_savant_fps_probe",
        ttl_seconds=1,
        max_bytes=1,
        min_keep_seconds=0,
        now=datetime.now(timezone.utc),
    )
    result = tool.apply_retention_plan(plan, dry_run=False)

    assert result["delete_candidate_count"] == 0
    assert result["unsafe_rows_skipped"][0]["reason"] == "outside_ring_segments_root"
    assert outside.exists()


def test_retention_never_deletes_evidence_referenced_segments(tmp_path: Path) -> None:
    tool = _load(RING_TOOL)
    source_root = tmp_path / "c2_post_savant_fps_probe" / "segments"
    old_dir = source_root / "old-referenced"
    new_dir = source_root / "active"
    _make_segment_dir(old_dir)
    _make_segment_dir(new_dir)
    rows = [
        _index_row(segment_id="old-referenced", segment_dir=old_dir, evidence_refs=["bundle-a"], created_at=_old_iso()),
        _index_row(segment_id="active", segment_dir=new_dir, created_at=_now_iso()),
    ]
    _write_index(tmp_path, rows)

    plan = tool.retention_plan(
        ring_root=tmp_path,
        source_id="c2_post_savant_fps_probe",
        ttl_seconds=1,
        max_bytes=1,
        min_keep_seconds=0,
        now=datetime.now(timezone.utc),
    )

    assert all(item["segment_id"] != "old-referenced" for item in plan["delete_candidates"])
    assert any("evidence_referenced" in item["skip_reasons"] for item in plan["skipped_segments"])


def test_retention_dry_run_does_not_delete_files(tmp_path: Path) -> None:
    tool = _load(RING_TOOL)
    segment_dir = tmp_path / "c2_post_savant_fps_probe" / "segments" / "old"
    _make_segment_dir(segment_dir)
    active_dir = tmp_path / "c2_post_savant_fps_probe" / "segments" / "active"
    _make_segment_dir(active_dir)
    _write_index(
        tmp_path,
        [
            _index_row(segment_id="old", segment_dir=segment_dir, created_at=_old_iso()),
            _index_row(segment_id="active", segment_dir=active_dir, created_at=_now_iso()),
        ],
    )

    plan = tool.retention_plan(
        ring_root=tmp_path,
        source_id="c2_post_savant_fps_probe",
        ttl_seconds=1,
        max_bytes=1,
        min_keep_seconds=0,
        now=datetime.now(timezone.utc),
    )
    result = tool.apply_retention_plan(plan, dry_run=True)

    assert result["delete_candidate_count"] == 1
    assert result["deleted_count"] == 0
    assert segment_dir.exists()


def test_clip_builder_cannot_pass_without_video_integrity() -> None:
    tool = _load(CLIP_TOOL)

    result = tool.evaluate_clip_acceptance(
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

    assert result["passed"] is False
    assert "video_integrity_failed" in result["failure_reasons"]


def test_clip_builder_cannot_use_db_fallback_as_evidence_pass() -> None:
    tool = _load(CLIP_TOOL)

    result = tool.evaluate_clip_acceptance(
        raw_clip_exists=True,
        metadata_exists=True,
        sidecar_exists=True,
        selected_segment_window_covers_event=True,
        decoded_video_frame_count=10,
        sidecar_frame_count=10,
        video_integrity_pass=True,
        fallback_used=False,
        legacy_used_for_visual_binding=False,
        db_window_fallback_used=True,
        event_style_replay_job_passed=False,
    )

    assert result["passed"] is False
    assert "db_window_fallback_used" in result["failure_reasons"]


def test_event_style_replay_job_passed_false_preserved() -> None:
    tool = _load(CLIP_TOOL)

    result = tool.evaluate_clip_acceptance(
        raw_clip_exists=True,
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

    assert result["passed"] is True
    assert result["event_style_replay_job_passed"] is False


def test_event_style_replay_job_true_cannot_pass() -> None:
    tool = _load(CLIP_TOOL)

    result = tool.evaluate_clip_acceptance(
        raw_clip_exists=True,
        metadata_exists=True,
        sidecar_exists=True,
        selected_segment_window_covers_event=True,
        decoded_video_frame_count=10,
        sidecar_frame_count=10,
        video_integrity_pass=True,
        fallback_used=False,
        legacy_used_for_visual_binding=False,
        db_window_fallback_used=False,
        event_style_replay_job_passed=True,
    )

    assert result["passed"] is False
    assert "event_style_replay_job_passed_must_be_false" in result["failure_reasons"]


def test_unsafe_payload_scan_rejects_embedding_and_image_bytes() -> None:
    tool = _load(RING_TOOL)

    scan = tool.scan_for_unsafe_payload(
        {
            "payload": {
                "embedding": [0.1] * 512,
                "image_base64": "abc",
                "safe_bbox": [1, 2, 3, 4],
            }
        }
    )

    assert scan["passed"] is False
    assert scan["payload_has_embedding"] is True
    assert scan["payload_has_image_bytes"] is True


def test_index_existing_skips_source_mismatch_instead_of_fake_index(tmp_path: Path) -> None:
    tool = _load(RING_TOOL)
    source_dir = tmp_path / "sink"
    segment = source_dir / "wrong" / "unknown"
    segment.mkdir(parents=True)
    (segment / "video.mov").write_bytes(b"not-real-video")
    _write_metadata(segment / "metadata.json", source_id="wrong_source")

    result = tool.index_existing(
        source_dir=source_dir,
        ring_root=tmp_path / "ring",
        source_id="c2_post_savant_fps_probe",
    )

    assert result["compatible_segments_found"] == 0
    assert result["skipped"][0]["reason"] == "source_id_mismatch"


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


def _make_segment_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "segment_manifest.json").write_text("{}", encoding="utf-8")
    (path / "video.mov").write_bytes(b"segment")
    (path / "metadata.json").write_text("{}", encoding="utf-8")


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
