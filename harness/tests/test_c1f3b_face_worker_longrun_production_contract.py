"""C1F.3b production-like face-worker long-run smoke contract tests."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = (
    REPO_ROOT
    / "scripts"
    / "smoke"
    / "check_c1f3b_face_worker_longrun_gallery_recognition.sh"
)
COMPOSE_FILE = REPO_ROOT / "infra" / "docker-compose.c1-official-replay-dev.yml"
DOC = REPO_ROOT / "docs" / "c1f3b_face_worker_longrun_gallery_recognition.md"
WORKER = REPO_ROOT / "services" / "face-worker" / "app" / "worker.py"
CONFIG = REPO_ROOT / "services" / "face-worker" / "app" / "config.py"


def _script() -> str:
    return SMOKE_SCRIPT.read_text(encoding="utf-8")


def _script_lower() -> str:
    return _script().lower()


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _face_worker_service() -> dict:
    return _compose()["services"]["face-worker"]


def test_longrun_smoke_script_exists() -> None:
    assert SMOKE_SCRIPT.exists()


def test_default_run_duration_at_least_600() -> None:
    content = _script()
    assert 'RUN_DURATION_SEC="${RUN_DURATION_SEC:-600}"' in content


def test_run_duration_allows_env_override() -> None:
    content = _script()
    assert "RUN_DURATION_SEC:-" in content
    assert "RUN_DURATION_SEC=1200" not in content
    assert "RUN_DURATION_SEC=1800" not in content


def test_script_starts_or_checks_face_worker_service() -> None:
    content = _script()
    assert "face-worker" in content
    assert "c1-official-face-worker" in content
    assert "wait_for_face_worker" in content
    assert "up -d --build --force-recreate face-worker" in content


def test_script_does_not_use_compatibility_path() -> None:
    assert "compatibility_repository_path" not in _script()


def test_script_does_not_directly_write_face_observations() -> None:
    content = _script()
    assert "FaceObservationRepository" not in content
    assert "insert_observation" not in content
    assert "INSERT INTO face_observations" not in content
    assert "app.worker" not in content


def test_script_checks_redis_face_observation_stream() -> None:
    content = _script()
    assert 'REDIS_STREAM="security.face_observations"' in content
    assert "xinfo_stream" in content
    assert "xrange" in content


def test_script_checks_face_worker_consumption_and_db_ingest() -> None:
    content = _script()
    assert "face_worker_consumed_messages" in content
    assert "new_observations_inserted" in content
    assert "FAIL_FACE_WORKER_INGEST" in content


def test_script_checks_postgresql_face_observations_added() -> None:
    content = _script()
    assert "face_observations_before" in content
    assert "face_observations_after" in content
    assert "new_observations_inserted" in content


def test_script_checks_source_observation_id_idempotency() -> None:
    content = _script()
    assert "COUNT(DISTINCT source_observation_id)" in content
    assert "idempotency_verified" in content


def test_script_checks_person_gallery_embeddings() -> None:
    content = _script()
    assert "person_gallery_embeddings" in content
    assert "gallery_embeddings_total" in content


def test_script_executes_gallery_search() -> None:
    content = _script()
    assert "FaceVectorStore" in content
    assert "search_gallery" in content
    assert "MatchResultRepository" in content


def test_script_distinguishes_match_and_no_match_pass() -> None:
    content = _script()
    assert "PASS_MATCH" in content
    assert "PASS_NO_MATCH_PRODUCTION_PIPELINE_OK" in content


def test_compose_contains_face_worker_service() -> None:
    services = _compose()["services"]
    assert "face-worker" in services
    assert _face_worker_service()["container_name"] == "c1-official-face-worker"


def test_face_worker_service_does_not_request_gpu() -> None:
    service = _face_worker_service()
    assert "deploy" not in service
    assert "devices" not in service
    assert "NVIDIA_VISIBLE_DEVICES" not in str(service)


def test_face_worker_service_uses_redis_and_postgres_env() -> None:
    env = _face_worker_service()["environment"]
    assert env["DATABASE_URL"] == "postgresql://video:video@postgres:5432/video_analytics"
    assert env["REDIS_URL"] == "redis://redis:6379/0"
    assert env["FACE_OBSERVATION_STREAM"] == "security.face_observations"
    assert env["FACE_WORKER_CONSUMER_GROUP"] == "face-worker"
    assert env["FACE_WORKER_CONSUMER_NAME"] == "c1f3b-smoke"
    assert env["FACE_WORKER_ENABLED"] == "true"


def test_worker_config_supports_face_worker_env_aliases() -> None:
    content = CONFIG.read_text(encoding="utf-8")
    assert "FACE_WORKER_CONSUMER_GROUP" in content
    assert "FACE_WORKER_CONSUMER_NAME" in content
    assert "FACE_WORKER_CONSUMER_START_ID" in content


def test_worker_ack_after_db_insert_and_idempotent_insert() -> None:
    worker = WORKER.read_text(encoding="utf-8")
    repo = (
        REPO_ROOT / "services" / "face-worker" / "app" / "repository.py"
    ).read_text(encoding="utf-8")
    assert "repo.insert_observation(obs)" in worker
    assert "consumer.ack(msg_id)" in worker
    assert worker.index("repo.insert_observation(obs)") < worker.index("consumer.ack(msg_id)")
    assert "ON CONFLICT (source_observation_id) DO NOTHING" in repo


def test_script_does_not_use_second_rtsp() -> None:
    content = _script()
    assert content.count("rtsp://10.37.57.112:8554/live/1080movie") == 1
    assert "SECOND_RTSP" not in content
    assert "EVIDENCE_SECOND_RTSP_PULL" not in content


def test_script_does_not_use_source_extraction_or_ffmpeg_clipping() -> None:
    content = _script_lower()
    assert "ffmpeg" not in content
    assert "source extraction: no" in content
    assert "source_extraction_fallback" not in content


def test_script_does_not_write_image_crop_base64_bytes() -> None:
    content = _script_lower()
    assert "base64.b64encode" not in content
    assert "xadd" not in content
    assert "image_bytes" in content
    assert "crop_bytes" in content
    assert "raw_bytes" in content
    assert "data:image/" in content


def test_doc_declares_production_like_not_compatibility_smoke() -> None:
    assert DOC.exists()
    content = DOC.read_text(encoding="utf-8")
    assert "C1F.3b" in content
    assert "production-like" in content.lower()
    assert "compatibility" in content
    assert "not" in content.lower()
    assert "face-worker service" in content
