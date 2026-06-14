"""Midterm deployment entrypoint contract tests."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "infra" / "docker-compose.midterm.yml"
ENV_FILE = ROOT / "infra" / "env" / "midterm.env"
SOURCES_CONFIG = ROOT / "infra" / "generated" / "sources.generated.yml"
API_FACE_RUNTIME_DOCKERFILE = ROOT / "services" / "api" / "Dockerfile.face-runtime"
API_FACE_RUNTIME_REQUIREMENTS = ROOT / "services" / "api" / "requirements.face-runtime.txt"
MEDIA_WORKER_DOCKERFILE = ROOT / "services" / "media-worker" / "Dockerfile"
REPLAY_CONFIG = ROOT / "modules" / "savant_replay" / "config.midterm.json"
CAMERA_CONFIG = ROOT / "modules" / "savant_security" / "config" / "cameras.midterm.yml"
SAVANT_MODULE = ROOT / "modules" / "savant_security" / "module.yml"
SAVANT_PATCH_DIR = ROOT / "modules" / "savant_security" / "savant_patches"
API_SAVANT_SUPERVISOR = ROOT / "services" / "api" / "app" / "services" / "savant_supervisor.py"
VIDEO_FILE_SINK_ENTRYPOINT = ROOT / "scripts" / "runtime" / "video_file_sink_entrypoint.sh"
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


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fobj:
        for chunk in iter(lambda: fobj.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _sources_config() -> dict:
    return yaml.safe_load(_text(SOURCES_CONFIG))


def _enabled_rtsp_source_count() -> int:
    sources = _sources_config()["sources"]
    return sum(
        1
        for source in sources.values()
        if source.get("enabled") is True
        and source.get("adapter_type") == "gstreamer"
        and str(source.get("uri") or "").startswith(("rtsp://", "rtsps://"))
    )


def _compose_env_default_int(value: str, env_name: str) -> int:
    prefix = "${" + env_name + ":-"
    assert value.startswith(prefix)
    assert value.endswith("}")
    return int(value[len(prefix):-1])


def test_midterm_deployment_files_exist() -> None:
    assert COMPOSE.exists()
    assert ENV_FILE.exists()
    assert API_FACE_RUNTIME_DOCKERFILE.exists()
    assert API_FACE_RUNTIME_REQUIREMENTS.exists()
    assert REPLAY_CONFIG.exists()
    assert CAMERA_CONFIG.exists()
    assert SAVANT_MODULE.exists()
    assert VIDEO_FILE_SINK_ENTRYPOINT.exists()
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
    assert scripts == [
        "check_midterm_deployment.sh",
        "check_operator_camera_and_face_registration.sh",
        "check_savant_perf_observability.sh",
    ]
    assert "infra/docker-compose.midterm.yml" in _text(
        CURRENT_SMOKE_DIR / "check_midterm_deployment.sh"
    )


def test_runtime_doctor_is_midterm_named() -> None:
    runtime_scripts = sorted(path.name for path in (ROOT / "scripts" / "runtime").glob("*.sh"))
    assert runtime_scripts == [
        "doctor_midterm.sh",
        "video_file_sink_entrypoint.sh",
    ]
    text = _text(RUNTIME_DOCTOR)
    assert "docker-compose.midterm.yml" in text
    assert "config.midterm.json" in text
    assert "cameras.midterm.yml" in text
    assert "enabled_rtsp_source_count" in text
    assert "recommended_max_parallel_streams" in text
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


def test_midterm_storage_maintenance_midterm_preview_flags_and_entrypoint() -> None:
    compose = _compose()
    env_file = _env()
    api_env = compose["services"]["api"]["environment"]
    viewer = compose["services"]["evidence-viewer"]

    assert api_env["STORAGE_MAINTENANCE_SUMMARY_ENABLED"] == (
        "${STORAGE_MAINTENANCE_SUMMARY_ENABLED:-true}"
    )
    assert api_env["STORAGE_MAINTENANCE_PREVIEW_ENABLED"] == (
        "${STORAGE_MAINTENANCE_PREVIEW_ENABLED:-true}"
    )
    assert api_env["STORAGE_MAINTENANCE_EXECUTE_ENABLED"] == (
        "${STORAGE_MAINTENANCE_EXECUTE_ENABLED:-false}"
    )
    assert env_file["STORAGE_MAINTENANCE_SUMMARY_ENABLED"] == "true"
    assert env_file["STORAGE_MAINTENANCE_PREVIEW_ENABLED"] == "true"
    assert env_file["STORAGE_MAINTENANCE_EXECUTE_ENABLED"] == "false"
    assert viewer["ports"] == ["8090:8090"]
    assert "/data/video-analytics/media/evidence:/evidence:ro" in viewer["volumes"]
    assert "ports" not in compose["services"]["api"]
    assert compose["services"]["api"]["expose"] == ["8000"]


def test_midterm_runtime_apply_stays_behind_8090_proxy() -> None:
    compose = _compose()
    api = compose["services"]["api"]
    env = api["environment"]

    assert "ports" not in api
    assert api["expose"] == ["8000"]
    assert env["CAMERA_RUNTIME_APPLY_ENABLED"] == "${CAMERA_RUNTIME_APPLY_ENABLED:-true}"
    assert env["CAMERA_RUNTIME_ZMQ_ENDPOINT"] == "dealer+connect:tcp://replay-service:5555"
    assert env["CAMERA_RUNTIME_DOCKER_NETWORK"] == "video-analytics-midterm_default"
    assert env["CAMERA_RUNTIME_REPLAY_CONTAINER"] == "video-analytics-midterm-replay-service"
    assert "../infra/generated:/app/infra/generated:rw" in api["volumes"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in api["volumes"]


def test_replay_first_topology_is_preserved() -> None:
    compose = _compose()
    replay = _replay_config()
    services = compose["services"]

    assert services["source-adapter"]["environment"]["ZMQ_ENDPOINT"] == (
        "dealer+connect:tcp://replay-service:5555"
    )
    assert replay["in_stream"]["url"] == "router+bind:tcp://0.0.0.0:5555"
    assert replay["out_stream"]["url"] == "dealer+connect:tcp://savant-security:5557"
    assert replay["out_stream"]["options"]["send_timeout"] == {"secs": 1, "nanos": 0}
    assert replay["out_stream"]["options"]["send_retries"] == 2
    assert services["savant-security"]["environment"]["ZMQ_SRC_ENDPOINT"] == (
        "router+bind:tcp://0.0.0.0:5557"
    )
    assert services["clip-worker"]["environment"]["REPLAY_JOB_SINK_URL"] == (
        "dealer+connect:tcp://video-file-sink:6666"
    )
    assert services["video-file-sink"]["environment"]["ZMQ_ENDPOINT"] == (
        "router+bind:tcp://0.0.0.0:6666"
    )
    assert services["video-file-sink"]["entrypoint"] == [
        "/bin/sh",
        "/opt/video-file-sink-entrypoint.sh",
    ]
    assert (
        "../scripts/runtime/video_file_sink_entrypoint.sh:/opt/video-file-sink-entrypoint.sh:ro"
        in services["video-file-sink"]["volumes"]
    )
    assert "video_analytics:midterm:runtime_epoch" in _text(VIDEO_FILE_SINK_ENTRYPOINT)
    assert "/media/replay-sink-output/midterm/epochs/${EPOCH_ID}" in _text(
        VIDEO_FILE_SINK_ENTRYPOINT
    )
    assert services["replay-service"]["environment"]["RUST_LOG"] == "${RUST_LOG:-info}"
    assert services["media-worker"]["environment"]["RUNTIME_EPOCH_STATE_PATH"] == (
        "/media/replay-sink-output/midterm/.current_epoch.json"
    )
    assert services["media-worker"]["environment"]["MEDIA_INVALID_SINK_OUTPUT_MAX_RETRIES"] == (
        "${MEDIA_INVALID_SINK_OUTPUT_MAX_RETRIES:-3}"
    )
    assert services["media-worker"]["environment"]["MEDIA_WORKER_STATE_PATH"] == (
        "${MEDIA_WORKER_STATE_PATH:-/media/replay-sink-output/midterm/.media-worker.processed.json}"
    )
    assert services["media-worker"]["environment"]["MEDIA_SINK_SCAN_MAX_METADATA_FILES"] == (
        "${MEDIA_SINK_SCAN_MAX_METADATA_FILES:-2000}"
    )
    assert services["media-worker"]["environment"]["MEDIA_PROBE_TIMEOUT_S"] == (
        "${MEDIA_PROBE_TIMEOUT_S:-30}"
    )
    assert services["media-worker"]["environment"]["MEDIA_DECODE_TIMEOUT_S"] == (
        "${MEDIA_DECODE_TIMEOUT_S:-120}"
    )
    assert services["media-worker"]["environment"]["EVIDENCE_RUNTIME_EPOCH_STRICT"] == (
        "${EVIDENCE_RUNTIME_EPOCH_STRICT:-true}"
    )


def test_midterm_savant_source_reset_patch_is_wired() -> None:
    compose = _compose()
    savant = compose["services"]["savant-security"]
    env = savant["environment"]
    entrypoint = " ".join(savant["entrypoint"])
    healthcheck = savant["healthcheck"]
    patch_root = SAVANT_PATCH_DIR / "v0.6.0"

    assert "savant_patches/apply_patches.py" in entrypoint
    assert "python -m savant.entrypoint" in entrypoint
    assert env["SAVANT_PATCH_ENABLED"] == "${SAVANT_PATCH_ENABLED:-true}"
    assert env["SAVANT_PATCH_ENFORCE"] == "${SAVANT_PATCH_ENFORCE:-true}"
    assert healthcheck["test"] == ["CMD", "sh", "/opt/savant/healthcheck.sh"]
    assert healthcheck["start_period"] == "15m"
    assert (SAVANT_PATCH_DIR / "apply_patches.py").is_file()
    assert (patch_root / "README.md").is_file()
    assert _md5(patch_root / "buffer_processor.py") == "556af89b356401efa1dc9d5c2c4d3c68"
    assert _md5(patch_root / "nvinfer_processor.py") == "9615f7cd134f623a3950b65e5ad71fb1"
    assert _md5(patch_root / "pipeline.py") == "7c5eabbe84697e591a0a31a1c3977f2c"


def test_midterm_savant_supervisor_is_owned_by_api() -> None:
    compose = _compose()
    services = compose["services"]
    api = services["api"]
    env = api["environment"]
    supervisor = _text(API_SAVANT_SUPERVISOR)

    assert "savant-watchdog" not in services
    assert "docker:27-cli" not in _text(COMPOSE)
    assert env["SAVANT_SUPERVISOR_ENABLED"] == "${SAVANT_SUPERVISOR_ENABLED:-true}"
    assert env["SAVANT_SUPERVISOR_DOCKER_SOCKET"] == "/var/run/docker.sock"
    assert env["SAVANT_SUPERVISOR_RESTART_REPLAY"] == (
        "${SAVANT_SUPERVISOR_RESTART_REPLAY:-false}"
    )
    assert env["SAVANT_SUPERVISOR_STATUS_FILE"] == "/opt/savant/status.txt"
    assert env["SAVANT_SUPERVISOR_ANNOTATION_STREAM"] == "security.frame_annotations"
    assert env["SAVANT_SUPERVISOR_COMPOSE_SOURCE_CONTAINER"] == (
        "video-analytics-midterm-source-adapter"
    )
    assert env["SAVANT_SUPERVISOR_DYNAMIC_SOURCE_PREFIX"] == "video-analytics-source-"
    assert "/var/run/docker.sock:/var/run/docker.sock" in api["volumes"]
    assert "DockerSocketClient" in supervisor
    assert "Redis.from_url" in supervisor
    assert "video-analytics-midterm-source-adapter" in supervisor
    assert "video-analytics-source-" in supervisor
    assert "docker restart" not in supervisor


def test_midterm_source_id_and_camera_config_are_neutral() -> None:
    compose = _compose()
    camera_config = yaml.safe_load(_text(CAMERA_CONFIG))
    primary_cameras = [
        camera
        for camera in camera_config["cameras"].values()
        if camera.get("source_id") == "primary_rtsp"
    ]

    assert "SOURCE_ID" not in compose["services"]["savant-security"]["environment"]
    max_parallel_streams = compose["services"]["savant-security"]["environment"][
        "MAX_PARALLEL_STREAMS"
    ]
    assert max_parallel_streams == "${MAX_PARALLEL_STREAMS:-4}"
    assert compose["services"]["savant-security"]["environment"]["BATCH_SIZE"] == "1"
    assert _compose_env_default_int(max_parallel_streams, "MAX_PARALLEL_STREAMS") >= max(
        2,
        _enabled_rtsp_source_count() * 2,
    )
    assert compose["services"]["source-adapter"]["environment"]["SOURCE_ID"] == "primary_rtsp"
    assert compose["services"]["event-worker"]["environment"]["RECORDING_SOURCE_ID"] == "${RECORDING_SOURCE_ID:-}"
    assert compose["services"]["event-worker"]["environment"]["DEFAULT_REPLAY_SOURCE_ID"] == "primary_rtsp"
    assert len(primary_cameras) == 1
    camera = primary_cameras[0]
    assert camera["source_id"] == "primary_rtsp"
    assert camera["name"] == "Primary RTSP Camera"


def test_midterm_runtime_calibration_is_explicit() -> None:
    compose = _compose()
    env_file = _env()
    module = yaml.safe_load(_text(SAVANT_MODULE))
    savant_env = compose["services"]["savant-security"]["environment"]
    face_worker_env = compose["services"]["face-worker"]["environment"]
    elements = {
        element["name"]: element
        for element in module["pipeline"]["elements"]
        if "name" in element
    }

    assert env_file["MAX_FPS_CONTROL"] == "false"
    assert env_file["INGRESS_FPS_GATE_ENABLED"] == "true"
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
    assert env_file["FACE_INFER_INTERVAL"] == "2"
    assert env_file["FACE_EMBEDDING_INFER_INTERVAL"] == "2"
    assert env_file["WATCHLIST_THRESHOLD"] == "0.60"
    assert savant_env["MAX_FPS_CONTROL"] == "${MAX_FPS_CONTROL:-false}"
    assert savant_env["INGRESS_FPS_GATE_ENABLED"] == "${INGRESS_FPS_GATE_ENABLED:-true}"
    assert savant_env["POSE_INFER_INTERVAL"] == "${POSE_INFER_INTERVAL:-1}"
    assert savant_env["FACE_INFER_INTERVAL"] == "${FACE_INFER_INTERVAL:-2}"
    assert savant_env["FACE_EMBEDDING_INFER_INTERVAL"] == (
        "${FACE_EMBEDDING_INFER_INTERVAL:-2}"
    )
    assert module["parameters"]["face_infer_interval"] == (
        "${oc.decode:${oc.env:FACE_INFER_INTERVAL, 0}}"
    )
    assert module["parameters"]["face_embedding_infer_interval"] == (
        "${oc.decode:${oc.env:FACE_EMBEDDING_INFER_INTERVAL, 0}}"
    )
    assert module["parameters"]["max_fps_control"] == (
        "${oc.decode:${oc.env:MAX_FPS_CONTROL, false}}"
    )
    assert module["parameters"]["ingress_fps_gate_enabled"] == (
        "${oc.decode:${oc.env:INGRESS_FPS_GATE_ENABLED, true}}"
    )
    assert module["pipeline"]["source"]["ingress_frame_filter"]["kwargs"]["enabled"] == (
        "${parameters.ingress_fps_gate_enabled}"
    )
    assert elements["yolov8_face"]["properties"]["interval"] == (
        "${parameters.face_infer_interval}"
    )
    assert elements["adaface"]["model"]["interval"] == (
        "${parameters.face_embedding_infer_interval}"
    )
    assert face_worker_env["WATCHLIST_THRESHOLD"] == "${WATCHLIST_THRESHOLD:-0.60}"


def test_midterm_savant_performance_observability_is_wired() -> None:
    compose = _compose()
    env_file = _env()
    module = yaml.safe_load(_text(SAVANT_MODULE))
    savant = compose["services"]["savant-security"]
    savant_env = savant["environment"]
    telemetry = module["parameters"]["telemetry"]["metrics"]
    smoke = _text(CURRENT_SMOKE_DIR / "check_savant_perf_observability.sh")

    assert savant_env["WEBSERVER_PORT"] == "8080"
    assert savant_env["METRICS_FRAME_PERIOD"] == "1000"
    assert savant_env["METRICS_TIME_PERIOD"] == "5"
    assert savant_env["METRICS_HISTORY"] == "100"
    assert savant_env["METRICS_EXTRA_LABELS"] == '{"service":"savant-security","profile":"midterm"}'
    assert savant["ports"] == ["${SAVANT_METRICS_HOST_PORT:-18080}:8080"]
    assert env_file["WEBSERVER_PORT"] == "8080"
    assert env_file["METRICS_FRAME_PERIOD"] == "1000"
    assert env_file["METRICS_TIME_PERIOD"] == "5"
    assert env_file["METRICS_HISTORY"] == "100"
    assert env_file["METRICS_EXTRA_LABELS"] == '{"service":"savant-security","profile":"midterm"}'
    assert telemetry["frame_period"] == "${oc.decode:${oc.env:METRICS_FRAME_PERIOD, 1000}}"
    assert telemetry["time_period"] == "${oc.decode:${oc.env:METRICS_TIME_PERIOD, 5}}"
    assert telemetry["history"] == "${oc.decode:${oc.env:METRICS_HISTORY, 100}}"
    assert telemetry["extra_labels"] == "${json:${oc.env:METRICS_EXTRA_LABELS, null}}"
    assert "PASS_SAVANT_PERF_OBSERVABILITY_READY" in smoke
    assert "XREVRANGE" in smoke
    assert "nvidia-smi" in smoke


def test_midterm_replay_storage_retention_covers_proof_wait() -> None:
    replay = _replay_config()
    rocksdb = replay["storage"]["rocksdb"]

    assert rocksdb["data_expiration_ttl"]["secs"] >= 300
    assert rocksdb["compaction_period"]["secs"] >= 120


def test_midterm_replay_duration_extra_slack_default_is_bounded() -> None:
    compose = _compose()
    clip_env = compose["services"]["clip-worker"]["environment"]

    slack = clip_env["REPLAY_DURATION_EXTRA_SLACK_S"]
    assert slack == "${REPLAY_DURATION_EXTRA_SLACK_S:-5}"
    assert _compose_env_default_int(slack, "REPLAY_DURATION_EXTRA_SLACK_S") <= 5


def test_midterm_clip_worker_queue_safety_defaults_are_explicit() -> None:
    compose = _compose()
    env_file = _env()
    clip_env = compose["services"]["clip-worker"]["environment"]

    assert clip_env["CLIP_WORKER_PENDING_CLAIM_MIN_IDLE_MS"] == (
        "${CLIP_WORKER_PENDING_CLAIM_MIN_IDLE_MS:-5000}"
    )
    assert clip_env["CLIP_WORKER_PENDING_CLAIM_COUNT"] == (
        "${CLIP_WORKER_PENDING_CLAIM_COUNT:-10}"
    )
    assert clip_env["CLIP_WORKER_PENDING_CLAIM_INTERVAL_S"] == (
        "${CLIP_WORKER_PENDING_CLAIM_INTERVAL_S:-5}"
    )
    assert clip_env["CLIP_WORKER_DEFERRED_RETRY_MAX_ATTEMPTS"] == (
        "${CLIP_WORKER_DEFERRED_RETRY_MAX_ATTEMPTS:-12}"
    )
    assert clip_env["POST_SAVANT_FRAME_PROOF_ATTEMPTS"] == (
        "${POST_SAVANT_FRAME_PROOF_ATTEMPTS:-1}"
    )
    assert env_file["CLIP_WORKER_PENDING_CLAIM_MIN_IDLE_MS"] == "5000"
    assert env_file["CLIP_WORKER_PENDING_CLAIM_COUNT"] == "10"
    assert env_file["CLIP_WORKER_PENDING_CLAIM_INTERVAL_S"] == "5"
    assert env_file["CLIP_WORKER_DEFERRED_RETRY_MAX_ATTEMPTS"] == "12"
    assert env_file["POST_SAVANT_FRAME_PROOF_ATTEMPTS"] == "1"


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


def test_midterm_media_worker_perf_controls_are_wired() -> None:
    compose = _compose()
    env_file = _env()
    media_env = compose["services"]["media-worker"]["environment"]
    dockerfile = _text(MEDIA_WORKER_DOCKERFILE)

    assert "apt-get install -y --no-install-recommends ffmpeg" in dockerfile
    assert media_env["FRAME_CACHE_SIDECAR_RANGE_COUNT"] == (
        "${FRAME_CACHE_SIDECAR_RANGE_COUNT:-2000}"
    )
    assert media_env["FRAME_CACHE_SIDECAR_LOOKBACK_COUNT"] == (
        "${FRAME_CACHE_SIDECAR_LOOKBACK_COUNT:-20000}"
    )
    assert media_env["FRAME_CACHE_SIDECAR_MAX_SCAN"] == (
        "${FRAME_CACHE_SIDECAR_MAX_SCAN:-20000}"
    )
    assert env_file["MEDIA_WORKER_STATE_PATH"] == (
        "/media/replay-sink-output/midterm/.media-worker.processed.json"
    )
    assert env_file["MEDIA_SINK_SCAN_MAX_METADATA_FILES"] == "2000"
    assert env_file["MEDIA_PROBE_TIMEOUT_S"] == "30"
    assert env_file["MEDIA_DECODE_TIMEOUT_S"] == "120"
    assert env_file["FRAME_CACHE_SIDECAR_RANGE_COUNT"] == "2000"


def test_midterm_operator_api_reuses_face_runtime_without_host_8000() -> None:
    compose = _compose()
    api = compose["services"]["api"]
    viewer = compose["services"]["evidence-viewer"]
    dockerfile = _text(API_FACE_RUNTIME_DOCKERFILE)
    requirements = _text(API_FACE_RUNTIME_REQUIREMENTS)

    assert api["build"]["dockerfile"] == "Dockerfile.face-runtime"
    assert api["build"]["args"]["FACE_RUNTIME_IMAGE"] == (
        "video-analytics-midterm-face-worker:latest"
    )
    assert api["expose"] == ["8000"]
    assert "ports" not in api
    assert "../libs:/app/libs:ro" in api["volumes"]
    assert viewer["environment"]["OPERATOR_API_BASE_URL"] == "http://api:8000"
    assert viewer["depends_on"]["api"]["condition"] == "service_started"

    assert "FROM ${FACE_RUNTIME_IMAGE}" in dockerfile
    assert "onnxruntime" not in requirements
    assert "opencv-python" not in requirements
    assert "opencv-python-headless" not in requirements
    assert "numpy" not in requirements


def test_docs_point_to_midterm_deployment_entrypoint() -> None:
    for doc in DOCS:
        text = _text(doc)
        assert "infra/docker-compose.midterm.yml" in text
