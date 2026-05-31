"""P1c-RTSP event-triggered Replay evidence bundle contract checks."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "p1c_rtsp_replay_event_evidence_bundle.md"
COMPOSE = ROOT / "infra" / "docker-compose.p1c-rtsp-replay-event-evidence.yml"
REPLAY_CONFIG = ROOT / "modules" / "savant_replay" / "config.p1c_rtsp_inline.json"
CAMERA_CONFIG = ROOT / "modules" / "savant_security" / "config" / "cameras.p1c_rtsp_replay.yml"
SMOKE = ROOT / "scripts" / "smoke" / "check_p1c_rtsp_replay_event_evidence_bundle.sh"


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
    assert event_env["DEFAULT_REPLAY_SOURCE_ID"] == "p1c_rtsp_replay"
    assert clip_env["REPLAY_API_URL"] == "http://replay-service:8080"
    assert clip_env["REPLAY_JOB_SINK_URL"] == "pub+connect:tcp://video-file-sink:6666"
    assert media_env["P1_RAW_CLIP_FINALIZER_ENABLED"] == "true"
    assert media_env["P1_SINK_STABILITY_CHECKS"] == "2"
    assert media_env["EVIDENCE_OUTPUT_DIR"] == "/media/evidence"
    assert media_env["SINK_OUTPUT_DIR"] == "/media/replay-sink-output/p1c-rtsp"


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
        "input_type=rtsp",
        "input_uri=${RTSP_URL}",
        "rtsp://10.37.57.112:8554/live/1080movie",
        "local_file_used=no",
        "test_video_used=no",
        "source_extraction_fallback=no",
        "second_rtsp_pull_used=no",
        "source_to_replay_to_savant_single_path=yes",
        "annotated_clip=no",
        "api_started=no",
        "production_compose_change=no",
        "raw_clip",
        "event_annotation.json",
        "metadata.json",
        "security.record_requests",
        "video-file-sink",
        "clip-worker",
        "media-worker",
    ):
        assert expected in smoke
