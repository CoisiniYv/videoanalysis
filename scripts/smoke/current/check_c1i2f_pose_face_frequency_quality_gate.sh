#!/usr/bin/env bash
# C1I.2f - pose 3 FPS and face 1 FPS quality gate calibration smoke.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/infra/docker-compose.c1-official-replay-dev.yml}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/data/video-analytics/artifacts/c1i2f}"
SUMMARY_JSON="${SUMMARY_JSON:-${ARTIFACT_DIR}/pose_face_frequency_quality_gate_summary.json}"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
VIEWER_URL="${VIEWER_URL:-http://localhost:8090}"
PG_CONTAINER="${PG_CONTAINER:-c1-official-postgres}"
SAVANT_CONTAINER="${SAVANT_CONTAINER:-c1-official-savant}"
DURATION_SECONDS="${DURATION_SECONDS:-180}"
POLL_SECONDS="${POLL_SECONDS:-15}"

mkdir -p "${ARTIFACT_DIR}"
RUN_ID="$(date +%Y%m%dT%H%M%S)"

log() {
  printf '[c1i2f] %s\n' "$*"
}

utc_now() {
  date -u +"%Y-%m-%dT%H:%M:%S+00:00"
}

epoch_now() {
  date +%s
}

export PERSON_BBOX_OBSERVATION_MIN_INTERVAL_MS="${PERSON_BBOX_OBSERVATION_MIN_INTERVAL_MS:-333}"
export PERSON_OBSERVATION_MIN_INTERVAL_MS="${PERSON_OBSERVATION_MIN_INTERVAL_MS:-333}"
export PERSON_OBSERVATION_BATCH_SIZE="${PERSON_OBSERVATION_BATCH_SIZE:-100}"
export PERSON_OBSERVATION_CONSUMER_GROUP="${PERSON_OBSERVATION_CONSUMER_GROUP:-person-observation-c1i2f-${RUN_ID}}"
export PERSON_OBSERVATION_CONSUMER_NAME="${PERSON_OBSERVATION_CONSUMER_NAME:-event-worker-c1i2f-person-${RUN_ID}}"
export PERSON_OBSERVATION_CONSUMER_START_ID="${PERSON_OBSERVATION_CONSUMER_START_ID:-\$}"
export FACE_REID_MIN_INTERVAL_MS="${FACE_REID_MIN_INTERVAL_MS:-1000}"
export FACE_CONFIDENCE_THRESHOLD="${FACE_CONFIDENCE_THRESHOLD:-0.25}"
export FACE_REID_MIN_CONFIDENCE="${FACE_REID_MIN_CONFIDENCE:-0.45}"
export FACE_REID_MIN_FACE_SIZE="${FACE_REID_MIN_FACE_SIZE:-40}"
export FACE_REID_NORM_TOLERANCE="${FACE_REID_NORM_TOLERANCE:-0.10}"
export DEFAULT_PRE_SECONDS="${DEFAULT_PRE_SECONDS:-5}"
export DEFAULT_POST_SECONDS="${DEFAULT_POST_SECONDS:-5}"
export RECORDING_PRE_SECONDS="${RECORDING_PRE_SECONDS:-5}"
export RECORDING_POST_SECONDS="${RECORDING_POST_SECONDS:-5}"
export RECORDING_MAX_REQUESTS_PER_RUN="${RECORDING_MAX_REQUESTS_PER_RUN:-200}"
export RECORDING_COOLDOWN_SECONDS="${RECORDING_COOLDOWN_SECONDS:-5}"
export CLIP_WORKER_MAX_JOBS_PER_RUN="${CLIP_WORKER_MAX_JOBS_PER_RUN:-200}"
export CLIP_WORKER_RUN_ONCE="${CLIP_WORKER_RUN_ONCE:-false}"

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

RELOAD_STATUS="fail"
log "reloading existing runtime services"
if docker compose -f "${COMPOSE_FILE}" up -d --build \
  savant-security source-adapter event-worker face-worker clip-worker media-worker evidence-viewer \
  >"${ARTIFACT_DIR}/reload.log" 2>&1; then
  RELOAD_STATUS="ok"
fi
docker compose -f "${COMPOSE_FILE}" ps >"${ARTIFACT_DIR}/compose.ps.txt" 2>&1 || true
log "reload_status=${RELOAD_STATUS}"

VALIDATION_START="$(utc_now)"
VALIDATION_START_EPOCH="$(epoch_now)"
log "validation_start=${VALIDATION_START}"
log "duration_seconds=${DURATION_SECONDS}"

ROOT_DIR="${ROOT_DIR}" \
ARTIFACT_DIR="${ARTIFACT_DIR}" \
SUMMARY_JSON="${SUMMARY_JSON}" \
EVIDENCE_ROOT="${EVIDENCE_ROOT}" \
VIEWER_URL="${VIEWER_URL}" \
PG_CONTAINER="${PG_CONTAINER}" \
SAVANT_CONTAINER="${SAVANT_CONTAINER}" \
DURATION_SECONDS="${DURATION_SECONDS}" \
POLL_SECONDS="${POLL_SECONDS}" \
VALIDATION_START="${VALIDATION_START}" \
VALIDATION_START_EPOCH="${VALIDATION_START_EPOCH}" \
DOCTOR_STATUS="${DOCTOR_STATUS}" \
COMPOSE_CONFIG_STATUS="${COMPOSE_CONFIG_STATUS}" \
RELOAD_STATUS="${RELOAD_STATUS}" \
PERSON_BBOX_OBSERVATION_MIN_INTERVAL_MS="${PERSON_BBOX_OBSERVATION_MIN_INTERVAL_MS}" \
FACE_REID_MIN_INTERVAL_MS="${FACE_REID_MIN_INTERVAL_MS}" \
FACE_CONFIDENCE_THRESHOLD="${FACE_CONFIDENCE_THRESHOLD}" \
FACE_REID_MIN_CONFIDENCE="${FACE_REID_MIN_CONFIDENCE}" \
FACE_REID_MIN_FACE_SIZE="${FACE_REID_MIN_FACE_SIZE}" \
FACE_REID_NORM_TOLERANCE="${FACE_REID_NORM_TOLERANCE}" \
PERSON_OBSERVATION_BATCH_SIZE="${PERSON_OBSERVATION_BATCH_SIZE}" \
PERSON_OBSERVATION_CONSUMER_GROUP="${PERSON_OBSERVATION_CONSUMER_GROUP}" \
python3 - <<'PY'
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(os.environ["ROOT_DIR"])
ARTIFACT_DIR = Path(os.environ["ARTIFACT_DIR"])
SUMMARY_JSON = Path(os.environ["SUMMARY_JSON"])
EVIDENCE_ROOT = Path(os.environ["EVIDENCE_ROOT"])
VIEWER_URL = os.environ["VIEWER_URL"].rstrip("/")
PG_CONTAINER = os.environ["PG_CONTAINER"]
SAVANT_CONTAINER = os.environ["SAVANT_CONTAINER"]
DURATION_SECONDS = int(os.environ["DURATION_SECONDS"])
POLL_SECONDS = max(int(os.environ["POLL_SECONDS"]), 1)
VALIDATION_START = os.environ["VALIDATION_START"]
VALIDATION_START_EPOCH = float(os.environ["VALIDATION_START_EPOCH"])
DOCTOR_STATUS = os.environ["DOCTOR_STATUS"]
COMPOSE_CONFIG_STATUS = os.environ["COMPOSE_CONFIG_STATUS"]
RELOAD_STATUS = os.environ["RELOAD_STATUS"]

PERSON_INTERVAL_MS = int(float(os.environ["PERSON_BBOX_OBSERVATION_MIN_INTERVAL_MS"]))
FACE_INTERVAL_MS = int(float(os.environ["FACE_REID_MIN_INTERVAL_MS"]))
FACE_DETECTOR_THRESHOLD = float(os.environ["FACE_CONFIDENCE_THRESHOLD"])
FACE_REID_MIN_CONFIDENCE = float(os.environ["FACE_REID_MIN_CONFIDENCE"])
FACE_REID_MIN_FACE_SIZE = float(os.environ["FACE_REID_MIN_FACE_SIZE"])
FACE_REID_NORM_TOLERANCE = float(os.environ["FACE_REID_NORM_TOLERANCE"])
PERSON_OBSERVATION_BATCH_SIZE = int(float(os.environ["PERSON_OBSERVATION_BATCH_SIZE"]))
PERSON_OBSERVATION_CONSUMER_GROUP = os.environ["PERSON_OBSERVATION_CONSUMER_GROUP"]

PASS_MARKER = "PASS_C1I2F_POSE_FACE_FREQUENCY_QUALITY_GATE"
PARTIAL_PERSON_RATE = "PARTIAL_C1I2F_PERSON_RATE_LIMITED_BY_INPUT"
FAIL_FACE_TOO_FREQUENT = "FAIL_C1I2F_FACE_EMBEDDING_TOO_FREQUENT"
FAIL_FACE_QUALITY = "FAIL_C1I2F_FACE_QUALITY_GATE"
FAIL_RUNTIME = "FAIL_C1I2F_RUNTIME"


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def psql_json(sql: str) -> list[dict[str, Any]]:
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
        timeout=90,
    )
    if res.returncode != 0:
        return []
    try:
        data = json.loads((res.stdout or "[]").strip() or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def fetch_text(url: str) -> dict[str, Any]:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=15) as response:
            return {
                "ok": response.status == 200,
                "status": response.status,
                "text": response.read().decode("utf-8", errors="replace"),
            }
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "status": None, "text": "", "error": str(exc)}


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def nested_get(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def find_raw_clip(bundle_dir: Path, metadata: dict[str, Any], summary: dict[str, Any]) -> Path | None:
    candidates = [
        nested_get(metadata, "media", "raw_clip_path"),
        nested_get(metadata, "payload", "media", "raw_clip_path"),
        summary.get("raw_clip_path"),
        summary.get("raw_clip_name"),
    ]
    for value in candidates:
        if not value:
            continue
        candidate = Path(str(value))
        if candidate.is_file():
            return candidate
        local = bundle_dir / candidate.name
        if local.is_file():
            return local
    for candidate in sorted(bundle_dir.glob("raw_clip.*")):
        if candidate.is_file():
            return candidate
    return None


def ffprobe_duration(path: Path | None) -> float | None:
    if not path or not path.is_file():
        return None
    res = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=60,
    )
    if res.returncode != 0:
        return None
    try:
        return float((res.stdout or "").strip())
    except ValueError:
        return None


def scan_new_bundles() -> list[dict[str, Any]]:
    bundles: list[dict[str, Any]] = []
    if not EVIDENCE_ROOT.is_dir():
        return bundles
    for bundle_dir in EVIDENCE_ROOT.iterdir():
        if not bundle_dir.is_dir():
            continue
        try:
            mtime = bundle_dir.stat().st_mtime
        except OSError:
            continue
        if mtime < VALIDATION_START_EPOCH:
            continue
        metadata = load_json(bundle_dir / "metadata.json")
        summary = load_json(bundle_dir / "summary.json")
        event_type = (
            metadata.get("event_type")
            or summary.get("event_type")
            or nested_get(metadata, "event", "event_type")
            or nested_get(metadata, "payload", "event_type")
            or ""
        )
        raw_clip = find_raw_clip(bundle_dir, metadata, summary)
        duration = ffprobe_duration(raw_clip)
        bundles.append(
            {
                "bundle_id": bundle_dir.name,
                "bundle_dir": str(bundle_dir),
                "event_type": event_type,
                "mtime": mtime,
                "raw_clip_path": str(raw_clip) if raw_clip else "",
                "raw_clip_duration": duration,
            }
        )
    return sorted(bundles, key=lambda item: item["mtime"], reverse=True)


def latest_bundle(bundles: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
    return next((item for item in bundles if item.get("event_type") == event_type), None)


def rows_since(table: str, columns: str, where: str = "") -> list[dict[str, Any]]:
    return psql_json(
        f"""
        SELECT {columns}
        FROM {table}
        WHERE created_at >= to_timestamp({VALIDATION_START_EPOCH})
        {where}
        ORDER BY created_at ASC
        """
    )


def observation_rates(rows: list[dict[str, Any]], elapsed_seconds: float) -> dict[str, Any]:
    elapsed = max(elapsed_seconds, 1.0)
    by_track: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        track_id = str(row.get("track_id") or "no_track")
        try:
            by_track[track_id].append(int(row.get("timestamp_ms") or 0))
        except Exception:
            pass

    track_rates: dict[str, float] = {}
    active_span_sum = 0.0
    for track_id, timestamps in by_track.items():
        if not timestamps:
            continue
        timestamps.sort()
        span = max((timestamps[-1] - timestamps[0]) / 1000.0, 1.0)
        active_span_sum += span
        track_rates[track_id] = len(timestamps) / span

    per_track_rate = len(rows) / active_span_sum if active_span_sum > 0 else 0.0
    mean_track_rate = sum(track_rates.values()) / len(track_rates) if track_rates else 0.0
    return {
        "count": len(rows),
        "track_count": len(by_track),
        "per_second": len(rows) / elapsed,
        "per_track_per_second": per_track_rate,
        "mean_per_track_per_second": mean_track_rate,
        "active_track_span_seconds": active_span_sum,
        "per_track_rates": track_rates,
    }


def face_bbox_size(face_bbox: Any) -> tuple[float, float]:
    if not isinstance(face_bbox, list) or len(face_bbox) < 4:
        return 0.0, 0.0
    try:
        return abs(float(face_bbox[2])), abs(float(face_bbox[3]))
    except Exception:
        return 0.0, 0.0


def landmark_count(landmarks: Any) -> int:
    if isinstance(landmarks, list):
        return len(landmarks)
    return 0


def invalid_face_observation_count(face_rows: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
    counts = {
        "low_confidence_inserted": 0,
        "too_small_inserted": 0,
        "missing_or_bad_landmarks_inserted": 0,
        "bad_embedding_norm_inserted": 0,
        "bad_embedding_dim_inserted": 0,
    }
    for row in face_rows:
        try:
            if float(row.get("face_confidence") or 0.0) < FACE_REID_MIN_CONFIDENCE:
                counts["low_confidence_inserted"] += 1
        except Exception:
            counts["low_confidence_inserted"] += 1
        width, height = face_bbox_size(row.get("face_bbox"))
        if width < FACE_REID_MIN_FACE_SIZE or height < FACE_REID_MIN_FACE_SIZE:
            counts["too_small_inserted"] += 1
        if landmark_count(row.get("landmarks")) not in (5, 10, 15):
            counts["missing_or_bad_landmarks_inserted"] += 1
        try:
            norm = float(row.get("embedding_norm") or 0.0)
            if norm < 1.0 - FACE_REID_NORM_TOLERANCE or norm > 1.0 + FACE_REID_NORM_TOLERANCE:
                counts["bad_embedding_norm_inserted"] += 1
        except Exception:
            counts["bad_embedding_norm_inserted"] += 1
        try:
            if int(row.get("embedding_dim") or 0) != 512:
                counts["bad_embedding_dim_inserted"] += 1
        except Exception:
            counts["bad_embedding_dim_inserted"] += 1
    return sum(counts.values()), counts


def parse_gate_counters() -> dict[str, Any]:
    res = run(
        ["docker", "logs", "--since", VALIDATION_START, SAVANT_CONTAINER],
        timeout=90,
    )
    logs = (res.stdout or "") + "\n" + (res.stderr or "")
    (ARTIFACT_DIR / "savant.logs").write_text(logs, encoding="utf-8")
    counters: dict[str, Any] = {}
    for line in logs.splitlines():
        if "[face_reid_gate_summary]" not in line:
            continue
        for key, value in re.findall(r"([a-z_]+)=([0-9.]+)", line):
            counters[key] = float(value) if "." in value else int(value)
    return counters


def watchlist_hit_count() -> int:
    rows = psql_json(
        f"""
        SELECT COUNT(*)::int AS count
        FROM events
        WHERE event_type = 'watchlist_hit'
          AND created_at >= to_timestamp({VALIDATION_START_EPOCH})
        """
    )
    if not rows:
        return 0
    return int(rows[0].get("count") or 0)


deadline = time.monotonic() + DURATION_SECONDS
last_bundles: list[dict[str, Any]] = []
while time.monotonic() < deadline:
    last_bundles = scan_new_bundles()
    intrusion = latest_bundle(last_bundles, "intrusion")
    watchlist = latest_bundle(last_bundles, "watchlist_hit")
    print(
        "monitor "
        f"elapsed={DURATION_SECONDS - int(deadline - time.monotonic())}s "
        f"new_bundles={len(last_bundles)} "
        f"intrusion={bool(intrusion)} watchlist={bool(watchlist)}",
        flush=True,
    )
    if intrusion and watchlist:
        break
    time.sleep(POLL_SECONDS)

elapsed_seconds = max(time.time() - VALIDATION_START_EPOCH, 1.0)
bundles = scan_new_bundles()
intrusion_bundle = latest_bundle(bundles, "intrusion")
watchlist_bundle = latest_bundle(bundles, "watchlist_hit")

person_rows = rows_since(
    "person_bbox_observations",
    "source_observation_id, track_id, timestamp_ms, person_bbox, person_confidence, gate_status, created_at::text AS created_at",
    "AND gate_status = 'accepted'",
)
face_rows = rows_since(
    "face_observations",
    "source_observation_id, track_id, timestamp_ms, face_bbox, landmarks, face_confidence, embedding_dim, embedding_norm, created_at::text AS created_at",
)
person_rates = observation_rates(person_rows, elapsed_seconds)
face_rates = observation_rates(face_rows, elapsed_seconds)
invalid_face_count, invalid_face_counts = invalid_face_observation_count(face_rows)
gate_counters = parse_gate_counters()
wl_count = watchlist_hit_count()
viewer = fetch_text(f"{VIEWER_URL}/")

low_quality_face_reject_counts = {
    "rejected_by_confidence": int(gate_counters.get("reid_rejected_by_confidence", 0) or 0),
    "rejected_by_size": int(gate_counters.get("reid_rejected_by_size", 0) or 0),
    "rejected_by_landmarks": int(gate_counters.get("reid_rejected_by_landmarks", 0) or 0),
    "rejected_by_embedding_norm": int(gate_counters.get("reid_rejected_by_embedding_norm", 0) or 0),
}
low_quality_reject_total = sum(low_quality_face_reject_counts.values())

raw_clip_durations = {
    item["event_type"]: item.get("raw_clip_duration")
    for item in (intrusion_bundle, watchlist_bundle)
    if item
}
raw_clip_duration = (
    watchlist_bundle.get("raw_clip_duration") if watchlist_bundle else None
) or (
    intrusion_bundle.get("raw_clip_duration") if intrusion_bundle else None
)

failure_reasons: list[str] = []
notes: list[str] = []
if DOCTOR_STATUS != "ok":
    failure_reasons.append("doctor_not_ok")
if COMPOSE_CONFIG_STATUS != "ok":
    failure_reasons.append("compose_config_not_ok")
if RELOAD_STATUS != "ok":
    failure_reasons.append("reload_not_ok")
if not viewer.get("ok"):
    failure_reasons.append("viewer_endpoint_not_200")
if not intrusion_bundle:
    failure_reasons.append("no_new_intrusion_evidence")
if not watchlist_bundle:
    failure_reasons.append("no_new_watchlist_evidence")
if raw_clip_duration is None:
    failure_reasons.append("no_raw_clip_duration")
elif not (8.0 <= float(raw_clip_duration) <= 14.0):
    failure_reasons.append("raw_clip_duration_not_about_10s")
if not person_rows:
    failure_reasons.append("no_person_bbox_observations")
if face_rates["per_track_per_second"] > 1.20:
    failure_reasons.append("face_observation_rate_above_1fps_per_track")
if invalid_face_count > 0:
    failure_reasons.append("low_quality_face_inserted")
if low_quality_reject_total == 0:
    notes.append("no_low_quality_face_candidates_observed_or_no_gate_summary")

config_correct = (
    PERSON_INTERVAL_MS == 333
    and FACE_INTERVAL_MS == 1000
    and abs(FACE_DETECTOR_THRESHOLD - 0.25) < 1e-9
    and FACE_REID_MIN_CONFIDENCE >= 0.35
)

if invalid_face_count > 0:
    result_marker = FAIL_FACE_QUALITY
elif face_rates["per_track_per_second"] > 1.20:
    result_marker = FAIL_FACE_TOO_FREQUENT
elif failure_reasons:
    result_marker = FAIL_RUNTIME
elif config_correct and person_rates["per_track_per_second"] < 2.0:
    result_marker = PARTIAL_PERSON_RATE
    notes.append("person_rate_below_3fps_but_calibration_env_is_correct")
else:
    result_marker = PASS_MARKER

summary = {
    "schema_version": "c1i2f.pose_face_frequency_quality_gate.v1",
    "result_marker": result_marker,
    "validation_start": VALIDATION_START,
    "duration_seconds": DURATION_SECONDS,
    "doctor_status": DOCTOR_STATUS,
    "compose_config_status": COMPOSE_CONFIG_STATUS,
    "reload_status": RELOAD_STATUS,
    "viewer_endpoint_status": viewer.get("status"),
    "viewer_endpoint_ok": bool(viewer.get("ok")),
    "face_detector_threshold_variable": "FACE_CONFIDENCE_THRESHOLD",
    "face_detector_threshold": FACE_DETECTOR_THRESHOLD,
    "face_reid_min_confidence_variable": "FACE_REID_MIN_CONFIDENCE",
    "face_reid_min_confidence": FACE_REID_MIN_CONFIDENCE,
    "face_reid_min_face_size": FACE_REID_MIN_FACE_SIZE,
    "face_reid_norm_tolerance": FACE_REID_NORM_TOLERANCE,
    "person_bbox_observation_min_interval_ms": PERSON_INTERVAL_MS,
    "person_observation_batch_size": PERSON_OBSERVATION_BATCH_SIZE,
    "person_observation_consumer_group": PERSON_OBSERVATION_CONSUMER_GROUP,
    "face_reid_min_interval_ms": FACE_INTERVAL_MS,
    "raw_face_detections": int(gate_counters.get("raw_face_detections", 0) or 0),
    "face_detections_passing_detector_confidence": int(gate_counters.get("detector_confidence_passed", 0) or 0),
    "face_detections_rejected_by_detector_confidence": int(gate_counters.get("detector_confidence_rejected", 0) or 0),
    "low_quality_face_reject_counts": low_quality_face_reject_counts,
    "face_observations_inserted": int(face_rates["count"]),
    "watchlist_hit_count": wl_count,
    "person_bbox_observations_per_second": person_rates["per_second"],
    "person_bbox_observations_per_track_per_second": person_rates["per_track_per_second"],
    "person_bbox_observations_mean_per_track_per_second": person_rates["mean_per_track_per_second"],
    "person_bbox_observations_active_track_span_seconds": person_rates["active_track_span_seconds"],
    "person_bbox_observations_track_count": person_rates["track_count"],
    "person_bbox_observations_count": person_rates["count"],
    "face_observations_per_second": face_rates["per_second"],
    "face_observations_per_track_per_second": face_rates["per_track_per_second"],
    "face_observations_mean_per_track_per_second": face_rates["mean_per_track_per_second"],
    "face_observations_active_track_span_seconds": face_rates["active_track_span_seconds"],
    "face_observations_track_count": face_rates["track_count"],
    "face_observations_count": face_rates["count"],
    "invalid_face_observation_count": invalid_face_count,
    "invalid_face_observation_counts": invalid_face_counts,
    "intrusion_evidence_id": intrusion_bundle.get("bundle_id") if intrusion_bundle else "",
    "watchlist_evidence_id": watchlist_bundle.get("bundle_id") if watchlist_bundle else "",
    "intrusion_bundle": intrusion_bundle,
    "watchlist_bundle": watchlist_bundle,
    "raw_clip_duration": raw_clip_duration,
    "raw_clip_durations": raw_clip_durations,
    "evidence_bundle_count": len(bundles),
    "new_evidence_bundles": bundles[:10],
    "gate_counters": gate_counters,
    "failure_reasons": failure_reasons,
    "notes": notes,
}
SUMMARY_JSON.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

print(json.dumps({
    "result_marker": result_marker,
    "person_bbox_observations_per_track_per_second": person_rates["per_track_per_second"],
    "face_observations_per_track_per_second": face_rates["per_track_per_second"],
    "low_quality_face_reject_counts": low_quality_face_reject_counts,
    "watchlist_hit_count": wl_count,
    "intrusion_evidence_id": summary["intrusion_evidence_id"],
    "watchlist_evidence_id": summary["watchlist_evidence_id"],
    "raw_clip_duration": raw_clip_duration,
    "failure_reasons": failure_reasons,
}, indent=2, sort_keys=True))

if result_marker in (PASS_MARKER, PARTIAL_PERSON_RATE):
    sys.exit(0)
sys.exit(1)
PY

log "summary=${SUMMARY_JSON}"
