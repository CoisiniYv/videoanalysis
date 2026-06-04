#!/usr/bin/env bash
# C1I.2g - face observation query window anchor smoke.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/infra/docker-compose.c1-official-replay-dev.yml}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/data/video-analytics/artifacts/c1i2g}"
SUMMARY_JSON="${SUMMARY_JSON:-${ARTIFACT_DIR}/face_observation_query_window_anchor_summary.json}"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
VIEWER_URL="${VIEWER_URL:-http://localhost:8090}"
DURATION_SECONDS="${DURATION_SECONDS:-300}"
POLL_SECONDS="${POLL_SECONDS:-15}"

mkdir -p "${ARTIFACT_DIR}"

log() {
  printf '[c1i2g] %s\n' "$*"
}

utc_now() {
  date -u +"%Y-%m-%dT%H:%M:%S+00:00"
}

epoch_now() {
  date +%s
}

export FACE_OBSERVATION_CREATED_AT_MARGIN_SECONDS="${FACE_OBSERVATION_CREATED_AT_MARGIN_SECONDS:-15}"
export ALLOW_FACE_TIMESTAMP_ONLY_QUERY="${ALLOW_FACE_TIMESTAMP_ONLY_QUERY:-false}"
export C1I2G_ALLOW_OFFLINE_REFALLBACK="${C1I2G_ALLOW_OFFLINE_REFALLBACK:-false}"
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
log "reloading active official runtime services"
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
DURATION_SECONDS="${DURATION_SECONDS}" \
POLL_SECONDS="${POLL_SECONDS}" \
VALIDATION_START="${VALIDATION_START}" \
VALIDATION_START_EPOCH="${VALIDATION_START_EPOCH}" \
DOCTOR_STATUS="${DOCTOR_STATUS}" \
COMPOSE_CONFIG_STATUS="${COMPOSE_CONFIG_STATUS}" \
RELOAD_STATUS="${RELOAD_STATUS}" \
C1I2G_ALLOW_OFFLINE_REFALLBACK="${C1I2G_ALLOW_OFFLINE_REFALLBACK}" \
python3 - <<'PY'
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(os.environ["ROOT_DIR"])
ARTIFACT_DIR = Path(os.environ["ARTIFACT_DIR"])
SUMMARY_JSON = Path(os.environ["SUMMARY_JSON"])
EVIDENCE_ROOT = Path(os.environ["EVIDENCE_ROOT"])
VIEWER_URL = os.environ["VIEWER_URL"].rstrip("/")
DURATION_SECONDS = int(os.environ["DURATION_SECONDS"])
POLL_SECONDS = max(int(os.environ["POLL_SECONDS"]), 1)
VALIDATION_START = os.environ["VALIDATION_START"]
VALIDATION_START_EPOCH = float(os.environ["VALIDATION_START_EPOCH"])
DOCTOR_STATUS = os.environ["DOCTOR_STATUS"]
COMPOSE_CONFIG_STATUS = os.environ["COMPOSE_CONFIG_STATUS"]
RELOAD_STATUS = os.environ["RELOAD_STATUS"]
ALLOW_OFFLINE_REFALLBACK = os.environ.get("C1I2G_ALLOW_OFFLINE_REFALLBACK", "false").lower() in {"1", "true", "yes"}

PASS_MARKER = "PASS_C1I2G_FACE_OBSERVATION_QUERY_WINDOW_ANCHOR"
FAIL_TIMESTAMP_ONLY = "FAIL_C1I2G_TIMESTAMP_ONLY_FACE_QUERY"
FAIL_CROSS_DAY = "FAIL_C1I2G_CROSS_DAY_FACE_OBSERVATION_CONTAMINATION"
PARTIAL_DEDUP = "PARTIAL_C1I2G_QUERY_FIXED_DEDUP_PENDING"
FAIL_RUNTIME = "FAIL_C1I2G_RUNTIME_CONTRACT"
FACE_COUNT_RULE = "face_annotation_count < 80"

DIAG_JSON = Path("/data/video-analytics/artifacts/c1i2f/face_overlay_overcount_diagnosis.json")
DIAG_MD = Path("/data/video-analytics/artifacts/c1i2f/face_overlay_overcount_diagnosis.md")


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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                records.append(item)
    except Exception:
        return records
    return records


def nested_get(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def objects_for_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    objects = [
        item for item in record.get("objects", [])
        if isinstance(item, dict)
    ] if isinstance(record.get("objects"), list) else []
    if record.get("record_type") == "object_annotation":
        obj = {
            key: record[key]
            for key in (
                "object_type",
                "object_id",
                "annotation_role",
                "track_id",
                "bbox",
                "identity",
                "style",
            )
            if key in record
        }
        if obj:
            objects.append(obj)
    return objects


def is_known_face(obj: dict[str, Any]) -> bool:
    identity = obj.get("identity") if isinstance(obj.get("identity"), dict) else {}
    return (
        identity.get("status") == "matched"
        or identity.get("match_status") == "above_threshold"
        or bool(identity.get("external_person_id"))
        or identity.get("person_id") not in (None, "")
    )


def is_propagated_face(obj: dict[str, Any]) -> bool:
    identity = obj.get("identity") if isinstance(obj.get("identity"), dict) else {}
    return identity.get("match_status") == "propagated_by_track"


def annotation_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    face_by_second: Counter[str] = Counter()
    face_annotation_count = 0
    known_face_count = 0
    unknown_face_count = 0
    propagated_face_count = 0
    for record in records:
        second = int(float(record.get("time_offset_ms") or 0) // 1000)
        for obj in objects_for_record(record):
            if obj.get("object_type") != "face":
                continue
            face_annotation_count += 1
            face_by_second[str(second)] += 1
            if is_known_face(obj):
                known_face_count += 1
            else:
                unknown_face_count += 1
            if is_propagated_face(obj):
                propagated_face_count += 1
    return {
        "face_annotation_count": face_annotation_count,
        "known_face_count": known_face_count,
        "unknown_face_count": unknown_face_count,
        "propagated_face_count": propagated_face_count,
        "face_by_second": dict(sorted(face_by_second.items(), key=lambda item: int(item[0]))),
    }


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
        bundles.append(
            {
                "bundle_id": bundle_dir.name,
                "bundle_dir": str(bundle_dir),
                "event_type": event_type,
                "mtime": mtime,
            }
        )
    return sorted(bundles, key=lambda item: item["mtime"], reverse=True)


def latest_watchlist_bundle(bundles: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in bundles if item.get("event_type") == "watchlist_hit"), None)


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


def scan_existing_watchlist_bundles() -> list[dict[str, Any]]:
    bundles: list[dict[str, Any]] = []
    if not EVIDENCE_ROOT.is_dir():
        return bundles
    for bundle_dir in EVIDENCE_ROOT.iterdir():
        if not bundle_dir.is_dir():
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
        if event_type != "watchlist_hit":
            continue
        raw_clip = find_raw_clip(bundle_dir, metadata, summary)
        event_id = (
            summary.get("event_id")
            or metadata.get("event_id")
            or nested_get(metadata, "event", "event_id")
            or bundle_dir.name
        )
        if not raw_clip or not event_id:
            continue
        try:
            mtime = bundle_dir.stat().st_mtime
        except OSError:
            mtime = 0.0
        bundles.append(
            {
                "bundle_id": bundle_dir.name,
                "bundle_dir": str(bundle_dir),
                "event_id": str(event_id),
                "event_type": event_type,
                "mtime": mtime,
                "raw_clip_path": str(raw_clip),
            }
        )
    return sorted(bundles, key=lambda item: item["mtime"], reverse=True)


def offline_refinalize_existing_watchlist_bundle() -> dict[str, Any] | None:
    existing = scan_existing_watchlist_bundles()
    if not existing:
        return None
    source = existing[0]
    source_dir = Path(source["bundle_dir"])
    new_id = str(uuid.uuid4())
    output_dir = EVIDENCE_ROOT / new_id
    output_dir.mkdir(parents=True, exist_ok=False)

    raw_clip = Path(source["raw_clip_path"])
    if raw_clip.is_file():
        shutil.copy2(raw_clip, output_dir / raw_clip.name)
    source_sink_metadata = source_dir / "sink_metadata.json"
    replay_metadata_path = source_sink_metadata if source_sink_metadata.is_file() else None
    if replay_metadata_path is not None:
        shutil.copy2(replay_metadata_path, output_dir / "sink_metadata.json")

    sys.path.insert(0, str(ROOT / "services" / "media-worker"))
    import psycopg
    from app.continuous_annotation import write_continuous_annotation_bundle
    from app.worker import _load_event_context

    with psycopg.connect("postgresql://video:video@localhost:5438/video_analytics") as pg_conn:
        event_context = _load_event_context(pg_conn, source["event_id"])
        summary = write_continuous_annotation_bundle(
            pg_conn,
            event_context,
            annotations_path=str(output_dir / "annotations.jsonl"),
            summary_path=str(output_dir / "summary.json"),
            replay_metadata_path=str(replay_metadata_path) if replay_metadata_path else None,
        )

    metadata = load_json(source_dir / "metadata.json")
    metadata.update(
        {
            "bundle_id": new_id,
            "event_id": source["event_id"],
            "event_type": "watchlist_hit",
            "source_bundle_id": source["bundle_id"],
            "generated_by": "c1i2g_offline_refinalize_existing_watchlist_bundle",
            "raw_clip_path": str(output_dir / raw_clip.name) if raw_clip.is_file() else "",
            "summary_json_path": str(output_dir / "summary.json"),
            "annotations_jsonl_path": str(output_dir / "annotations.jsonl"),
        }
    )
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "bundle_id": new_id,
        "bundle_dir": str(output_dir),
        "event_type": "watchlist_hit",
        "event_id": source["event_id"],
        "source_bundle_id": source["bundle_id"],
        "offline_refinalized": True,
        "summary_annotation_status": summary.get("annotation_status"),
    }


deadline = time.monotonic() + DURATION_SECONDS
last_bundles: list[dict[str, Any]] = []
while time.monotonic() < deadline:
    last_bundles = scan_new_bundles()
    watchlist = latest_watchlist_bundle(last_bundles)
    print(
        "monitor "
        f"elapsed={DURATION_SECONDS - int(deadline - time.monotonic())}s "
        f"new_bundles={len(last_bundles)} watchlist={bool(watchlist)}",
        flush=True,
    )
    if watchlist:
        break
    time.sleep(POLL_SECONDS)

bundles = scan_new_bundles()
watchlist_bundle = latest_watchlist_bundle(bundles)
offline_refinalized_bundle: dict[str, Any] | None = None
if not watchlist_bundle and ALLOW_OFFLINE_REFALLBACK:
    offline_refinalized_bundle = offline_refinalize_existing_watchlist_bundle()
    if offline_refinalized_bundle:
        bundles = scan_new_bundles()
        watchlist_bundle = latest_watchlist_bundle(bundles)
        if not watchlist_bundle:
            watchlist_bundle = offline_refinalized_bundle
viewer = fetch_text(f"{VIEWER_URL}/")

bundle_summary: dict[str, Any] = {}
records: list[dict[str, Any]] = []
if watchlist_bundle:
    bundle_dir = Path(watchlist_bundle["bundle_dir"])
    bundle_summary = load_json(bundle_dir / "summary.json")
    records = load_jsonl(bundle_dir / "annotations.jsonl")

stats = annotation_stats(records)
query = bundle_summary.get("face_observation_query")
query = query if isinstance(query, dict) else {}
dedup = bundle_summary.get("face_dedup")
dedup = dedup if isinstance(dedup, dict) else {}
before = {
    "watchlist_bundle_id": "23cab4d1-a7ab-4d8d-ac24-1618f785c82d",
    "face_annotation_count": 233,
    "face_by_second": {
        "0": 21,
        "1": 26,
        "2": 23,
        "3": 24,
        "4": 26,
        "5": 26,
        "6": 26,
        "7": 22,
        "8": 17,
        "9": 22,
    },
}
if DIAG_JSON.is_file():
    diag = load_json(DIAG_JSON)
    before["diagnosis_json_available"] = True
elif DIAG_MD.is_file():
    before["diagnosis_md_available"] = True

timestamp_only = bool(query.get("timestamp_only_fallback_used"))
cross_day = int(query.get("cross_day_result_count") or 0)
face_count = int(stats["face_annotation_count"])

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
if not watchlist_bundle:
    failure_reasons.append("no_new_watchlist_evidence_after_validation_start")
if watchlist_bundle and not query:
    failure_reasons.append("missing_face_observation_query_summary")
if query and query.get("mode") not in {"timestamp_and_created_at", "created_at_fallback"}:
    failure_reasons.append("unexpected_face_observation_query_mode")
if query and query.get("db_result_count") and not query.get("db_result_created_at_min"):
    failure_reasons.append("missing_db_result_created_at_min")
if query and query.get("db_result_count") and not query.get("db_result_created_at_max"):
    failure_reasons.append("missing_db_result_created_at_max")
if watchlist_bundle and face_count == 0:
    failure_reasons.append("no_face_annotations_in_watchlist_bundle")
if int(stats["known_face_count"]) <= 0:
    failure_reasons.append("known_face_count_not_positive")

if timestamp_only:
    result_marker = FAIL_TIMESTAMP_ONLY
elif cross_day > 0:
    result_marker = FAIL_CROSS_DAY
elif failure_reasons:
    result_marker = FAIL_RUNTIME
elif face_count >= 80:
    result_marker = PARTIAL_DEDUP
    notes.append("query anchored but face_annotation_count >= 80; inspect for real crowd or dedup gap")
else:
    result_marker = PASS_MARKER

if offline_refinalized_bundle:
    notes.append("offline_refinalized_existing_watchlist_bundle_due_to_no_live_rtsp_bundle")

summary = {
    "schema_version": "c1i2g.face_observation_query_window_anchor.v1",
    "result_marker": result_marker,
    "validation_start": VALIDATION_START,
    "duration_seconds": DURATION_SECONDS,
    "doctor_status": DOCTOR_STATUS,
    "compose_config_status": COMPOSE_CONFIG_STATUS,
    "reload_status": RELOAD_STATUS,
    "viewer_endpoint_status": viewer.get("status"),
    "viewer_endpoint_ok": bool(viewer.get("ok")),
    "viewer_url": f"{VIEWER_URL}/",
    "watchlist_bundle_id": watchlist_bundle.get("bundle_id") if watchlist_bundle else "",
    "watchlist_bundle": watchlist_bundle,
    "offline_refinalized_bundle": offline_refinalized_bundle,
    "face_annotation_count": face_count,
    "known_face_count": int(stats["known_face_count"]),
    "unknown_face_count": int(stats["unknown_face_count"]),
    "propagated_face_count": int(stats["propagated_face_count"]),
    "face_by_second": stats["face_by_second"],
    "face_observation_query_mode": query.get("mode", ""),
    "timestamp_only_fallback_used": timestamp_only,
    "db_result_created_at_min": query.get("db_result_created_at_min"),
    "db_result_created_at_max": query.get("db_result_created_at_max"),
    "cross_day_result_count": cross_day,
    "face_observation_query": query,
    "face_dedup_input_count": int(dedup.get("input_count") or 0),
    "face_dedup_output_count": int(dedup.get("output_count") or 0),
    "same_track_second_removed": int(dedup.get("same_track_second_removed") or 0),
    "iou_duplicate_removed": int(dedup.get("iou_duplicate_removed") or 0),
    "face_dedup": dedup,
    "before_fix": before,
    "face_count_rule": FACE_COUNT_RULE,
    "failure_reasons": failure_reasons,
    "notes": notes,
}
SUMMARY_JSON.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

print(json.dumps({
    "result_marker": result_marker,
    "watchlist_bundle_id": summary["watchlist_bundle_id"],
    "face_annotation_count": face_count,
    "face_observation_query_mode": summary["face_observation_query_mode"],
    "timestamp_only_fallback_used": timestamp_only,
    "cross_day_result_count": cross_day,
    "face_dedup_input_count": summary["face_dedup_input_count"],
    "face_dedup_output_count": summary["face_dedup_output_count"],
    "failure_reasons": failure_reasons,
}, indent=2, sort_keys=True))

if result_marker in (PASS_MARKER, PARTIAL_DEDUP):
    raise SystemExit(0)
raise SystemExit(1)
PY

log "summary=${SUMMARY_JSON}"
