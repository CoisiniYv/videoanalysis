"""C1G.2c evidence bundle boundary and annotation integrity regressions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MW_DIR = str(ROOT / "services" / "media-worker")
CW_DIR = str(ROOT / "services" / "clip-worker")
EW_DIR = str(ROOT / "services" / "event-worker")
COMPOSE = ROOT / "infra" / "docker-compose.c1-official-replay-dev.yml"
SMOKE = ROOT / "scripts" / "smoke" / "check_c1g2c_evidence_bundle_integrity.sh"
DOC = ROOT / "docs" / "c1g2c_evidence_bundle_boundary_annotation_integrity.md"


def _activate_service_path(path: str) -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)


def _event_context() -> dict:
    return {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "source_event_id": "savant_security:cam:1:intrusion:1234567890",
        "event_type": "intrusion",
        "camera_id": "cam_c1e_rtsp_replay",
        "source_id": "c1e_rtsp_replay",
        "track_id": "1",
        "event_ts_ms": 1_780_366_707_664,
        "frame_uuid": "frame-1",
        "keyframe_uuid": "keyframe-1",
        "previous_keyframe_uuid": "keyframe-1",
        "confidence": 0.7,
        "severity": "medium",
        "payload": {"media": {"pre_seconds": 5, "post_seconds": 5}},
        "evidence_policy": {"pre_seconds": 5, "post_seconds": 5},
    }


def _job_request() -> dict:
    return {
        "anchor_keyframe": "keyframe-1",
        "offset": {"seconds": 5.0},
        "stop_condition": {"ts_delta_sec": {"max_delta_sec": 10.0}},
        "configuration": {
            "stored_stream_id": "c1e_rtsp_replay",
            "resulting_stream_id": "replay-event-11111111-1111-4111-8111-111111111111",
            "min_duration": {"secs": 0, "nanos": 33_333_333},
        },
    }


def test_empty_annotations_are_explicit_and_not_overlay_success(
    monkeypatch, tmp_path: Path
) -> None:
    _activate_service_path(MW_DIR)
    from app import continuous_annotation

    monkeypatch.setattr(
        continuous_annotation,
        "_load_observations",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        continuous_annotation,
        "_load_gallery_candidates",
        lambda *args, **kwargs: {},
    )

    annotations_path = tmp_path / "annotations.jsonl"
    summary_path = tmp_path / "summary.json"
    summary = continuous_annotation.write_continuous_annotation_bundle(
        object(),
        _event_context(),
        annotations_path=str(annotations_path),
        summary_path=str(summary_path),
    )

    assert annotations_path.stat().st_size > 0
    assert not (
        annotations_path.stat().st_size == 0
        and summary.get("overlay_available") is True
    )
    assert summary["annotation_lines"] == 0
    assert summary["annotation_status"] == "empty"
    assert summary["annotation_empty_reason"]
    assert summary["overlay_available"] is False
    assert summary["frontend_overlay_required"] is False

    records = [
        json.loads(line)
        for line in annotations_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records[0]["record_type"] == "annotation_status"
    assert records[0]["annotation_status"] == "empty"


def test_annotation_jsonl_uses_tmp_file_and_atomic_rename() -> None:
    content = (ROOT / "services" / "media-worker" / "app" / "continuous_annotation.py").read_text(
        encoding="utf-8"
    )
    assert ".tmp" in content
    assert ".replace(path)" in content
    assert "os.fsync" in content


def test_frame_pts_remains_nanoseconds_and_time_offset_remains_milliseconds() -> None:
    _activate_service_path(MW_DIR)
    from app import continuous_annotation

    start_ts_ms = 1_780_366_702_664
    observation = {
        "timestamp_ms": 1_780_366_707_664,
        "camera_id": "cam_c1e_rtsp_replay",
        "source_id": "c1e_rtsp_replay",
        "frame_num": 27514,
        "payload": {
            "media": {
                "frame_uuid": "frame-1",
                "keyframe_uuid": "keyframe-1",
                "previous_keyframe_uuid": "keyframe-1",
                "frame_pts": 1_156_135_566_666,
                "ntp_timestamp": 1_780_366_707_654_548_000,
            }
        },
    }

    line = continuous_annotation._line_base(
        observation=observation,
        start_ts_ms=start_ts_ms,
    )

    assert line["time_offset_ms"] == 5000
    assert line["frame_pts"] == 1_156_135_566_666
    assert line["ntp_timestamp"] == 1_780_366_707_654_548_000


def test_raw_clip_duration_guard_marks_overlong_bundle(monkeypatch, tmp_path: Path) -> None:
    _activate_service_path(MW_DIR)
    from app import worker

    raw_clip = tmp_path / "raw_clip.mov"
    raw_clip.write_bytes(b"video")
    monkeypatch.setenv("EVIDENCE_MAX_DURATION_SLACK_SEC", "10")
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 942.36)
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

    metadata = worker._build_business_metadata(
        event_context=_event_context(),
        replay_job_id="job-1",
        replay_job_request=_job_request(),
        sink_metadata_path="/media/evidence/event/sink_metadata.json",
        sink_video_path="/media/sink/video.mov",
        sink_output_dir="/media/sink",
        raw_clip_path=str(raw_clip),
        event_annotation_path="/media/evidence/event/event_annotation.json",
        annotations_jsonl_path="/media/evidence/event/annotations.jsonl",
        summary_json_path="/media/evidence/event/summary.json",
        annotation_summary={
            "annotation_status": "empty",
            "annotation_lines": 0,
            "overlay_available": False,
            "frontend_overlay_required": False,
        },
    )

    validation = metadata["media"]["clip_validation"]
    assert metadata["media"]["expected_duration_seconds"] == 10.0
    assert validation["max_allowed_duration_seconds"] == 20.0
    assert validation["duration_guard_failed"] is True
    assert validation["duration_guard_status"] == "failed"
    assert metadata["status"]["clip_status"] == "duration_guard_failed"


def test_annotation_generation_failure_marks_bundle_not_clean(
    monkeypatch, tmp_path: Path
) -> None:
    _activate_service_path(MW_DIR)
    from app import worker

    raw_clip = tmp_path / "raw_clip.mov"
    raw_clip.write_bytes(b"video")
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

    metadata = worker._build_business_metadata(
        event_context=_event_context(),
        replay_job_id="job-1",
        replay_job_request=_job_request(),
        sink_metadata_path="/media/evidence/event/sink_metadata.json",
        sink_video_path="/media/sink/video.mov",
        sink_output_dir="/media/sink",
        raw_clip_path=str(raw_clip),
        event_annotation_path="/media/evidence/event/event_annotation.json",
        annotations_jsonl_path="/media/evidence/event/annotations.jsonl",
        summary_json_path="/media/evidence/event/summary.json",
        annotation_summary={
            "annotation_status": "unavailable",
            "annotation_lines": 0,
            "annotation_unavailable_reason": "annotation_generation_failed:RuntimeError",
            "annotation_generation_failed": True,
            "overlay_available": False,
            "frontend_overlay_required": False,
        },
    )

    assert metadata["media"]["clip_validation"]["annotation_generation_failed"] is True
    assert metadata["status"]["clip_status"] == "generated_annotation_failed"


def test_runtime_duration_does_not_become_replay_post_seconds(monkeypatch) -> None:
    _activate_service_path(CW_DIR)
    from app.replay_client import build_job_payload

    monkeypatch.setenv("RUN_DURATION_SEC", "900")
    payload = build_job_payload(
        source_id="c1e_rtsp_replay",
        keyframe_uuid="keyframe-1",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        labels={"event_id": "event-1"},
        stop_condition_mode="ts_delta_sec",
        fps=30,
    )

    assert payload["offset"]["seconds"] == 5.0
    assert payload["stop_condition"]["ts_delta_sec"]["max_delta_sec"] == 10.0
    assert "900" not in json.dumps(payload)


def test_suppressed_event_creates_no_record_request_or_evidence_task() -> None:
    _activate_service_path(EW_DIR)
    from app.alert_policy import AlertPolicyDecision
    from app.worker import _handle_event

    class Repo:
        evidence_tasks = 0
        clip_status_updates = 0

        def insert_event(self, _event: dict) -> str:
            return "11111111-1111-4111-8111-111111111111"

        def create_evidence_task(self, _event: dict, _event_id: str) -> None:
            self.evidence_tasks += 1

        def get_media_clip_status(self, _source_event_id: str) -> str:
            return ""

        def set_clip_status(self, *_args, **_kwargs) -> None:
            self.clip_status_updates += 1

    class Consumer:
        acked = False

        def ack(self, _msg_id: str) -> bool:
            self.acked = True
            return True

    class Publisher:
        published = 0

        def publish(self, *_args, **_kwargs) -> str:
            self.published += 1
            return "1-0"

        def has_request(self, *_args, **_kwargs) -> bool:
            return False

    class Suppressor:
        def apply(self, _event: dict, _event_id: str) -> AlertPolicyDecision:
            return AlertPolicyDecision("suppress", reason="camera_global_cooldown")

    repo = Repo()
    consumer = Consumer()
    alerts = Publisher()
    records = Publisher()
    inserted, event_id = _handle_event(
        {
            "source_event_id": "savant_security:cam:1:intrusion:123",
            "event_type": "intrusion",
            "camera_id": "cam_c1e_rtsp_replay",
            "source_id": "c1e_rtsp_replay",
            "event_ts_ms": 123,
        },
        "1-0",
        repo,
        consumer,
        alert_publisher=alerts,
        record_publisher=records,
        alert_policy_service=Suppressor(),
    )

    assert inserted is True
    assert event_id == "11111111-1111-4111-8111-111111111111"
    assert repo.evidence_tasks == 0
    assert alerts.published == 0
    assert records.published == 0
    assert consumer.acked is True


def test_evidence_task_can_exist_without_record_request_when_recording_gate_blocks() -> None:
    _activate_service_path(EW_DIR)
    from app.worker import RecordingPolicyState, _handle_event

    class Repo:
        evidence_tasks = 0

        def insert_event(self, _event: dict) -> str:
            return "11111111-1111-4111-8111-111111111111"

        def create_evidence_task(self, _event: dict, _event_id: str) -> None:
            self.evidence_tasks += 1

        def get_media_clip_status(self, _source_event_id: str) -> str:
            return ""

        def set_clip_status(self, *_args, **_kwargs) -> None:
            raise AssertionError("record_request should be gated before clip_status")

    class Consumer:
        def ack(self, _msg_id: str) -> bool:
            return True

    class RecordPublisher:
        published = 0

        def publish(self, *_args, **_kwargs) -> str:
            self.published += 1
            return "1-0"

        def has_request(self, *_args, **_kwargs) -> bool:
            return False

    records = RecordPublisher()
    repo = Repo()
    _handle_event(
        {
            "source_event_id": "savant_security:cam:2:intrusion:456",
            "event_type": "intrusion",
            "camera_id": "cam_c1e_rtsp_replay",
            "source_id": "c1e_rtsp_replay",
            "event_ts_ms": 456,
        },
        "1-1",
        repo,
        Consumer(),
        record_publisher=records,
        recording_state=RecordingPolicyState(published_requests=1),
        recording_max_requests_per_run=1,
    )

    assert repo.evidence_tasks == 1
    assert records.published == 0


def test_c1g2c_artifacts_exist_and_compose_exposes_guard_parameter() -> None:
    assert SMOKE.exists()
    assert DOC.exists()
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    media_env = compose["services"]["media-worker"]["environment"]
    assert media_env["EVIDENCE_MAX_DURATION_SLACK_SEC"] == "10"
