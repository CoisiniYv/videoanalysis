"""C2.15 replay-first single-stream evidence baseline contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "infra" / "docker-compose.c2-replay-first-dev.yml"
REPLAY_CONFIG = ROOT / "modules" / "savant_replay" / "config.c2_replay_first_dev.json"
SAVANT_MODULE = ROOT / "modules" / "savant_security" / "module.yml"
CAMERA_CONFIG = ROOT / "modules" / "savant_security" / "config" / "cameras.c1e_replay.yml"
DOC = ROOT / "docs" / "phase_c2_15_replay_first_single_stream_evidence.md"
FIXED_RTSP = "rtsp://10.37.57.112:8554/live/1080movie"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compose() -> dict:
    return yaml.safe_load(_text(COMPOSE))


def _replay_config() -> dict:
    return json.loads(_text(REPLAY_CONFIG))


def test_files_exist() -> None:
    assert COMPOSE.exists()
    assert REPLAY_CONFIG.exists()
    assert SAVANT_MODULE.exists()
    assert CAMERA_CONFIG.exists()
    assert DOC.exists()


def test_topology_records_rtsp_in_replay_before_savant() -> None:
    compose = _compose()
    replay = _replay_config()
    services = compose["services"]

    assert services["source-adapter"]["environment"]["ZMQ_ENDPOINT"] == (
        "dealer+connect:tcp://replay-service:5555"
    )
    assert replay["in_stream"]["url"] == "router+bind:tcp://0.0.0.0:5555"
    assert replay["out_stream"]["url"] == "dealer+connect:tcp://savant-security:5557"
    assert services["savant-security"]["environment"]["ZMQ_SRC_ENDPOINT"] == (
        "router+bind:tcp://0.0.0.0:5557"
    )
    assert services["clip-worker"]["environment"]["REPLAY_JOB_SINK_URL"] == (
        "dealer+connect:tcp://video-file-sink:6666"
    )
    assert services["video-file-sink"]["environment"]["ZMQ_ENDPOINT"] == (
        "router+bind:tcp://0.0.0.0:6666"
    )


def test_replay_cache_is_bounded_to_30_seconds() -> None:
    replay = _replay_config()
    storage = replay["storage"]["rocksdb"]
    assert storage["data_expiration_ttl"] == {"secs": 30, "nanos": 0}
    assert storage["compaction_period"] == {"secs": 30, "nanos": 0}


def test_savant_fps_limit_is_parameterized_to_2_8fps() -> None:
    compose = _compose()
    env = compose["services"]["savant-security"]["environment"]
    module = _text(SAVANT_MODULE)

    assert env["MAX_FPS_CONTROL"] == "${MAX_FPS_CONTROL:-true}"
    assert env["MAX_FPS"] == "${MAX_FPS:-8/1}"
    assert env["MIN_FPS"] == "${MIN_FPS:-2/1}"
    assert "max_fps_control: ${oc.decode:${oc.env:MAX_FPS_CONTROL, true}}" in module
    assert "max_fps: ${oc.env:MAX_FPS, 8/1}" in module
    assert "min_fps: ${oc.env:MIN_FPS, 2/1}" in module


def test_existing_yolo_pose_face_adaface_chain_is_preserved() -> None:
    module = _text(SAVANT_MODULE)
    assert "yolo26_pose" in module
    assert "face_detector" in module or "yolov8_face" in module or "scrfd" in module
    assert "adaface" in module.lower()
    assert "face_observation_exporter" in module


def test_watchlist_targets_are_registered_reese_and_finch() -> None:
    compose = _compose()
    env = compose["services"]["face-worker"]["environment"]
    assert env["WATCHLIST_MATCH_ENABLED"] == "true"
    assert env["WATCHLIST_THRESHOLD"] == "${WATCHLIST_THRESHOLD:-0.65}"
    assert env["WATCHLIST_TARGET_EXTERNAL_PERSON_IDS"] == (
        "${WATCHLIST_TARGET_EXTERNAL_PERSON_IDS:-demo:f4_3:reese,demo:f4_3:finch}"
    )
    assert env["WATCHLIST_TARGET_NAMES"] == "${WATCHLIST_TARGET_NAMES:-Reese,Finch}"


def test_workers_default_to_existing_gallery_postgres() -> None:
    compose = _compose()
    services = compose["services"]
    expected = (
        "${C2_REPLAY_FIRST_DATABASE_URL:-"
        "postgresql://video:video@host.docker.internal:5432/video_analytics}"
    )

    assert services["postgres"]["profiles"] == ["c2-local-postgres"]
    for service_name in ("event-worker", "face-worker", "clip-worker", "media-worker"):
        service = services[service_name]
        assert service["environment"]["DATABASE_URL"] == expected
        assert "host.docker.internal:host-gateway" in service["extra_hosts"]
        assert "postgres" not in service.get("depends_on", {})


def test_camera_config_includes_replay_first_source() -> None:
    doc = yaml.safe_load(_text(CAMERA_CONFIG))
    camera = doc["cameras"]["c2_replay_first_rtsp"]
    assert camera["enabled"] is True
    assert camera["source_id"] == "c2_replay_first_rtsp"
    assert camera["rtsp_url"] == FIXED_RTSP
    assert camera["rules"]["intrusion"]["clip_required"] is True
    assert camera["rules"]["intrusion"]["snapshot_required"] is False


def test_evidence_uses_raw_clip_and_jsonl_not_annotated_video() -> None:
    compose = _compose()
    media_env = compose["services"]["media-worker"]["environment"]
    video_sink_env = compose["services"]["video-file-sink"]["environment"]
    doc = _text(DOC)

    assert media_env["EVIDENCE_TOPOLOGY"] == "post_savant_replay"
    assert media_env["RAW_CLIP_SANITIZE_MODE"] == "off"
    assert video_sink_env["CHUNK_SIZE"] == "0"
    assert video_sink_env["METADATA_JSON_FORMAT"] == "native"
    assert "raw_clip.mov" in doc
    assert "annotations.frame_cache.identity.jsonl" in doc
    assert "annotated video clips are not generated" in doc


def test_replay_job_uses_original_stream_cadence_not_inference_fps() -> None:
    compose = _compose()
    clip_env = compose["services"]["clip-worker"]["environment"]

    assert clip_env["REPLAY_STOP_CONDITION_MODE"] == "ts_delta_sec"
    assert clip_env["REPLAY_FPS"] == "${REPLAY_FPS:-24}"
    assert clip_env["REPLAY_FORCE_CONSTANT_CADENCE"] == "true"


def test_replay_first_uses_frame_cache_for_annotations() -> None:
    compose = _compose()
    savant_env = compose["services"]["savant-security"]["environment"]
    media_env = compose["services"]["media-worker"]["environment"]

    assert savant_env["FRAME_ANNOTATION_EXPORT_ENABLED"] == "true"
    assert savant_env["FRAME_ANNOTATION_STREAM"] == "security.frame_annotations"
    assert savant_env["FRAME_ANNOTATION_INCLUDE_EMBEDDING"] == "false"
    assert media_env["FRAME_CACHE_SIDECAR_ENABLED"] == "true"
    assert media_env["FRAME_CACHE_SIDECAR_EVENT_TYPES"] == "intrusion,watchlist_hit"
    assert media_env["FRAME_CACHE_SIDECAR_REQUIRE_TRIGGER_FACE"] == "false"
    assert media_env["FRAME_CACHE_SIDECAR_STREAM"] == "security.frame_annotations"


def test_evidence_viewer_serves_port_8090() -> None:
    compose = _compose()
    viewer = compose["services"]["evidence-viewer"]
    assert viewer["environment"]["EVIDENCE_VIEWER_PORT"] == "8090"
    assert "8090:8090" in viewer["ports"]
    assert "/data/video-analytics/media/evidence:/evidence:ro" in viewer["volumes"]


def test_source_and_clip_lengths_are_configurable() -> None:
    compose = _compose()
    source_env = compose["services"]["source-adapter"]["environment"]
    event_env = compose["services"]["event-worker"]["environment"]
    clip_env = compose["services"]["clip-worker"]["environment"]

    assert source_env["RTSP_URI"] == (
        "${C2_REPLAY_FIRST_RTSP_URI:-rtsp://10.37.57.112:8554/live/1080movie}"
    )
    assert source_env["LOCATION"] == (
        "${C2_REPLAY_FIRST_RTSP_URI:-rtsp://10.37.57.112:8554/live/1080movie}"
    )
    assert FIXED_RTSP in _text(DOC)
    assert event_env["RECORDING_PRE_SECONDS"] == "${RECORDING_PRE_SECONDS:-5}"
    assert event_env["RECORDING_POST_SECONDS"] == "${RECORDING_POST_SECONDS:-5}"
    assert clip_env["DEFAULT_PRE_SECONDS"] == "${DEFAULT_PRE_SECONDS:-5}"
    assert clip_env["DEFAULT_POST_SECONDS"] == "${DEFAULT_POST_SECONDS:-5}"
