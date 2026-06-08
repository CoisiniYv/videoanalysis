"""C1E.1 evidence trust hardening contract tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[2]
CW_DIR = str(ROOT / "services" / "clip-worker")
MW_DIR = str(ROOT / "services" / "media-worker")
EVIDENCE_WORKER = ROOT / "services" / "evidence-worker" / "app" / "evidence.py"
SMOKE = ROOT / "scripts" / "smoke" / "check_c1e_official_replay_evidence_integration.sh"
DOC = ROOT / "docs" / "c1e_1_evidence_trust_hardening.md"


def _activate_service_path(path: str) -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)


def _load_evidence_module():
    spec = importlib.util.spec_from_file_location("evidence_worker_evidence", EVIDENCE_WORKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_duration_probe_helper_exists_and_uses_ffprobe(monkeypatch) -> None:
    _activate_service_path(MW_DIR)
    from app import worker

    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout='{"format": {"duration": "10.051731"}}')

    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/ffprobe")
    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._probe_video_duration_seconds("/tmp/raw_clip.mov") == 10.051731
    assert calls[0][:5] == [
        "/usr/bin/ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
    ]


def test_business_metadata_duration_schema_not_hardcoded(monkeypatch, tmp_path: Path) -> None:
    _activate_service_path(MW_DIR)
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
    event_context = {
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
    raw_clip = tmp_path / "raw_clip.mov"
    raw_clip.write_bytes(b"video")

    metadata = worker._build_business_metadata(
        event_context=event_context,
        replay_job_id="job-1",
        replay_job_request={
            "anchor_keyframe": "prev-kf",
            "offset": {"seconds": 5},
            "stop_condition": {"ts_delta_sec": {"max_delta_sec": 10}},
            "configuration": {
                "stored_stream_id": "c1e_rtsp_replay",
                "resulting_stream_id": "replay-event-ev-1",
            },
        },
        sink_metadata_path="/media/evidence/ev-1/sink_metadata.json",
        sink_video_path="/media/sink/video.mov",
        sink_output_dir="/media/sink",
        raw_clip_path=str(raw_clip),
        event_annotation_path="/media/evidence/ev-1/event_annotation.json",
    )

    assert metadata["media"]["raw_clip_duration"] == 10.05
    assert metadata["media"]["raw_clip_duration"] > 0
    assert metadata["media"]["duration_probe_status"] == "ok"
    assert metadata["media"]["clip_validation"]["ok"] is True
    assert metadata["status"]["clip_status"] == "generated"


def test_duration_probe_failure_is_null_not_zero(monkeypatch, tmp_path: Path) -> None:
    _activate_service_path(MW_DIR)
    from app import worker

    monkeypatch.setattr(worker.shutil, "which", lambda _name: None)
    monkeypatch.setattr(worker, "_probe_duration_with_imageio_ffmpeg", lambda _path: None)
    raw_clip = tmp_path / "raw_clip.mov"
    raw_clip.write_bytes(b"video")

    assert worker._probe_video_duration_seconds(str(raw_clip)) is None


def test_find_keyframe_sends_from_to_for_timestamp_window(monkeypatch) -> None:
    _activate_service_path(CW_DIR)
    from app.replay_client import ReplayClient

    captured = {}

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"keyframes": ["c1e_rtsp_replay", ["kf-1"]]}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("app.replay_client.httpx.post", fake_post)
    client = ReplayClient("http://replay-service:8080")

    assert client.find_keyframe("c1e_rtsp_replay", ts_ms=100_000, window_s=10) == "kf-1"
    assert captured["json"]["from"] == 90
    assert captured["json"]["to"] == 110
    assert captured["json"]["limit"] == 20


def test_unbounded_keyframe_fallback_disabled_by_default(monkeypatch) -> None:
    _activate_service_path(CW_DIR)
    from app.config import load_config

    monkeypatch.delenv("ALLOW_UNBOUNDED_KEYFRAME_FALLBACK", raising=False)
    assert load_config().allow_unbounded_keyframe_fallback is False


def test_missing_keyframe_without_safe_lookup_becomes_failed() -> None:
    _activate_service_path(CW_DIR)
    from app.worker import MISSING_KEYFRAME_ERROR, _keyframe_from_request

    keyframe, source = _keyframe_from_request(
        {"source_id": "c1e_rtsp_replay", "event_ts_ms": 123456}
    )
    assert keyframe is None
    assert source == MISSING_KEYFRAME_ERROR


def test_event_annotation_bbox_xywh_to_xyxy_still_works() -> None:
    _activate_service_path(MW_DIR)
    from app.worker import _event_annotation_from_context

    annotation = _event_annotation_from_context(
        {
            "event_id": "ev-1",
            "source_event_id": "source-1",
            "event_type": "intrusion",
            "camera_id": "cam_c1e_rtsp_replay",
            "source_id": "c1e_rtsp_replay",
            "track_id": "7",
            "event_ts_ms": 123,
            "frame_uuid": "frame-1",
            "keyframe_uuid": "kf-1",
            "previous_keyframe_uuid": "prev-kf",
            "confidence": 0.91,
            "payload": {"bbox": {"x": 1, "y": 2, "width": 3, "height": 4}},
        }
    )
    overlay = annotation["overlays"][0]
    assert overlay["bbox"] == [1.0, 2.0, 4.0, 6.0]
    assert overlay["bbox_format"] == "xyxy"
    assert overlay["bbox_source_format"] == "xywh"


def test_roi_polygon_loaded_from_payload() -> None:
    _activate_service_path(MW_DIR)
    from app.worker import _event_annotation_from_context

    points = [[0, 0], [100, 0], [100, 100], [0, 100]]
    annotation = _event_annotation_from_context(
        {
            "event_id": "ev-1",
            "source_event_id": "source-1",
            "event_type": "intrusion",
            "camera_id": "cam_c1e_rtsp_replay",
            "source_id": "c1e_rtsp_replay",
            "track_id": "7",
            "event_ts_ms": 123,
            "frame_uuid": "frame-1",
            "keyframe_uuid": "kf-1",
            "previous_keyframe_uuid": "prev-kf",
            "confidence": 0.91,
            "payload": {
                "bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
                "zone_id": "payload_zone",
                "roi_polygon": points,
            },
        }
    )
    roi = [item for item in annotation["overlays"] if item["type"] == "roi_polygon"][0]
    assert roi["points"] == points
    assert roi["source"] == "event.payload.roi_polygon"
    assert annotation["roi_lookup_status"] == "payload"
    assert annotation["annotation_status"] == "complete"


def test_roi_polygon_loaded_from_camera_config(tmp_path: Path) -> None:
    _activate_service_path(MW_DIR)
    from app.worker import _event_annotation_from_context

    config = tmp_path / "cameras.yml"
    config.write_text(
        """
cameras:
  cam_c1e_rtsp_replay:
    enabled: true
    source_id: c1e_rtsp_replay
    zones:
      c1e_rtsp_full_frame:
        type: polygon
        points:
          - [0.0, 0.0]
          - [1920.0, 0.0]
          - [1920.0, 1080.0]
          - [0.0, 1080.0]
""",
        encoding="utf-8",
    )
    annotation = _event_annotation_from_context(
        {
            "event_id": "ev-1",
            "source_event_id": "source-1",
            "event_type": "intrusion",
            "camera_id": "cam_c1e_rtsp_replay",
            "source_id": "c1e_rtsp_replay",
            "track_id": "7",
            "event_ts_ms": 123,
            "frame_uuid": "frame-1",
            "keyframe_uuid": "kf-1",
            "previous_keyframe_uuid": "prev-kf",
            "confidence": 0.91,
            "payload": {
                "bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
                "zone_id": "c1e_rtsp_full_frame",
            },
        },
        cameras_config_path=str(config),
    )
    roi = [item for item in annotation["overlays"] if item["type"] == "roi_polygon"][0]
    assert roi["source"] == "camera_config"
    assert roi["zone_id"] == "c1e_rtsp_full_frame"
    assert roi["points"][2] == [1920.0, 1080.0]
    assert annotation["roi_lookup_status"] == "found"
    assert annotation["annotation_status"] == "complete"


def test_evidence_worker_find_metadata_file_filters_source_id(tmp_path: Path) -> None:
    evidence = _load_evidence_module()
    root = tmp_path / "video"
    (root / "source_a%" / "unknown%").mkdir(parents=True)
    (root / "source_b%" / "unknown%").mkdir(parents=True)
    source_a = root / "source_a%" / "unknown%" / "metadata.json"
    source_b = root / "source_b%" / "unknown%" / "metadata.json"
    source_a.write_text(json.dumps({"source_id": "source_a"}) + "\n", encoding="utf-8")
    source_b.write_text(json.dumps({"source_id": "source_b"}) + "\n", encoding="utf-8")

    assert evidence.find_metadata_file(str(root), "source_b") == str(source_b)
    assert evidence.find_metadata_file(str(root), "missing") is None


def test_c1e_smoke_contains_trust_checks() -> None:
    smoke = SMOKE.read_text(encoding="utf-8")
    for expected in (
        "metadata_raw_clip_duration=",
        "duration_probe_status=",
        "clip_validation.ok=",
        "clip_validation.decode_error_count=",
        "duration_range_check=",
        "keyframe_lookup_used=",
        "roi_overlay_generated=",
        "roi_lookup_status=",
        "annotation_status=",
        "fallback_reason=",
        "annotated_clip=no",
        "source_extraction_fallback=false",
        "second_rtsp_pull=false",
    ):
        assert expected in smoke


def test_c1e_1_doc_exists_and_documents_known_limitation() -> None:
    doc = DOC.read_text(encoding="utf-8")
    assert "raw_clip_duration" in doc
    assert "missing_keyframe_uuid_and_anchored_lookup_unavailable" in doc
    assert "ROI" in doc
    assert "source_id" in doc
    assert "unknown%" in doc
