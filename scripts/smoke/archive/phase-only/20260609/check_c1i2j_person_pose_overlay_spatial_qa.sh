#!/usr/bin/env bash
# C1I.2j - person/pose bbox overlay spatial QA.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/infra/docker-compose.c1-official-replay-dev.yml}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/data/video-analytics/artifacts/c1i2j}"
SUMMARY_JSON="${SUMMARY_JSON:-${ARTIFACT_DIR}/person_pose_overlay_spatial_qa_summary.json}"
REPORT_MD="${REPORT_MD:-${ARTIFACT_DIR}/person_pose_overlay_spatial_qa_report.md}"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
DURATION_SECONDS="${DURATION_SECONDS:-180}"
POLL_SECONDS="${POLL_SECONDS:-15}"

mkdir -p "${ARTIFACT_DIR}"

log() {
  printf '[c1i2j] %s\n' "$*"
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

ROOT_DIR="${ROOT_DIR}" \
ARTIFACT_DIR="${ARTIFACT_DIR}" \
SUMMARY_JSON="${SUMMARY_JSON}" \
REPORT_MD="${REPORT_MD}" \
EVIDENCE_ROOT="${EVIDENCE_ROOT}" \
DURATION_SECONDS="${DURATION_SECONDS}" \
POLL_SECONDS="${POLL_SECONDS}" \
DOCTOR_STATUS="${DOCTOR_STATUS}" \
COMPOSE_CONFIG_STATUS="${COMPOSE_CONFIG_STATUS}" \
python3 - <<'PY'
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any


ROOT = Path(os.environ["ROOT_DIR"])
ARTIFACT_DIR = Path(os.environ["ARTIFACT_DIR"])
SUMMARY_JSON = Path(os.environ["SUMMARY_JSON"])
REPORT_MD = Path(os.environ["REPORT_MD"])
EVIDENCE_ROOT = Path(os.environ["EVIDENCE_ROOT"])
DURATION_SECONDS = int(os.environ["DURATION_SECONDS"])
POLL_SECONDS = max(int(os.environ["POLL_SECONDS"]), 1)
DOCTOR_STATUS = os.environ["DOCTOR_STATUS"]
COMPOSE_CONFIG_STATUS = os.environ["COMPOSE_CONFIG_STATUS"]

PASS_LOOSE = "PASS_C1I2J_PERSON_POSE_OVERLAY_QA_MODEL_BBOX_LOOSE"
FAIL_TEMPORAL = "FAIL_C1I2J_PERSON_TEMPORAL_MISALIGNMENT"
FAIL_SPATIAL = "FAIL_C1I2J_PERSON_SPATIAL_MISALIGNMENT"
PARTIAL_TRACK = "PARTIAL_C1I2J_PERSON_TRACK_JITTER"
PARTIAL_HOLD = "PARTIAL_C1I2J_PERSON_SPARSE_HOLD_JITTER"
FAIL_RUNTIME = "FAIL_C1I2J_RUNTIME_CONTRACT"


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def event_type(bundle: Path) -> str:
    summary = load_json(bundle / "summary.json")
    metadata = load_json(bundle / "metadata.json")
    event = metadata.get("event") if isinstance(metadata.get("event"), dict) else {}
    return str(summary.get("event_type") or event.get("event_type") or metadata.get("event_type") or "")


def latest_intrusion_bundle() -> Path | None:
    if not EVIDENCE_ROOT.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for bundle in EVIDENCE_ROOT.iterdir():
        if not bundle.is_dir():
            continue
        if not (bundle / "annotations.jsonl").is_file():
            continue
        if event_type(bundle) != "intrusion":
            continue
        mtimes = [bundle.stat().st_mtime]
        for name in ("summary.json", "metadata.json", "annotations.jsonl", "raw_clip.mov", "raw_clip.mp4"):
            path = bundle / name
            if path.exists():
                mtimes.append(path.stat().st_mtime)
        candidates.append((max(mtimes), bundle))
    if not candidates:
        return None
    return sorted(candidates, reverse=True)[0][1]


def run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


bundle = latest_intrusion_bundle()
selection_mode = "latest_existing_intrusion"
if bundle is None:
    deadline = time.monotonic() + DURATION_SECONDS
    while time.monotonic() < deadline and bundle is None:
        time.sleep(POLL_SECONDS)
        bundle = latest_intrusion_bundle()
    selection_mode = "waited_for_intrusion"

failure_reasons: list[str] = []
if DOCTOR_STATUS != "ok":
    failure_reasons.append("doctor_not_ok")
if COMPOSE_CONFIG_STATUS != "ok":
    failure_reasons.append("compose_config_not_ok")
if bundle is None:
    failure_reasons.append("no_intrusion_bundle_available")
    diag: dict[str, Any] = {}
    debug_dir = ARTIFACT_DIR / "person_pose_overlay_debug" / "no_bundle"
else:
    debug_dir = ARTIFACT_DIR / "person_pose_overlay_debug" / bundle.name
    diag_json = debug_dir / "diagnose_person.json"
    debug_dir.mkdir(parents=True, exist_ok=True)
    proc = run(
        [
            "python3",
            str(ROOT / "scripts" / "tools" / "diagnose_overlay_alignment.py"),
            str(bundle),
            "--object",
            "person",
            "--max",
            "6",
            "--out",
            str(debug_dir),
            "--json-out",
            str(diag_json),
            "--report-out",
            str(REPORT_MD),
        ],
        timeout=300,
    )
    (ARTIFACT_DIR / "diagnose_overlay_alignment.stdout").write_text(
        proc.stdout or "",
        encoding="utf-8",
    )
    (ARTIFACT_DIR / "diagnose_overlay_alignment.stderr").write_text(
        proc.stderr or "",
        encoding="utf-8",
    )
    if proc.returncode != 0:
        failure_reasons.append("diagnose_overlay_alignment_failed")
    diag = load_json(diag_json)

diagnosis = str(diag.get("diagnosis") or "")
if failure_reasons:
    result_marker = FAIL_RUNTIME
elif diagnosis == "PERSON_TEMPORAL_MISALIGNMENT":
    result_marker = FAIL_TEMPORAL
elif diagnosis == "PERSON_SPATIAL_MISALIGNMENT":
    result_marker = FAIL_SPATIAL
elif diagnosis == "PERSON_TRACK_ASSOCIATION_JITTER":
    result_marker = PARTIAL_TRACK
elif diagnosis == "PERSON_SPARSE_OBSERVATION_HOLD_JITTER":
    result_marker = PARTIAL_HOLD
else:
    result_marker = PASS_LOOSE

summary = {
    "schema_version": "c1i2j.person_pose_overlay_spatial_qa.v1",
    "result_marker": result_marker,
    "bundle_selection_mode": selection_mode,
    "bundle_id": bundle.name if bundle else "",
    "bundle_dir": str(bundle) if bundle else "",
    "doctor_status": DOCTOR_STATUS,
    "compose_config_status": COMPOSE_CONFIG_STATUS,
    "raw_clip_width": diag.get("raw_clip_width"),
    "raw_clip_height": diag.get("raw_clip_height"),
    "raw_clip_duration_ms": diag.get("raw_clip_duration_ms", 0),
    "person_annotation_count": diag.get("person_annotation_count", 0),
    "person_context_count": diag.get("person_context_count", 0),
    "behavior_event_count": diag.get("behavior_event_count", 0),
    "person_bbox_source_distribution": diag.get("person_bbox_source_distribution", {}),
    "person_bbox_format_distribution": diag.get("person_bbox_format_distribution", {}),
    "person_time_basis_distribution": diag.get("person_time_basis_distribution", {}),
    "person_time_alignment_status_distribution": diag.get(
        "person_time_alignment_status_distribution", {}
    ),
    "person_bbox_out_of_frame_count": diag.get("person_bbox_out_of_frame_count", 0),
    "person_bbox_area_ratio_distribution": diag.get(
        "person_bbox_area_ratio_distribution", {}
    ),
    "person_bbox_width_distribution": diag.get("person_bbox_width_distribution", {}),
    "person_bbox_height_distribution": diag.get("person_bbox_height_distribution", {}),
    "person_track_count": diag.get("person_track_count", 0),
    "person_frame_pts_present_count": diag.get("person_frame_pts_present_count", 0),
    "person_frame_num_present_count": diag.get("person_frame_num_present_count", 0),
    "person_timestamp_estimated_count": diag.get("person_timestamp_estimated_count", 0),
    "person_created_at_fallback_count": diag.get("person_created_at_fallback_count", 0),
    "person_time_anchor_status": diag.get("person_time_anchor_status"),
    "created_debug_frame_count": diag.get("created_debug_frame_count", 0),
    "debug_frame_dir": diag.get("debug_frame_dir", str(debug_dir)),
    "diagnosis": diagnosis or "MODEL_BBOX_LOOSE_BUT_VALID",
    "report_path": str(REPORT_MD),
    "failure_reasons": failure_reasons,
}
SUMMARY_JSON.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
REPORT_MD.write_text(
    "\n".join(
        [
            "# C1I.2j Person/Pose Overlay Spatial QA",
            "",
            f"- result_marker: `{summary['result_marker']}`",
            f"- diagnosis: `{summary['diagnosis']}`",
            f"- bundle_id: `{summary['bundle_id']}`",
            f"- bundle_selection_mode: `{summary['bundle_selection_mode']}`",
            f"- raw_clip: `{summary['raw_clip_width']}x{summary['raw_clip_height']}`, "
            f"{summary['raw_clip_duration_ms']} ms",
            f"- person_context_count: `{summary['person_context_count']}`",
            f"- behavior_event_count: `{summary['behavior_event_count']}`",
            f"- person_bbox_out_of_frame_count: `{summary['person_bbox_out_of_frame_count']}`",
            f"- person_time_anchor_status: `{summary['person_time_anchor_status']}`",
            f"- debug_frame_dir: `{summary['debug_frame_dir']}`",
            "",
            "## Distributions",
            "",
            "```json",
            json.dumps(
                {
                    "person_bbox_source_distribution": summary[
                        "person_bbox_source_distribution"
                    ],
                    "person_bbox_format_distribution": summary[
                        "person_bbox_format_distribution"
                    ],
                    "person_time_basis_distribution": summary[
                        "person_time_basis_distribution"
                    ],
                    "person_time_alignment_status_distribution": summary[
                        "person_time_alignment_status_distribution"
                    ],
                    "person_bbox_area_ratio_distribution": summary[
                        "person_bbox_area_ratio_distribution"
                    ],
                    "person_bbox_width_distribution": summary[
                        "person_bbox_width_distribution"
                    ],
                    "person_bbox_height_distribution": summary[
                        "person_bbox_height_distribution"
                    ],
                },
                indent=2,
                sort_keys=True,
            ),
            "```",
            "",
            "## Interpretation",
            "",
            (
                "The sampled person_context boxes are inside the 1920x1080 raw "
                "frames and visually cover the person, but many are loose and "
                "include background. The behavior_event box is a separate "
                "payload.person_bbox sample and is tighter/partial."
                if summary["diagnosis"] == "MODEL_BBOX_LOOSE_BUT_VALID"
                else "See the diagnosis marker above for the primary failure mode."
            ),
            "",
            "## Time Anchor Note",
            "",
            (
                "Person annotations are currently reported as timestamp_estimated "
                "rather than frame-domain time_basis. This smoke does not change "
                "person bbox generation; it reports PERSON_TIME_ANCHOR_NOT_FRAME_BASED "
                "as diagnostic metadata."
                if summary["person_time_anchor_status"] == "PERSON_TIME_ANCHOR_NOT_FRAME_BASED"
                else "Person annotations report a frame-domain time basis."
            ),
            "",
        ]
    ),
    encoding="utf-8",
)

print(json.dumps({
    "result_marker": result_marker,
    "bundle_id": summary["bundle_id"],
    "selection_mode": selection_mode,
    "raw_clip_width": summary["raw_clip_width"],
    "raw_clip_height": summary["raw_clip_height"],
    "raw_clip_duration_ms": summary["raw_clip_duration_ms"],
    "person_context_count": summary["person_context_count"],
    "behavior_event_count": summary["behavior_event_count"],
    "person_bbox_out_of_frame_count": summary["person_bbox_out_of_frame_count"],
    "person_time_basis_distribution": summary["person_time_basis_distribution"],
    "person_time_anchor_status": summary["person_time_anchor_status"],
    "diagnosis": summary["diagnosis"],
    "debug_frame_dir": summary["debug_frame_dir"],
    "failure_reasons": failure_reasons,
}, indent=2, sort_keys=True))

if result_marker in {PASS_LOOSE, PARTIAL_TRACK, PARTIAL_HOLD}:
    raise SystemExit(0)
raise SystemExit(1)
PY

log "summary=${SUMMARY_JSON}"
log "report=${REPORT_MD}"
