"""C1F.3c 15-minute face recognition replay monitor contract tests."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = (
    REPO_ROOT
    / "scripts"
    / "smoke"
    / "check_c1f3c_15min_face_recognition_replay_monitor.sh"
)
COMPOSE_FILE = REPO_ROOT / "infra" / "docker-compose.c1-official-replay-dev.yml"
DOC = REPO_ROOT / "docs" / "c1f3c_15min_face_recognition_replay_monitor.md"


def _script() -> str:
    return SMOKE_SCRIPT.read_text(encoding="utf-8")


def _script_lower() -> str:
    return _script().lower()


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _face_worker_service() -> dict:
    return _compose()["services"]["face-worker"]


# --- Task 3.1: Script exists ---
def test_15min_soak_script_exists() -> None:
    assert SMOKE_SCRIPT.exists()


# --- Task 3.2: Default RUN_DURATION_SEC=900 ---
def test_default_run_duration_900() -> None:
    content = _script()
    assert 'RUN_DURATION_SEC="${RUN_DURATION_SEC:-900}"' in content


# --- Task 3.3: Supports env override RUN_DURATION_SEC ---
def test_run_duration_allows_env_override() -> None:
    content = _script()
    assert "RUN_DURATION_SEC:-" in content
    # Must not hardcode a fixed duration
    assert "RUN_DURATION_SEC=900" not in content.split("RUN_DURATION_SEC:-")[0]


# --- Task 3.4: Script checks face-worker service ---
def test_script_checks_face_worker_service() -> None:
    content = _script()
    assert "face-worker" in content
    assert "c1-official-face-worker" in content
    assert "wait_for_face_worker" in content
    assert "up -d --build --force-recreate face-worker" in content


# --- Task 3.5: Script does not use compatibility_repository_path ---
def test_script_does_not_use_compatibility_path() -> None:
    assert "compatibility_repository_path" not in _script()


# --- Task 3.6: Script does not directly write face observations ---
def test_script_does_not_directly_write_face_observations() -> None:
    content = _script()
    assert "FaceObservationRepository" not in content
    assert "insert_observation" not in content
    assert "INSERT INTO face_observations" not in content
    assert "app.worker" not in content


# --- Task 3.7: Script monitors Redis security.face_observations ---
def test_script_monitors_redis_face_observations() -> None:
    content = _script()
    assert 'REDIS_STREAM="security.face_observations"' in content
    assert "xinfo_stream" in content
    assert "xrange" in content


# --- Task 3.8: Script monitors face-worker ingest ---
def test_script_monitors_face_worker_ingest() -> None:
    content = _script()
    assert "face_worker_consumed_messages" in content
    assert "new_observations_inserted" in content
    assert "FAIL_FACE_WORKER_INGEST" in content


# --- Task 3.9: Script monitors PostgreSQL face_observations ---
def test_script_monitors_postgresql_face_observations() -> None:
    content = _script()
    assert "face_observations_before" in content
    assert "face_observations_after" in content
    assert "new_observations_inserted" in content
    assert "get_pg_face_observations_count" in content


# --- Task 3.10: Script monitors Replay directory size ---
def test_script_monitors_replay_directory_size() -> None:
    content = _script()
    assert "replay_dir" in content
    assert "replay_dir_size_bytes" in content
    assert "get_dir_size_bytes" in content
    assert "replay_size_growth" in content


# --- Task 3.11: Script samples every 60 seconds ---
def test_script_samples_every_60_seconds() -> None:
    content = _script()
    assert 'SAMPLE_INTERVAL_SEC="${SAMPLE_INTERVAL_SEC:-60}"' in content
    assert "take_sample" in content


# --- Task 3.12: Script outputs JSONL monitor log ---
def test_script_outputs_jsonl_monitor_log() -> None:
    content = _script()
    assert "MONITOR_JSONL" in content
    assert "15min_face_recognition_monitor.jsonl" in content
    assert "monitor_jsonl" in content


# --- Task 3.13: Script executes Finch/Reese gallery search ---
def test_script_executes_gallery_search() -> None:
    content = _script()
    assert "FaceVectorStore" in content
    assert "search_gallery" in content
    assert "MatchResultRepository" in content
    assert "test:archive:finch" in content
    assert "test:archive:reese" in content


# --- Task 3.14: Script distinguishes PASS_MATCH and PASS_NO_MATCH ---
def test_script_distinguishes_match_and_no_match_pass() -> None:
    content = _script()
    assert "PASS_MATCH" in content
    assert "PASS_NO_MATCH_PRODUCTION_PIPELINE_OK" in content


# --- Task 3.15: Script allows visual output NOT_AVAILABLE ---
def test_script_allows_visual_output_not_available() -> None:
    content = _script()
    assert "VISUAL_OUTPUT_NOT_AVAILABLE" in content
    assert "attempt_visual_output" in content
    assert "visual_output_status" in content


# --- Task 3.16: Script prohibits second RTSP ---
def test_script_does_not_use_second_rtsp() -> None:
    content = _script()
    assert content.count("rtsp://10.37.57.112:8554/live/1080movie") == 1
    assert "SECOND_RTSP" not in content
    assert "EVIDENCE_SECOND_RTSP_PULL" not in content


# --- Task 3.17: Script prohibits source extraction ---
def test_script_does_not_use_source_extraction() -> None:
    content = _script_lower()
    assert "source extraction" in content
    assert "source_extraction_fallback" not in content


# --- Task 3.18: Script prohibits ffmpeg RTSP clipping ---
def test_script_does_not_use_ffmpeg_clipping() -> None:
    content = _script_lower()
    assert "ffmpeg" not in content


# --- Task 3.19: Script prohibits image/crop/base64 bytes in Redis/DB ---
def test_script_does_not_write_image_crop_base64_bytes() -> None:
    content = _script_lower()
    assert "base64.b64encode" not in content
    assert "xadd" not in content
    assert "image_bytes" in content
    assert "crop_bytes" in content
    assert "raw_bytes" in content
    assert "data:image/" in content


# --- Task 3.20: Doc declares visual output is debug, not production evidence ---
def test_doc_declares_visual_output_is_debug_only() -> None:
    assert DOC.exists()
    content = DOC.read_text(encoding="utf-8")
    assert "C1F.3c" in content
    assert "debug" in content.lower()
    assert "production evidence" in content.lower() or "production_evidence" in content
    assert "VISUAL_OUTPUT_NOT_AVAILABLE" in content
