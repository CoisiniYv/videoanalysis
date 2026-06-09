#!/usr/bin/env bash
# C1I - evidence visual QA baseline before algorithm completion work.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/infra/docker-compose.c1-official-replay-dev.yml}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/data/video-analytics/artifacts/c1i_baseline}"
SUMMARY_JSON="${SUMMARY_JSON:-${ARTIFACT_DIR}/evidence_visual_baseline_summary.json}"
REPORT_MD="${REPORT_MD:-${ARTIFACT_DIR}/evidence_visual_baseline_report.md}"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
VIEWER_URL="${VIEWER_URL:-http://localhost:8090}"
DURATION_SECONDS="${DURATION_SECONDS:-600}"
POLL_SECONDS="${POLL_SECONDS:-15}"

mkdir -p "${ARTIFACT_DIR}"

log() {
  printf '[c1i-baseline] %s\n' "$*"
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
if docker compose -f "${COMPOSE_FILE}" up -d --build --force-recreate \
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
REPORT_MD="${REPORT_MD}" \
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
import subprocess
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
REPORT_MD = Path(os.environ["REPORT_MD"])
EVIDENCE_ROOT = Path(os.environ["EVIDENCE_ROOT"])
VIEWER_URL = os.environ["VIEWER_URL"].rstrip("/")
DURATION_SECONDS = int(os.environ["DURATION_SECONDS"])
POLL_SECONDS = max(int(os.environ["POLL_SECONDS"]), 1)
VALIDATION_START = os.environ["VALIDATION_START"]
VALIDATION_START_EPOCH = float(os.environ["VALIDATION_START_EPOCH"])
DOCTOR_STATUS = os.environ["DOCTOR_STATUS"]
COMPOSE_CONFIG_STATUS = os.environ["COMPOSE_CONFIG_STATUS"]
RELOAD_STATUS = os.environ["RELOAD_STATUS"]

PASS_MARKER = "PASS_C1I_EVIDENCE_VISUAL_BASELINE_READY"
FAIL_FACE_OVERCOUNT = "FAIL_C1I_BASELINE_FACE_OVERCOUNT"
FAIL_FACE_TIME = "FAIL_C1I_BASELINE_FACE_TIME_ESTIMATED"
FAIL_PERSON_SPATIAL = "FAIL_C1I_BASELINE_PERSON_SPATIAL"
PARTIAL_MISSING = "PARTIAL_C1I_BASELINE_MISSING_EVENT_TYPE"
FAIL_RUNTIME = "FAIL_C1I_BASELINE_RUNTIME_CONTRACT"


def run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


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
    rows: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
    except Exception:
        return rows
    return rows


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
                "annotation_role",
                "track_id",
                "bbox",
                "identity",
                "label",
                "action",
                "style",
            )
            if key in record
        }
        if obj:
            objects.append(obj)
    return objects


def event_type(bundle: Path, metadata: dict[str, Any], summary: dict[str, Any]) -> str:
    return str(
        summary.get("event_type")
        or nested_get(metadata, "event", "event_type")
        or metadata.get("event_type")
        or ""
    )


def bundle_mtime(bundle: Path) -> float:
    mtimes = [bundle.stat().st_mtime]
    for name in ("summary.json", "metadata.json", "annotations.jsonl", "raw_clip.mov", "raw_clip.mp4"):
        path = bundle / name
        if path.exists():
            mtimes.append(path.stat().st_mtime)
    return max(mtimes)


def raw_clip_duration_ms(metadata: dict[str, Any], summary: dict[str, Any]) -> int:
    for value in (
        summary.get("raw_clip_duration"),
        nested_get(metadata, "media", "raw_clip_duration"),
        nested_get(metadata, "media", "clip_validation", "duration_seconds"),
        nested_get(summary, "time_anchor", "actual_clip_duration_ms"),
    ):
        try:
            if value is not None:
                parsed = float(value)
                return int(parsed * 1000) if parsed < 1000 else int(parsed)
        except (TypeError, ValueError):
            continue
    return 0


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


def bbox_xyxy(bbox: Any) -> list[float] | None:
    if not isinstance(bbox, dict):
        return None
    values = bbox.get("xyxy") or bbox.get("values") or bbox.get("bbox")
    fmt = str(bbox.get("format") or "xyxy").lower()
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
        return [cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2]
    return nums


def person_out_of_frame(records: list[dict[str, Any]], width: int = 1920, height: int = 1080) -> int:
    count = 0
    for record in records:
        for obj in objects_for_record(record):
            if obj.get("object_type") != "person":
                continue
            xyxy = bbox_xyxy(obj.get("bbox"))
            if xyxy is None:
                count += 1
                continue
            x1, y1, x2, y2 = xyxy
            if x1 < -2 or y1 < -2 or x2 > width + 2 or y2 > height + 2:
                count += 1
    return count


def analyze_bundle(bundle: Path) -> dict[str, Any]:
    metadata = load_json(bundle / "metadata.json")
    summary = load_json(bundle / "summary.json")
    records = load_jsonl(bundle / "annotations.jsonl")
    et = event_type(bundle, metadata, summary)
    face_count = 0
    known_face_count = 0
    unknown_face_count = 0
    known_face_sources: Counter[str] = Counter()
    face_basis: Counter[str] = Counter()
    person_sources: Counter[str] = Counter()
    behavior_event_count = 0
    person_context_count = 0
    annotation_status = str(summary.get("annotation_status") or "")
    for record in records:
        basis = str(record.get("time_basis") or "")
        for obj in objects_for_record(record):
            if obj.get("object_type") == "face":
                face_count += 1
                face_basis[basis] += 1
                bbox = obj.get("bbox") if isinstance(obj.get("bbox"), dict) else {}
                if is_known_face(obj):
                    known_face_count += 1
                    known_face_sources[str(bbox.get("source") or "")] += 1
                else:
                    unknown_face_count += 1
            elif obj.get("object_type") == "person":
                bbox = obj.get("bbox") if isinstance(obj.get("bbox"), dict) else {}
                source = str(bbox.get("source") or "")
                if source:
                    person_sources[source] += 1
                role = str(obj.get("annotation_role") or record.get("annotation_role") or "")
                label = obj.get("label") if isinstance(obj.get("label"), dict) else {}
                action = obj.get("action") if isinstance(obj.get("action"), dict) else {}
                if (
                    role == "behavior_event"
                    or label.get("kind") == "behavior_event"
                    or action.get("event_type") == "intrusion"
                ):
                    behavior_event_count += 1
                elif role == "person_context":
                    person_context_count += 1
    face_frame_alignment = summary.get("face_frame_alignment")
    if not isinstance(face_frame_alignment, dict):
        face_frame_alignment = {}
    query = summary.get("face_observation_query")
    if not isinstance(query, dict):
        query = {}
    if not face_basis and isinstance(summary.get("known_face_time_basis_distribution"), dict):
        face_basis.update(summary["known_face_time_basis_distribution"])
    if not person_sources and isinstance(summary.get("person_bbox_source_distribution"), dict):
        person_sources.update(summary["person_bbox_source_distribution"])
    if person_context_count == 0:
        person_context_count = int(summary.get("person_context_count") or 0)
    return {
        "bundle_id": bundle.name,
        "bundle_dir": str(bundle),
        "event_type": et,
        "post_validation": bundle_mtime(bundle) >= VALIDATION_START_EPOCH,
        "mtime": bundle_mtime(bundle),
        "annotation_status": annotation_status,
        "raw_clip_duration_ms": raw_clip_duration_ms(metadata, summary),
        "effective_pre_seconds": summary.get("effective_pre_seconds"),
        "effective_post_seconds": summary.get("effective_post_seconds"),
        "known_face_count": int(summary.get("known_face_count") or known_face_count),
        "unknown_face_count": int(summary.get("unknown_face_count") or unknown_face_count),
        "face_annotation_count": int(summary.get("face_objects") or face_count),
        "face_time_basis_distribution": dict(face_basis),
        "face_timestamp_estimated_count": int(
            summary.get("face_timestamp_estimated_count")
            or face_frame_alignment.get("face_timestamp_estimated_count")
            or 0
        ),
        "face_query_mode": str(query.get("mode") or ""),
        "cross_day_result_count": int(query.get("cross_day_result_count") or 0),
        "known_face_bbox_source_distribution": dict(
            summary.get("known_face_bbox_source_distribution") or known_face_sources
        ),
        "person_context_count": person_context_count,
        "behavior_event_count": behavior_event_count,
        "person_bbox_source_distribution": dict(person_sources),
        "person_bbox_out_of_frame_count": person_out_of_frame(records),
        "records": records,
    }


def scan_post_validation() -> list[dict[str, Any]]:
    if not EVIDENCE_ROOT.is_dir():
        return []
    bundles: list[dict[str, Any]] = []
    for bundle in EVIDENCE_ROOT.iterdir():
        if not bundle.is_dir() or not (bundle / "annotations.jsonl").is_file():
            continue
        analyzed = analyze_bundle(bundle)
        if analyzed["post_validation"] and analyzed["event_type"] in {"intrusion", "watchlist_hit"}:
            bundles.append(analyzed)
    return sorted(bundles, key=lambda item: item["mtime"], reverse=True)


def select_bundle(bundles: list[dict[str, Any]], event_type_name: str) -> dict[str, Any] | None:
    return next((item for item in bundles if item["event_type"] == event_type_name), None)


def generate_diagnostics(bundle: dict[str, Any] | None, object_name: str) -> str:
    if not bundle:
        return ""
    out_dir = ARTIFACT_DIR / "overlay_debug" / object_name / str(bundle["bundle_id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    diag_json = out_dir / "diagnose.json"
    report = out_dir / "diagnose.md"
    res = run(
        [
            "python3",
            str(ROOT / "scripts" / "tools" / "diagnose_overlay_alignment.py"),
            str(bundle["bundle_dir"]),
            "--object",
            object_name,
            "--max",
            "4",
            "--out",
            str(out_dir),
            "--json-out",
            str(diag_json),
            "--report-out",
            str(report),
        ],
        timeout=300,
    )
    (out_dir / "stdout.txt").write_text(res.stdout or "", encoding="utf-8")
    (out_dir / "stderr.txt").write_text(res.stderr or "", encoding="utf-8")
    return str(out_dir) if res.returncode == 0 else ""


deadline = time.monotonic() + DURATION_SECONDS
bundles: list[dict[str, Any]] = []
while time.monotonic() < deadline:
    bundles = scan_post_validation()
    intrusion = select_bundle(bundles, "intrusion")
    watchlist = select_bundle(bundles, "watchlist_hit")
    print(
        "[c1i-baseline] monitor "
        f"remaining_seconds={max(0, int(deadline - time.monotonic()))} "
        f"post_validation_bundles={len(bundles)} "
        f"intrusion={bool(intrusion)} watchlist={bool(watchlist)}",
        flush=True,
    )
    if intrusion and watchlist:
        break
    time.sleep(POLL_SECONDS)

bundles = scan_post_validation()
intrusion = select_bundle(bundles, "intrusion")
watchlist = select_bundle(bundles, "watchlist_hit")
viewer = fetch_text(f"{VIEWER_URL}/")
overlay_debug_dirs = [
    path
    for path in (
        generate_diagnostics(watchlist, "all"),
        generate_diagnostics(intrusion, "person"),
    )
    if path
]

failure_reasons: list[str] = []
partial_reasons: list[str] = []
if DOCTOR_STATUS != "ok":
    failure_reasons.append("doctor_not_ok")
if COMPOSE_CONFIG_STATUS != "ok":
    failure_reasons.append("compose_config_not_ok")
if RELOAD_STATUS != "ok":
    failure_reasons.append("reload_not_ok")
if not viewer.get("ok"):
    failure_reasons.append("viewer_endpoint_not_200")
if not intrusion or not watchlist:
    partial_reasons.append("missing_intrusion_or_watchlist")

for bundle in (intrusion, watchlist):
    if not bundle:
        continue
    if bundle["raw_clip_duration_ms"] <= 0 or bundle["raw_clip_duration_ms"] > 15_000:
        failure_reasons.append(f"{bundle['event_type']}_raw_clip_duration_outside_15s")
    if float(bundle.get("effective_pre_seconds") or -1) != 5.0:
        failure_reasons.append(f"{bundle['event_type']}_effective_pre_not_5")
    if float(bundle.get("effective_post_seconds") or -1) != 5.0:
        failure_reasons.append(f"{bundle['event_type']}_effective_post_not_5")
    if bundle.get("annotation_status") != "complete":
        failure_reasons.append(f"{bundle['event_type']}_annotation_status_not_complete")

if watchlist:
    if int(watchlist["face_annotation_count"]) >= 200:
        failure_reasons.append("face_annotation_count_over_200")
    if watchlist["face_query_mode"] in {"timestamp_only_debug_fallback", "timestamp_only"}:
        failure_reasons.append("face_query_mode_timestamp_only")
    if int(watchlist["cross_day_result_count"]) != 0:
        failure_reasons.append("cross_day_result_count_not_zero")
    if int(watchlist["known_face_count"]) <= 0:
        failure_reasons.append("known_face_count_not_positive")
    if "observation.face_bbox" not in watchlist["known_face_bbox_source_distribution"]:
        failure_reasons.append("known_face_bbox_source_not_observation_face_bbox")
    if int(watchlist["face_timestamp_estimated_count"]) != 0:
        failure_reasons.append("face_timestamp_estimated_count_not_zero")
    face_basis_keys = set(watchlist["face_time_basis_distribution"])
    if face_basis_keys and not face_basis_keys.issubset({"frame_pts", "frame_num"}):
        failure_reasons.append("face_time_basis_not_frame_based")

person_context_count = 0
person_source_distribution: Counter[str] = Counter()
person_oob_count = 0
behavior_event_count = 0
for bundle in (intrusion, watchlist):
    if not bundle:
        continue
    person_context_count += int(bundle["person_context_count"] or 0)
    behavior_event_count += int(bundle["behavior_event_count"] or 0)
    person_oob_count += int(bundle["person_bbox_out_of_frame_count"] or 0)
    person_source_distribution.update(bundle["person_bbox_source_distribution"])

if person_context_count <= 0:
    failure_reasons.append("person_context_count_not_positive")
if "person_bbox_observations.person_bbox" not in person_source_distribution:
    failure_reasons.append("missing_person_bbox_observations_source")
if person_oob_count > 0:
    failure_reasons.append("person_bbox_out_of_frame_count_positive")
if not overlay_debug_dirs:
    failure_reasons.append("overlay_diagnostics_not_generated")

if any(reason == "face_annotation_count_over_200" for reason in failure_reasons):
    result_marker = FAIL_FACE_OVERCOUNT
elif any(reason == "face_timestamp_estimated_count_not_zero" for reason in failure_reasons):
    result_marker = FAIL_FACE_TIME
elif person_oob_count > 0:
    result_marker = FAIL_PERSON_SPATIAL
elif partial_reasons:
    result_marker = PARTIAL_MISSING
elif failure_reasons:
    result_marker = FAIL_RUNTIME
else:
    result_marker = PASS_MARKER

annotation_status_distribution = Counter(
    str(item.get("annotation_status") or "") for item in bundles
)
summary = {
    "schema_version": "c1i.evidence_visual_baseline.v1",
    "result_marker": result_marker,
    "validation_start": VALIDATION_START,
    "validation_start_epoch": VALIDATION_START_EPOCH,
    "validation_end": datetime.now(timezone.utc).isoformat(),
    "duration_seconds": DURATION_SECONDS,
    "doctor_status": DOCTOR_STATUS,
    "compose_config_status": COMPOSE_CONFIG_STATUS,
    "reload_status": RELOAD_STATUS,
    "intrusion_bundle_id": intrusion["bundle_id"] if intrusion else "",
    "watchlist_bundle_id": watchlist["bundle_id"] if watchlist else "",
    "intrusion_raw_clip_duration_ms": intrusion["raw_clip_duration_ms"] if intrusion else 0,
    "watchlist_raw_clip_duration_ms": watchlist["raw_clip_duration_ms"] if watchlist else 0,
    "effective_pre_seconds": watchlist.get("effective_pre_seconds") if watchlist else None,
    "effective_post_seconds": watchlist.get("effective_post_seconds") if watchlist else None,
    "known_face_count": watchlist["known_face_count"] if watchlist else 0,
    "unknown_face_count": watchlist["unknown_face_count"] if watchlist else 0,
    "face_annotation_count": watchlist["face_annotation_count"] if watchlist else 0,
    "face_time_basis_distribution": watchlist["face_time_basis_distribution"] if watchlist else {},
    "face_timestamp_estimated_count": watchlist["face_timestamp_estimated_count"] if watchlist else 0,
    "face_query_mode": watchlist["face_query_mode"] if watchlist else "",
    "cross_day_result_count": watchlist["cross_day_result_count"] if watchlist else 0,
    "known_face_bbox_source_distribution": watchlist["known_face_bbox_source_distribution"] if watchlist else {},
    "person_context_count": person_context_count,
    "behavior_event_count": behavior_event_count,
    "person_bbox_source_distribution": dict(person_source_distribution),
    "person_bbox_out_of_frame_count": person_oob_count,
    "annotation_status_distribution": dict(annotation_status_distribution),
    "viewer_status": viewer.get("status"),
    "viewer_ok": bool(viewer.get("ok")),
    "overlay_debug_dirs": overlay_debug_dirs,
    "failure_reasons": failure_reasons,
    "partial_reasons": partial_reasons,
}
SUMMARY_JSON.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
REPORT_MD.write_text(
    "\n".join(
        [
            "# C1I Evidence Visual Baseline",
            "",
            f"- result_marker: `{result_marker}`",
            f"- duration_seconds: `{DURATION_SECONDS}`",
            f"- intrusion_bundle_id: `{summary['intrusion_bundle_id']}`",
            f"- watchlist_bundle_id: `{summary['watchlist_bundle_id']}`",
            f"- intrusion_raw_clip_duration_ms: `{summary['intrusion_raw_clip_duration_ms']}`",
            f"- watchlist_raw_clip_duration_ms: `{summary['watchlist_raw_clip_duration_ms']}`",
            f"- known_face_count: `{summary['known_face_count']}`",
            f"- face_annotation_count: `{summary['face_annotation_count']}`",
            f"- face_query_mode: `{summary['face_query_mode']}`",
            f"- person_context_count: `{summary['person_context_count']}`",
            f"- person_bbox_out_of_frame_count: `{summary['person_bbox_out_of_frame_count']}`",
            f"- viewer_status: `{summary['viewer_status']}`",
            "",
            "## Summary",
            "",
            "```json",
            json.dumps(summary, indent=2, sort_keys=True),
            "```",
            "",
        ]
    ),
    encoding="utf-8",
)
print(json.dumps({
    "result_marker": result_marker,
    "intrusion_bundle_id": summary["intrusion_bundle_id"],
    "watchlist_bundle_id": summary["watchlist_bundle_id"],
    "known_face_count": summary["known_face_count"],
    "face_annotation_count": summary["face_annotation_count"],
    "face_time_basis_distribution": summary["face_time_basis_distribution"],
    "person_context_count": summary["person_context_count"],
    "person_bbox_source_distribution": summary["person_bbox_source_distribution"],
    "overlay_debug_dirs": summary["overlay_debug_dirs"],
    "failure_reasons": failure_reasons,
    "partial_reasons": partial_reasons,
}, indent=2, sort_keys=True))
if result_marker == PASS_MARKER:
    raise SystemExit(0)
if result_marker == PARTIAL_MISSING:
    raise SystemExit(2)
raise SystemExit(1)
PY

log "summary=${SUMMARY_JSON}"
log "report=${REPORT_MD}"
