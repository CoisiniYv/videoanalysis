"""C2.3B-R2 video integrity gate contract tests."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"


def test_fixed_frame_count_240_is_not_accepted_as_time_window() -> None:
    replay_client = _activate_clip_module("app.replay_client")

    payload = replay_client.build_job_payload(
        source_id="c2_post_savant_fps_probe",
        keyframe_uuid="frame-0",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        labels={"event_id": "event-c2"},
        stop_condition_mode="ts_delta_sec",
        fps=24,
        force_constant_cadence=True,
    )

    assert "ts_delta_sec" in payload["stop_condition"]
    assert payload["stop_condition"]["ts_delta_sec"]["max_delta_sec"] == 10.0
    assert "frame_count" not in payload["stop_condition"]


def test_first_frame_not_keyframe_with_late_keyframe_fails() -> None:
    gate = _activate_media_module("app.post_savant_video_integrity")

    result = gate.evaluate_video_integrity(_base_stats(
        first_frame_keyframe=False,
        first_keyframe_pts_time=18.685,
    ))

    assert result["integrity_status"] == "fail"
    assert "first_frame_not_keyframe_and_first_keyframe_far_from_start" in result["failure_reasons"]


def test_pts_large_jump_fails() -> None:
    gate = _activate_media_module("app.post_savant_video_integrity")

    result = gate.evaluate_video_integrity(_base_stats(
        pts_large_jump_detected=True,
        max_packet_duration_s=5.13,
    ))

    assert result["integrity_status"] == "fail"
    assert "pts_large_jump_detected" in result["failure_reasons"]
    assert "max_packet_duration_exceeds_threshold" in result["failure_reasons"]


def test_decode_errors_fail() -> None:
    gate = _activate_media_module("app.post_savant_video_integrity")

    result = gate.evaluate_video_integrity(_base_stats(decode_error_count=1))

    assert result["integrity_status"] == "fail"
    assert "decode_error_count_gt_zero" in result["failure_reasons"]


def test_clean_video_with_matching_sidecar_passes() -> None:
    gate = _activate_media_module("app.post_savant_video_integrity")

    result = gate.evaluate_video_integrity(_base_stats())

    assert result["integrity_status"] == "pass"
    assert result["production_gate_passed"] is True


def test_clean_replay_video_with_sparse_sidecar_passes() -> None:
    gate = _activate_media_module("app.post_savant_video_integrity")

    result = gate.evaluate_video_integrity(_base_stats(sidecar_frame_count=75))

    assert result["integrity_status"] == "pass"
    assert result["production_gate_passed"] is True
    assert "decoded_frame_count_sidecar_frame_count_mismatch" not in result["failure_reasons"]


def test_trim_without_time_domain_crop_fails() -> None:
    gate = _activate_media_module("app.post_savant_video_integrity")

    result = gate.evaluate_video_integrity(_base_stats(
        trim_occurred=True,
        time_domain_crop_applied=False,
    ))

    assert result["integrity_status"] == "fail"
    assert "trim_occurred_without_declared_time_domain_crop" in result["failure_reasons"]


def test_known_face_zero_does_not_affect_c2_3b_r2_gate() -> None:
    gate = _activate_media_module("app.post_savant_video_integrity")
    stats = _base_stats()
    stats["object_counts"] = {"person": 1, "face": 1, "known_face": 0}

    result = gate.evaluate_video_integrity(stats)

    assert result["integrity_status"] == "pass"
    assert result["production_gate_passed"] is True
    assert result["object_counts"]["known_face"] == 0


def test_c2_bundle_summary_is_blocked_by_video_integrity_gate(tmp_path: Path) -> None:
    bundle = _activate_media_module("app.post_savant_evidence_bundle")
    input_dir = _make_sink_output(tmp_path, frame_count=3)
    output_dir = tmp_path / "evidence"

    result = bundle.build_post_savant_evidence_bundle(
        input_dir=input_dir,
        output_dir=output_dir,
        copy_video=True,
        trim_sidecar_to_video=True,
        decoded_frame_count_reader=lambda _path: 3,
        video_integrity_required=False,
        event_metadata={"replay_source_kind": "post_savant"},
    )
    failed_integrity = _activate_media_module("app.post_savant_video_integrity").evaluate_video_integrity(
        _base_stats(first_frame_keyframe=False, first_keyframe_pts_time=18.685)
    )
    blocked = bundle._bundle_summary(
        sidecar_summary=result.summary,
        source_metadata_frame_count=3,
        original_metadata_frame_count=3,
        decoded_video_frame_count=3,
        sidecar_frame_count=3,
        trim_occurred=False,
        timeline_reconciliation_status="frame_counts_match",
        fps={},
        event_metadata={"replay_source_kind": "post_savant"},
        video_integrity=failed_integrity,
        video_integrity_required=True,
    )

    assert blocked["production_ready"] is False
    assert blocked["annotation_status"] == "video_integrity_failed"
    assert blocked["video_integrity"]["production_gate_passed"] is False


def _activate_media_module(module_name: str) -> Any:
    return _activate_service_module(MEDIA_WORKER_ROOT, module_name)


def _activate_clip_module(module_name: str) -> Any:
    return _activate_service_module(ROOT / "services" / "clip-worker", module_name)


def _activate_service_module(service_root: Path, module_name: str) -> Any:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    service_root_text = str(service_root)
    if service_root_text in sys.path:
        sys.path.remove(service_root_text)
    sys.path.insert(0, service_root_text)
    return importlib.import_module(module_name)


def _base_stats(**overrides: Any) -> dict[str, Any]:
    stats = {
        "duration_s": 10.0,
        "decoded_frame_count": 240,
        "sidecar_frame_count": 240,
        "requested_duration_s": 10.0,
        "decode_error_count": 0,
        "first_frame_keyframe": True,
        "first_keyframe_pts_time": 0.0,
        "pts_monotonic": True,
        "dts_monotonic": True,
        "pts_large_jump_detected": False,
        "dts_large_jump_detected": False,
        "max_packet_duration_s": 0.0417,
        "trim_occurred": False,
        "time_domain_crop_applied": False,
    }
    stats.update(overrides)
    return stats


def _make_sink_output(tmp_path: Path, *, frame_count: int) -> Path:
    input_dir = tmp_path / "sink"
    input_dir.mkdir()
    (input_dir / "video.mov").write_bytes(b"fake video")
    rows = [
        {
            "type": "VideoFrame",
            "source_id": "c2-poc",
            "uuid": f"frame-{index}",
            "pts": 1_000_000 + index * 41_666_667,
            "width": 1920,
            "height": 1080,
            "objects": [
                {
                    "namespace": "yolo26_pose",
                    "label": "person",
                    "id": index,
                    "confidence": 0.8,
                    "detection_box": {
                        "xc": 55,
                        "yc": 110,
                        "width": 90,
                        "height": 180,
                    },
                }
            ],
        }
        for index in range(frame_count)
    ]
    (input_dir / "metadata.json").write_text(
        "".join(__import__("json").dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return input_dir
