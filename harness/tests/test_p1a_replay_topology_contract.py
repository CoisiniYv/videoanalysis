"""P1a Replay inline pass-through topology contract checks."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "p1a_replay_inline_pass_through_topology.md"
COMPOSE = ROOT / "infra" / "docker-compose.p1a-replay-inline-poc.yml"
REPLAY_CONFIG = ROOT / "modules" / "savant_replay" / "config.p1a_inline.json"
CAMERA_CONFIG = ROOT / "modules" / "savant_security" / "config" / "cameras.p1a_inline.yml"
SMOKE = ROOT / "scripts" / "smoke" / "check_p1a_replay_inline_pass_through.sh"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compose() -> dict:
    return yaml.safe_load(_text(COMPOSE))


def _replay_config() -> dict:
    return json.loads(_text(REPLAY_CONFIG))


def test_p1a_files_exist() -> None:
    assert DOC.exists()
    assert COMPOSE.exists()
    assert REPLAY_CONFIG.exists()
    assert CAMERA_CONFIG.exists()
    assert SMOKE.exists()


def test_replay_inline_streams_are_configured() -> None:
    replay = _replay_config()
    assert replay["in_stream"]["url"] == "router+bind:tcp://0.0.0.0:5555"
    assert replay["out_stream"] is not None
    assert replay["out_stream"]["url"] == "dealer+connect:tcp://savant-security:5557"


def test_compose_single_path_source_to_replay_to_savant() -> None:
    services = _compose()["services"]
    source_env = services["source-adapter"]["environment"]
    savant_env = services["savant-security"]["environment"]

    assert source_env["SOURCE_ID"] == "p1a_replay_inline"
    assert source_env["ZMQ_ENDPOINT"] == "dealer+connect:tcp://replay-service:5555"
    assert savant_env["SOURCE_ID"] == "p1a_replay_inline"
    assert savant_env["ZMQ_SRC_ENDPOINT"] == "router+bind:tcp://0.0.0.0:5557"
    assert savant_env["EVENT_EXPORTER"] == "redis"
    assert savant_env["EVENT_STREAM"] == "security.events"


def test_compose_forbids_clip_media_sink_and_second_rtsp() -> None:
    services = set(_compose()["services"])
    forbidden = {
        "video-file-sink",
        "clip-worker",
        "media-worker",
        "event-worker",
        "metadata-sink",
        "postgres",
        "rtsp-server",
        "ffmpeg-source",
    }
    assert forbidden.isdisjoint(services)

    content = _text(COMPOSE)
    assert "rtsp://" not in content
    assert "rtsps://" not in content
    assert "/api/v1/job" not in content
    assert "REPLAY_JOB_SINK_URL" not in content


def test_p1a_camera_config_is_poc_scoped() -> None:
    doc = yaml.safe_load(_text(CAMERA_CONFIG))
    cameras = doc["cameras"]
    assert set(cameras) == {"cam_p1a_replay_inline"}
    camera = cameras["cam_p1a_replay_inline"]
    assert camera["source_id"] == "p1a_replay_inline"
    assert camera["rules"]["intrusion"]["enabled"] is True
    assert camera["rules"]["intrusion"]["clip_required"] is False
    assert camera["rules"]["intrusion"]["snapshot_required"] is False


def test_smoke_does_not_use_forbidden_fallbacks_or_clip_generation() -> None:
    smoke = _text(SMOKE)
    forbidden_snippets = [
        "ffmpeg ",
        "ffprobe",
        "generate_visual_result.py",
        "export_local_video_debug_evidence.py",
        "process_evidence_task.py",
        "/api/v1/job",
        "raw_clip",
        "annotated_clip",
    ]
    for snippet in forbidden_snippets:
        assert snippet not in smoke
    assert "up -d source-adapter" in smoke
    assert "video-file-sink" in smoke
    assert "clip-worker" in smoke
    assert "media-worker" in smoke
    assert (
        "docker compose -f \"$COMPOSE_FILE\" up -d --force-recreate "
        "redis replay-service savant-security"
    ) in smoke
    assert "docker compose -f \"$COMPOSE_FILE\" up -d source-adapter" in smoke


def test_doc_records_required_report_fields_and_boundaries() -> None:
    doc = _text(DOC)
    for expected in (
        "Replay input `in_stream`",
        "Source adapter output",
        "Replay output `out_stream`",
        "Savant input",
        "source-adapter -> replay-service -> savant-security",
        "No second RTSP",
        "No production compose",
    ):
        assert expected.lower() in doc.lower()
