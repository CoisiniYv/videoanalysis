"""Midterm deployment entrypoint contract tests."""

from __future__ import annotations

import hashlib
import json
import os
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
REPLAY_A_CONFIG = ROOT / "modules" / "savant_replay" / "config.midterm.replay-a.json"
REPLAY_B_CONFIG = ROOT / "modules" / "savant_replay" / "config.midterm.replay-b.json"
CAMERA_CONFIG = ROOT / "modules" / "savant_security" / "config" / "cameras.midterm.yml"
SAVANT_MODULE = ROOT / "modules" / "savant_security" / "module.yml"
SAVANT_PATCH_DIR = ROOT / "modules" / "savant_security" / "savant_patches"
API_SAVANT_SUPERVISOR = ROOT / "services" / "api" / "app" / "services" / "savant_supervisor.py"
SAVANT_CUSTOM_SERVICES = ROOT / "modules" / "savant_security" / "custom" / "services"
VIDEO_FILE_SINK_ENTRYPOINT = ROOT / "scripts" / "runtime" / "video_file_sink_entrypoint.sh"
CURRENT_SMOKE_DIR = ROOT / "scripts" / "smoke" / "current"
RUNTIME_DOCTOR = ROOT / "scripts" / "runtime" / "doctor_midterm.sh"
MIDTERM_START = ROOT / "scripts" / "midterm_start.sh"
MIDTERM_STOP = ROOT / "scripts" / "midterm_stop.sh"
MIDTERM_HEALTH = ROOT / "scripts" / "midterm_health.sh"
MIDTERM_PACKAGE_CLEAN = ROOT / "scripts" / "midterm_package_clean.sh"
MIDTERM_DEPLOY_CLEAN = ROOT / "scripts" / "midterm_deploy_clean.sh"
CLEAN_MIGRATION_DOC = ROOT / "docs" / "midterm_clean_machine_migration_2026-06-25.md"
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
    assert MIDTERM_START.exists()
    assert MIDTERM_STOP.exists()
    assert MIDTERM_HEALTH.exists()
    assert MIDTERM_PACKAGE_CLEAN.exists()
    assert MIDTERM_DEPLOY_CLEAN.exists()
    assert CLEAN_MIGRATION_DOC.exists()
    assert API_FACE_RUNTIME_DOCKERFILE.exists()
    assert API_FACE_RUNTIME_REQUIREMENTS.exists()
    assert REPLAY_CONFIG.exists()
    assert REPLAY_A_CONFIG.exists()
    assert REPLAY_B_CONFIG.exists()
    assert CAMERA_CONFIG.exists()
    assert SAVANT_MODULE.exists()
    assert VIDEO_FILE_SINK_ENTRYPOINT.exists()
    for doc in DOCS:
        assert doc.exists()


def test_savant_mux_allows_single_source_bs4_without_changing_dense_shards() -> None:
    services = _compose()["services"]

    assert (
        services["savant-security"]["environment"]["MAX_SAME_SOURCE_FRAMES"]
        == "${SAVANT_SINGLE_MAX_SAME_SOURCE_FRAMES:-4}"
    )
    for service in ("savant-a", "savant-b"):
        assert (
            services[service]["environment"]["MAX_SAME_SOURCE_FRAMES"]
            == "${SAVANT_DUAL_MAX_SAME_SOURCE_FRAMES:-1}"
        )
    assert "${oc.env:MAX_SAME_SOURCE_FRAMES, 1}" in _text(SAVANT_MODULE)


def test_midterm_one_click_startup_scripts_are_the_customer_entrypoint() -> None:
    start = _text(MIDTERM_START)
    stop = _text(MIDTERM_STOP)
    health = _text(MIDTERM_HEALTH)

    for script in (MIDTERM_START, MIDTERM_STOP, MIDTERM_HEALTH):
        assert os.access(script, os.X_OK)

    assert 'COMPOSE_FILE="$REPO_ROOT/infra/docker-compose.midterm.yml"' in start
    assert 'STORAGE_OVERRIDE="$REPO_ROOT/infra/midterm-storage.override.yml"' in start
    assert 'ENV_FILE="$REPO_ROOT/infra/env/midterm.env"' in start
    assert 'COMPOSE_ARGS=(--env-file "$ENV_FILE" -f "$COMPOSE_FILE")' in start
    assert 'COMPOSE_ARGS+=(-f "$STORAGE_OVERRIDE")' in start
    storage_override = _text(ROOT / "infra" / "midterm-storage.override.yml")
    assert "FACE_TRAJECTORY_CACHE_HOST_ROOT" in storage_override
    assert "/home/user/video-analytics-fast/face_trajectory_cache" in storage_override
    assert 'FACE_TRAJECTORY_CACHE_LIMIT_PER_PERSON: "100"' in storage_override
    assert 'docker compose "${COMPOSE_ARGS[@]}" build face-worker' in start
    assert "check_model_assets" in start
    assert "POSE_MODEL_FILE" in start
    assert "FACE_DETECTOR_MODEL_FILE" in start
    assert "build_yolo_dynamic_batch_engines.sh" in start
    assert "ensure_yolov8_face_symlinks" in start
    assert "http://127.0.0.1:8090/operator" in start
    assert "http://127.0.0.1:8000" not in start

    assert 'COMPOSE_ARGS=(--env-file "$ENV_FILE" -f "$COMPOSE_FILE")' in stop
    assert 'COMPOSE_ARGS+=(-f "$STORAGE_OVERRIDE")' in stop
    assert "local-postgres" in stop
    assert "dual-4090-two-source" in stop

    assert 'COMPOSE_ARGS=(--env-file "$ENV_FILE" -f "$COMPOSE_FILE")' in health
    assert 'COMPOSE_ARGS+=(-f "$STORAGE_OVERRIDE")' in health
    assert '"savant-security"' in health
    assert '"source-adapter"' in health
    assert '"18080:Savant metrics"' in health


def test_midterm_clean_machine_migration_scripts_exclude_old_runtime_data() -> None:
    package = _text(MIDTERM_PACKAGE_CLEAN)
    deploy = _text(MIDTERM_DEPLOY_CLEAN)
    doc = _text(CLEAN_MIGRATION_DOC)

    for script in (MIDTERM_PACKAGE_CLEAN, MIDTERM_DEPLOY_CLEAN):
        assert os.access(script, os.X_OK)

    assert "models.tgz" in package
    assert "repo.tgz" in package
    assert "--include-images" in package
    assert "images.tar" in package
    assert "contains_docker_images=$INCLUDE_IMAGES" in package
    assert "docker save -o" in package
    assert "ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0" in package
    assert "video-analytics-midterm-rolling-cache-sink:latest" in package
    assert "contains_postgres_dump=false" in package
    assert "contains_redis_state=false" in package
    assert "contains_media_evidence=false" in package
    assert "contains_replay_rocksdb=false" in package
    assert "contains_downloads=false" in package
    assert "contains_models_savant_b=false" in package
    assert "models-savant-b" not in " ".join(
        line.strip()
        for line in package.splitlines()
        if line.strip().startswith("tar -C")
    )

    assert "repo.tgz" in deploy
    assert "models.tgz" in deploy
    assert "images.tar" in deploy
    assert "docker load -i" in deploy
    assert "--no-build" in deploy
    assert "media/face_uploads" in deploy
    assert "media/face_registration" in deploy
    assert "replay-midterm-a" in deploy
    assert "replay-midterm-b" in deploy
    assert "scripts/midterm_start.sh" in deploy

    assert "新机器干净迁移" in doc
    assert "不迁移旧数据库" in doc
    assert "PostgreSQL persons" in doc
    assert "person_gallery_embeddings" in doc
    assert "media/face-registration/reese.jpg" in doc
    assert "media/face-registration/finch.jpg" in doc


def test_active_deploy_surface_has_only_midterm_compose_and_env_files() -> None:
    root_compose_files = sorted(path.name for path in (ROOT / "infra").glob("docker-compose*.yml"))
    env_files = sorted(path.name for path in (ROOT / "infra" / "env").glob("*.env"))
    replay_configs = sorted(path.name for path in (ROOT / "modules" / "savant_replay").glob("config*.json"))
    camera_configs = sorted(
        path.name for path in (ROOT / "modules" / "savant_security" / "config").glob("cameras*.yml")
    )

    assert root_compose_files == ["docker-compose.midterm.yml"]
    assert env_files == ["midterm.env"]
    assert replay_configs == [
        "config.midterm.json",
        "config.midterm.replay-a.json",
        "config.midterm.replay-b.json",
        "config.midterm.replay-c.json",
        "config.midterm.replay-d.json",
        "config.midterm.replay-e.json",
        "config.midterm.replay-f.json",
        "config.midterm.replay-g.json",
        "config.midterm.replay-h.json",
    ]
    assert camera_configs == ["cameras.midterm.yml"]


def test_current_smoke_surface_is_midterm_only() -> None:
    scripts = sorted(path.name for path in CURRENT_SMOKE_DIR.glob("*.sh"))
    assert scripts == [
        "check_dual_4090_two_source_replay_inference_evidence.sh",
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
        "precreate_operator_dual_runtime.sh",
        "prepare_dual_4090_savant_b_model_cache.sh",
        "rolling_cache_sink_entrypoint.sh",
        "run_pressure60_dual1gpu_profile.sh",
        "run_pressure60_t4_analysis_matrix.sh",
        "video_file_sink_entrypoint.sh",
    ]
    text = _text(RUNTIME_DOCTOR)
    assert "docker-compose.midterm.yml" in text
    assert "config.midterm.json" in text
    assert "cameras.midterm.yml" in text
    assert "enabled_rtsp_source_count" in text
    assert "recommended_max_parallel_streams" in text
    assert "video-analytics-midterm" in text
    assert (ROOT / "scripts" / "runtime" / "prepare_dual_4090_savant_b_model_cache.sh").is_file()


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

    for service_name in ("savant-security", "event-worker", "media-worker"):
        assert services[service_name]["env_file"] == expected_env
    assert "env_file" not in services["source-adapter"]
    assert services["source-adapter"]["profiles"] == ["legacy-primary-rtsp"]

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
    assert env_file["STORAGE_MAINTENANCE_EXECUTE_ENABLED"] == "true"
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
    assert env["REPLAY_SHARDS_CONFIG_PATH"] == "${REPLAY_SHARDS_CONFIG_PATH:-}"
    assert env["CAMERA_RUNTIME_DOCKER_NETWORK"] == "video-analytics-midterm_default"
    assert env["CAMERA_RUNTIME_REPLAY_CONTAINER"] == "video-analytics-midterm-replay-service"
    assert "../infra/generated:/app/infra/generated:rw" in api["volumes"]
    assert "../infra/config:/app/infra/config:ro" in api["volumes"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in api["volumes"]


def test_replay_first_topology_is_preserved() -> None:
    compose = _compose()
    replay = _replay_config()
    services = compose["services"]

    assert services["source-adapter"]["environment"]["ZMQ_ENDPOINT"] == (
        "dealer+connect:tcp://replay-service:5555"
    )
    assert services["source-adapter"]["profiles"] == ["legacy-primary-rtsp"]
    assert services["replay-a"]["profiles"] == [
        "dual-replay-shards",
        "dual-4090-two-source",
        "operator-dual-runtime",
    ]
    assert services["replay-b"]["profiles"] == [
        "dual-replay-shards",
        "dual-4090-two-source",
        "operator-dual-runtime",
    ]
    assert services["replay-a"]["volumes"][0] == (
        "../modules/savant_replay/config.midterm.replay-a.json:/opt/etc/config.json:ro"
    )
    assert services["replay-b"]["volumes"][0] == (
        "../modules/savant_replay/config.midterm.replay-b.json:/opt/etc/config.json:ro"
    )
    assert services["replay-a"]["volumes"][-1] == (
        "/data/video-analytics/replay-midterm-a:/opt/rocksdb:rw"
    )
    assert services["replay-b"]["volumes"][-1] == (
        "/data/video-analytics/replay-midterm-b:/opt/rocksdb:rw"
    )
    assert replay["in_stream"]["url"] == "router+bind:tcp://0.0.0.0:5555"
    assert replay["out_stream"]["url"] == "dealer+connect:tcp://replay-raw-fanout:5557"
    assert replay["out_stream"]["options"]["send_timeout"] == {"secs": 1, "nanos": 0}
    assert replay["out_stream"]["options"]["send_retries"] == 2
    assert services["replay-raw-fanout"]["environment"]["FORWARDER_IN_ENDPOINT"] == (
        "router+bind:tcp://0.0.0.0:5557"
    )
    assert services["replay-raw-fanout"]["environment"]["FORWARDER_OUT_ENDPOINT"] == (
        "dealer+connect:tcp://analysis-forwarder:5557"
    )
    assert services["replay-raw-fanout"]["environment"]["FORWARDER_RAW_OUT_ENDPOINT"] == (
        "pub+bind:tcp://0.0.0.0:5560"
    )
    assert services["replay-raw-fanout"]["environment"]["FORWARDER_SAMPLER_ENABLED"] == "false"
    assert services["analysis-forwarder"]["environment"]["FORWARDER_IN_ENDPOINT"] == (
        "router+bind:tcp://0.0.0.0:5557"
    )
    assert services["analysis-forwarder"]["environment"]["FORWARDER_OUT_ENDPOINT"] == (
        "${FORWARDER_OUT_ENDPOINT:-dealer+connect:tcp://savant-security:5557}"
    )
    assert services["analysis-forwarder"]["environment"]["FORWARDER_RAW_OUT_ENDPOINT"] == (
        "${FORWARDER_RAW_OUT_ENDPOINT:-null://}"
    )
    assert services["analysis-forwarder"]["environment"]["FORWARDER_QUEUE_MAX_SIZE"] == (
        "${FORWARDER_QUEUE_MAX_SIZE:-2048}"
    )
    assert services["analysis-forwarder"]["environment"]["FORWARDER_SEND_TIMEOUT_MS"] == (
        "${FORWARDER_SEND_TIMEOUT_MS:-2000}"
    )
    assert services["analysis-forwarder"]["environment"]["FORWARDER_SEND_RETRIES"] == (
        "${FORWARDER_SEND_RETRIES:-3}"
    )
    assert services["analysis-forwarder"]["environment"]["FORWARDER_SEND_HWM"] == (
        "${FORWARDER_SEND_HWM:-1000}"
    )
    assert services["savant-security"]["environment"]["ZMQ_SRC_ENDPOINT"] == (
        "router+bind:tcp://0.0.0.0:5557"
    )
    assert services["clip-worker"]["environment"]["REPLAY_JOB_SINK_URL"] == (
        "dealer+connect:tcp://video-file-sink:6666"
    )
    assert services["clip-worker"]["environment"]["REPLAY_SHARDS_CONFIG_PATH"] == (
        "${REPLAY_SHARDS_CONFIG_PATH:-}"
    )
    assert "../infra/config:/app/infra/config:ro" in services["clip-worker"]["volumes"]
    assert services["video-file-sink"]["environment"]["ZMQ_ENDPOINT"] == (
        "router+bind:tcp://0.0.0.0:6666"
    )
    assert services["video-file-sink"]["entrypoint"] == [
        "/bin/sh",
        "/opt/video-file-sink-entrypoint.sh",
    ]
    for service_name, created_by in (
        ("video-file-sink-a", "video-file-sink-a.startup"),
        ("video-file-sink-b", "video-file-sink-b.startup"),
    ):
        sink = services[service_name]
        assert sink["profiles"] == [
            "dual-replay-shards",
            "dual-4090-two-source",
            "operator-dual-runtime",
        ]
        assert sink["environment"]["ZMQ_ENDPOINT"] == "router+bind:tcp://0.0.0.0:6666"
        assert sink["environment"]["VIDEO_FILE_SINK_REUSE_CURRENT_EPOCH"] == "true"
        assert sink["environment"]["VIDEO_FILE_SINK_CREATED_BY"] == created_by
        assert sink["depends_on"]["video-file-sink"]["condition"] == "service_started"
        assert sink["entrypoint"] == [
            "/bin/sh",
            "/opt/video-file-sink-entrypoint.sh",
        ]
    assert (
        "../scripts/runtime/video_file_sink_entrypoint.sh:/opt/video-file-sink-entrypoint.sh:ro"
        in services["video-file-sink"]["volumes"]
    )
    assert (
        "../scripts/runtime/video_file_sink_entrypoint.sh:/opt/video-file-sink-entrypoint.sh:ro"
        in services["video-file-sink-a"]["volumes"]
    )
    assert (
        "../scripts/runtime/video_file_sink_entrypoint.sh:/opt/video-file-sink-entrypoint.sh:ro"
        in services["video-file-sink-b"]["volumes"]
    )
    assert "video_analytics:midterm:runtime_epoch" in _text(VIDEO_FILE_SINK_ENTRYPOINT)
    assert 'VIDEO_FILE_SINK_EPOCH_ROOT:-/media/replay-sink-output/midterm' in _text(
        VIDEO_FILE_SINK_ENTRYPOINT
    )
    assert 'DIR_LOCATION="${EPOCH_ROOT}/epochs/${EPOCH_ID}' in _text(
        VIDEO_FILE_SINK_ENTRYPOINT
    )
    assert "/%source_id/%src_filename/" in _text(VIDEO_FILE_SINK_ENTRYPOINT)
    assert "%source_id%" not in _text(VIDEO_FILE_SINK_ENTRYPOINT)
    assert "%src_filename%" not in _text(VIDEO_FILE_SINK_ENTRYPOINT)
    assert "VIDEO_FILE_SINK_REUSE_CURRENT_EPOCH" in _text(VIDEO_FILE_SINK_ENTRYPOINT)
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
        "${MEDIA_SINK_SCAN_MAX_METADATA_FILES:-20000}"
    )
    assert services["media-worker"]["environment"]["MEDIA_PROBE_TIMEOUT_S"] == (
        "${MEDIA_PROBE_TIMEOUT_S:-30}"
    )
    assert services["media-worker"]["environment"]["MEDIA_DECODE_TIMEOUT_S"] == (
        "${MEDIA_DECODE_TIMEOUT_S:-120}"
    )
    assert services["media-worker"]["environment"][
        "MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE"
    ] == "${MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE:-4}"
    assert services["media-worker"]["environment"][
        "MEDIA_WORKER_MATERIALIZATION_TIMEOUT_S"
    ] == "${MEDIA_WORKER_MATERIALIZATION_TIMEOUT_S:-180}"
    assert services["media-worker"]["environment"][
        "MEDIA_WORKER_MATERIALIZATION_MAX_BACKLOG"
    ] == "${MEDIA_WORKER_MATERIALIZATION_MAX_BACKLOG:-200}"
    assert services["media-worker"]["environment"][
        "MEDIA_WORKER_MATERIALIZATION_MAX_PER_POLL"
    ] == "${MEDIA_WORKER_MATERIALIZATION_MAX_PER_POLL:-0}"
    assert services["media-worker"]["environment"]["MEDIA_WORKER_FINALIZER_WORKERS"] == (
        "${MEDIA_WORKER_FINALIZER_WORKERS:-16}"
    )
    assert services["media-worker"]["environment"][
        "MEDIA_WORKER_FINALIZER_PROCESS_WORKERS"
    ] == "${MEDIA_WORKER_FINALIZER_PROCESS_WORKERS:-0}"
    assert services["media-worker"]["environment"][
        "MEDIA_WORKER_FINALIZER_MAX_PER_SOURCE_PER_POLL"
    ] == "${MEDIA_WORKER_FINALIZER_MAX_PER_SOURCE_PER_POLL:-4}"
    assert services["media-worker"]["environment"][
        "MEDIA_WORKER_FINALIZER_SOURCE_SERIAL"
    ] == "${MEDIA_WORKER_FINALIZER_SOURCE_SERIAL:-false}"
    assert services["media-worker"]["environment"][
        "MEDIA_WORKER_SINGLE_FINALIZER_V2_ENABLED"
    ] == "${MEDIA_WORKER_SINGLE_FINALIZER_V2_ENABLED:-true}"
    assert services["media-worker"]["environment"][
        "MEDIA_WORKER_MATERIALIZATION_THROTTLE_SLEEP_S"
    ] == "${MEDIA_WORKER_MATERIALIZATION_THROTTLE_SLEEP_S:-0}"
    assert services["media-worker"]["environment"][
        "MEDIA_WORKER_MATERIALIZATION_THROTTLE_DEADLINE_GUARD_S"
    ] == "${MEDIA_WORKER_MATERIALIZATION_THROTTLE_DEADLINE_GUARD_S:-90}"
    assert services["media-worker"]["environment"][
        "MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT"
    ] == "${MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT:-4}"
    assert services["media-worker"]["environment"]["MEDIA_WORKER_FFMPEG_X264_PRESET"] == (
        "${MEDIA_WORKER_FFMPEG_X264_PRESET:-ultrafast}"
    )
    assert services["clip-worker"]["environment"][
        "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD"
    ] == "${EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD:-9}"
    assert services["clip-worker"]["environment"][
        "EVIDENCE_UNKNOWN_SOURCE_FAIL_CLOSED"
    ] == "${EVIDENCE_UNKNOWN_SOURCE_FAIL_CLOSED:-true}"
    assert services["event-worker"]["environment"][
        "EVIDENCE_REPLAY_TTL_SECONDS"
    ] == "${EVIDENCE_REPLAY_TTL_SECONDS:-300}"
    assert services["event-worker"]["environment"][
        "EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS"
    ] == "${EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS:-600}"
    assert services["media-worker"]["environment"][
        "EVIDENCE_FINAL_ROOT_MAX_BYTES"
    ] == "${EVIDENCE_FINAL_ROOT_MAX_BYTES:-0}"
    assert services["media-worker"]["environment"][
        "MEDIA_WORKER_CLEANUP_REPLAY_SINK_OUTPUT_ENABLED"
    ] == "${MEDIA_WORKER_CLEANUP_REPLAY_SINK_OUTPUT_ENABLED:-true}"
    assert services["media-worker"]["environment"][
        "MEDIA_WORKER_CLEANUP_REPLAY_SINK_OUTPUT_STATUSES"
    ] == (
        "${MEDIA_WORKER_CLEANUP_REPLAY_SINK_OUTPUT_STATUSES:-"
        "ready,generated,generated_unverified,generated_annotation_failed,"
        "duration_guard_failed,generated_corrupt,failed}"
    )
    assert services["media-worker"]["environment"]["EVIDENCE_RUNTIME_EPOCH_STRICT"] == (
        "${EVIDENCE_RUNTIME_EPOCH_STRICT:-true}"
    )
    assert services["media-worker"]["environment"]["RAW_CLIP_SANITIZE_MODE"] == (
        "${RAW_CLIP_SANITIZE_MODE:-auto}"
    )
    assert services["media-worker"]["environment"][
        "POST_SAVANT_FAST_RAW_CLIP_ENABLED"
    ] == "${POST_SAVANT_FAST_RAW_CLIP_ENABLED:-true}"


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
    assert _md5(patch_root / "pipeline.py") == "d2a5581899765f173fdfc6e2f1515b2d"


def test_midterm_production_savant_hot_path_excludes_debug_and_unused_encoding() -> None:
    compose = _compose()
    module = yaml.safe_load(_text(SAVANT_MODULE))
    debug_elements = {
        "face_embedding_debug",
        "same_frame_detection_debug",
        "face_debug",
        "replay_savant_frame_dump",
    }
    element_names = {
        element.get("name")
        for element in module["pipeline"]["elements"]
    }

    assert element_names.isdisjoint(debug_elements)

    for service_name in ("savant-security", "savant-a", "savant-b"):
        env = compose["services"][service_name]["environment"]
        assert env["OUTPUT_FRAME"] == '{"codec":"copy"}'

    savant_env = compose["services"]["savant-security"]["environment"]
    assert savant_env["SAME_FRAME_DEBUG_ENABLED"] == "${SAME_FRAME_DEBUG_ENABLED:-false}"
    assert "nvenc" not in _text(COMPOSE)
    assert "h264" not in savant_env["OUTPUT_FRAME"]


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
    assert env["SAVANT_SUPERVISOR_AUTO_REPAIR_STOPPED_SOURCES"] == (
        "${SAVANT_SUPERVISOR_AUTO_REPAIR_STOPPED_SOURCES:-false}"
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
    configured_cameras = list(camera_config["cameras"].values())

    assert "SOURCE_ID" not in compose["services"]["savant-security"]["environment"]
    max_parallel_streams = compose["services"]["savant-security"]["environment"][
        "MAX_PARALLEL_STREAMS"
    ]
    assert max_parallel_streams == "${MAX_PARALLEL_STREAMS:-64}"
    assert compose["services"]["savant-security"]["environment"]["BATCH_SIZE"] == (
        "${BATCH_SIZE:-4}"
    )
    assert _compose_env_default_int(max_parallel_streams, "MAX_PARALLEL_STREAMS") >= max(
        2,
        _enabled_rtsp_source_count() * 2,
    )
    assert compose["services"]["source-adapter"]["environment"]["SOURCE_ID"] == "primary_rtsp"
    assert compose["services"]["source-adapter"]["environment"]["SYNC_OUTPUT"] == "false"
    assert compose["services"]["source-adapter"]["profiles"] == ["legacy-primary-rtsp"]
    assert "env_file" not in compose["services"]["source-adapter"]
    assert compose["services"]["event-worker"]["environment"]["RECORDING_SOURCE_ID"] == "${RECORDING_SOURCE_ID:-}"
    assert compose["services"]["event-worker"]["environment"]["DEFAULT_REPLAY_SOURCE_ID"] == "primary_rtsp"
    assert configured_cameras
    source_ids = [camera.get("source_id") for camera in configured_cameras]
    assert all(source_ids)
    assert len(source_ids) == len(set(source_ids))
    assert all(camera.get("name") for camera in configured_cameras)


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
    assert env_file["BATCH_SIZE"] == "4"
    assert env_file["POSE_BATCH_SIZE"] == "4"
    assert env_file["FACE_DETECTOR_BATCH_SIZE"] == "4"
    assert env_file["FACE_EMBEDDING_BATCH_SIZE"] == "16"
    assert env_file["POSE_MODEL_FILE"] == "/models/yolo26_pose/yolo26_pose.dynamic.raw56.onnx"
    assert env_file["POSE_DECODER_LAYOUT"] == "decoded_56"
    assert env_file["FACE_DETECTOR_MODEL_FILE"] == (
        "/models/yolov8_face/yolov8n-face.dynamic.onnx"
    )
    assert env_file["MAX_PARALLEL_STREAMS"] == "64"
    assert env_file["BATCHED_PUSH_TIMEOUT"] == "40000"
    assert env_file["STREAM_SESSION_PTS_ROLLBACK_TOLERANCE_NS"] == "5000000000"
    assert env_file["POSE_INFER_INTERVAL"] == "1"
    assert env_file["POSE_CONFIDENCE_THRESHOLD"] == "0.60"
    assert env_file["POSE_KEYPOINT_THRESHOLD"] == "0.35"
    assert env_file["POSE_SELECTOR_CONFIDENCE_THRESHOLD"] == "0.60"
    assert env_file["POSE_SELECTOR_NMS_IOU_THRESHOLD"] == "0.50"
    assert env_file["POSE_MIN_WIDTH"] == "60"
    assert env_file["POSE_MIN_HEIGHT"] == "100"
    assert env_file["FACE_CONFIDENCE_THRESHOLD"] == "0.50"
    assert env_file["FACE_INFER_INTERVAL"] == "7"
    assert env_file["FACE_EMBEDDING_INFER_INTERVAL"] == "7"
    assert env_file["WATCHLIST_THRESHOLD"] == "0.60"
    assert savant_env["MAX_FPS_CONTROL"] == "${MAX_FPS_CONTROL:-false}"
    assert savant_env["INGRESS_FPS_GATE_ENABLED"] == "${INGRESS_FPS_GATE_ENABLED:-true}"
    assert savant_env["STREAM_SESSION_PTS_ROLLBACK_TOLERANCE_NS"] == (
        "${STREAM_SESSION_PTS_ROLLBACK_TOLERANCE_NS:-5000000000}"
    )
    assert savant_env["POSE_INFER_INTERVAL"] == "${POSE_INFER_INTERVAL:-1}"
    assert savant_env["FACE_INFER_INTERVAL"] == "${FACE_INFER_INTERVAL:-7}"
    assert savant_env["FACE_EMBEDDING_INFER_INTERVAL"] == (
        "${FACE_EMBEDDING_INFER_INTERVAL:-7}"
    )
    assert savant_env["BATCH_SIZE"] == "${BATCH_SIZE:-4}"
    assert savant_env["POSE_BATCH_SIZE"] == "${POSE_BATCH_SIZE:-4}"
    assert savant_env["FACE_DETECTOR_BATCH_SIZE"] == "${FACE_DETECTOR_BATCH_SIZE:-4}"
    assert savant_env["FACE_EMBEDDING_BATCH_SIZE"] == "${FACE_EMBEDDING_BATCH_SIZE:-16}"
    assert savant_env["POSE_MODEL_FILE"] == (
        "${POSE_MODEL_FILE:-/models/yolo26_pose/yolo26_pose.dynamic.raw56.onnx}"
    )
    assert savant_env["POSE_DECODER_LAYOUT"] == "${POSE_DECODER_LAYOUT:-decoded_56}"
    assert savant_env["FACE_DETECTOR_MODEL_FILE"] == (
        "${FACE_DETECTOR_MODEL_FILE:-/models/yolov8_face/yolov8n-face.dynamic.onnx}"
    )
    assert savant_env["BATCHED_PUSH_TIMEOUT"] == "${BATCHED_PUSH_TIMEOUT:-40000}"
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
    assert elements["yolov8_face"]["model"]["model_file"] == (
        "${oc.env:FACE_DETECTOR_MODEL_FILE,/models/yolov8_face/yolov8n-face.dynamic.onnx}"
    )
    assert elements["yolo26_pose"]["model"]["model_file"] == (
        "${oc.env:POSE_MODEL_FILE,/models/yolo26_pose/yolo26_pose.dynamic.raw56.onnx}"
    )
    assert elements["yolo26_pose"]["model"]["output"]["converter"]["kwargs"]["decoder_layout"] == (
        "${oc.env:POSE_DECODER_LAYOUT,decoded_56}"
    )
    assert elements["adaface"]["model"]["interval"] == (
        "${parameters.face_embedding_infer_interval}"
    )
    assert face_worker_env["WATCHLIST_THRESHOLD"] == "${WATCHLIST_THRESHOLD:-0.60}"


def test_midterm_savant_redis_exporters_are_async_and_bounded() -> None:
    compose = _compose()
    env_file = _env()
    savant_env = compose["services"]["savant-security"]["environment"]
    writer = _text(SAVANT_CUSTOM_SERVICES / "redis_stream_writer.py")

    assert env_file["SAVANT_REDIS_EXPORTER_SOCKET_TIMEOUT_MS"] == "500"
    assert env_file["SAVANT_REDIS_EXPORTER_CONNECT_TIMEOUT_MS"] == "500"
    assert env_file["SAVANT_REDIS_EXPORTER_QUEUE_MAXSIZE"] == "8192"
    assert env_file["SAVANT_REDIS_EXPORTER_WRITE_RETRIES"] == "10"
    assert env_file["SAVANT_REDIS_EXPORTER_RETRY_SLEEP_MS"] == "20"
    assert env_file["FRAME_ANNOTATION_WRITE_TIMEOUT_MS"] == "500"
    assert env_file["FRAME_ANNOTATION_REDIS_QUEUE_MAXSIZE"] == "8192"
    assert savant_env["SAVANT_REDIS_EXPORTER_SOCKET_TIMEOUT_MS"] == (
        "${SAVANT_REDIS_EXPORTER_SOCKET_TIMEOUT_MS:-500}"
    )
    assert savant_env["SAVANT_REDIS_EXPORTER_CONNECT_TIMEOUT_MS"] == (
        "${SAVANT_REDIS_EXPORTER_CONNECT_TIMEOUT_MS:-500}"
    )
    assert savant_env["SAVANT_REDIS_EXPORTER_QUEUE_MAXSIZE"] == (
        "${SAVANT_REDIS_EXPORTER_QUEUE_MAXSIZE:-8192}"
    )
    assert savant_env["SAVANT_REDIS_EXPORTER_WRITE_RETRIES"] == (
        "${SAVANT_REDIS_EXPORTER_WRITE_RETRIES:-10}"
    )
    assert savant_env["SAVANT_REDIS_EXPORTER_RETRY_SLEEP_MS"] == (
        "${SAVANT_REDIS_EXPORTER_RETRY_SLEEP_MS:-20}"
    )
    assert savant_env["FRAME_ANNOTATION_WRITE_TIMEOUT_MS"] == (
        "${FRAME_ANNOTATION_WRITE_TIMEOUT_MS:-500}"
    )
    assert savant_env["FRAME_ANNOTATION_REDIS_QUEUE_MAXSIZE"] == (
        "${FRAME_ANNOTATION_REDIS_QUEUE_MAXSIZE:-8192}"
    )
    assert "queue.Queue" in writer
    assert "put_nowait" in writer
    assert "socket_connect_timeout" in writer
    assert "socket_timeout" in writer
    for filename in (
        "event_exporter.py",
        "person_observation_exporter.py",
        "face_observation_exporter.py",
        "frame_annotation_exporter.py",
    ):
        service = _text(SAVANT_CUSTOM_SERVICES / filename)
        assert "AsyncRedisStreamWriter" in service
        assert ".xadd(" not in service


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
    assert env_file["SAVANT_PERF_METRICS_ENABLED"] == "true"
    assert env_file["SAVANT_PERF_METRICS_STAGE_RATES_ENABLED"] == "true"
    assert env_file["SAVANT_PERF_METRICS_FPS_WINDOW_S"] == "10"
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


def test_midterm_replay_uses_constant_cadence_by_default() -> None:
    compose = _compose()
    env_file = _env()
    clip_env = compose["services"]["clip-worker"]["environment"]

    assert clip_env["REPLAY_FORCE_CONSTANT_CADENCE"] == (
        "${REPLAY_FORCE_CONSTANT_CADENCE:-true}"
    )
    assert clip_env["CLIP_WORKER_PLANNER_SHADOW_ENABLED"] == (
        "${CLIP_WORKER_PLANNER_SHADOW_ENABLED:-true}"
    )
    assert clip_env["CLIP_WORKER_COORDINATOR_V2_ENABLED"] == (
        "${CLIP_WORKER_COORDINATOR_V2_ENABLED:-true}"
    )
    assert clip_env["CLIP_WORKER_CRASH_INJECT_POINT"] == (
        "${CLIP_WORKER_CRASH_INJECT_POINT:-}"
    )
    assert clip_env["CLIP_WORKER_CRASH_INJECT_MARKER"] == (
        "${CLIP_WORKER_CRASH_INJECT_MARKER:-}"
    )
    assert clip_env["CLIP_WORKER_CRASH_INJECT_EXIT_CODE"] == (
        "${CLIP_WORKER_CRASH_INJECT_EXIT_CODE:-91}"
    )
    assert clip_env["REPLAY_TS_SYNC"] == "${REPLAY_TS_SYNC:-false}"
    assert env_file["REPLAY_FORCE_CONSTANT_CADENCE"] == "true"
    assert env_file["CLIP_WORKER_PLANNER_SHADOW_ENABLED"] == "true"
    assert env_file["CLIP_WORKER_COORDINATOR_V2_ENABLED"] == "true"
    assert env_file["CLIP_WORKER_CRASH_INJECT_POINT"] == ""
    assert env_file["CLIP_WORKER_CRASH_INJECT_MARKER"] == ""
    assert env_file["CLIP_WORKER_CRASH_INJECT_EXIT_CODE"] == "91"
    assert env_file["REPLAY_TS_SYNC"] == "false"


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
    assert clip_env["CLIP_WORKER_MAX_CONCURRENT_JOBS"] == (
        "${CLIP_WORKER_MAX_CONCURRENT_JOBS:-8}"
    )
    assert clip_env["CLIP_WORKER_CONSUMER_COUNT"] == (
        "${CLIP_WORKER_CONSUMER_COUNT:-8}"
    )
    assert clip_env["POST_SAVANT_FRAME_PROOF_ATTEMPTS"] == (
        "${POST_SAVANT_FRAME_PROOF_ATTEMPTS:-1}"
    )
    assert clip_env["POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S"] == (
        "${POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S:-3}"
    )
    assert clip_env["POST_SAVANT_FRAME_PROOF_POLL_INTERVAL_S"] == (
        "${POST_SAVANT_FRAME_PROOF_POLL_INTERVAL_S:-0.5}"
    )
    assert clip_env["POST_SAVANT_FRAME_PROOF_FAST_PATH_BATCH_SIZE"] == (
        "${POST_SAVANT_FRAME_PROOF_FAST_PATH_BATCH_SIZE:-0}"
    )
    assert clip_env["POST_SAVANT_FRAME_PROOF_FAST_PATH_LAG"] == (
        "${POST_SAVANT_FRAME_PROOF_FAST_PATH_LAG:-0}"
    )
    assert clip_env["POST_SAVANT_FRAME_PROOF_FAST_PATH_PENDING"] == (
        "${POST_SAVANT_FRAME_PROOF_FAST_PATH_PENDING:-0}"
    )
    assert clip_env["CLIP_WORKER_FRAME_ANNOTATION_LOOKUP_CONCURRENCY"] == (
        "${CLIP_WORKER_FRAME_ANNOTATION_LOOKUP_CONCURRENCY:-8}"
    )
    assert clip_env["CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_TTL_S"] == (
        "${CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_TTL_S:-0.75}"
    )
    assert clip_env["CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_BUCKET_MS"] == (
        "${CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_BUCKET_MS:-1000}"
    )
    assert clip_env["CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_MAX_ENTRIES"] == (
        "${CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_MAX_ENTRIES:-64}"
    )
    assert clip_env["FRAME_ANNOTATION_ANCHOR_PAGE_COUNT"] == (
        "${FRAME_ANNOTATION_ANCHOR_PAGE_COUNT:-2000}"
    )
    assert clip_env["EVIDENCE_REPLAY_ACTIVE_SLOT_EXTRA_SECONDS"] == (
        "${EVIDENCE_REPLAY_ACTIVE_SLOT_EXTRA_SECONDS:-0}"
    )
    assert env_file["CLIP_WORKER_PENDING_CLAIM_MIN_IDLE_MS"] == "5000"
    assert env_file["CLIP_WORKER_PENDING_CLAIM_COUNT"] == "10"
    assert env_file["CLIP_WORKER_PENDING_CLAIM_INTERVAL_S"] == "5"
    assert env_file["CLIP_WORKER_DEFERRED_RETRY_MAX_ATTEMPTS"] == "12"
    assert env_file["CLIP_WORKER_MAX_CONCURRENT_JOBS"] == "8"
    assert env_file["CLIP_WORKER_CONSUMER_COUNT"] == "8"
    assert env_file["POST_SAVANT_FRAME_PROOF_ATTEMPTS"] == "1"
    assert env_file["POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S"] == "3"
    assert env_file["POST_SAVANT_FRAME_PROOF_POLL_INTERVAL_S"] == "0.5"
    assert env_file["POST_SAVANT_FRAME_PROOF_FAST_PATH_BATCH_SIZE"] == "0"
    assert env_file["POST_SAVANT_FRAME_PROOF_FAST_PATH_LAG"] == "0"
    assert env_file["POST_SAVANT_FRAME_PROOF_FAST_PATH_PENDING"] == "0"
    assert env_file["CLIP_WORKER_FRAME_ANNOTATION_LOOKUP_CONCURRENCY"] == "8"
    assert env_file["CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_TTL_S"] == "0.75"
    assert env_file["CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_BUCKET_MS"] == "1000"
    assert env_file["CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_MAX_ENTRIES"] == "64"
    assert env_file["FRAME_ANNOTATION_ANCHOR_LOOKBACK_COUNT"] == "20000"
    assert env_file["FRAME_ANNOTATION_ANCHOR_PAGE_COUNT"] == "2000"


def test_midterm_media_worker_materialization_defaults_are_bounded() -> None:
    compose = _compose()
    env_file = _env()
    media_env = compose["services"]["media-worker"]["environment"]

    assert env_file["MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE"] == "4"
    assert env_file["MEDIA_WORKER_MATERIALIZATION_TIMEOUT_S"] == "180"
    assert env_file["MEDIA_WORKER_MATERIALIZATION_MAX_BACKLOG"] == "200"
    assert env_file["MEDIA_WORKER_MATERIALIZATION_MAX_PER_POLL"] == "0"
    assert env_file["MEDIA_WORKER_FINALIZER_WORKERS"] == "32"
    assert env_file["MEDIA_WORKER_FINALIZER_PROCESS_WORKERS"] == "0"
    assert env_file["MEDIA_WORKER_FINALIZER_MAX_PER_SOURCE_PER_POLL"] == "4"
    assert env_file["MEDIA_WORKER_FINALIZER_SOURCE_SERIAL"] == "false"
    assert env_file["MEDIA_WORKER_SINGLE_FINALIZER_V2_ENABLED"] == "true"
    assert env_file["MEDIA_WORKER_MATERIALIZATION_THROTTLE_SLEEP_S"] == "0"
    assert env_file["MEDIA_WORKER_MATERIALIZATION_THROTTLE_DEADLINE_GUARD_S"] == "90"
    assert env_file["MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT"] == "4"
    assert env_file["MEDIA_WORKER_FFMPEG_X264_PRESET"] == "ultrafast"
    assert media_env["MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE"] == (
        "${MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE:-4}"
    )
    assert media_env["MEDIA_WORKER_MATERIALIZATION_TIMEOUT_S"] == (
        "${MEDIA_WORKER_MATERIALIZATION_TIMEOUT_S:-180}"
    )
    assert media_env["MEDIA_WORKER_MATERIALIZATION_MAX_BACKLOG"] == (
        "${MEDIA_WORKER_MATERIALIZATION_MAX_BACKLOG:-200}"
    )
    assert media_env["MEDIA_WORKER_MATERIALIZATION_MAX_PER_POLL"] == (
        "${MEDIA_WORKER_MATERIALIZATION_MAX_PER_POLL:-0}"
    )
    assert media_env["MEDIA_WORKER_FINALIZER_WORKERS"] == (
        "${MEDIA_WORKER_FINALIZER_WORKERS:-16}"
    )
    assert media_env["MEDIA_WORKER_FINALIZER_MAX_PER_SOURCE_PER_POLL"] == (
        "${MEDIA_WORKER_FINALIZER_MAX_PER_SOURCE_PER_POLL:-4}"
    )
    assert media_env["MEDIA_WORKER_FINALIZER_SOURCE_SERIAL"] == (
        "${MEDIA_WORKER_FINALIZER_SOURCE_SERIAL:-false}"
    )
    assert media_env["MEDIA_WORKER_SINGLE_FINALIZER_V2_ENABLED"] == (
        "${MEDIA_WORKER_SINGLE_FINALIZER_V2_ENABLED:-true}"
    )
    assert media_env["MEDIA_WORKER_MATERIALIZATION_THROTTLE_SLEEP_S"] == (
        "${MEDIA_WORKER_MATERIALIZATION_THROTTLE_SLEEP_S:-0}"
    )
    assert media_env["MEDIA_WORKER_MATERIALIZATION_THROTTLE_DEADLINE_GUARD_S"] == (
        "${MEDIA_WORKER_MATERIALIZATION_THROTTLE_DEADLINE_GUARD_S:-90}"
    )
    assert media_env["MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT"] == (
        "${MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT:-4}"
    )
    assert media_env["MEDIA_WORKER_FFMPEG_X264_PRESET"] == (
        "${MEDIA_WORKER_FFMPEG_X264_PRESET:-ultrafast}"
    )


def test_midterm_rolling_cache_controls_are_disabled_and_wired_by_default() -> None:
    compose = _compose()
    env_file = _env()
    services = compose["services"]
    event_env = compose["services"]["event-worker"]["environment"]
    media_env = compose["services"]["media-worker"]["environment"]

    assert env_file["ROLLING_CACHE_ENABLED"] == "false"
    assert env_file["ROLLING_CACHE_MATERIALIZATION_ENABLED"] == "false"
    assert env_file["ROLLING_CACHE_RETENTION_SECONDS"] == "300"
    assert env_file["ROLLING_CACHE_SEGMENT_SECONDS"] == "4"
    assert env_file["ROLLING_CACHE_FPS"] == "24"
    assert env_file["ROLLING_CACHE_SEGMENT_FRAMES"] == ""
    assert env_file["ROLLING_CACHE_FALLBACK_TO_REPLAY"] == "true"
    assert env_file["ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS"] == "false"
    assert env_file["EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED"] == "false"
    assert env_file["EVIDENCE_EVENT_COVERAGE_WINDOW_SECONDS"] == "60"
    assert env_file["EVIDENCE_COVERAGE_PARENT_MAX_DURATION_SECONDS"] == "60"

    assert event_env["ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS"] == (
        "${ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS:-false}"
    )
    assert event_env["EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED"] == (
        "${EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED:-false}"
    )
    assert event_env["EVIDENCE_EVENT_COVERAGE_WINDOW_SECONDS"] == (
        "${EVIDENCE_EVENT_COVERAGE_WINDOW_SECONDS:-30}"
    )
    assert event_env["EVIDENCE_COVERAGE_PARENT_MAX_DURATION_SECONDS"] == (
        "${EVIDENCE_COVERAGE_PARENT_MAX_DURATION_SECONDS:-60}"
    )
    assert media_env["ROLLING_CACHE_ENABLED"] == "${ROLLING_CACHE_ENABLED:-false}"
    assert media_env["ROLLING_CACHE_MATERIALIZATION_ENABLED"] == (
        "${ROLLING_CACHE_MATERIALIZATION_ENABLED:-false}"
    )
    assert media_env["ROLLING_CACHE_MATERIALIZATION_MAX_PER_POLL"] == (
        "${ROLLING_CACHE_MATERIALIZATION_MAX_PER_POLL:-16}"
    )
    assert services["rolling-cache-sink"]["environment"]["ZMQ_ENDPOINT"] == (
        "sub+connect:tcp://replay-raw-fanout:5560"
    )
    assert services["rolling-cache-sink-a"]["environment"]["ZMQ_ENDPOINT"] == (
        "sub+connect:tcp://replay-raw-fanout-a:5560"
    )
    assert services["rolling-cache-sink-b"]["environment"]["ZMQ_ENDPOINT"] == (
        "sub+connect:tcp://replay-raw-fanout-b:5560"
    )
    entrypoint = _text(ROOT / "scripts" / "runtime" / "rolling_cache_sink_entrypoint.sh")
    sink_source = _text(
        ROOT / "services" / "rolling-cache-sink" / "app" / "gst_sink.py"
    )
    assert "ROLLING_CACHE_SEGMENT_FRAMES" not in entrypoint
    assert "CHUNK_SIZE" not in entrypoint
    assert "video_files.py" not in entrypoint
    assert "/opt/rolling-cache-sink/app/main.py" in entrypoint
    assert "splitmuxsink name=segmenter" in sink_source
    assert "async-finalize=true" in sink_source
    # The dedicated sink preserves upstream access units; injecting codec
    # headers at every IDR can create an extra header buffer and skew the
    # fixed-cadence mux clock.
    assert "h264parse name=parser config-interval=0" in sink_source
    assert "ROLLING_CACHE_RETENTION_SECONDS" in entrypoint
    assert "[rolling-cache-maintenance]" in entrypoint
    assert "/opt/rolling-cache-maintenance.py" in entrypoint
    assert services["rolling-cache-sink"]["environment"][
        "ROLLING_CACHE_MAINTENANCE_OWNER"
    ] == "true"
    assert services["rolling-cache-sink-a"]["environment"][
        "ROLLING_CACHE_MAINTENANCE_OWNER"
    ] == "true"
    assert services["rolling-cache-sink-b"]["environment"][
        "ROLLING_CACHE_MAINTENANCE_OWNER"
    ] == "false"
    for service_name in (
        "rolling-cache-sink",
        "rolling-cache-sink-a",
        "rolling-cache-sink-b",
    ):
        service = services[service_name]
        assert service["environment"]["ROLLING_CACHE_PUBLICATION_WORKERS"] == (
            "${ROLLING_CACHE_PUBLICATION_WORKERS:-1}"
        )
        assert service["environment"][
            "ROLLING_CACHE_PUBLICATION_COMMIT_SLOTS"
        ] == "${ROLLING_CACHE_PUBLICATION_COMMIT_SLOTS:-0}"
        assert service["environment"][
            "ROLLING_CACHE_PUBLICATION_FINAL_PARENT_GROUP_LIMIT"
        ] == "${ROLLING_CACHE_PUBLICATION_FINAL_PARENT_GROUP_LIMIT:-1}"
        assert service["environment"][
            "ROLLING_CACHE_PUBLICATION_FILE_SYNC_MODE"
        ] == "${ROLLING_CACHE_PUBLICATION_FILE_SYNC_MODE:-fsync}"
        assert service["environment"][
            "ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT"
        ] == "${ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT:-split}"
        assert service["image"] == "video-analytics-midterm-rolling-cache-sink:latest"
        assert service["build"]["context"] == ".."
        assert service["build"]["dockerfile"] == (
            "services/rolling-cache-sink/Dockerfile"
        )
        assert service["pids_limit"] == "${ROLLING_CACHE_SINK_PIDS_LIMIT:-2048}"
        assert any(
            str(volume).endswith(
                "rolling_cache_maintenance.py:/opt/rolling-cache-maintenance.py:ro"
            )
            for volume in service["volumes"]
        )


def test_midterm_evidence_version_is_project_named() -> None:
    compose = _compose()
    env_file = _env()
    media_env = compose["services"]["media-worker"]["environment"]

    assert env_file["EVIDENCE_VERSION"] == "midterm"
    assert (
        env_file["ROLLING_CACHE_PUBLICATION_FINAL_PARENT_GROUP_LIMIT"] == "1"
    )
    assert env_file["ROLLING_CACHE_PUBLICATION_FILE_SYNC_MODE"] == "fsync"
    assert env_file["ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT"] == "split"
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
    assert media_env["FRAME_CACHE_SIDECAR_SCAN_HARD_LIMIT"] == (
        "${FRAME_CACHE_SIDECAR_SCAN_HARD_LIMIT:-500}"
    )
    assert media_env["FRAME_CACHE_SIDECAR_STREAM_SESSION_FILTER_MODE"] == (
        "${FRAME_CACHE_SIDECAR_STREAM_SESSION_FILTER_MODE:-event_window}"
    )
    assert env_file["MEDIA_WORKER_STATE_PATH"] == (
        "/media/replay-sink-output/midterm/.media-worker.processed.json"
    )
    assert env_file["MEDIA_SINK_SCAN_MAX_METADATA_FILES"] == "20000"
    assert env_file["MEDIA_PROBE_TIMEOUT_S"] == "30"
    assert env_file["MEDIA_DECODE_TIMEOUT_S"] == "120"
    assert env_file["MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE"] == "4"
    assert env_file["MEDIA_WORKER_MATERIALIZATION_TIMEOUT_S"] == "180"
    assert env_file["MEDIA_WORKER_MATERIALIZATION_MAX_BACKLOG"] == "200"
    assert env_file["MEDIA_WORKER_MATERIALIZATION_MAX_PER_POLL"] == "0"
    assert env_file["MEDIA_WORKER_FINALIZER_WORKERS"] == "32"
    assert env_file["MEDIA_WORKER_FINALIZER_MAX_PER_SOURCE_PER_POLL"] == "4"
    assert env_file["MEDIA_WORKER_FINALIZER_SOURCE_SERIAL"] == "false"
    assert env_file["MEDIA_WORKER_MATERIALIZATION_THROTTLE_SLEEP_S"] == "0"
    assert env_file["MEDIA_WORKER_MATERIALIZATION_THROTTLE_DEADLINE_GUARD_S"] == "90"
    assert env_file["MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT"] == "4"
    assert env_file["MEDIA_WORKER_FFMPEG_X264_PRESET"] == "ultrafast"
    assert env_file["EVIDENCE_MATERIALIZATION_POLICY"] == "priority"
    assert env_file["EVIDENCE_MATERIALIZATION_DEFER_LOW_PRIORITY"] == "false"
    assert env_file["EVIDENCE_REPLAY_TTL_SECONDS"] == "300"
    assert env_file["EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS"] == "600"
    assert env_file["EVIDENCE_UNKNOWN_SOURCE_FAIL_CLOSED"] == "true"
    assert env_file["EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY"] == "36"
    assert env_file["EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD"] == "9"
    assert env_file["EVIDENCE_REPLAY_ACTIVE_SLOT_EXTRA_SECONDS"] == "0"
    assert env_file["MEDIA_POLL_INTERVAL_S"] == "2"
    assert env_file["MIDTERM_SINK_STABILITY_CHECKS"] == "2"
    assert env_file["EVIDENCE_FINAL_ROOT_MAX_BYTES"] == "0"
    assert env_file["REPLAY_SINK_OUTPUT_MAX_BYTES"] == "0"
    assert env_file["RAW_CLIP_SANITIZE_MODE"] == "auto"
    assert env_file["POST_SAVANT_FAST_RAW_CLIP_ENABLED"] == "true"
    assert env_file["MEDIA_WORKER_CLEANUP_REPLAY_SINK_OUTPUT_STATUSES"] == (
        "ready,generated,generated_unverified,generated_annotation_failed,"
        "duration_guard_failed,generated_corrupt,failed"
    )
    assert env_file["FRAME_CACHE_SIDECAR_RANGE_COUNT"] == "2000"
    assert env_file["FRAME_CACHE_SIDECAR_STREAM_SESSION_FILTER_MODE"] == "event_window"


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
