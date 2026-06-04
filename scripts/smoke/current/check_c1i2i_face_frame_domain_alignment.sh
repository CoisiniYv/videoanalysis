#!/usr/bin/env bash
# C1I.2i - face overlay frame-domain time alignment smoke.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/infra/docker-compose.c1-official-replay-dev.yml}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/data/video-analytics/artifacts/c1i2i}"
SUMMARY_JSON="${SUMMARY_JSON:-${ARTIFACT_DIR}/face_frame_domain_alignment_summary.json}"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
VIEWER_URL="${VIEWER_URL:-http://localhost:8090}"
DURATION_SECONDS="${DURATION_SECONDS:-240}"
POLL_SECONDS="${POLL_SECONDS:-15}"

mkdir -p "${ARTIFACT_DIR}"

log() {
  printf '[c1i2i] %s\n' "$*"
}

utc_now() {
  date -u +"%Y-%m-%dT%H:%M:%S+00:00"
}

epoch_now() {
  date +%s
}

export DEFAULT_PRE_SECONDS="${DEFAULT_PRE_SECONDS:-5}"
export DEFAULT_POST_SECONDS="${DEFAULT_POST_SECONDS:-5}"
export RECORDING_PRE_SECONDS="${RECORDING_PRE_SECONDS:-5}"
export RECORDING_POST_SECONDS="${RECORDING_POST_SECONDS:-5}"
export RECORDING_MAX_REQUESTS_PER_RUN="${RECORDING_MAX_REQUESTS_PER_RUN:-200}"
export RECORDING_COOLDOWN_SECONDS="${RECORDING_COOLDOWN_SECONDS:-5}"
export CLIP_WORKER_MAX_JOBS_PER_RUN="${CLIP_WORKER_MAX_JOBS_PER_RUN:-200}"
export CLIP_WORKER_RUN_ONCE="${CLIP_WORKER_RUN_ONCE:-false}"
export FACE_OBSERVATION_CREATED_AT_MARGIN_SECONDS="${FACE_OBSERVATION_CREATED_AT_MARGIN_SECONDS:-15}"
export ALLOW_FACE_TIMESTAMP_ONLY_QUERY="${ALLOW_FACE_TIMESTAMP_ONLY_QUERY:-false}"

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
python3 - <<'PY'
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
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

PASS_MARKER = "PASS_C1I2I_FACE_FRAME_DOMAIN_ALIGNMENT"
PARTIAL_FRAME_PTS = "PARTIAL_C1I2I_FRAME_PTS_FALLBACK_ALIGNMENT"
FAIL_TIMESTAMP = "FAIL_C1I2I_FACE_STILL_TIMESTAMP_ESTIMATED"
FAIL_WRONG_DOMAIN = "FAIL_C1I2I_WRONG_TIME_DOMAIN"
FAIL_RUNTIME = "FAIL_C1I2I_RUNTIME_CONTRACT"

DEBUG_SECONDS = [1, 3, 5, 7, 9]
FACE_HOLD_MS = 500.0


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


def fetch_json(url: str) -> dict[str, Any]:
    result = fetch_text(url)
    try:
        result["data"] = json.loads(result.get("text") or "{}")
    except json.JSONDecodeError:
        result["data"] = {}
        result["ok"] = False
    return result


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
        obj: dict[str, Any] = {}
        for key in (
            "object_type",
            "object_id",
            "annotation_role",
            "track_id",
            "person_track_id",
            "face_track_id",
            "track_id_semantics",
            "bbox",
            "identity",
            "style",
            "label",
        ):
            if key in record:
                obj[key] = record[key]
        if obj:
            objects.append(obj)
    return objects


def is_known_face(obj: dict[str, Any]) -> bool:
    if obj.get("object_type") != "face":
        return False
    identity = obj.get("identity") if isinstance(obj.get("identity"), dict) else {}
    return (
        identity.get("status") == "matched"
        or identity.get("match_status") == "above_threshold"
        or bool(identity.get("external_person_id"))
        or identity.get("person_id") not in (None, "")
    )


def display_name(obj: dict[str, Any]) -> str:
    identity = obj.get("identity") if isinstance(obj.get("identity"), dict) else {}
    return str(
        identity.get("display_name")
        or identity.get("name")
        or identity.get("person_id")
        or "face"
    )


def bbox_xyxy(bbox: Any) -> list[float] | None:
    if not isinstance(bbox, dict):
        return None
    values = bbox.get("xyxy") or bbox.get("values") or bbox.get("bbox")
    fmt = str(bbox.get("format") or bbox.get("bbox_format") or "xyxy").lower()
    if not isinstance(values, list) or len(values) < 4:
        return None
    try:
        nums = [float(item) for item in values[:4]]
    except (TypeError, ValueError):
        return None
    if fmt == "xywh":
        x, y, width, height = nums
        return [x, y, x + width, y + height]
    if fmt == "cxcywh":
        cx, cy, width, height = nums
        return [cx - width / 2.0, cy - height / 2.0, cx + width / 2.0, cy + height / 2.0]
    return nums


def bundle_event_type(bundle_dir: Path, metadata: dict[str, Any], summary: dict[str, Any]) -> str:
    return str(
        summary.get("event_type")
        or nested_get(metadata, "event", "event_type")
        or metadata.get("event_type")
        or ""
    )


def bundle_mtime(bundle_dir: Path) -> float:
    mtimes = [bundle_dir.stat().st_mtime]
    for name in ("summary.json", "metadata.json", "annotations.jsonl", "raw_clip.mp4", "raw_clip.mov"):
        path = bundle_dir / name
        if path.exists():
            mtimes.append(path.stat().st_mtime)
    return max(mtimes)


def find_raw_clip(bundle_dir: Path, metadata: dict[str, Any], summary: dict[str, Any]) -> Path | None:
    for value in (
        nested_get(metadata, "media", "raw_clip_path"),
        summary.get("raw_clip_path"),
        summary.get("raw_clip_name"),
    ):
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


def raw_clip_duration_ms(metadata: dict[str, Any], summary: dict[str, Any]) -> int:
    for value in (
        summary.get("raw_clip_duration"),
        nested_get(metadata, "media", "raw_clip_duration"),
        nested_get(metadata, "media", "clip_validation", "duration_seconds"),
    ):
        try:
            if value is not None:
                return int(float(value) * 1000)
        except (TypeError, ValueError):
            continue
    duration = nested_get(summary, "time_anchor", "actual_clip_duration_ms")
    try:
        return int(duration)
    except (TypeError, ValueError):
        return 0


def known_face_lines(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for record in records:
        if any(is_known_face(obj) for obj in objects_for_record(record)):
            lines.append(record)
    return lines


def analyze_bundle(bundle_dir: Path) -> dict[str, Any]:
    metadata = load_json(bundle_dir / "metadata.json")
    summary = load_json(bundle_dir / "summary.json")
    records = load_jsonl(bundle_dir / "annotations.jsonl")
    event_type = bundle_event_type(bundle_dir, metadata, summary)
    raw_clip = find_raw_clip(bundle_dir, metadata, summary)
    viewer = fetch_json(f"{VIEWER_URL}/api/bundles/{bundle_dir.name}/annotations")
    known_lines = known_face_lines(records)
    known_offsets: list[int] = []
    basis_distribution: Counter[str] = Counter()
    bbox_sources: Counter[str] = Counter()
    for record in known_lines:
        basis_distribution[str(record.get("time_basis") or "")] += 1
        if record.get("time_offset_ms") is not None:
            known_offsets.append(int(float(record["time_offset_ms"])))
        for obj in objects_for_record(record):
            if not is_known_face(obj):
                continue
            bbox = obj.get("bbox") if isinstance(obj.get("bbox"), dict) else {}
            bbox_sources[str(bbox.get("source") or "")] += 1
    face_frame_alignment = summary.get("face_frame_alignment")
    if not isinstance(face_frame_alignment, dict):
        face_frame_alignment = {}
    query = summary.get("face_observation_query")
    if not isinstance(query, dict):
        query = {}
    face_bbox_format = summary.get("face_bbox_format")
    if not isinstance(face_bbox_format, dict):
        face_bbox_format = {}
    face_track_semantics = summary.get("face_track_semantics")
    if not isinstance(face_track_semantics, dict):
        face_track_semantics = {}

    return {
        "bundle_id": bundle_dir.name,
        "bundle_dir": str(bundle_dir),
        "event_type": event_type,
        "mtime": bundle_mtime(bundle_dir),
        "postfix_bundle": bundle_mtime(bundle_dir) >= VALIDATION_START_EPOCH,
        "metadata": metadata,
        "summary": summary,
        "records": records,
        "raw_clip_path": str(raw_clip) if raw_clip else "",
        "raw_clip_duration_ms": raw_clip_duration_ms(metadata, summary),
        "known_face_count": int(summary.get("known_face_count") or len(known_lines)),
        "face_objects": int(summary.get("face_objects") or 0),
        "known_face_time_offsets_ms": known_offsets,
        "known_face_time_basis_distribution": dict(basis_distribution),
        "face_overlay_time_alignment_status": str(
            summary.get("face_overlay_time_alignment_status") or ""
        ),
        "frame_anchored_face_count": int(
            summary.get("frame_anchored_face_count")
            or face_frame_alignment.get("frame_anchored_face_count")
            or 0
        ),
        "face_frame_alignment": face_frame_alignment,
        "face_observation_query_mode": str(query.get("mode") or ""),
        "cross_day_result_count": int(query.get("cross_day_result_count") or 0),
        "known_face_bbox_source_distribution": dict(
            summary.get("known_face_bbox_source_distribution") or bbox_sources
        ),
        "face_bbox_format": face_bbox_format,
        "face_track_semantics": face_track_semantics,
        "viewer_annotations_status": viewer.get("status"),
        "viewer_annotations_ok": bool(viewer.get("ok")),
    }


def scan_bundles() -> list[dict[str, Any]]:
    if not EVIDENCE_ROOT.is_dir():
        return []
    bundles: list[dict[str, Any]] = []
    for bundle_dir in EVIDENCE_ROOT.iterdir():
        if not bundle_dir.is_dir():
            continue
        if not (bundle_dir / "annotations.jsonl").is_file():
            continue
        bundle = analyze_bundle(bundle_dir)
        if bundle["event_type"] == "watchlist_hit":
            bundles.append(bundle)
    return sorted(bundles, key=lambda item: item["mtime"], reverse=True)


def latest_postfix_watchlist() -> dict[str, Any] | None:
    for bundle in scan_bundles():
        if bundle.get("postfix_bundle"):
            return bundle
    return None


def active_face_records(records: list[dict[str, Any]], sample_ms: int) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for record in records:
        if not any(obj.get("object_type") == "face" for obj in objects_for_record(record)):
            continue
        try:
            offset_ms = float(record.get("time_offset_ms"))
        except (TypeError, ValueError):
            continue
        if abs(offset_ms - sample_ms) <= FACE_HOLD_MS:
            active.append(record)
    return active


def generate_overlay_debug(bundle: dict[str, Any] | None) -> tuple[str, list[str], list[str]]:
    bundle_id = str(bundle.get("bundle_id") if bundle else "no_bundle")
    out_dir = ARTIFACT_DIR / "overlay_debug" / bundle_id
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[str] = []
    failures: list[str] = []
    if not bundle:
        return str(out_dir), outputs, ["missing_bundle_for_overlay_debug"]

    raw_clip = Path(str(bundle.get("raw_clip_path") or ""))
    if not raw_clip.is_file():
        return str(out_dir), outputs, ["missing_raw_clip_for_overlay_debug"]
    try:
        import cv2  # type: ignore[import-not-found]
    except Exception as exc:
        return str(out_dir), outputs, [f"opencv_unavailable:{exc.__class__.__name__}"]

    records = bundle.get("records") if isinstance(bundle.get("records"), list) else []
    cap = cv2.VideoCapture(str(raw_clip))
    if not cap.isOpened():
        return str(out_dir), outputs, ["raw_clip_open_failed_for_overlay_debug"]

    for second in DEBUG_SECONDS:
        sample_ms = second * 1000
        cap.set(cv2.CAP_PROP_POS_MSEC, float(sample_ms))
        ok, frame = cap.read()
        raw_path = out_dir / f"raw_t{second:03d}.jpg"
        overlay_path = out_dir / f"overlay_t{second:03d}.jpg"
        if not ok or frame is None:
            failures.append(f"frame_read_failed_t{second:03d}")
            continue
        cv2.imwrite(str(raw_path), frame)
        overlay = frame.copy()
        for record in active_face_records(records, sample_ms):
            for obj in objects_for_record(record):
                if obj.get("object_type") != "face":
                    continue
                xyxy = bbox_xyxy(obj.get("bbox"))
                if xyxy is None:
                    continue
                x1, y1, x2, y2 = [int(round(value)) for value in xyxy]
                color = (0, 0, 255) if is_known_face(obj) else (180, 180, 180)
                cv2.rectangle(overlay, (x1, y1), (x2, y2), color, 3)
                label = display_name(obj)
                cv2.putText(
                    overlay,
                    label[:32],
                    (x1, max(24, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    color,
                    2,
                    cv2.LINE_AA,
                )
        cv2.imwrite(str(overlay_path), overlay)
        outputs.extend([str(raw_path), str(overlay_path)])
    cap.release()

    expected_count = len(DEBUG_SECONDS) * 2
    if len(outputs) != expected_count:
        failures.append("overlay_debug_frame_count_mismatch")
    return str(out_dir), outputs, failures


deadline = time.monotonic() + DURATION_SECONDS
watchlist_bundle: dict[str, Any] | None = None
while time.monotonic() < deadline:
    watchlist_bundle = latest_postfix_watchlist()
    print(
        "[c1i2i] monitor "
        f"remaining_seconds={max(0, int(deadline - time.monotonic()))} "
        f"watchlist={bool(watchlist_bundle)}",
        flush=True,
    )
    if watchlist_bundle:
        break
    time.sleep(POLL_SECONDS)

watchlist_bundle = latest_postfix_watchlist()
viewer_root = fetch_text(f"{VIEWER_URL}/")
overlay_debug_dir, overlay_debug_frames, overlay_debug_failures = generate_overlay_debug(watchlist_bundle)

failure_reasons: list[str] = []
if DOCTOR_STATUS != "ok":
    failure_reasons.append("doctor_not_ok")
if COMPOSE_CONFIG_STATUS != "ok":
    failure_reasons.append("compose_config_not_ok")
if RELOAD_STATUS != "ok":
    failure_reasons.append("reload_not_ok")
if not viewer_root.get("ok"):
    failure_reasons.append("viewer_endpoint_not_200")
if not watchlist_bundle:
    failure_reasons.append("no_new_watchlist_evidence_after_validation_start")

duration_ms = int(watchlist_bundle.get("raw_clip_duration_ms") or 0) if watchlist_bundle else 0
known_face_count = int(watchlist_bundle.get("known_face_count") or 0) if watchlist_bundle else 0
face_objects = int(watchlist_bundle.get("face_objects") or 0) if watchlist_bundle else 0
known_offsets = (
    list(watchlist_bundle.get("known_face_time_offsets_ms") or [])
    if watchlist_bundle
    else []
)
basis_distribution = (
    dict(watchlist_bundle.get("known_face_time_basis_distribution") or {})
    if watchlist_bundle
    else {}
)
face_frame_alignment = (
    dict(watchlist_bundle.get("face_frame_alignment") or {})
    if watchlist_bundle
    else {}
)
face_overlay_status = (
    str(watchlist_bundle.get("face_overlay_time_alignment_status") or "")
    if watchlist_bundle
    else ""
)
frame_anchored_face_count = (
    int(watchlist_bundle.get("frame_anchored_face_count") or 0)
    if watchlist_bundle
    else 0
)
face_timestamp_estimated_count = int(
    face_frame_alignment.get("face_timestamp_estimated_count") or 0
)

if watchlist_bundle:
    if duration_ms <= 0 or duration_ms > 15_000:
        failure_reasons.append("raw_clip_duration_outside_15s_contract")
    if known_face_count <= 0:
        failure_reasons.append("known_face_count_not_positive")
    if not face_frame_alignment.get("sink_first_pts_present"):
        failure_reasons.append("sink_first_pts_not_present")
    if int(face_frame_alignment.get("sink_frame_index_count") or 0) <= 0:
        failure_reasons.append("sink_frame_index_empty")
    if frame_anchored_face_count <= 0:
        failure_reasons.append("frame_anchored_face_count_not_positive")
    if face_overlay_status not in {"aligned_frame_based", "partial_frame_based"}:
        failure_reasons.append("face_overlay_status_not_frame_based")
    if not watchlist_bundle.get("viewer_annotations_ok"):
        failure_reasons.append("viewer_annotations_endpoint_not_200")
    for offset in known_offsets:
        if offset < 0 or (duration_ms > 0 and offset > duration_ms):
            failure_reasons.append("known_face_time_offset_outside_raw_clip_duration")
            break
    if known_face_count > 0 and not basis_distribution:
        failure_reasons.append("missing_known_face_time_basis_distribution")
    if any(key not in {"frame_num", "frame_pts"} for key in basis_distribution):
        failure_reasons.append("known_face_time_basis_not_frame_domain")
    if face_timestamp_estimated_count > 0:
        failure_reasons.append("face_timestamp_estimated_count_positive")
if overlay_debug_failures:
    failure_reasons.extend(overlay_debug_failures)

if watchlist_bundle and any(key == "timestamp_estimated" for key in basis_distribution):
    result_marker = FAIL_TIMESTAMP
elif face_timestamp_estimated_count > 0:
    result_marker = FAIL_TIMESTAMP
elif watchlist_bundle and not basis_distribution and known_face_count > 0:
    result_marker = FAIL_WRONG_DOMAIN
elif failure_reasons:
    result_marker = FAIL_RUNTIME
elif int(basis_distribution.get("frame_num") or 0) > 0:
    result_marker = PASS_MARKER
elif int(basis_distribution.get("frame_pts") or 0) > 0:
    result_marker = PARTIAL_FRAME_PTS
else:
    result_marker = FAIL_WRONG_DOMAIN

summary = {
    "schema_version": "c1i2i.face_frame_domain_alignment.v1",
    "result_marker": result_marker,
    "validation_start": VALIDATION_START,
    "validation_start_epoch": VALIDATION_START_EPOCH,
    "validation_end": datetime.now(timezone.utc).isoformat(),
    "duration_seconds": DURATION_SECONDS,
    "doctor_status": DOCTOR_STATUS,
    "compose_config_status": COMPOSE_CONFIG_STATUS,
    "reload_status": RELOAD_STATUS,
    "viewer_endpoint_status": viewer_root.get("status"),
    "viewer_endpoint_ok": bool(viewer_root.get("ok")),
    "watchlist_bundle_id": watchlist_bundle.get("bundle_id", "") if watchlist_bundle else "",
    "watchlist_bundle_dir": watchlist_bundle.get("bundle_dir", "") if watchlist_bundle else "",
    "raw_clip_duration_ms": duration_ms,
    "known_face_count": known_face_count,
    "face_objects": face_objects,
    "known_face_time_offsets_ms": known_offsets,
    "known_face_time_basis_distribution": basis_distribution,
    "face_overlay_time_alignment_status": face_overlay_status,
    "frame_anchored_face_count": frame_anchored_face_count,
    "face_frame_alignment": {
        "sink_first_pts_present": bool(face_frame_alignment.get("sink_first_pts_present")),
        "sink_frame_index_count": int(face_frame_alignment.get("sink_frame_index_count") or 0),
        "face_frame_num_present_count": int(face_frame_alignment.get("face_frame_num_present_count") or 0),
        "face_frame_num_matched_count": int(face_frame_alignment.get("face_frame_num_matched_count") or 0),
        "face_frame_pts_present_count": int(face_frame_alignment.get("face_frame_pts_present_count") or 0),
        "face_frame_pts_fallback_count": int(face_frame_alignment.get("face_frame_pts_fallback_count") or 0),
        "face_timestamp_estimated_count": face_timestamp_estimated_count,
        "frame_anchored_face_count": int(face_frame_alignment.get("frame_anchored_face_count") or 0),
    },
    "face_timestamp_estimated_reason": nested_get(
        watchlist_bundle.get("summary", {}) if watchlist_bundle else {},
        "face_timestamp_estimated_reason",
    ),
    "face_observation_query_mode": (
        watchlist_bundle.get("face_observation_query_mode", "")
        if watchlist_bundle
        else ""
    ),
    "cross_day_result_count": (
        int(watchlist_bundle.get("cross_day_result_count") or 0)
        if watchlist_bundle
        else 0
    ),
    "known_face_bbox_source_distribution": (
        watchlist_bundle.get("known_face_bbox_source_distribution", {})
        if watchlist_bundle
        else {}
    ),
    "face_bbox_format": (
        watchlist_bundle.get("face_bbox_format", {})
        if watchlist_bundle
        else {}
    ),
    "face_track_semantics": (
        watchlist_bundle.get("face_track_semantics", {})
        if watchlist_bundle
        else {}
    ),
    "overlay_debug_dir": overlay_debug_dir,
    "overlay_debug_frames": overlay_debug_frames,
    "overlay_debug_failures": overlay_debug_failures,
    "failure_reasons": failure_reasons,
}
SUMMARY_JSON.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

print(json.dumps({
    "result_marker": result_marker,
    "watchlist_bundle_id": summary["watchlist_bundle_id"],
    "raw_clip_duration_ms": duration_ms,
    "known_face_count": known_face_count,
    "known_face_time_basis_distribution": basis_distribution,
    "known_face_time_offsets_ms": known_offsets,
    "face_overlay_time_alignment_status": face_overlay_status,
    "face_timestamp_estimated_count": face_timestamp_estimated_count,
    "overlay_debug_dir": overlay_debug_dir,
    "failure_reasons": failure_reasons,
}, indent=2, sort_keys=True))

if result_marker in {PASS_MARKER, PARTIAL_FRAME_PTS}:
    raise SystemExit(0)
raise SystemExit(1)
PY

log "summary=${SUMMARY_JSON}"
