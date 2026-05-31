"""P1c-RTSP event-triggered Replay evidence bundle contract checks."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import yaml


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "p1c_rtsp_replay_event_evidence_bundle.md"
COMPOSE = ROOT / "infra" / "docker-compose.p1c-rtsp-replay-event-evidence.yml"
REPLAY_CONFIG = ROOT / "modules" / "savant_replay" / "config.p1c_rtsp_inline.json"
CAMERA_CONFIG = ROOT / "modules" / "savant_security" / "config" / "cameras.p1c_rtsp_replay.yml"
SMOKE = ROOT / "scripts" / "smoke" / "check_p1c_rtsp_replay_event_evidence_bundle.sh"
CW_DIR = str(ROOT / "services" / "clip-worker")
MW_DIR = str(ROOT / "services" / "media-worker")
FIXED_RTSP = "rtsp://10.37.57.112:8554/live/1080movie"


def _activate_service_path(path: str) -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compose(path: Path) -> dict:
    return yaml.safe_load(_text(path))


def test_p1c_files_exist() -> None:
    assert DOC.exists()
    assert COMPOSE.exists()
    assert REPLAY_CONFIG.exists()
    assert CAMERA_CONFIG.exists()
    assert SMOKE.exists()


def test_p1c_compose_scopes_single_rtsp_path_only() -> None:
    compose = _compose(COMPOSE)
    services = set(compose["services"])
    required = {
        "redis",
        "postgres",
        "replay-service",
        "savant-security",
        "source-adapter",
        "video-file-sink",
        "event-worker",
        "clip-worker",
        "media-worker",
    }
    assert required.issubset(services)
    assert {"api", "rtsp-server", "ffmpeg-source", "metadata-sink", "evidence-worker"}.isdisjoint(services)

    source_env = compose["services"]["source-adapter"]["environment"]
    savant_env = compose["services"]["savant-security"]["environment"]
    sink_env = compose["services"]["video-file-sink"]["environment"]
    event_env = compose["services"]["event-worker"]["environment"]
    clip_env = compose["services"]["clip-worker"]["environment"]
    media_env = compose["services"]["media-worker"]["environment"]

    assert source_env["RTSP_URI"] == "rtsp://10.37.57.112:8554/live/1080movie"
    assert source_env["LOCATION"] == "rtsp://10.37.57.112:8554/live/1080movie"
    assert source_env["ZMQ_ENDPOINT"] == "dealer+connect:tcp://replay-service:5555"
    assert savant_env["ZMQ_SRC_ENDPOINT"] == "router+bind:tcp://0.0.0.0:5557"
    assert savant_env["SOURCE_ID"] == "p1c_rtsp_replay"
    assert sink_env["ZMQ_ENDPOINT"] == "sub+bind:tcp://0.0.0.0:6666"
    assert event_env["RECORDING_ENABLED"] == "true"
    assert event_env["RECORDING_EVENT_TYPES"] == "intrusion"
    assert event_env["RECORDING_SOURCE_ID"] == "p1c_rtsp_replay"
    assert event_env["RECORDING_MAX_REQUESTS_PER_RUN"] == "1"
    assert event_env["RECORDING_COOLDOWN_SECONDS"] == "30"
    assert event_env["DEFAULT_REPLAY_SOURCE_ID"] == "p1c_rtsp_replay"
    assert clip_env["REPLAY_API_URL"] == "http://replay-service:8080"
    assert clip_env["REPLAY_JOB_SINK_URL"] == "pub+connect:tcp://video-file-sink:6666"
    assert clip_env["CLIP_WORKER_MAX_JOBS_PER_RUN"] == "1"
    assert clip_env["CLIP_WORKER_MAX_CONCURRENT_JOBS"] == "1"
    assert clip_env["CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS"] == "30"
    assert clip_env["REPLAY_STOP_CONDITION_MODE"] == "ts_delta_sec"
    assert "../services/event-worker:/app:rw" in compose["services"]["event-worker"]["volumes"]
    assert "../services/clip-worker:/app:rw" in compose["services"]["clip-worker"]["volumes"]
    assert "../services/media-worker:/app:rw" in compose["services"]["media-worker"]["volumes"]
    assert media_env["P1_RAW_CLIP_FINALIZER_ENABLED"] == "true"
    assert media_env["P1_SINK_STABILITY_CHECKS"] == "2"
    assert media_env["EVIDENCE_OUTPUT_DIR"] == "/media/evidence"
    assert media_env["EVIDENCE_PHASE"] == "P1c-RTSP"
    assert media_env["EVIDENCE_RUN_ID"] == "${P1C_RTSP_RUN_ID:-}"
    assert media_env["EVIDENCE_INPUT_TYPE"] == "rtsp"
    assert media_env["EVIDENCE_INPUT_URI"] == FIXED_RTSP
    assert media_env["SINK_OUTPUT_DIR"] in {
        "/media/replay-sink-output/p1c-rtsp",
        "${P1C_RTSP_SINK_ROOT:-/media/replay-sink-output/p1c-rtsp}",
    }


def test_p1c_replay_and_camera_config_are_rtsp_inline() -> None:
    replay = json.loads(_text(REPLAY_CONFIG))
    assert replay["in_stream"]["url"] == "router+bind:tcp://0.0.0.0:5555"
    assert replay["out_stream"]["url"] == "dealer+connect:tcp://savant-security:5557"

    doc = yaml.safe_load(_text(CAMERA_CONFIG))
    cameras = doc["cameras"]
    assert set(cameras) == {"cam_p1c_rtsp_replay"}
    camera = cameras["cam_p1c_rtsp_replay"]
    assert camera["source_id"] == "p1c_rtsp_replay"
    assert camera["rtsp_url"] == "rtsp://10.37.57.112:8554/live/1080movie"
    assert camera["rules"]["intrusion"]["clip_required"] is True
    assert camera["rules"]["intrusion"]["snapshot_required"] is False
    assert camera["rules"]["intrusion"]["cooldown_s"] == 60


def test_p1c_smoke_records_required_boundaries() -> None:
    smoke = _text(SMOKE)
    for expected in (
        "max_events=1",
        "max_record_requests=1",
        "max_replay_jobs=1",
        "max_evidence_bundles=1",
        "replay_ttl_field=",
        "replay_ttl_seconds=",
        "ttl_requirement_seconds=",
        "replay_ttl_ok=",
        "replay_rocksdb_path=",
        "docker_access=",
        "docker_command_prefix=",
        "compose_command_prefix=",
        "sudo_used=",
        "actual_containers_started=",
        "build_used=",
        "pull_used=",
        "bind_mount_status=",
        "services_restarted=",
        "input_type=rtsp",
        "input_uri=${RTSP_URL}",
        FIXED_RTSP,
        "local_file_used=false",
        "test_video_used=false",
        "source_extraction_fallback=false",
        "second_rtsp_pull=false",
        "second_rtsp_pull_used=no",
        "source_to_replay_to_savant_single_path=yes",
        "annotated_clip=no",
        "api_started=no",
        "production_compose_change=no",
        "raw_clip",
        "event_annotation.json",
        "metadata.json",
        "sink_metadata.json",
        "business_metadata_generated=",
        "sink_metadata_preserved=",
        "event_annotation_bbox_conversion=",
        "security.record_requests",
        "video-file-sink",
        "clip-worker",
        "media-worker",
        "uncontrolled_clip_generation",
    ):
        assert expected in smoke


def test_p1c_smoke_one_shot_policy_and_ttl_checks() -> None:
    smoke = _text(SMOKE)
    assert "max_events=1" in smoke
    assert "source stopped after first clip" in smoke
    assert "replay_ttl_ok" in smoke
    assert "uncontrolled_clip_generation" in smoke


def test_p1c_smoke_docker_detection_and_no_bare_runtime_after_detection() -> None:
    smoke = _text(SMOKE)
    assert "detect_docker()" in smoke
    assert "DOCKER_ACCESS_OK" in smoke
    assert "SUDO_DOCKER_REQUIRED" in smoke
    assert "DOCKER_ACCESS_BLOCKED" in smoke
    assert 'DOCKER="sudo docker"' in smoke
    assert 'COMPOSE="sudo docker compose"' in smoke

    after_detection = smoke.split("\ndetect_docker\n", 1)[1]
    forbidden = (
        "docker ps",
        "docker compose",
        "docker exec",
        "docker logs",
        "docker stop",
        "docker rm",
        "docker run",
    )
    for command in forbidden:
        assert command not in after_detection
    assert "$DOCKER exec" in after_detection
    assert "$COMPOSE -f" in after_detection


def test_p1c_smoke_default_no_build_and_build_gate() -> None:
    smoke = _text(SMOKE)
    assert 'P1C_ALLOW_BUILD="${P1C_ALLOW_BUILD:-0}"' in smoke
    assert 'if [[ "$P1C_ALLOW_BUILD" == "1" ]]' in smoke
    assert "up -d --no-build --force-recreate" in smoke
    assert "up -d --build --force-recreate" in smoke
    assert "worker_image_missing_and_build_not_allowed" in smoke
    assert "docker pull" not in smoke


def test_p1c_smoke_verifies_worker_bind_mount_hashes() -> None:
    smoke = _text(SMOKE)
    assert "assert_worker_bind_mounts_configured" in smoke
    assert "verify_worker_bind_mounts" in smoke
    assert "sha256sum" in smoke
    assert "MOUNT_OK" in smoke
    assert "MOUNT_NOT_ACTIVE" in smoke
    assert "worker_bind_mount_not_active" in smoke


def test_p1c_replay_job_uses_ts_delta_sec_when_supported() -> None:
    _activate_service_path(CW_DIR)
    from app.replay_client import build_job_payload

    payload = build_job_payload(
        source_id="p1c_rtsp_replay",
        keyframe_uuid="kf-123",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="pub+connect:tcp://video-file-sink:6666",
        labels={"event_id": "ev-123"},
        stop_condition_mode="ts_delta_sec",
    )
    assert payload["stop_condition"]["ts_delta_sec"]["max_delta_sec"] == 10


def test_p1c_replay_job_frame_count_fallback_declares_reason() -> None:
    _activate_service_path(CW_DIR)
    from app.replay_client import build_job_payload

    payload = build_job_payload(
        source_id="p1c_rtsp_replay",
        keyframe_uuid="kf-123",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="pub+connect:tcp://video-file-sink:6666",
        labels={"event_id": "ev-123"},
        stop_condition_mode="frame_count",
        fallback_reason="configured_frame_count_fallback",
    )
    assert payload["stop_condition"]["frame_count"] == 300
    assert payload["fallback_reason"] == "configured_frame_count_fallback"


def test_p1c_event_annotation_converts_bbox_object_xywh_to_xyxy() -> None:
    _activate_service_path(MW_DIR)
    from app.worker import _load_event_annotation

    event_id = "11111111-1111-4111-8111-111111111111"
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (
        "intrusion",
        "cam_p1c_rtsp_replay",
        "p1c_rtsp_replay",
        "track-7",
        123456,
        "frame-uuid",
        {
            "bbox": {"x": 1653, "y": 715, "width": 121, "height": 142},
            "media": {"previous_keyframe_uuid": "prev-kf"},
        },
        0.91,
        "source-event-1",
        "keyframe-uuid",
    )
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    annotation = _load_event_annotation(mock_conn, event_id)
    overlay = annotation["overlays"][0]
    assert overlay["type"] == "person_bbox"
    assert overlay["bbox"] == [1653.0, 715.0, 1774.0, 857.0]
    assert overlay["bbox_format"] == "xyxy"
    assert overlay["bbox_source_format"] == "xywh"
    assert overlay["bbox_raw"] == {"x": 1653, "y": 715, "width": 121, "height": 142}
    assert overlay["source"] == "event.payload.bbox"
    assert annotation["event"]["source_event_id"] == "source-event-1"
    assert annotation["event"]["keyframe_uuid"] == "keyframe-uuid"


def test_p1c_business_metadata_schema(monkeypatch, tmp_path: Path) -> None:
    _activate_service_path(MW_DIR)
    from app.worker import _finalize_p1_evidence_bundle

    monkeypatch.setenv("EVIDENCE_PHASE", "P1c-RTSP")
    monkeypatch.setenv("EVIDENCE_RUN_ID", "run-123")
    monkeypatch.setenv("EVIDENCE_INPUT_TYPE", "rtsp")
    monkeypatch.setenv("EVIDENCE_INPUT_URI", FIXED_RTSP)
    monkeypatch.setenv("EVIDENCE_LOCAL_FILE_USED", "false")
    monkeypatch.setenv("EVIDENCE_TEST_VIDEO_USED", "false")
    monkeypatch.setenv("EVIDENCE_SOURCE_EXTRACTION_FALLBACK", "false")
    monkeypatch.setenv("EVIDENCE_SECOND_RTSP_PULL", "false")

    event_id = "11111111-1111-4111-8111-111111111111"
    sink_dir = tmp_path / "sink"
    sink_dir.mkdir()
    video = sink_dir / "video.mov"
    metadata = sink_dir / "metadata.json"
    video.write_bytes(b"raw replay video")
    metadata.write_text(
        json.dumps({"labels": {"event_id": event_id}, "job_id": "job-p1c"}) + "\n",
        encoding="utf-8",
    )

    event_payload = {
        "bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
        "media": {
            "previous_keyframe_uuid": "prev-kf",
            "replay_job_id": "job-p1c",
            "replay_job_request": {
                "anchor_keyframe": "prev-kf",
                "offset": {"seconds": 5},
                "stop_condition": {"ts_delta_sec": {"max_delta_sec": 10}},
                "configuration": {
                    "stored_stream_id": "p1c_rtsp_replay",
                    "resulting_stream_id": f"replay-event-{event_id}",
                },
            },
        },
    }
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (
        "intrusion",
        "cam_p1c_rtsp_replay",
        "p1c_rtsp_replay",
        "track-7",
        123456,
        "frame-uuid",
        event_payload,
        0.91,
        "source-event-1",
        "keyframe-uuid",
    )
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    bundle = _finalize_p1_evidence_bundle(
        mock_conn,
        event_id=event_id,
        meta_dir=str(sink_dir),
        video_file=str(video),
        metadata_file=str(metadata),
        evidence_output_dir=str(tmp_path / "evidence"),
    )

    business = json.loads(Path(bundle["metadata"]).read_text(encoding="utf-8"))
    assert Path(bundle["sink_metadata"]).name == "sink_metadata.json"
    assert business["phase"] == "P1c-RTSP"
    assert business["run_id"] == "run-123"
    assert business["input"]["input_type"] == "rtsp"
    assert business["input"]["input_uri"] == FIXED_RTSP
    assert business["input"]["local_file_used"] is False
    assert business["replay"]["stop_condition_mode"] == "ts_delta_sec"
    assert business["media"]["sink_metadata_path"].endswith("sink_metadata.json")
    assert "single-event evidence POC" in business["limitations"]
    assert "not incident coalescing" in business["limitations"]
    assert "not continuous recording" in business["limitations"]
    assert "no annotated_clip generated" in business["limitations"]
    assert not (Path(bundle["evidence_dir"]) / "annotated_clip.mp4").exists()
