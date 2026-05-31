"""C1E.2 raw clip decode validation contract tests."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MW_DIR = str(ROOT / "services" / "media-worker")


def _activate_media_worker_path() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if MW_DIR in sys.path:
        sys.path.remove(MW_DIR)
    sys.path.insert(0, MW_DIR)


def _event_context() -> dict:
    return {
        "event_id": "ev-1",
        "source_event_id": "source-1",
        "event_type": "intrusion",
        "camera_id": "cam_c1e_rtsp_replay",
        "source_id": "c1e_rtsp_replay",
        "track_id": "7",
        "event_ts_ms": 123456,
        "frame_uuid": "frame-1",
        "keyframe_uuid": "kf-1",
        "previous_keyframe_uuid": "prev-kf",
        "payload": {"media": {}},
    }


def _job_request() -> dict:
    return {
        "anchor_keyframe": "prev-kf",
        "offset": {"seconds": 5},
        "stop_condition": {"ts_delta_sec": {"max_delta_sec": 10}},
        "configuration": {
            "stored_stream_id": "c1e_rtsp_replay",
            "resulting_stream_id": "replay-event-ev-1",
            "min_duration": {"secs": 0, "nanos": 33333333},
        },
    }


def _metadata(worker, tmp_path: Path) -> dict:
    raw_clip = tmp_path / "raw_clip.mov"
    raw_clip.write_bytes(b"video")
    return worker._build_business_metadata(
        event_context=_event_context(),
        replay_job_id="job-1",
        replay_job_request=_job_request(),
        sink_metadata_path="/media/evidence/ev-1/sink_metadata.json",
        sink_video_path="/media/sink/video.mov",
        sink_output_dir="/media/sink",
        raw_clip_path=str(raw_clip),
        event_annotation_path="/media/evidence/ev-1/event_annotation.json",
    )


def test_clip_validation_ok_maps_to_generated(monkeypatch, tmp_path: Path) -> None:
    _activate_media_worker_path()
    from app import worker

    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 10.05)
    monkeypatch.setattr(
        worker,
        "_probe_clip_decode",
        lambda _path: {
            "decode_error_count": 0,
            "decode_error_sample": [],
            "decode_ok": True,
            "probe_tool": "/usr/bin/ffmpeg",
            "probe_error": "",
        },
    )

    metadata = _metadata(worker, tmp_path)

    assert metadata["media"]["raw_clip_duration"] == 10.05
    assert metadata["media"]["duration_probe_status"] == "ok"
    assert metadata["media"]["expected_duration_seconds"] == 10.0
    assert metadata["media"]["clip_validation"]["ok"] is True
    assert metadata["media"]["clip_validation"]["decode_error_count"] == 0
    assert metadata["media"]["clip_validation"]["duration_ok"] is True
    assert metadata["status"]["clip_status"] == "generated"


def test_decode_errors_map_to_generated_corrupt(monkeypatch, tmp_path: Path) -> None:
    _activate_media_worker_path()
    from app import worker

    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 10.05)
    monkeypatch.setattr(
        worker,
        "_probe_clip_decode",
        lambda _path: {
            "decode_error_count": 2,
            "decode_error_sample": ["missing reference picture"],
            "decode_ok": False,
            "probe_tool": "/usr/bin/ffmpeg",
            "probe_error": "ffmpeg exited with status 1",
        },
    )

    metadata = _metadata(worker, tmp_path)

    assert metadata["media"]["duration_probe_status"] == "ok"
    assert metadata["media"]["clip_validation"]["ok"] is False
    assert metadata["media"]["clip_validation"]["decode_error_count"] == 2
    assert metadata["status"]["clip_status"] == "generated_corrupt"


def test_probe_unavailable_maps_to_generated_unverified(monkeypatch, tmp_path: Path) -> None:
    _activate_media_worker_path()
    from app import worker

    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 10.05)
    monkeypatch.setattr(
        worker,
        "_probe_clip_decode",
        lambda _path: {
            "decode_error_count": 0,
            "decode_error_sample": [],
            "decode_ok": None,
            "probe_tool": None,
            "probe_error": "ffmpeg unavailable",
        },
    )

    metadata = _metadata(worker, tmp_path)

    assert metadata["media"]["duration_probe_status"] == "ok"
    assert metadata["media"]["clip_validation"]["ok"] is None
    assert metadata["media"]["clip_validation"]["probe_tool"] is None
    assert metadata["media"]["clip_validation"]["probe_error"] == "ffmpeg unavailable"
    assert metadata["status"]["clip_status"] == "generated_unverified"


def test_missing_duration_does_not_report_success(monkeypatch, tmp_path: Path) -> None:
    _activate_media_worker_path()
    from app import worker

    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: None)
    monkeypatch.setattr(
        worker,
        "_probe_clip_decode",
        lambda _path: {
            "decode_error_count": 0,
            "decode_error_sample": [],
            "decode_ok": True,
            "probe_tool": "/usr/bin/ffmpeg",
            "probe_error": "",
        },
    )

    metadata = _metadata(worker, tmp_path)

    assert metadata["media"]["raw_clip_duration"] is None
    assert metadata["media"]["duration_probe_status"] == "failed"
    assert metadata["media"]["clip_validation"]["ok"] is None
    assert metadata["media"]["clip_validation"]["duration_ok"] is False
    assert metadata["status"]["clip_status"] == "generated_unverified"
