"""Midterm deployment entrypoint contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "infra" / "docker-compose.midterm.yml"
ENV_FILE = ROOT / "infra" / "env" / "midterm.env"
REPLAY_CONFIG = ROOT / "modules" / "savant_replay" / "config.midterm.json"
CAMERA_CONFIG = ROOT / "modules" / "savant_security" / "config" / "cameras.midterm.yml"
SAVANT_MODULE = ROOT / "modules" / "savant_security" / "module.yml"
CURRENT_SMOKE_DIR = ROOT / "scripts" / "smoke" / "current"
RUNTIME_DOCTOR = ROOT / "scripts" / "runtime" / "doctor_midterm.sh"
DOCS = (
    ROOT / "README.md",
    ROOT / "CLAUDE.md",
    ROOT / "docs" / "compose_inventory.md",
    ROOT / "docs" / "current_mainline_status.md",
    ROOT / "docs" / "midterm_deployment.md",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compose() -> dict:
    return yaml.safe_load(_text(COMPOSE))


def _env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in _text(ENV_FILE).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _replay_config() -> dict:
    return json.loads(_text(REPLAY_CONFIG))


def test_midterm_deployment_files_exist() -> None:
    assert COMPOSE.exists()
    assert ENV_FILE.exists()
    assert REPLAY_CONFIG.exists()
    assert CAMERA_CONFIG.exists()
    assert SAVANT_MODULE.exists()
    for doc in DOCS:
        assert doc.exists()


def test_active_deploy_surface_has_only_midterm_compose_and_env_files() -> None:
    root_compose_files = sorted(path.name for path in (ROOT / "infra").glob("docker-compose*.yml"))
    env_files = sorted(path.name for path in (ROOT / "infra" / "env").glob("*.env"))
    replay_configs = sorted(path.name for path in (ROOT / "modules" / "savant_replay").glob("config*.json"))
    camera_configs = sorted(
        path.name for path in (ROOT / "modules" / "savant_security" / "config").glob("cameras*.yml")
    )

    assert root_compose_files == ["docker-compose.midterm.yml"]
    assert env_files == ["midterm.env"]
    assert replay_configs == ["config.midterm.json"]
    assert camera_configs == ["cameras.midterm.yml"]


def test_current_smoke_surface_is_midterm_only() -> None:
    scripts = sorted(path.name for path in CURRENT_SMOKE_DIR.glob("*.sh"))
    assert scripts == ["check_midterm_deployment.sh"]
    assert "infra/docker-compose.midterm.yml" in _text(CURRENT_SMOKE_DIR / scripts[0])


def test_runtime_doctor_is_midterm_named() -> None:
    runtime_scripts = sorted(path.name for path in (ROOT / "scripts" / "runtime").glob("*.sh"))
    assert runtime_scripts == ["doctor_midterm.sh"]
    text = _text(RUNTIME_DOCTOR)
    assert "docker-compose.midterm.yml" in text
    assert "config.midterm.json" in text
    assert "cameras.midterm.yml" in text
    assert "video-analytics-midterm" in text


def test_midterm_compose_uses_neutral_project_names() -> None:
    compose = _compose()
    assert compose["name"] == "video-analytics-midterm"
    for service in compose["services"].values():
        container_name = service.get("container_name")
        if container_name:
            assert container_name.startswith("video-analytics-midterm-")


def test_midterm_compose_uses_midterm_config_files() -> None:
    compose = _compose()
    services = compose["services"]
    expected_env = ["./env/midterm.env"]

    for service_name in ("savant-security", "source-adapter", "event-worker", "media-worker"):
        assert services[service_name]["env_file"] == expected_env

    assert (
        "../modules/savant_replay/config.midterm.json:/opt/etc/config.json:ro"
        in services["replay-service"]["volumes"]
    )
    assert services["savant-security"]["environment"]["CAMERAS_CONFIG_PATH"] == (
        "/opt/savant/src/module/config/cameras.midterm.yml"
    )
    assert services["media-worker"]["environment"]["CAMERAS_CONFIG_PATH"] == (
        "/opt/savant/src/module/config/cameras.midterm.yml"
    )


def test_replay_first_topology_is_preserved() -> None:
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


def test_midterm_source_id_and_camera_config_are_neutral() -> None:
    compose = _compose()
    camera_config = yaml.safe_load(_text(CAMERA_CONFIG))
    camera = camera_config["cameras"]["primary_rtsp"]

    assert compose["services"]["savant-security"]["environment"]["SOURCE_ID"] == "primary_rtsp"
    assert compose["services"]["source-adapter"]["environment"]["SOURCE_ID"] == "primary_rtsp"
    assert compose["services"]["event-worker"]["environment"]["RECORDING_SOURCE_ID"] == "primary_rtsp"
    assert compose["services"]["event-worker"]["environment"]["DEFAULT_REPLAY_SOURCE_ID"] == "primary_rtsp"
    assert camera["source_id"] == "primary_rtsp"
    assert camera["name"] == "Primary RTSP Camera"


def test_midterm_runtime_calibration_is_explicit() -> None:
    compose = _compose()
    env_file = _env()
    savant_env = compose["services"]["savant-security"]["environment"]
    face_worker_env = compose["services"]["face-worker"]["environment"]

    assert env_file["MAX_FPS_CONTROL"] == "true"
    assert env_file["MAX_FPS"] == "8/1"
    assert env_file["MIN_FPS"] == "2/1"
    assert env_file["POSE_INFER_INTERVAL"] == "1"
    assert env_file["POSE_CONFIDENCE_THRESHOLD"] == "0.50"
    assert env_file["POSE_KEYPOINT_THRESHOLD"] == "0.35"
    assert env_file["POSE_SELECTOR_CONFIDENCE_THRESHOLD"] == "0.50"
    assert env_file["POSE_SELECTOR_NMS_IOU_THRESHOLD"] == "0.50"
    assert env_file["POSE_MIN_WIDTH"] == "60"
    assert env_file["POSE_MIN_HEIGHT"] == "100"
    assert env_file["FACE_CONFIDENCE_THRESHOLD"] == "0.50"
    assert env_file["WATCHLIST_THRESHOLD"] == "0.60"
    assert savant_env["MAX_FPS_CONTROL"] == "${MAX_FPS_CONTROL:-true}"
    assert savant_env["POSE_INFER_INTERVAL"] == "${POSE_INFER_INTERVAL:-1}"
    assert face_worker_env["WATCHLIST_THRESHOLD"] == "${WATCHLIST_THRESHOLD:-0.60}"


def test_midterm_evidence_version_is_project_named() -> None:
    compose = _compose()
    env_file = _env()
    media_env = compose["services"]["media-worker"]["environment"]

    assert env_file["EVIDENCE_VERSION"] == "midterm"
    assert env_file["EVIDENCE_SCHEMA_VERSION"] == "2.0-midterm"
    assert env_file["EVIDENCE_INCLUDE_LEGACY_METADATA_FIELDS"] == "false"
    assert media_env["EVIDENCE_VERSION"] == "midterm"
    assert media_env["EVIDENCE_SCHEMA_VERSION"] == "2.0-midterm"
    assert media_env["EVIDENCE_INCLUDE_LEGACY_METADATA_FIELDS"] == "false"
    assert "EVIDENCE_PHASE" not in media_env


def test_docs_point_to_midterm_deployment_entrypoint() -> None:
    for doc in DOCS:
        text = _text(doc)
        assert "infra/docker-compose.midterm.yml" in text
