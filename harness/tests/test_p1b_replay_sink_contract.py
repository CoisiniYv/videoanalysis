"""P1b-RTSP Replay manual job to Video File Sink contract checks."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "p1b_rtsp_replay_manual_job_to_video_sink.md"
P1B_COMPOSE = ROOT / "infra" / "docker-compose.p1b-rtsp-replay-manual-sink.yml"
REPLAY_CONFIG = ROOT / "modules" / "savant_replay" / "config.p1b_rtsp_inline.json"
CAMERA_CONFIG = ROOT / "modules" / "savant_security" / "config" / "cameras.p1b_rtsp_replay.yml"
REDIS_SHIM = ROOT / "modules" / "savant_security" / "poc_deps" / "redis.py"
SMOKE = ROOT / "scripts" / "smoke" / "check_p1b_rtsp_replay_manual_job_to_video_sink.sh"
PRODUCTION_COMPOSE = ROOT / "infra" / "docker-compose.c1-official-adapter.yml"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compose(path: Path) -> dict:
    return yaml.safe_load(_text(path))


def test_p1b_files_exist() -> None:
    assert DOC.exists()
    assert REPLAY_CONFIG.exists()
    assert CAMERA_CONFIG.exists()
    assert REDIS_SHIM.exists()
    assert P1B_COMPOSE.exists()
    assert SMOKE.exists()


def test_p1b_rtsp_compose_services_and_network_are_scoped() -> None:
    compose = _compose(P1B_COMPOSE)
    services = set(compose["services"])
    forbidden_services = {"api", "clip-worker", "event-worker", "ffmpeg-source", "media-worker", "postgres", "rtsp-server"}
    assert forbidden_services.isdisjoint(services)
    assert {"redis", "replay-service", "savant-security", "source-adapter", "video-file-sink"}.issubset(
        services
    )

    source_env = compose["services"]["source-adapter"]["environment"]
    sink_env = compose["services"]["video-file-sink"]["environment"]
    replay_env = compose["services"]["replay-service"]["environment"]

    assert source_env["SOURCE_ID"] == "p1b_rtsp_replay"
    assert source_env["RTSP_URI"] == "rtsp://10.37.57.112:8554/live/1080movie"
    assert source_env["LOCATION"] == "rtsp://10.37.57.112:8554/live/1080movie"
    assert source_env["ZMQ_ENDPOINT"] == "dealer+connect:tcp://replay-service:5555"

    assert "debug" in replay_env["RUST_LOG"]
    assert "replaydb=trace" in replay_env["RUST_LOG"]
    assert sink_env["ZMQ_ENDPOINT"] == "sub+bind:tcp://0.0.0.0:6666"
    assert "p1b-rtsp-replay-manual-sink" in sink_env["DIR_LOCATION"]
    assert "%source_id%" in sink_env["DIR_LOCATION"]
    assert "%src_filename%" in sink_env["DIR_LOCATION"]

    content = _text(P1B_COMPOSE)
    assert "rtsp://10.37.57.112:8554/live/1080movie" in content
    assert "pip install" not in content
    assert "file:///testVideo" not in content
    assert "/testVideo/test.mp4" not in content
    assert "video_loop.sh" not in content
    assert "source extraction" not in content
    assert "annotated_clip" not in content
    assert "raw_clip" not in content
    assert "event-worker" not in content
    assert "clip-worker" not in content
    assert "media-worker" not in content
    assert "postgres" not in content
    assert "api:" not in content
    assert "rtsp-server" not in content
    assert "ffmpeg-source" not in content
    assert "video-file-sink" in content


def test_p1b_replay_config_is_inline_pass_through() -> None:
    replay = json.loads(_text(REPLAY_CONFIG))
    assert replay["in_stream"]["url"] == "router+bind:tcp://0.0.0.0:5555"
    assert replay["out_stream"] is not None
    assert replay["out_stream"]["url"] == "dealer+connect:tcp://savant-security:5557"
    assert replay["storage"]["rocksdb"]["path"] == "/opt/rocksdb"


def test_p1b_camera_config_is_rtsp_scoped() -> None:
    doc = yaml.safe_load(_text(CAMERA_CONFIG))
    cameras = doc["cameras"]
    assert set(cameras) == {"cam_p1b_rtsp_replay"}
    camera = cameras["cam_p1b_rtsp_replay"]
    assert camera["source_id"] == "p1b_rtsp_replay"
    assert camera["rtsp_url"] == "rtsp://10.37.57.112:8554/live/1080movie"
    assert camera["rules"]["intrusion"]["clip_required"] is False
    assert camera["rules"]["intrusion"]["snapshot_required"] is False


def test_smoke_uses_real_rtsp_and_manual_replay_job() -> None:
    smoke = _text(SMOKE)
    assert "rtsp://10.37.57.112:8554/live/1080movie" in smoke
    assert "local_file_used=no" in smoke
    assert "test_video_used=no" in smoke
    assert "source_extraction_fallback=no" in smoke
    assert "input_type=rtsp" in smoke
    assert "input_uri=${RTSP_URL}" in smoke
    assert "local Redis shim exists" in smoke
    assert "p1b_rtsp_replay" in smoke
    assert "cam_p1b_rtsp_replay" in smoke
    assert "ffprobe -rtsp_transport tcp" in smoke
    assert "/api/v1/keyframes/find" in smoke
    assert "/api/v1/job" in smoke
    assert "pub+connect:tcp://video-file-sink:6666" in smoke
    assert "sub+bind:tcp://0.0.0.0:6666" in smoke


def test_smoke_preserves_rtsp_only_boundaries() -> None:
    smoke = _text(SMOKE)
    assert "second_rtsp_pull_used=no" in smoke
    assert "local_file_used=no" in smoke
    assert "test_video_used=no" in smoke
    assert "source_extraction_fallback=no" in smoke
    assert "annotated_clip=no" in smoke
    assert "source_to_replay_to_savant_single_path=yes" in smoke
    assert "no local file source" not in smoke  # report text lives in docs, not smoke.


def test_production_compose_not_used_for_p1b_rtsp() -> None:
    smoke = _text(SMOKE)
    assert str(PRODUCTION_COMPOSE) not in smoke
    assert "docker-compose.c1-official-adapter.yml" not in smoke


def test_doc_records_p1b_rtsp_required_report_fields_and_boundaries() -> None:
    doc = _text(DOC)
    for expected in (
        "input_type: rtsp",
        "input_uri: rtsp://10.37.57.112:8554/live/1080movie",
        "local_file_used: no",
        "test_video_used: no",
        "source_extraction_fallback: no",
        "source-adapter -> replay-service -> savant-security",
        "manual Replay job -> video-file-sink",
        "Replay job id",
        "anchor keyframe uuid",
        "video duration",
        "no local file source",
        "no source extraction fallback",
        "no second RTSP pull",
        "no clip-worker",
        "no media-worker",
        "no production compose change",
        "boundary: no local file source",
    ):
        assert expected.lower() in doc.lower()
