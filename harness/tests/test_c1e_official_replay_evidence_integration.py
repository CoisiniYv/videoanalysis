"""C1E official dev Replay evidence integration contract checks."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import yaml


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "c1e_replay_evidence_integration.md"
C1E_COMPOSE = ROOT / "infra" / "docker-compose.c1-official-replay-dev.yml"
C1_ADAPTER_COMPOSE = ROOT / "infra" / "docker-compose.c1-official-adapter.yml"
REPLAY_CONFIG = ROOT / "modules" / "savant_replay" / "config.p1c_rtsp_inline.json"
CAMERA_CONFIG = ROOT / "modules" / "savant_security" / "config" / "cameras.c1e_replay.yml"
SMOKE = ROOT / "scripts" / "smoke" / "check_c1e_official_replay_evidence_integration.sh"
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


def test_c1e_files_exist() -> None:
    assert DOC.exists()
    assert C1E_COMPOSE.exists()
    assert C1_ADAPTER_COMPOSE.exists()
    assert REPLAY_CONFIG.exists()
    assert CAMERA_CONFIG.exists()
    assert SMOKE.exists()


def test_c1e_compose_has_replay_evidence_services() -> None:
    compose = _compose(C1E_COMPOSE)
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
    assert {"rtsp-server", "ffmpeg-source", "metadata-sink", "evidence-worker"}.isdisjoint(
        services
    )


def test_c1e_source_path_is_source_to_replay_to_savant() -> None:
    compose = _compose(C1E_COMPOSE)
    replay = json.loads(_text(REPLAY_CONFIG))
    source_env = compose["services"]["source-adapter"]["environment"]
    savant_env = compose["services"]["savant-security"]["environment"]
    sink_env = compose["services"]["video-file-sink"]["environment"]
    clip_env = compose["services"]["clip-worker"]["environment"]

    assert source_env["ZMQ_ENDPOINT"] == "dealer+connect:tcp://replay-service:5555"
    assert replay["in_stream"]["url"] == "router+bind:tcp://0.0.0.0:5555"
    assert replay["out_stream"]["url"] == "dealer+connect:tcp://savant-security:5557"
    assert savant_env["ZMQ_SRC_ENDPOINT"] == "router+bind:tcp://0.0.0.0:5557"
    assert sink_env["ZMQ_ENDPOINT"] == "sub+bind:tcp://0.0.0.0:6666"
    assert clip_env["REPLAY_JOB_SINK_URL"] == "pub+connect:tcp://video-file-sink:6666"


def test_c1e_fixed_rtsp_and_no_file_source() -> None:
    compose_text = _text(C1E_COMPOSE)
    camera_text = _text(CAMERA_CONFIG)
    for content in (compose_text, camera_text):
        lowered = content.lower()
        assert FIXED_RTSP in content
        assert "file://" not in lowered
        assert "testvideo" not in lowered
        assert "test.mp4" not in lowered
        assert "video_loop.sh" not in lowered
        assert "ffmpeg-source" not in lowered

    compose = _compose(C1E_COMPOSE)
    source_env = compose["services"]["source-adapter"]["environment"]
    media_env = compose["services"]["media-worker"]["environment"]
    assert source_env["RTSP_URI"] == FIXED_RTSP
    assert source_env["LOCATION"] == FIXED_RTSP
    assert media_env["EVIDENCE_INPUT_TYPE"] == "rtsp"
    assert media_env["EVIDENCE_INPUT_URI"] == FIXED_RTSP
    assert media_env["EVIDENCE_LOCAL_FILE_USED"] == "false"
    assert media_env["EVIDENCE_TEST_VIDEO_USED"] == "false"
    assert media_env["EVIDENCE_SOURCE_EXTRACTION_FALLBACK"] == "false"
    assert media_env["EVIDENCE_SECOND_RTSP_PULL"] == "false"


def test_c1e_camera_config_matches_source_and_clip_policy() -> None:
    doc = yaml.safe_load(_text(CAMERA_CONFIG))
    cameras = doc["cameras"]
    assert set(cameras) == {"cam_c1e_rtsp_replay"}
    camera = cameras["cam_c1e_rtsp_replay"]
    assert camera["source_id"] == "c1e_rtsp_replay"
    assert camera["rtsp_url"] == FIXED_RTSP
    assert camera["rules"]["intrusion"]["clip_required"] is True
    assert camera["rules"]["intrusion"]["snapshot_required"] is False


def test_c1e_replay_ttl_exists() -> None:
    replay = json.loads(_text(REPLAY_CONFIG))
    ttl = replay["storage"]["rocksdb"]["data_expiration_ttl"]
    assert ttl == {"secs": 60, "nanos": 0}


def test_c1e_event_worker_recording_env() -> None:
    compose = _compose(C1E_COMPOSE)
    event_env = compose["services"]["event-worker"]["environment"]
    assert event_env["RECORDING_ENABLED"] == "true"
    assert event_env["RECORDING_EVENT_TYPES"] == "intrusion"
    assert event_env["RECORDING_SOURCE_ID"] == "c1e_rtsp_replay"
    assert event_env["RECORDING_MAX_REQUESTS_PER_RUN"] == "1"
    assert event_env["RECORDING_COOLDOWN_SECONDS"] == "30"
    assert event_env["RECORDING_PRE_SECONDS"] == "5"
    assert event_env["RECORDING_POST_SECONDS"] == "5"
    assert event_env["DEFAULT_REPLAY_SOURCE_ID"] == "c1e_rtsp_replay"


def test_c1e_clip_worker_replay_job_env() -> None:
    compose = _compose(C1E_COMPOSE)
    clip_env = compose["services"]["clip-worker"]["environment"]
    assert clip_env["REPLAY_API_URL"] == "http://replay-service:8080"
    assert clip_env["REPLAY_JOB_SINK_URL"] == "pub+connect:tcp://video-file-sink:6666"
    assert clip_env["CLIP_WORKER_MAX_JOBS_PER_RUN"] == "1"
    assert clip_env["CLIP_WORKER_MAX_CONCURRENT_JOBS"] == "1"
    assert clip_env["REPLAY_STOP_CONDITION_MODE"] == "ts_delta_sec"
    assert clip_env["ALLOW_UNBOUNDED_KEYFRAME_FALLBACK"] == "false"


def test_c1e_worker_bind_mounts_and_official_drift_fix() -> None:
    c1e = _compose(C1E_COMPOSE)
    adapter = _compose(C1_ADAPTER_COMPOSE)
    assert "../modules/savant_security:/opt/savant/src/module:rw" in c1e["services"][
        "savant-security"
    ]["volumes"]
    assert "../services/event-worker:/app:rw" in c1e["services"]["event-worker"][
        "volumes"
    ]
    assert "../services/clip-worker:/app:rw" in c1e["services"]["clip-worker"][
        "volumes"
    ]
    assert "../services/media-worker:/app:rw" in c1e["services"]["media-worker"][
        "volumes"
    ]
    assert (
        "../modules/savant_security/config:/opt/savant/src/module/config:ro"
        in c1e["services"]["media-worker"]["volumes"]
    )
    assert "../services/event-worker:/app:rw" in adapter["services"]["event-worker"][
        "volumes"
    ]
    assert "../services/face-worker:/app:rw" in adapter["services"]["face-worker"][
        "volumes"
    ]
    assert "../services/api:/app:rw" in adapter["services"]["api"]["volumes"]


def test_c1e_smoke_runtime_discipline_and_outputs() -> None:
    smoke = _text(SMOKE)
    for expected in (
        "detect_docker()",
        "DOCKER_ACCESS_OK",
        "SUDO_DOCKER_REQUIRED",
        "DOCKER_ACCESS_BLOCKED",
        'DOCKER="sudo docker"',
        'COMPOSE="sudo docker compose"',
        'C1E_ALLOW_BUILD="${C1E_ALLOW_BUILD:-0}"',
        'if [[ "$C1E_ALLOW_BUILD" == "1" ]]',
        "up -d --no-build --force-recreate",
        "up -d --build --force-recreate",
        "worker_image_missing_and_build_not_allowed",
        "replay_ttl_field=",
        "replay_ttl_seconds=",
        "ttl_requirement_seconds=",
        "replay_ttl_ok=",
        "events_created=",
        "record_requests_created=",
        "replay_jobs_created=",
        "evidence_bundles_created=",
        "extra_clips_detected=",
        "uncontrolled_clip_generation",
        "source_stopped_after_smoke=",
        "business_metadata_generated=",
        "event_annotation_bbox_conversion=",
        "duration_probe_status=",
        "metadata_raw_clip_duration=",
        "keyframe_lookup_used=",
        "roi_overlay_generated=",
        "roi_lookup_status=",
        "annotated_clip=no",
        "source_to_replay_to_savant_single_path=yes",
    ):
        assert expected in smoke
    assert "docker pull" not in smoke


def test_c1e_smoke_no_bare_runtime_after_detection() -> None:
    smoke = _text(SMOKE)
    after_detection = smoke.split("\ndetect_docker\n", 1)[1]
    for command in (
        "docker ps",
        "docker compose",
        "docker exec",
        "docker logs",
        "docker stop",
        "docker rm",
        "docker run",
    ):
        assert command not in after_detection
    assert "$DOCKER" in after_detection
    assert "$COMPOSE" in after_detection


def test_c1e_replay_job_uses_ts_delta_sec_when_supported() -> None:
    _activate_service_path(CW_DIR)
    from app.replay_client import build_job_payload

    payload = build_job_payload(
        source_id="c1e_rtsp_replay",
        keyframe_uuid="kf-123",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="pub+connect:tcp://video-file-sink:6666",
        labels={"event_id": "ev-123"},
        stop_condition_mode="ts_delta_sec",
    )
    assert payload["offset"]["seconds"] == 5
    assert payload["stop_condition"]["ts_delta_sec"]["max_delta_sec"] == 10
    assert payload["configuration"]["stored_stream_id"] == "c1e_rtsp_replay"
    assert "ev-123" in payload["configuration"]["resulting_stream_id"]


def test_c1e_frame_count_fallback_requires_reason() -> None:
    _activate_service_path(CW_DIR)
    from app.replay_client import build_job_payload

    payload = build_job_payload(
        source_id="c1e_rtsp_replay",
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


def test_c1e_event_annotation_converts_bbox_object_xywh_to_xyxy() -> None:
    _activate_service_path(MW_DIR)
    from app.worker import _load_event_annotation

    event_id = "11111111-1111-4111-8111-111111111111"
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (
        "intrusion",
        "cam_c1e_rtsp_replay",
        "c1e_rtsp_replay",
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
    assert overlay["bbox"] == [1653.0, 715.0, 1774.0, 857.0]
    assert overlay["bbox_format"] == "xyxy"
    assert overlay["bbox_source_format"] == "xywh"
    assert overlay["bbox_raw"] == {"x": 1653, "y": 715, "width": 121, "height": 142}


def test_c1e_business_metadata_schema(monkeypatch, tmp_path: Path) -> None:
    _activate_service_path(MW_DIR)
    from app.worker import _finalize_p1_evidence_bundle

    monkeypatch.setenv("EVIDENCE_PHASE", "C1E-RTSP")
    monkeypatch.setenv("EVIDENCE_RUN_ID", "run-c1e")
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
        json.dumps({"labels": {"event_id": event_id}, "job_id": "job-c1e"}) + "\n",
        encoding="utf-8",
    )

    event_payload = {
        "bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
        "media": {
            "previous_keyframe_uuid": "prev-kf",
            "replay_job_id": "job-c1e",
            "replay_job_request": {
                "anchor_keyframe": "prev-kf",
                "offset": {"seconds": 5},
                "stop_condition": {"ts_delta_sec": {"max_delta_sec": 10}},
                "configuration": {
                    "stored_stream_id": "c1e_rtsp_replay",
                    "resulting_stream_id": f"replay-event-{event_id}",
                },
            },
        },
    }
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (
        "intrusion",
        "cam_c1e_rtsp_replay",
        "c1e_rtsp_replay",
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
    assert business["phase"] == "C1E-RTSP"
    assert business["run_id"] == "run-c1e"
    assert business["input"]["input_type"] == "rtsp"
    assert business["input"]["input_uri"] == FIXED_RTSP
    assert business["input"]["local_file_used"] is False
    assert business["input"]["source_extraction_fallback"] is False
    assert business["input"]["second_rtsp_pull"] is False
    assert business["event"]["source_id"] == "c1e_rtsp_replay"
    assert business["replay"]["stop_condition_mode"] == "ts_delta_sec"
    assert business["media"]["sink_metadata_path"].endswith("sink_metadata.json")
    assert "raw_clip_duration" in business["media"]
    assert "duration_probe_status" in business["media"]
    assert "single-event evidence POC" in business["limitations"]
    assert "not incident coalescing" in business["limitations"]
    assert "not continuous recording" in business["limitations"]
    assert "no annotated_clip generated" in business["limitations"]
    assert not (Path(bundle["evidence_dir"]) / "annotated_clip.mp4").exists()


def test_c1e_docs_record_scope_and_image_mode() -> None:
    doc = _text(DOC)
    assert "single-event evidence POC" in doc
    assert "not incident coalescing" in doc
    assert "not continuous recording" in doc
    assert "bind mount" in doc
    assert "restart-only supported: yes" in doc
    assert "worker rebuild required: no" in doc
