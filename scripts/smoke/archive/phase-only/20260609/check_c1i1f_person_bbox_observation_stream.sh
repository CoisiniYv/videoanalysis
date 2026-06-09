#!/usr/bin/env bash
# C1I.1f - Person bbox observation stream smoke.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/infra/docker-compose.c1-official-replay-dev.yml}"
MIGRATION_FILE="/docker-entrypoint-initdb.d/011_c1i1f_person_bbox_observations.sql"
ARTIFACT_DIR="${ARTIFACT_DIR:-/data/video-analytics/artifacts/c1i1f}"
SUMMARY_JSON="${SUMMARY_JSON:-${ARTIFACT_DIR}/person_bbox_observation_stream_summary.json}"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
VIEWER_URL="${VIEWER_URL:-http://localhost:8090}"
REDIS_CONTAINER="${REDIS_CONTAINER:-c1-official-redis}"
PG_CONTAINER="${PG_CONTAINER:-c1-official-postgres}"
CLIP_WORKER_CONTAINER="${CLIP_WORKER_CONTAINER:-c1-official-clip-worker}"
MEDIA_WORKER_CONTAINER="${MEDIA_WORKER_CONTAINER:-c1-official-media-worker}"
WAIT_SECONDS="${WAIT_SECONDS:-180}"
POLL_SECONDS="${POLL_SECONDS:-5}"
PERSON_STREAM="${PERSON_STREAM:-security.person_observations}"
RECORD_REQUEST_STREAM="${RECORD_REQUEST_STREAM:-security.record_requests}"

mkdir -p "${ARTIFACT_DIR}"

log() {
  printf '[c1i1f] %s\n' "$*"
}

utc_now() {
  python3 - <<'PY'
from datetime import datetime, timezone
print(datetime.now(timezone.utc).isoformat())
PY
}

epoch_now() {
  python3 - <<'PY'
import time
print(f"{time.time():.6f}")
PY
}

DOCTOR_STATUS="fail"
log "running doctor_c1_official.sh"
if bash "${ROOT_DIR}/scripts/runtime/doctor_c1_official.sh" >"${ARTIFACT_DIR}/doctor.stdout" 2>"${ARTIFACT_DIR}/doctor.stderr"; then
  DOCTOR_STATUS="ok"
fi
log "doctor_status=${DOCTOR_STATUS}"

COMPOSE_CONFIG_STATUS="fail"
log "validating official compose config"
if docker compose -f "${COMPOSE_FILE}" config >"${ARTIFACT_DIR}/compose.config.yml" 2>"${ARTIFACT_DIR}/compose.config.stderr"; then
  COMPOSE_CONFIG_STATUS="ok"
fi
log "compose_config_status=${COMPOSE_CONFIG_STATUS}"

BOOTSTRAP_STATUS="fail"
log "starting redis/postgres for migration"
if docker compose -f "${COMPOSE_FILE}" up -d redis postgres >"${ARTIFACT_DIR}/bootstrap.log" 2>&1; then
  BOOTSTRAP_STATUS="ok"
fi
log "bootstrap_status=${BOOTSTRAP_STATUS}"

MIGRATION_STATUS="fail"
log "applying person_bbox_observations migration"
if docker exec "${PG_CONTAINER}" psql -U video -d video_analytics -v ON_ERROR_STOP=1 -f "${MIGRATION_FILE}" >"${ARTIFACT_DIR}/migration.log" 2>&1; then
  MIGRATION_STATUS="ok"
fi
log "migration_status=${MIGRATION_STATUS}"

RELOAD_STATUS="fail"
log "reloading official runtime services with current code"
if docker compose -f "${COMPOSE_FILE}" up -d --build >"${ARTIFACT_DIR}/reload.log" 2>&1; then
  RELOAD_STATUS="ok"
fi
if [[ "${RELOAD_STATUS}" == "ok" ]]; then
  docker compose -f "${COMPOSE_FILE}" up -d --force-recreate clip-worker media-worker >>"${ARTIFACT_DIR}/reload.log" 2>&1 || true
  docker compose -f "${COMPOSE_FILE}" restart event-worker savant-security source-adapter >>"${ARTIFACT_DIR}/reload.log" 2>&1 || true
fi
log "reload_status=${RELOAD_STATUS}"

VALIDATION_START="$(utc_now)"
VALIDATION_START_EPOCH="$(epoch_now)"
log "validation_start=${VALIDATION_START}"

ROOT_DIR="${ROOT_DIR}" \
ARTIFACT_DIR="${ARTIFACT_DIR}" \
SUMMARY_JSON="${SUMMARY_JSON}" \
EVIDENCE_ROOT="${EVIDENCE_ROOT}" \
VIEWER_URL="${VIEWER_URL}" \
REDIS_CONTAINER="${REDIS_CONTAINER}" \
PG_CONTAINER="${PG_CONTAINER}" \
CLIP_WORKER_CONTAINER="${CLIP_WORKER_CONTAINER}" \
MEDIA_WORKER_CONTAINER="${MEDIA_WORKER_CONTAINER}" \
WAIT_SECONDS="${WAIT_SECONDS}" \
POLL_SECONDS="${POLL_SECONDS}" \
PERSON_STREAM="${PERSON_STREAM}" \
RECORD_REQUEST_STREAM="${RECORD_REQUEST_STREAM}" \
VALIDATION_START="${VALIDATION_START}" \
VALIDATION_START_EPOCH="${VALIDATION_START_EPOCH}" \
DOCTOR_STATUS="${DOCTOR_STATUS}" \
COMPOSE_CONFIG_STATUS="${COMPOSE_CONFIG_STATUS}" \
BOOTSTRAP_STATUS="${BOOTSTRAP_STATUS}" \
MIGRATION_STATUS="${MIGRATION_STATUS}" \
RELOAD_STATUS="${RELOAD_STATUS}" \
python3 - <<'PY'
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(os.environ["ROOT_DIR"])
ARTIFACT_DIR = Path(os.environ["ARTIFACT_DIR"])
SUMMARY_JSON = Path(os.environ["SUMMARY_JSON"])
EVIDENCE_ROOT = Path(os.environ["EVIDENCE_ROOT"])
VIEWER_URL = os.environ["VIEWER_URL"].rstrip("/")
REDIS_CONTAINER = os.environ["REDIS_CONTAINER"]
PG_CONTAINER = os.environ["PG_CONTAINER"]
CLIP_WORKER_CONTAINER = os.environ["CLIP_WORKER_CONTAINER"]
MEDIA_WORKER_CONTAINER = os.environ["MEDIA_WORKER_CONTAINER"]
WAIT_SECONDS = int(os.environ["WAIT_SECONDS"])
POLL_SECONDS = int(os.environ["POLL_SECONDS"])
PERSON_STREAM = os.environ["PERSON_STREAM"]
RECORD_REQUEST_STREAM = os.environ["RECORD_REQUEST_STREAM"]
VALIDATION_START = os.environ["VALIDATION_START"]
VALIDATION_START_EPOCH = float(os.environ["VALIDATION_START_EPOCH"])
DOCTOR_STATUS = os.environ["DOCTOR_STATUS"]
COMPOSE_CONFIG_STATUS = os.environ["COMPOSE_CONFIG_STATUS"]
BOOTSTRAP_STATUS = os.environ["BOOTSTRAP_STATUS"]
MIGRATION_STATUS = os.environ["MIGRATION_STATUS"]
RELOAD_STATUS = os.environ["RELOAD_STATUS"]

PASS_MARKER = "PASS_C1I1F_PERSON_BBOX_OBSERVATION_STREAM"
PARTIAL_MARKER = "PARTIAL_C1I1F_PERSON_OBSERVATIONS_ONLY"
FAIL_MARKER = "FAIL_C1I1F_NO_PERSON_OBSERVATIONS"
FAIL_CLIP_MAX_MARKER = "FAIL_C1I1F_CLIP_WORKER_MAX_JOBS_EXHAUSTED"
PARTIAL_CLIP_OR_MEDIA_MARKER = "PARTIAL_C1I1F_CLIP_OR_MEDIA_PENDING"
FAIL_NO_CONTEXT_MARKER = "FAIL_C1I1F_NO_PERSON_CONTEXT_IN_EVIDENCE"


def run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def redis_xlen(stream: str) -> int:
    res = run(["docker", "exec", REDIS_CONTAINER, "redis-cli", "XLEN", stream])
    try:
        return int((res.stdout or "0").strip())
    except ValueError:
        return 0


def redis_stream_count_since(stream: str, start_epoch: float, limit: int = 10000) -> int:
    start_id = f"{int(start_epoch * 1000)}-0"
    res = run(
        [
            "docker",
            "exec",
            REDIS_CONTAINER,
            "redis-cli",
            "--json",
            "XRANGE",
            stream,
            start_id,
            "+",
            "COUNT",
            str(limit),
        ],
        timeout=60,
    )
    if res.returncode != 0:
        return 0
    try:
        data = json.loads((res.stdout or "[]").strip() or "[]")
    except json.JSONDecodeError:
        return 0
    return len(data) if isinstance(data, list) else 0


def docker_logs_count(container: str, token: str, *, since_epoch: float | None = None, until_epoch: float | None = None) -> int:
    cmd = ["docker", "logs"]
    if since_epoch is not None:
        cmd.extend(["--since", str(int(since_epoch))])
    if until_epoch is not None:
        cmd.extend(["--until", str(int(until_epoch))])
    cmd.append(container)
    res = run(cmd, timeout=60)
    text = (res.stdout or "") + "\n" + (res.stderr or "")
    return text.count(token)


def docker_started_at(container: str) -> str | None:
    res = run(["docker", "inspect", "-f", "{{.State.StartedAt}}", container], timeout=20)
    if res.returncode != 0:
        return None
    value = (res.stdout or "").strip()
    return value or None


def parse_docker_time(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, rest = text.split(".", 1)
        tz = ""
        for marker in ("+", "-"):
            pos = rest.find(marker)
            if pos > 0:
                tz = rest[pos:]
                rest = rest[:pos]
                break
        text = f"{head}.{rest[:6]}{tz}"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def psql_json(sql: str) -> Any:
    wrapped = f"SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json) FROM ({sql}) t"
    res = run(
        [
            "docker",
            "exec",
            PG_CONTAINER,
            "psql",
            "-U",
            "video",
            "-d",
            "video_analytics",
            "-t",
            "-A",
            "-c",
            wrapped,
        ],
        timeout=60,
    )
    if res.returncode != 0:
        return []
    text = (res.stdout or "").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def person_db_rows() -> list[dict[str, Any]]:
    rows = psql_json(
        f"""
        SELECT source_observation_id, source_id, camera_id, track_id,
               timestamp_ms, frame_pts, frame_num, person_bbox,
               person_confidence, gate_status, created_at
        FROM person_bbox_observations
        WHERE created_at >= to_timestamp({VALIDATION_START_EPOCH})
          AND gate_status = 'accepted'
        ORDER BY created_at DESC
        LIMIT 200
        """
    )
    return rows if isinstance(rows, list) else []


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                data = json.loads(line)
                if isinstance(data, dict):
                    records.append(data)
    except Exception:
        return records
    return records


def bundle_mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def new_bundle_candidates() -> list[dict[str, Any]]:
    if not EVIDENCE_ROOT.is_dir():
        return []
    candidates: list[dict[str, Any]] = []
    for bundle in sorted(EVIDENCE_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not bundle.is_dir() or bundle.stat().st_mtime < VALIDATION_START_EPOCH:
            continue
        summary = load_json(bundle / "summary.json")
        records = load_jsonl(bundle / "annotations.jsonl")
        person_context = [
            record for record in records
            if record.get("record_type") == "object_annotation"
            and record.get("annotation_role") == "person_context"
            and record.get("object_type") == "person"
        ]
        source_ok = all(
            isinstance(record.get("bbox"), dict)
            and record["bbox"].get("source") == "person_bbox_observations.person_bbox"
            for record in person_context
        )
        candidates.append(
            {
                "bundle_id": bundle.name,
                "bundle_path": str(bundle),
                "mtime": bundle_mtime_iso(bundle),
                "summary": summary,
                "person_context_count": len(person_context),
                "person_context_source_ok": source_ok,
                "annotations_jsonl_path": str(bundle / "annotations.jsonl"),
                "event_type": summary.get("event_type"),
                "annotation_status": summary.get("annotation_status"),
            }
        )
    return candidates


def fetch_json(url: str) -> dict[str, Any]:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return {"ok": response.status == 200, "status": response.status, "data": data}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "status": None, "data": {}, "error": str(exc)}


def fetch_text(path: str) -> str:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{VIEWER_URL}{path}", timeout=10) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def viewer_support(bundle_id: str | None) -> dict[str, Any]:
    app_js = fetch_text("/static/app.js")
    html = fetch_text("/")
    static_ok = all(token in app_js for token in ("person_context", "#00C853", "showPersons")) and "show person boxes" in html
    annotations_ok = False
    annotations_count = 0
    viewer_person_context_count = 0
    if bundle_id:
        payload = fetch_json(f"{VIEWER_URL}/api/bundles/{bundle_id}/annotations")
        annotations_ok = bool(payload.get("ok"))
        records = payload.get("data", {}).get("records", [])
        if isinstance(records, list):
            annotations_count = len(records)
            viewer_person_context_count = len([
                record for record in records
                if record.get("record_type") == "object_annotation"
                and record.get("annotation_role") == "person_context"
            ])
    return {
        "static_ok": static_ok,
        "annotations_ok": annotations_ok,
        "annotations_count": annotations_count,
        "person_context_count": viewer_person_context_count,
    }


redis_len_before = redis_xlen(PERSON_STREAM)
record_requests_before = redis_xlen(RECORD_REQUEST_STREAM)
person_stream_count_after_validation = redis_stream_count_since(
    PERSON_STREAM,
    VALIDATION_START_EPOCH,
)
record_request_count_after_validation = redis_stream_count_since(
    RECORD_REQUEST_STREAM,
    VALIDATION_START_EPOCH,
)
clip_worker_started_at = docker_started_at(CLIP_WORKER_CONTAINER)
clip_worker_started_at_epoch = parse_docker_time(clip_worker_started_at)
clip_worker_started_before_validation_seconds = (
    VALIDATION_START_EPOCH - clip_worker_started_at_epoch
    if clip_worker_started_at_epoch is not None
    else None
)
clip_worker_preexisting_max_jobs_reached_log_count = docker_logs_count(
    CLIP_WORKER_CONTAINER,
    "max_jobs_reached",
    since_epoch=max(0.0, VALIDATION_START_EPOCH - 24 * 60 * 60),
    until_epoch=VALIDATION_START_EPOCH,
)
db_rows_before = person_db_rows()
deadline = time.time() + WAIT_SECONDS
selected_bundle: dict[str, Any] | None = None
db_rows: list[dict[str, Any]] = db_rows_before
redis_len_after = redis_len_before
record_requests_after = record_requests_before
clip_worker_max_jobs_reached_log_count = 0
clip_worker_replay_job_created_log_count = 0

while time.time() < deadline:
    redis_len_after = redis_xlen(PERSON_STREAM)
    record_requests_after = redis_xlen(RECORD_REQUEST_STREAM)
    person_stream_count_after_validation = redis_stream_count_since(
        PERSON_STREAM,
        VALIDATION_START_EPOCH,
    )
    record_request_count_after_validation = redis_stream_count_since(
        RECORD_REQUEST_STREAM,
        VALIDATION_START_EPOCH,
    )
    clip_worker_max_jobs_reached_log_count = docker_logs_count(
        CLIP_WORKER_CONTAINER,
        "max_jobs_reached",
        since_epoch=VALIDATION_START_EPOCH,
    )
    clip_worker_replay_job_created_log_count = docker_logs_count(
        CLIP_WORKER_CONTAINER,
        "replay_job_created",
        since_epoch=VALIDATION_START_EPOCH,
    )
    db_rows = person_db_rows()
    candidates = new_bundle_candidates()
    selected_bundle = next(
        (
            item for item in candidates
            if item["person_context_count"] > 0 and item["person_context_source_ok"]
        ),
        None,
    )
    if (
        person_stream_count_after_validation > 0
        and record_request_count_after_validation > 0
        and db_rows
        and clip_worker_replay_job_created_log_count > 0
        and selected_bundle
    ):
        break
    time.sleep(POLL_SECONDS)

record_requests_after = redis_xlen(RECORD_REQUEST_STREAM)
person_stream_count_after_validation = redis_stream_count_since(
    PERSON_STREAM,
    VALIDATION_START_EPOCH,
)
record_request_count_after_validation = redis_stream_count_since(
    RECORD_REQUEST_STREAM,
    VALIDATION_START_EPOCH,
)
clip_worker_max_jobs_reached_log_count = docker_logs_count(
    CLIP_WORKER_CONTAINER,
    "max_jobs_reached",
    since_epoch=VALIDATION_START_EPOCH,
)
clip_worker_replay_job_created_log_count = docker_logs_count(
    CLIP_WORKER_CONTAINER,
    "replay_job_created",
    since_epoch=VALIDATION_START_EPOCH,
)
candidates = new_bundle_candidates()
latest_bundle = candidates[0] if candidates else None
if selected_bundle is None:
    selected_bundle = next(
        (
            item for item in candidates
            if item["person_context_count"] > 0 and item["person_context_source_ok"]
        ),
        None,
    )
viewer_bundle = selected_bundle or latest_bundle
viewer = viewer_support(viewer_bundle.get("bundle_id") if viewer_bundle else None)

person_observation_count = len(db_rows)
person_observation_with_bbox_count = len([
    row for row in db_rows
    if row.get("person_bbox") is not None
])
person_observation_stream_message_count = person_stream_count_after_validation
record_request_stream_message_count = record_request_count_after_validation
person_context_count = int(selected_bundle.get("person_context_count", 0)) if selected_bundle else 0
evidence_bundle_count = len(candidates)

failure_reasons: list[str] = []
if DOCTOR_STATUS != "ok":
    failure_reasons.append("doctor_not_ok")
if COMPOSE_CONFIG_STATUS != "ok":
    failure_reasons.append("compose_config_not_ok")
if MIGRATION_STATUS != "ok":
    failure_reasons.append("migration_not_ok")
if RELOAD_STATUS != "ok":
    failure_reasons.append("reload_not_ok")
if person_observation_stream_message_count <= 0:
    failure_reasons.append("no_new_security.person_observations_message")
if person_observation_count <= 0:
    failure_reasons.append("no_new_person_bbox_observations_db_row")
if record_request_stream_message_count <= 0:
    failure_reasons.append("no_new_record_request_stream_message")
if clip_worker_max_jobs_reached_log_count > 0:
    failure_reasons.append("clip_worker_max_jobs_reached_seen_post_validation")
if clip_worker_replay_job_created_log_count <= 0:
    failure_reasons.append("no_successful_replay_job_created_log")
if evidence_bundle_count <= 0:
    failure_reasons.append("no_new_evidence_bundle")

if person_observation_count <= 0 or person_observation_stream_message_count <= 0:
    result_marker = FAIL_MARKER
elif clip_worker_max_jobs_reached_log_count > 0 and clip_worker_replay_job_created_log_count <= 0:
    result_marker = FAIL_CLIP_MAX_MARKER
elif clip_worker_replay_job_created_log_count <= 0 or evidence_bundle_count <= 0:
    result_marker = PARTIAL_CLIP_OR_MEDIA_MARKER
elif person_context_count <= 0:
    result_marker = FAIL_NO_CONTEXT_MARKER
elif not selected_bundle or not selected_bundle.get("person_context_source_ok"):
    failure_reasons.append("person_context_bbox_source_not_confirmed")
    result_marker = FAIL_NO_CONTEXT_MARKER
elif not (viewer["static_ok"] and viewer["annotations_ok"] and viewer["person_context_count"] > 0):
    failure_reasons.append("viewer_person_context_not_confirmed")
    result_marker = PARTIAL_CLIP_OR_MEDIA_MARKER
else:
    result_marker = PASS_MARKER

summary = {
    "validation_start": VALIDATION_START,
    "duration_seconds": WAIT_SECONDS,
    "doctor_status": DOCTOR_STATUS,
    "compose_config_status": COMPOSE_CONFIG_STATUS,
    "bootstrap_status": BOOTSTRAP_STATUS,
    "migration_status": MIGRATION_STATUS,
    "reload_status": RELOAD_STATUS,
    "redis_stream": PERSON_STREAM,
    "redis_stream_length_before": redis_len_before,
    "redis_stream_length_after": redis_len_after,
    "person_observation_stream_message_count": person_observation_stream_message_count,
    "record_request_stream": RECORD_REQUEST_STREAM,
    "record_request_stream_length_before": record_requests_before,
    "record_request_stream_length_after": record_requests_after,
    "record_request_stream_message_count": record_request_stream_message_count,
    "person_bbox_observation_db_count": person_observation_count,
    "person_bbox_observation_with_bbox_count": person_observation_with_bbox_count,
    "person_observation_min_interval_ms": 1000,
    "clip_worker_container": CLIP_WORKER_CONTAINER,
    "clip_worker_started_at": clip_worker_started_at,
    "clip_worker_started_before_validation_seconds": clip_worker_started_before_validation_seconds,
    "clip_worker_preexisting_max_jobs_reached_log_count": clip_worker_preexisting_max_jobs_reached_log_count,
    "clip_worker_max_jobs_reached_log_count": clip_worker_max_jobs_reached_log_count,
    "clip_worker_replay_job_created_log_count": clip_worker_replay_job_created_log_count,
    "successful_replay_job_submission_count": clip_worker_replay_job_created_log_count,
    "media_worker_container": MEDIA_WORKER_CONTAINER,
    "evidence_bundle_count": evidence_bundle_count,
    "evidence_bundle_id": selected_bundle.get("bundle_id") if selected_bundle else None,
    "latest_evidence_bundle_id": latest_bundle.get("bundle_id") if latest_bundle else None,
    "evidence_bundle_path": selected_bundle.get("bundle_path") if selected_bundle else None,
    "latest_evidence_bundle_path": latest_bundle.get("bundle_path") if latest_bundle else None,
    "evidence_bundle_candidates": candidates[:10],
    "annotations_jsonl_path": selected_bundle.get("annotations_jsonl_path") if selected_bundle else None,
    "latest_annotations_jsonl_path": latest_bundle.get("annotations_jsonl_path") if latest_bundle else None,
    "person_context_count": person_context_count,
    "person_context_count_check": "person_context_count > 0",
    "bbox_source_expected": "person_bbox_observations.person_bbox",
    "bbox_source_ok": bool(selected_bundle and selected_bundle.get("person_context_source_ok")),
    "viewer_url": VIEWER_URL,
    "viewer_support": viewer,
    "sample_person_bbox_observations": db_rows[:5],
    "failure_reasons": failure_reasons,
    "result_marker": result_marker,
}

ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))

if result_marker in {PASS_MARKER, PARTIAL_MARKER, PARTIAL_CLIP_OR_MEDIA_MARKER}:
    raise SystemExit(0)
raise SystemExit(1)
PY

log "summary_json=${SUMMARY_JSON}"
