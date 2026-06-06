#!/usr/bin/env python3
"""Audit one C1M.8 frame-cache freshness guard evidence bundle."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PASS_READY = "PASS_C1M8_FRESHNESS_GUARD_SIDECAR_READY"
PASS_FAIL_CLOSED = "PASS_C1M8_FAIL_CLOSED_ON_STALE_CACHE"
FAIL_STALE_DISPLAYABLE = "FAIL_C1M8_STALE_ROWS_STILL_DISPLAYABLE"
FAIL_LEGACY_FALLBACK = "FAIL_C1M8_LEGACY_OR_SQL_FALLBACK_SELECTED"
PARTIAL_EPOCH_ID_REQUIRED = "PARTIAL_C1M8_EPOCH_ID_REQUIRED"

SIDECAR_SUMMARY = "summary.frame_cache.identity.json"
SIDECAR_ROWS = "annotations.frame_cache.identity.jsonl"
DROPPED_ROWS = "annotations.frame_cache.identity.dropped.debug.jsonl"
LEGACY_ROWS = "annotations.jsonl"


def audit_evidence(
    *,
    event_id: str,
    evidence_root: Path,
    output_root: Path,
    viewer_url: str,
    record_requests_delta: int | None,
    replay_jobs_after: dict[str, Any] | None,
    env_restored: bool | None,
) -> dict[str, Any]:
    evidence_dir = evidence_root / event_id
    output_dir = output_root / event_id
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = load_json(evidence_dir / SIDECAR_SUMMARY)
    rows = load_jsonl(evidence_dir / SIDECAR_ROWS)
    dropped = load_jsonl(evidence_dir / DROPPED_ROWS)
    legacy_exists = (evidence_dir / LEGACY_ROWS).is_file()
    viewer_auto = viewer_annotations(viewer_url, event_id)
    sidecar_stale_displayable = [
        row_public(row)
        for row in rows
        if row.get("displayable") is not False
        and (
            row.get("stale_or_epoch_mismatch") is True
            or row.get("frame_cache_rejection_reason") == "stale_or_epoch_mismatch"
        )
    ]
    dropped_stale = [
        row_public(row)
        for row in dropped
        if row.get("stale_or_epoch_mismatch") is True
        or row.get("frame_cache_rejection_reason") == "stale_or_epoch_mismatch"
    ]

    production_ready = summary.get("production_ready") is True
    annotation_status = str(summary.get("annotation_status") or "")
    rows_rejected_stale = int(summary.get("rows_rejected_stale_cache") or 0)
    rows_rejected_epoch = int(summary.get("rows_rejected_epoch_mismatch") or 0)
    rows_rejected_pts = int(summary.get("rows_rejected_pts_non_unique") or 0)
    viewer_source = str(viewer_auto.get("annotation_source") or "")
    viewer_fallback = bool(viewer_auto.get("fallback_used"))
    replay_jobs_empty = replay_jobs_after is None or replay_jobs_after.get("jobs") == []

    if sidecar_stale_displayable:
        marker = FAIL_STALE_DISPLAYABLE
    elif viewer_source in {"legacy", "sql"} or viewer_fallback:
        marker = FAIL_LEGACY_FALLBACK
    elif production_ready and rows_rejected_stale == 0 and rows_rejected_epoch == 0:
        marker = PARTIAL_EPOCH_ID_REQUIRED
    elif (
        not production_ready
        and annotation_status == "cache_stale_or_epoch_mismatch"
        and rows_rejected_stale > 0
        and rows_rejected_epoch > 0
        and rows_rejected_pts > 0
        and viewer_source == "unavailable"
        and not viewer_fallback
    ):
        marker = PASS_FAIL_CLOSED
    else:
        marker = PARTIAL_EPOCH_ID_REQUIRED

    if record_requests_delta is not None and record_requests_delta != 1:
        marker = "FAIL_C1M8_UNSAFE_RECORD_REQUEST_DELTA"
    if replay_jobs_after is not None and not replay_jobs_empty:
        marker = "FAIL_C1M8_REPLAY_JOBS_NOT_EMPTY_AFTER"
    if env_restored is False:
        marker = "FAIL_C1M8_ENV_NOT_RESTORED"

    overlay_policy = {
        "production_overlay_generated": False,
        "reason": (
            "production_ready_false"
            if not production_ready
            else "not_generated_by_c1m8_audit"
        ),
        "debug_overlay_allowed": not production_ready,
        "debug_overlay_label": (
            "debug/non-production stale cache diagnostic"
            if not production_ready
            else None
        ),
    }
    result = {
        "phase": "C1M.8",
        "generated_at": utc_now(),
        "result_marker": marker,
        "event_id": event_id,
        "evidence_dir": str(evidence_dir),
        "sidecar_summary_path": str(evidence_dir / SIDECAR_SUMMARY),
        "sidecar_annotations_path": str(evidence_dir / SIDECAR_ROWS),
        "dropped_debug_path": str(evidence_dir / DROPPED_ROWS) if (evidence_dir / DROPPED_ROWS).is_file() else None,
        "production_ready": production_ready,
        "annotation_status": annotation_status,
        "rows_written": int(summary.get("rows_written") or summary.get("annotations_written") or 0),
        "rows_displayable": int(summary.get("rows_displayable") or 0),
        "rows_matched_by_frame_uuid": int(summary.get("rows_matched_by_frame_uuid") or 0),
        "rows_matched_by_pts_fallback": int(summary.get("rows_matched_by_pts_fallback") or 0),
        "rows_rejected_stale_cache": rows_rejected_stale,
        "rows_rejected_epoch_mismatch": rows_rejected_epoch,
        "rows_rejected_pts_non_unique": rows_rejected_pts,
        "production_ready_failures": summary.get("production_ready_failures") or [],
        "sidecar_stale_displayable_count": len(sidecar_stale_displayable),
        "sidecar_stale_displayable_sample": sidecar_stale_displayable[:10],
        "dropped_stale_count": len(dropped_stale),
        "dropped_stale_sample": dropped_stale[:10],
        "viewer_auto": viewer_auto,
        "legacy_annotations_exists": legacy_exists,
        "legacy_auto_selected": viewer_source in {"legacy", "sql"} or viewer_fallback,
        "record_requests_delta": record_requests_delta,
        "replay_jobs_after": replay_jobs_after,
        "replay_jobs_empty_after": replay_jobs_empty,
        "env_restored": env_restored,
        "overlay_policy": overlay_policy,
        "epoch_id_required": True,
    }
    write_json(output_dir / "c1m8_freshness_guard_audit.json", result)
    write_report(output_dir / "c1m8_freshness_guard_report.md", result)
    return result


def viewer_annotations(base_url: str, event_id: str) -> dict[str, Any]:
    if not base_url:
        return {"checked": False, "error": "viewer_url_missing"}
    ensure_no_proxy()
    url = f"{base_url.rstrip('/')}/api/bundles/{event_id}/annotations?source=auto"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else {"raw": payload}
    except urllib.error.HTTPError as exc:
        return {"checked": False, "error": exc.reason, "status": exc.code}
    except Exception as exc:
        return {"checked": False, "error": f"{type(exc).__name__}:{exc}"}


def active_replay_jobs(base_url: str) -> dict[str, Any]:
    ensure_no_proxy()
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/v1/job", timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else {"jobs": payload}
    except Exception as exc:
        return {"jobs": None, "error": f"{type(exc).__name__}:{exc}"}


def redis_xlen(container: str, stream: str) -> int:
    proc = subprocess.run(
        ["docker", "exec", container, "redis-cli", "--raw", "XLEN", stream],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        return int((proc.stdout or "0").strip() or "0")
    except ValueError:
        return 0


def container_env(container: str, names: list[str]) -> dict[str, str]:
    proc = subprocess.run(
        ["docker", "inspect", container, "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result: dict[str, str] = {}
    wanted = set(names)
    for line in (proc.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in wanted:
            result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def row_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "object_type": row.get("object_type"),
        "annotation_role": row.get("annotation_role"),
        "frame_uuid": row.get("frame_uuid"),
        "frame_pts": row.get("frame_pts"),
        "frame_num": row.get("frame_num") or row.get("source_frame_num"),
        "frame_annotation_created_at": row.get("frame_annotation_created_at") or row.get("redis_created_at"),
        "frame_annotation_created_delta_to_event_s": row.get("frame_annotation_created_delta_to_event_s"),
        "clip_timeline_match": row.get("clip_timeline_match"),
        "displayable": row.get("displayable"),
        "frame_cache_rejection_reason": row.get("frame_cache_rejection_reason"),
        "source_message_id": row.get("source_message_id") or row.get("frame_annotation_redis_id"),
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_report(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# C1M.8 Frame Annotation Cache Freshness Guard",
        "",
        f"- Marker: `{result.get('result_marker')}`",
        f"- Event: `{result.get('event_id')}`",
        f"- production_ready: `{result.get('production_ready')}`",
        f"- annotation_status: `{result.get('annotation_status')}`",
        f"- rows_matched_by_frame_uuid: `{result.get('rows_matched_by_frame_uuid')}`",
        f"- rows_matched_by_pts_fallback: `{result.get('rows_matched_by_pts_fallback')}`",
        f"- rows_rejected_stale_cache: `{result.get('rows_rejected_stale_cache')}`",
        f"- rows_rejected_epoch_mismatch: `{result.get('rows_rejected_epoch_mismatch')}`",
        f"- rows_rejected_pts_non_unique: `{result.get('rows_rejected_pts_non_unique')}`",
        f"- sidecar_stale_displayable_count: `{result.get('sidecar_stale_displayable_count')}`",
        f"- viewer_auto_source: `{(result.get('viewer_auto') or {}).get('annotation_source')}`",
        f"- viewer_auto_fallback_used: `{(result.get('viewer_auto') or {}).get('fallback_used')}`",
        f"- record_requests_delta: `{result.get('record_requests_delta')}`",
        f"- replay_jobs_empty_after: `{result.get('replay_jobs_empty_after')}`",
        f"- env_restored: `{result.get('env_restored')}`",
        "",
        "## Production Ready Failures",
    ]
    failures = result.get("production_ready_failures") or []
    if failures:
        lines.extend(f"- `{item}`" for item in failures)
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Overlay Policy",
            f"- production_overlay_generated: `{(result.get('overlay_policy') or {}).get('production_overlay_generated')}`",
            f"- reason: `{(result.get('overlay_policy') or {}).get('reason')}`",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_no_proxy() -> None:
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    parts = [part for part in existing.split(",") if part]
    for item in ("127.0.0.1", "localhost", "::1"):
        if item not in parts:
            parts.append(item)
    os.environ["NO_PROXY"] = ",".join(parts)
    os.environ["no_proxy"] = os.environ["NO_PROXY"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--evidence-root", default="/data/video-analytics/media/evidence")
    parser.add_argument("--output-root", default="/data/video-analytics/artifacts/c1m8")
    parser.add_argument("--viewer-url", default="http://127.0.0.1:8090")
    parser.add_argument("--record-requests-delta", type=int)
    parser.add_argument("--replay-jobs-after-json")
    parser.add_argument("--env-restored", choices=("true", "false", "unknown"), default="unknown")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    replay_jobs = None
    if args.replay_jobs_after_json:
        try:
            parsed = json.loads(args.replay_jobs_after_json)
            replay_jobs = parsed if isinstance(parsed, dict) else {"jobs": parsed}
        except json.JSONDecodeError:
            replay_jobs = {"jobs": None, "error": "invalid_replay_jobs_after_json"}
    env_restored = None if args.env_restored == "unknown" else args.env_restored == "true"
    result = audit_evidence(
        event_id=args.event_id,
        evidence_root=Path(args.evidence_root),
        output_root=Path(args.output_root),
        viewer_url=args.viewer_url,
        record_requests_delta=args.record_requests_delta,
        replay_jobs_after=replay_jobs,
        env_restored=env_restored,
    )
    print(f"result_marker={result.get('result_marker')}")
    print(f"event_id={result.get('event_id')}")
    print(f"production_ready={result.get('production_ready')}")
    print(f"annotation_status={result.get('annotation_status')}")
    print(f"rows_rejected_stale_cache={result.get('rows_rejected_stale_cache')}")
    print(f"viewer_auto_source={(result.get('viewer_auto') or {}).get('annotation_source')}")
    print(f"audit={Path(args.output_root) / args.event_id / 'c1m8_freshness_guard_audit.json'}")
    return 2 if str(result.get("result_marker", "")).startswith("FAIL_") else 0


if __name__ == "__main__":
    raise SystemExit(main())
