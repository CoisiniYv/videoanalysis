#!/usr/bin/env bash
# C1G.2c evidence bundle boundary and annotation integrity smoke.

set -Eeuo pipefail

EVIDENCE_ROOT="${EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/data/video-analytics/artifacts/c1g2c}"
SUMMARY_JSON="${SUMMARY_JSON:-${ARTIFACT_DIR}/evidence_bundle_integrity_summary.json}"
TARGET_BUNDLE_ID="${TARGET_BUNDLE_ID:-6208d5a0-0bc8-4660-9e09-b9309f12e67d}"
RECENT_LIMIT="${RECENT_LIMIT:-50}"
EVIDENCE_MAX_DURATION_SLACK_SEC="${EVIDENCE_MAX_DURATION_SLACK_SEC:-10}"
DEFAULT_PRE_SECONDS="${DEFAULT_PRE_SECONDS:-5}"
DEFAULT_POST_SECONDS="${DEFAULT_POST_SECONDS:-10}"

mkdir -p "${ARTIFACT_DIR}"

EVIDENCE_ROOT="${EVIDENCE_ROOT}" \
SUMMARY_JSON="${SUMMARY_JSON}" \
TARGET_BUNDLE_ID="${TARGET_BUNDLE_ID}" \
RECENT_LIMIT="${RECENT_LIMIT}" \
EVIDENCE_MAX_DURATION_SLACK_SEC="${EVIDENCE_MAX_DURATION_SLACK_SEC}" \
DEFAULT_PRE_SECONDS="${DEFAULT_PRE_SECONDS}" \
DEFAULT_POST_SECONDS="${DEFAULT_POST_SECONDS}" \
python3 - <<'PY'
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        return {"_parse_error": str(exc)}


def first_jsonl(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                data = json.loads(raw)
                return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def count_jsonl_records(path: Path) -> int:
    if not path.is_file():
        return 0
    count = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                if raw.strip():
                    count += 1
    except Exception:
        return 0
    return count


def raw_clip_path(bundle_dir: Path, metadata: dict[str, Any]) -> Path | None:
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
    raw_from_meta = media.get("raw_clip_path") if isinstance(media, dict) else None
    if isinstance(raw_from_meta, str) and raw_from_meta:
        candidate = bundle_dir / Path(raw_from_meta).name
        if candidate.is_file():
            return candidate
    for suffix in ("mov", "mp4", "mkv", "webm"):
        candidate = bundle_dir / f"raw_clip.{suffix}"
        if candidate.is_file():
            return candidate
    for candidate in sorted(bundle_dir.glob("raw_clip.*")):
        if candidate.is_file():
            return candidate
    return None


def ffprobe_duration(path: Path | None, metadata: dict[str, Any]) -> tuple[float | None, str]:
    if path is None:
        return None, "missing_raw_clip"
    try:
        out = subprocess.check_output(
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
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        ).strip()
        if out:
            return float(out), "ffprobe"
    except Exception:
        pass
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
    duration = media.get("raw_clip_duration") if isinstance(media, dict) else None
    try:
        return float(duration), "metadata"
    except (TypeError, ValueError):
        return None, "unavailable"


def stop_condition_expected(metadata: dict[str, Any]) -> float | None:
    replay = metadata.get("replay") if isinstance(metadata.get("replay"), dict) else {}
    stop = replay.get("stop_condition") if isinstance(replay, dict) else {}
    if not isinstance(stop, dict):
        return None
    ts_delta = stop.get("ts_delta_sec")
    if isinstance(ts_delta, dict):
        try:
            return float(ts_delta.get("max_delta_sec"))
        except (TypeError, ValueError):
            return None
    try:
        frame_count = float(stop.get("frame_count"))
    except (TypeError, ValueError):
        return None
    if frame_count > 0:
        return frame_count / 30.0
    return None


def expected_duration(summary: dict[str, Any], metadata: dict[str, Any]) -> float:
    for source in (
        summary,
        metadata.get("media") if isinstance(metadata.get("media"), dict) else {},
    ):
        if not isinstance(source, dict):
            continue
        expected = source.get("expected_duration_seconds")
        try:
            value = float(expected)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    stop_expected = stop_condition_expected(metadata)
    if stop_expected is not None and stop_expected > 0:
        return stop_expected
    try:
        pre = float(summary.get("pre_seconds", os.environ["DEFAULT_PRE_SECONDS"]))
        post = float(summary.get("post_seconds", os.environ["DEFAULT_POST_SECONDS"]))
        return pre + post
    except (TypeError, ValueError):
        return float(os.environ["DEFAULT_PRE_SECONDS"]) + float(os.environ["DEFAULT_POST_SECONDS"])


def sink_pts(path: Path) -> dict[str, Any]:
    first: dict[str, Any] | None = None
    last: dict[str, Any] | None = None
    records = 0
    if not path.is_file():
        return {"records_count": 0}
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            records += 1
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if item.get("schema") == "VideoFrame":
                if first is None:
                    first = item
                last = item
    return {
        "records_count": records,
        "first_frame_pts": first.get("pts") if first else None,
        "last_frame_pts": last.get("pts") if last else None,
        "first_frame_num": first.get("frame_num") if first else None,
        "last_frame_num": last.get("frame_num") if last else None,
    }


def overlay_available(summary: dict[str, Any], metadata: dict[str, Any]) -> bool:
    if "overlay_available" in summary:
        return bool(summary.get("overlay_available"))
    annotations = (
        metadata.get("annotations")
        if isinstance(metadata.get("annotations"), dict)
        else {}
    )
    if "overlay_available" in annotations:
        return bool(annotations.get("overlay_available"))
    if "frontend_overlay_required" in summary:
        return bool(summary.get("frontend_overlay_required"))
    return bool(annotations.get("frontend_overlay_required"))


def bundle_diag(bundle_dir: Path, slack: float) -> dict[str, Any]:
    metadata = load_json(bundle_dir / "metadata.json")
    summary = load_json(bundle_dir / "summary.json")
    annotations = bundle_dir / "annotations.jsonl"
    raw = raw_clip_path(bundle_dir, metadata)
    duration, duration_source = ffprobe_duration(raw, metadata)
    expected = expected_duration(summary, metadata)
    max_allowed = expected + slack if expected > 0 else None
    event = metadata.get("event") if isinstance(metadata.get("event"), dict) else {}
    replay = metadata.get("replay") if isinstance(metadata.get("replay"), dict) else {}
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
    status = metadata.get("status") if isinstance(metadata.get("status"), dict) else {}
    clip_validation = (
        media.get("clip_validation") if isinstance(media.get("clip_validation"), dict) else {}
    )
    first_annotation = first_jsonl(annotations)
    sink = sink_pts(bundle_dir / "sink_metadata.json")
    annotation_size = annotations.stat().st_size if annotations.is_file() else None
    over_duration = (
        duration is not None and max_allowed is not None and duration > max_allowed
    )
    return {
        "bundle_id": bundle_dir.name,
        "bundle_creation_time": datetime.fromtimestamp(
            bundle_dir.stat().st_mtime, timezone.utc
        ).isoformat(),
        "event_id": event.get("event_id") or summary.get("event_id") or bundle_dir.name,
        "event_type": event.get("event_type") or summary.get("event_type"),
        "source_id": event.get("source_id") or summary.get("source_id"),
        "camera_id": event.get("camera_id") or summary.get("camera_id"),
        "event_ts_ms": event.get("event_ts_ms"),
        "frame_pts": media.get("frame_pts"),
        "keyframe_uuid": event.get("keyframe_uuid"),
        "replay_job_id": replay.get("replay_job_id"),
        "record_request_id": None,
        "evidence_task_id": None,
        "raw_clip_file_name": raw.name if raw else None,
        "raw_clip_size": raw.stat().st_size if raw and raw.is_file() else None,
        "raw_clip_duration": duration,
        "raw_clip_duration_source": duration_source,
        "expected_duration_seconds": expected,
        "max_allowed_duration_seconds": max_allowed,
        "duration_over_max": over_duration,
        "duration_guard_failed": bool(
            summary.get("duration_guard_failed")
            or clip_validation.get("duration_guard_failed")
            or status.get("clip_status") == "duration_guard_failed"
        ),
        "clip_status": status.get("clip_status") or summary.get("clip_status"),
        "metadata_clip_start_ts_ms": summary.get("clip_start_ts_ms"),
        "metadata_clip_end_ts_ms": summary.get("clip_end_ts_ms"),
        "pre_seconds": summary.get("pre_seconds"),
        "post_seconds": summary.get("post_seconds"),
        "summary_annotation_lines": summary.get("annotation_lines"),
        "summary_annotation_status": summary.get("annotation_status"),
        "summary_overlay_available": overlay_available(summary, metadata),
        "annotation_empty_reason": summary.get("annotation_empty_reason"),
        "annotation_unavailable_reason": summary.get("annotation_unavailable_reason"),
        "annotations_jsonl_size": annotation_size,
        "annotations_jsonl_records_count": count_jsonl_records(annotations),
        "annotations_first_record_type": first_annotation.get("record_type"),
        "sink_metadata_records_count": sink.get("records_count"),
        "sink_metadata_first_frame_pts": sink.get("first_frame_pts"),
        "sink_metadata_last_frame_pts": sink.get("last_frame_pts"),
        "sink_metadata_first_frame_num": sink.get("first_frame_num"),
        "sink_metadata_last_frame_num": sink.get("last_frame_num"),
        "decode_warnings": {
            "decode_error_count": clip_validation.get("decode_error_count", 0),
            "decode_error_sample": clip_validation.get("decode_error_sample", []),
        },
    }


root = Path(os.environ["EVIDENCE_ROOT"])
summary_path = Path(os.environ["SUMMARY_JSON"])
target_id = os.environ["TARGET_BUNDLE_ID"]
recent_limit = int(os.environ["RECENT_LIMIT"])
slack = float(os.environ["EVIDENCE_MAX_DURATION_SLACK_SEC"])

bundles = []
if root.is_dir():
    bundles = sorted(
        [path for path in root.iterdir() if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:recent_limit]

diagnostics = [bundle_diag(path, slack) for path in bundles]
target_path = root / target_id
target_diagnostic = (
    bundle_diag(target_path, slack) if target_path.is_dir() else {"bundle_id": target_id, "missing": True}
)
if target_path.is_dir() and all(item["bundle_id"] != target_id for item in diagnostics):
    diagnostics.append(target_diagnostic)

failures: list[dict[str, Any]] = []
for item in diagnostics:
    if (
        item.get("annotations_jsonl_size") == 0
        and item.get("summary_overlay_available") is True
    ):
        failures.append(
            {
                "bundle_id": item["bundle_id"],
                "reason": "annotations_jsonl_zero_byte_but_overlay_available",
            }
        )
    clip_status = str(item.get("clip_status") or "")
    clean_duration_status = clip_status in ("", "ready", "generated", "generated_unverified")
    if item.get("duration_over_max") is True and (
        clean_duration_status and item.get("duration_guard_failed") is not True
    ):
        failures.append(
            {
                "bundle_id": item["bundle_id"],
                "reason": "raw_clip_duration_exceeds_configured_max",
                "raw_clip_duration": item.get("raw_clip_duration"),
                "max_allowed_duration_seconds": item.get("max_allowed_duration_seconds"),
            }
        )

result = "PASS_C1G2C_EVIDENCE_BUNDLE_INTEGRITY_SMOKE"
if failures:
    result = "FAIL_C1G2C_EVIDENCE_BUNDLE_INTEGRITY_SMOKE"

out = {
    "schema_version": "1.0",
    "generated_at": utc_now(),
    "evidence_root": str(root),
    "recent_limit": recent_limit,
    "target_bundle_id": target_id,
    "evidence_max_duration_slack_sec": slack,
    "default_pre_seconds": float(os.environ["DEFAULT_PRE_SECONDS"]),
    "default_post_seconds": float(os.environ["DEFAULT_POST_SECONDS"]),
    "result": result,
    "failures": failures,
    "bundles_scanned": len(diagnostics),
    "abnormal_bundles": [
        item for item in diagnostics
        if item.get("annotations_jsonl_size") == 0 or item.get("duration_over_max")
    ],
    "target_diagnostic": target_diagnostic,
    "bundle_diagnostics": diagnostics,
}

tmp = summary_path.with_suffix(summary_path.suffix + ".tmp")
tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
tmp.replace(summary_path)

print("=== C1G.2c Evidence Bundle Integrity ===")
print(f"result: {result}")
print(f"summary_json: {summary_path}")
print(f"bundles_scanned: {len(diagnostics)}")
print(f"failures: {len(failures)}")
print(f"target_bundle_id: {target_id}")
if target_diagnostic:
    print(
        "target raw_clip_duration: "
        f"{target_diagnostic.get('raw_clip_duration')}"
    )
    print(
        "target annotations_jsonl_size: "
        f"{target_diagnostic.get('annotations_jsonl_size')}"
    )

sys.exit(1 if failures else 0)
PY
