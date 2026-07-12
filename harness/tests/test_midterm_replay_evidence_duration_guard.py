"""Post-Savant replay evidence duration guard tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
MEDIA_WORKER_ROOT = str(REPO_ROOT / "services" / "media-worker")
for name in list(sys.modules):
    if name == "app" or name.startswith("app."):
        del sys.modules[name]
if MEDIA_WORKER_ROOT in sys.path:
    sys.path.remove(MEDIA_WORKER_ROOT)
sys.path.insert(0, MEDIA_WORKER_ROOT)

from app import worker  # noqa: E402


EVENT_ID = "11111111-1111-4111-8111-111111111111"


def test_time_domain_crop_failure_does_not_publish_source_replay_output(
    monkeypatch, tmp_path: Path
) -> None:
    sink_dir = tmp_path / "sink" / f"replay-event-{EVENT_ID}-00000000"
    sink_dir.mkdir(parents=True)
    source_video = sink_dir / "video.mov"
    source_video.write_bytes(b"full replay source bytes")
    metadata_file = sink_dir / "metadata.json"
    metadata_file.write_text(json.dumps({"job_id": "job-1"}) + "\n", encoding="utf-8")
    output_root = tmp_path / "evidence"
    calls: dict[str, object] = {}

    monkeypatch.setenv("FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED", "true")
    monkeypatch.setenv("POST_SAVANT_DURATION_GUARD_SLACK_SEC", "1")
    monkeypatch.setenv("EVIDENCE_RUNTIME_EPOCH_STRICT", "false")
    monkeypatch.setattr(worker, "_load_event_context", lambda _conn, _event_id: _event_context())
    monkeypatch.setattr(worker, "load_native_metadata", lambda _path: _metadata_rows_outside_window())
    monkeypatch.setattr(
        worker,
        "read_decoded_video_frame_count",
        lambda _path: (_ for _ in ()).throw(AssertionError("raw clip should be absent")),
    )
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 61.35)
    monkeypatch.setattr(
        worker,
        "write_frame_cache_identity_sidecar",
        lambda **kwargs: _sidecar_result(kwargs, calls),
    )

    bundle = worker._finalize_post_savant_evidence_bundle(
        None,
        event_id=EVENT_ID,
        meta_dir=str(sink_dir),
        metadata_file=str(metadata_file),
        evidence_output_dir=str(output_root),
    )

    raw_clip = output_root / EVENT_ID / "raw_clip.mov"
    metadata = _read_json(output_root / EVENT_ID / "metadata.json")
    summary = _read_json(output_root / EVENT_ID / "summary.json")

    assert calls["raw_clip_path"] is None
    assert not raw_clip.exists()
    assert bundle["raw_clip"] == ""
    assert bundle["clip_status"] == "duration_guard_failed"
    assert bundle["duration_guard_failed"] is True
    assert summary["time_domain_crop_failed"] is True
    assert summary["duration_guard_status"] == "failed"
    assert summary["raw_clip_path"] is None
    assert metadata["media"]["raw_clip_path"] == ""
    assert metadata["status"]["clip_status"] == "duration_guard_failed"
    assert metadata["event"]["alarm_machine_time"] == "2026-06-11T02:05:06+00:00"
    assert metadata["event"]["alarm_machine_time_source"] == "events.created_at"
    assert source_video.read_bytes() == b"full replay source bytes"


def test_post_savant_final_duration_guard_removes_overlong_raw_clip(
    monkeypatch, tmp_path: Path
) -> None:
    sink_dir = tmp_path / "sink" / f"replay-event-{EVENT_ID}-00000000"
    sink_dir.mkdir(parents=True)
    source_video = sink_dir / "video.mov"
    source_video.write_bytes(b"overlong replay source")
    metadata_file = sink_dir / "metadata.json"
    metadata_file.write_text(json.dumps({"job_id": "job-1"}) + "\n", encoding="utf-8")
    output_root = tmp_path / "evidence"

    monkeypatch.setenv("FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED", "true")
    monkeypatch.setenv("POST_SAVANT_DURATION_GUARD_SLACK_SEC", "1")
    monkeypatch.setenv("EVIDENCE_RUNTIME_EPOCH_STRICT", "false")
    monkeypatch.setattr(worker, "_load_event_context", lambda _conn, _event_id: _event_context())
    monkeypatch.setattr(worker, "load_native_metadata", lambda _path: _metadata_rows_inside_window())
    monkeypatch.setattr(worker, "read_decoded_video_frame_count", lambda _path: 10)
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 61.35)
    monkeypatch.setattr(worker, "_copy_or_crop_video", _write_overlong_crop)
    monkeypatch.setattr(
        worker,
        "write_frame_cache_identity_sidecar",
        lambda **kwargs: _sidecar_result(kwargs, {}),
    )

    bundle = worker._finalize_post_savant_evidence_bundle(
        None,
        event_id=EVENT_ID,
        meta_dir=str(sink_dir),
        metadata_file=str(metadata_file),
        evidence_output_dir=str(output_root),
    )

    raw_clip = output_root / EVENT_ID / "raw_clip.mov"
    metadata = _read_json(output_root / EVENT_ID / "metadata.json")
    summary = _read_json(output_root / EVENT_ID / "summary.json")

    assert not raw_clip.exists()
    assert bundle["raw_clip"] == ""
    assert bundle["clip_status"] == "duration_guard_failed"
    assert bundle["duration_guard_failed"] is True
    assert bundle["max_allowed_duration_seconds"] == 11.0
    assert summary["duration_guard_status"] == "failed"
    assert summary["duration_guard_failed"] is True
    assert summary["raw_clip_duration"] == 61.35
    assert summary["max_allowed_duration_seconds"] == 11.0
    assert metadata["media"]["raw_clip_path"] == ""
    assert metadata["status"]["clip_status"] == "duration_guard_failed"


def test_fast_raw_clip_keeps_overlong_imprecise_replay_output(
    monkeypatch, tmp_path: Path
) -> None:
    sink_dir = tmp_path / "sink" / f"replay-event-{EVENT_ID}-00000000"
    sink_dir.mkdir(parents=True)
    source_video = sink_dir / "video.mov"
    source_video.write_bytes(b"full replay source bytes")
    metadata_file = sink_dir / "metadata.json"
    metadata_file.write_text(json.dumps({"job_id": "job-1"}) + "\n", encoding="utf-8")
    output_root = tmp_path / "evidence"

    monkeypatch.setenv("POST_SAVANT_FAST_RAW_CLIP_ENABLED", "true")
    monkeypatch.setenv("FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED", "true")
    monkeypatch.setenv("POST_SAVANT_DURATION_GUARD_SLACK_SEC", "1")
    monkeypatch.setenv("EVIDENCE_RUNTIME_EPOCH_STRICT", "false")
    monkeypatch.setattr(worker, "_load_event_context", lambda _conn, _event_id: _event_context())
    monkeypatch.setattr(worker, "load_native_metadata", lambda _path: _metadata_rows_outside_window())
    monkeypatch.setattr(worker, "read_decoded_video_frame_count", lambda _path: 10)
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 61.35)
    monkeypatch.setattr(
        worker,
        "write_frame_cache_identity_sidecar",
        lambda **kwargs: _sidecar_result(kwargs, {}),
    )

    bundle = worker._finalize_post_savant_evidence_bundle(
        None,
        event_id=EVENT_ID,
        meta_dir=str(sink_dir),
        metadata_file=str(metadata_file),
        evidence_output_dir=str(output_root),
    )

    raw_clip = output_root / EVENT_ID / "raw_clip.mov"
    metadata = _read_json(output_root / EVENT_ID / "metadata.json")
    summary = _read_json(output_root / EVENT_ID / "summary.json")

    assert raw_clip.read_bytes() == b"full replay source bytes"
    assert bundle["raw_clip"] == str(raw_clip)
    assert bundle["clip_status"] == "generated_unverified"
    assert bundle["duration_guard_failed"] is False
    assert summary["fast_raw_clip_enabled"] is True
    assert summary["canonical_clip"] is False
    assert summary["duration_guard_status"] == "relaxed"
    assert summary["duration_guard_failed"] is False
    assert summary["sink_window_guard_status"] == "relaxed"
    assert summary["sink_window_guard_failed"] is False
    assert summary["video_crop"]["materialization_mode"] == "fast_raw_copy"
    assert summary["video_crop"]["crop_video_to_time_window"] is False
    assert "fast_raw_clip_time_precision_relaxed" in summary["limitations"]
    assert metadata["media"]["raw_clip_path"] == str(raw_clip)
    assert metadata["status"]["clip_status"] == "generated_unverified"


def test_duration_guard_uses_pts_window_when_requested_duration_is_stale(
    monkeypatch,
) -> None:
    monkeypatch.setenv("POST_SAVANT_DURATION_GUARD_SLACK_SEC", "1")

    guard = worker._post_savant_duration_guard(
        None,
        {
            "requested_duration_s": 10.0,
            "requested_start_pts": 90_000_000_000,
            "requested_end_pts": 120_000_000_000,
        },
        actual_duration=29.94,
    )

    assert guard["expected_duration_seconds"] == 30.0
    assert guard["duration_guard_status"] == "passed"
    assert guard["duration_guard_failed"] is False
    assert guard["max_allowed_duration_seconds"] == 31.0


def test_sink_metadata_pts_gap_fails_closed_even_when_duration_is_valid(
    monkeypatch, tmp_path: Path
) -> None:
    sink_dir = tmp_path / "sink" / f"replay-event-{EVENT_ID}-00000000"
    sink_dir.mkdir(parents=True)
    source_video = sink_dir / "video.mov"
    source_video.write_bytes(b"stitched replay source")
    metadata_file = sink_dir / "metadata.json"
    metadata_file.write_text(json.dumps({"job_id": "job-1"}) + "\n", encoding="utf-8")
    output_root = tmp_path / "evidence"

    monkeypatch.setenv("FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED", "true")
    monkeypatch.setenv("POST_SAVANT_DURATION_GUARD_SLACK_SEC", "1")
    monkeypatch.setenv("POST_SAVANT_MAX_PTS_GAP_SEC", "2")
    monkeypatch.setenv("EVIDENCE_RUNTIME_EPOCH_STRICT", "false")
    monkeypatch.setattr(worker, "_load_event_context", lambda _conn, _event_id: _event_context())
    monkeypatch.setattr(worker, "load_native_metadata", lambda _path: _metadata_rows_with_pts_gap())
    monkeypatch.setattr(worker, "read_decoded_video_frame_count", lambda _path: 10)
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 10.0)
    monkeypatch.setattr(worker, "_copy_or_crop_video", _write_valid_duration_crop)
    monkeypatch.setattr(
        worker,
        "write_frame_cache_identity_sidecar",
        lambda **kwargs: _sidecar_result(kwargs, {}),
    )

    bundle = worker._finalize_post_savant_evidence_bundle(
        None,
        event_id=EVENT_ID,
        meta_dir=str(sink_dir),
        metadata_file=str(metadata_file),
        evidence_output_dir=str(output_root),
    )

    raw_clip = output_root / EVENT_ID / "raw_clip.mov"
    metadata = _read_json(output_root / EVENT_ID / "metadata.json")
    summary = _read_json(output_root / EVENT_ID / "summary.json")

    assert not raw_clip.exists()
    assert bundle["raw_clip"] == ""
    assert bundle["clip_status"] == "duration_guard_failed"
    assert summary["duration_guard_status"] == "passed"
    assert summary["sink_window_guard_status"] == "failed"
    assert summary["sink_window_guard_failed"] is True
    assert summary["sink_window_guard_reason"] == "sink_metadata_pts_gap_exceeds_limit"
    assert "sink_window_guard_failed" in summary["production_ready_failures"]
    assert metadata["media"]["raw_clip_path"] == ""
    assert metadata["status"]["clip_status"] == "duration_guard_failed"


def test_rolling_cache_finalizer_reuses_materializer_output_duration(
    monkeypatch, tmp_path: Path
) -> None:
    sink_dir = tmp_path / "sink" / f"rolling-cache-event-{EVENT_ID}"
    sink_dir.mkdir(parents=True)
    source_video = sink_dir / "video.mov"
    source_video.write_bytes(b"rolling cache source")
    metadata_file = sink_dir / "metadata.json"
    metadata_file.write_text(
        json.dumps(
            {
                "job_id": "rolling-job-1",
                "rolling_cache": {
                    "time_domain_crop_applied": True,
                    "requested_start_pts": 95_000_000_000,
                    "requested_end_pts": 105_000_000_000,
                    "actual_start_pts": 95_000_000_000,
                    "actual_end_pts": 105_000_000_000,
                    "output_duration_s": 10.0,
                },
                "frames": _metadata_rows_inside_window(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output_root = tmp_path / "evidence"

    monkeypatch.setenv("POST_SAVANT_FAST_RAW_CLIP_ENABLED", "true")
    monkeypatch.setenv("FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED", "true")
    monkeypatch.setenv("POST_SAVANT_DURATION_GUARD_SLACK_SEC", "1")
    monkeypatch.setenv("EVIDENCE_RUNTIME_EPOCH_STRICT", "false")
    monkeypatch.setattr(worker, "_load_event_context", lambda _conn, _event_id: _event_context())
    monkeypatch.setattr(worker, "load_native_metadata", lambda _path: _metadata_rows_inside_window())
    monkeypatch.setattr(worker, "read_decoded_video_frame_count", lambda _path: 10)
    monkeypatch.setattr(worker, "_copy_or_crop_video", _write_valid_duration_crop)
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 8.75)
    monkeypatch.setattr(
        worker,
        "write_frame_cache_identity_sidecar",
        lambda **kwargs: _sidecar_result(kwargs, {}),
    )

    bundle = worker._finalize_post_savant_evidence_bundle(
        None,
        event_id=EVENT_ID,
        meta_dir=str(sink_dir),
        metadata_file=str(metadata_file),
        evidence_output_dir=str(output_root),
    )

    raw_clip = output_root / EVENT_ID / "raw_clip.mov"
    summary = _read_json(output_root / EVENT_ID / "summary.json")
    metadata = _read_json(output_root / EVENT_ID / "metadata.json")

    assert not raw_clip.exists()
    assert bundle["raw_clip"] == ""
    assert bundle["clip_status"] == "duration_guard_failed"
    assert summary["raw_clip_duration"] == 10.0
    assert summary["materialization_metrics"]["ffprobe_invocation_count"] == 0
    assert summary["expected_duration_seconds"] == 10.0
    assert summary["duration_guard_status"] == "passed"
    assert summary["duration_guard_failed"] is False
    assert summary["duration_guard_reason"] == ""
    assert summary["min_allowed_duration_seconds"] == 9.0
    assert summary["sink_window_guard_status"] == "failed"
    assert summary["materialization_guard_attribution"]["primary_category"] == (
        "sink_window"
    )
    assert summary["materialization_guard_attribution"]["primary_reason"] == (
        "sink_metadata_pts_gap_exceeds_limit"
    )
    assert worker._evidence_reason_for_bundle(bundle["clip_status"], bundle) == (
        "sink_metadata_pts_gap_exceeds_limit"
    )
    assert summary["raw_clip_path"] is None
    assert metadata["media"]["raw_clip_duration"] == 10.0
    assert metadata["media"]["raw_clip_path"] == ""
    assert metadata["status"]["clip_status"] == "duration_guard_failed"


def test_stable_invalid_sink_output_is_marked_failed_and_not_retried(
    monkeypatch, tmp_path: Path
) -> None:
    event_id = "22222222-2222-4222-8222-222222222222"
    meta_dir = tmp_path / "sink" / f"replay-event-{event_id}"
    meta_dir.mkdir(parents=True)
    (meta_dir / "video.mov").write_bytes(b"not a valid mov")
    (meta_dir / "metadata.json").write_text(
        json.dumps({"labels": {"event_id": event_id}}) + "\n",
        encoding="utf-8",
    )
    failures: list[dict[str, str]] = []

    monkeypatch.setenv("EVIDENCE_TOPOLOGY", "post_savant_replay")
    monkeypatch.setenv("MEDIA_INVALID_SINK_OUTPUT_MAX_RETRIES", "2")
    monkeypatch.setattr(worker, "_is_already_ready", lambda _conn, _event_id: False)
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: None)
    monkeypatch.setattr(
        worker,
        "_mark_media_finalize_failed",
        lambda _conn, **kwargs: failures.append(kwargs),
    )

    processed: set[str] = set()
    candidate_dirs: dict[str, tuple[int, int]] = {}
    invalid_failures: dict[str, int] = {}

    first = worker._process_sink_output(
        None,
        str(tmp_path / "sink"),
        processed,
        candidate_dirs=candidate_dirs,
        invalid_output_failures=invalid_failures,
        midterm_sink_stability_checks=1,
    )
    second = worker._process_sink_output(
        None,
        str(tmp_path / "sink"),
        processed,
        candidate_dirs=candidate_dirs,
        invalid_output_failures=invalid_failures,
        midterm_sink_stability_checks=1,
    )
    third = worker._process_sink_output(
        None,
        str(tmp_path / "sink"),
        processed,
        candidate_dirs=candidate_dirs,
        invalid_output_failures=invalid_failures,
        midterm_sink_stability_checks=1,
    )

    assert (first, second, third) == (0, 0, 0)
    assert failures == [
        {
            "event_id": event_id,
            "sink_path": str(meta_dir),
            "error_message": "sink_output_invalid:video_duration_unavailable",
        }
    ]
    assert str(meta_dir) in processed
    marker = meta_dir / worker.INVALID_SINK_OUTPUT_MARKER
    assert marker.exists()
    assert json.loads(marker.read_text(encoding="utf-8"))["reason"] == (
        "video_duration_unavailable"
    )


def _event_context() -> dict:
    return {
        "event_id": EVENT_ID,
        "source_event_id": "source-event-1",
        "event_type": "intrusion",
        "camera_id": "primary_rtsp",
        "source_id": "primary_rtsp",
        "track_id": "track-1",
        "created_at": "2026-06-11T02:05:06+00:00",
        "event_ts_ms": 1_780_000_000_000,
        "frame_uuid": "event-frame",
        "frame_pts": 100_000_000_000,
        "payload": {
            "media": {
                "frame_uuid": "event-frame",
                "frame_pts": 100_000_000_000,
                "replay_job_id": "job-1",
                "replay_job_request": _replay_job_request(),
            }
        },
    }


def _replay_job_request() -> dict:
    return {
        "anchor_keyframe": "anchor-frame",
        "offset": {"seconds": 5},
        "stop_condition": {"ts_delta_sec": 10},
        "configuration": {
            "labels": {
                "event_id": EVENT_ID,
                "event_frame_uuid": "event-frame",
                "event_frame_pts": "100000000000",
                "requested_start_pts": "95000000000",
                "requested_end_pts": "105000000000",
            }
        },
    }


def _metadata_rows_outside_window() -> list[dict]:
    return [
        {"type": "VideoFrame", "pts": 10_000_000_000, "frame_uuid": "old-1"},
        {"type": "VideoFrame", "pts": 20_000_000_000, "frame_uuid": "old-2"},
    ]


def _metadata_rows_inside_window() -> list[dict]:
    return [
        {"type": "VideoFrame", "pts": 95_000_000_000, "frame_uuid": "start"},
        {"type": "VideoFrame", "pts": 100_000_000_000, "frame_uuid": "event-frame"},
        {"type": "VideoFrame", "pts": 105_000_000_000, "frame_uuid": "end"},
    ]


def _metadata_rows_with_pts_gap() -> list[dict]:
    return [
        {"type": "VideoFrame", "pts": 95_000_000_000, "frame_uuid": "start"},
        {"type": "VideoFrame", "pts": 96_000_000_000, "frame_uuid": "before-gap"},
        {"type": "VideoFrame", "pts": 100_000_000_000, "frame_uuid": "event-frame"},
        {"type": "VideoFrame", "pts": 105_000_000_000, "frame_uuid": "end"},
    ]


def _sidecar_result(kwargs: dict, calls: dict[str, object]) -> tuple[dict, dict]:
    evidence_dir = Path(kwargs["evidence_dir"])
    annotations_path = evidence_dir / worker.SIDECAR_ANNOTATIONS_FILE
    annotations_path.write_text(
        json.dumps({"frame_uuid": "event-frame", "objects": []}) + "\n",
        encoding="utf-8",
    )
    calls["raw_clip_path"] = kwargs.get("raw_clip_path")
    return (
        {
            "annotation_status": "complete",
            "annotations_written": 1,
            "rows_written": 1,
            "rows_total_input": 1,
            "embedding_vectors_in_output": 0,
            "image_bytes_in_output": 0,
            "production_ready": True,
            "production_ready_failures": [],
        },
        {"written": True, "annotations_path": str(annotations_path)},
    )


def _write_overlong_crop(**kwargs: object) -> dict:
    output_video_path = Path(kwargs["output_video_path"])
    output_video_path.write_bytes(b"cropped but still overlong")
    return {
        "method": "ffmpeg_time_domain_transcode",
        "crop_video_to_time_window": True,
        "duration_seconds": 10.0,
    }


def _write_valid_duration_crop(**kwargs: object) -> dict:
    output_video_path = Path(kwargs["output_video_path"])
    output_video_path.write_bytes(b"valid duration crop")
    return {
        "method": "ffmpeg_time_domain_transcode",
        "crop_video_to_time_window": True,
        "duration_seconds": 10.0,
    }


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
