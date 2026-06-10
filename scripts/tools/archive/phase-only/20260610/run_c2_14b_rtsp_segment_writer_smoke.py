#!/usr/bin/env python3
"""C2.14B bounded RTSP segment writer and event clip smoke.

This smoke verifies the production-oriented path introduced by C2.14:
continuous RTSP post-Savant frames are written as chunked ring segments, indexed
under the managed ring root, and optionally joined to a real watchlist or
intrusion event. It never creates synthetic events or visual evidence.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import redis
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.tools import build_c2_14_event_clip_from_ring as clip_builder  # noqa: E402
from scripts.tools import manage_c2_14_rtsp_segment_ring as ring  # noqa: E402
from scripts.tools import run_c2_13_rtsp_watchlist_intrusion_probe as c2_13  # noqa: E402
from scripts.tools import run_c2_13v_rtsp_video_retention_audit as c2_13v  # noqa: E402


SCHEMA_VERSION = "1.0-c2.14b-rtsp-segment-writer-smoke"
DEFAULT_SOURCE_ID = "c2_post_savant_fps_probe"
DEFAULT_RING_ROOT = Path("/data/video-analytics/media/rtsp-ring")
DEFAULT_EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")
DEFAULT_DATABASE_URL = c2_13.DEFAULT_DATABASE_URL
DEFAULT_REDIS_URL = c2_13.DEFAULT_REDIS_URL
DEFAULT_RUNTIME_SECONDS = 150
DEFAULT_MAX_RUNTIME_SECONDS = 300
DEFAULT_CHUNK_SIZE = 120
DEFAULT_TTL_SECONDS = 600
DEFAULT_MAX_BYTES = 5 * 1024 * 1024 * 1024
DEFAULT_MIN_KEEP_SECONDS = 120
EVENT_TYPES = {"intrusion", "watchlist_hit"}

MARKER_WRITER_READY = "PASS_C2_14B_RTSP_SEGMENT_WRITER_READY"
MARKER_EVENT_CLIP_READY = "PASS_C2_14B_RTSP_EVENT_CLIP_READY"
MARKER_NO_EVENT = "PARTIAL_C2_14B_NO_EVENT_IN_WINDOW"
MARKER_WINDOW_GAP = "PARTIAL_C2_14B_EVENT_WINDOW_NOT_FULLY_COVERED"
MARKER_VIDEO_GAP = "PARTIAL_C2_14B_VIDEO_INTEGRITY_GAP"
MARKER_WRITER_GAP = "PARTIAL_C2_14B_RUNTIME_WRITER_GAP"
MARKER_FAIL = "FAIL_C2_14B_RTSP_SEGMENT_WRITER_BLOCKED"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run_stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%S")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def docker_inspect_env(container_name: str) -> dict[str, str]:
    return ring.docker_env(container_name)


def docker_container_status(container_name: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["docker", "inspect", container_name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return {"name": container_name, "inspect_status": "error", "error": str(exc), "running": False}
    if proc.returncode != 0:
        return {"name": container_name, "inspect_status": "missing", "running": False, "stderr": proc.stderr[-500:]}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"name": container_name, "inspect_status": "invalid_json", "error": str(exc), "running": False}
    state = ((payload[0] or {}).get("State") or {}) if payload else {}
    return {
        "name": container_name,
        "inspect_status": "ok",
        "running": bool(state.get("Running")),
        "status": state.get("Status"),
        "health": ((state.get("Health") or {}).get("Status")),
    }


def inspect_runtime(*, source_id: str, ring_root: Path, expected_chunk_size: int, expected_dir_location: str) -> dict[str, Any]:
    containers = {
        name: docker_container_status(name)
        for name in (
            "c2-poc-source-adapter",
            "c2-poc-savant",
            "c2-poc-replay-service",
            "c2-poc-video-file-sink",
            "c2-poc-redis",
        )
    }
    source_env = docker_inspect_env("c2-poc-source-adapter")
    savant_env = docker_inspect_env("c2-poc-savant")
    replay_env = docker_inspect_env("c2-poc-replay-service")
    sink_env = docker_inspect_env("c2-poc-video-file-sink")
    rtsp_url = source_env.get("RTSP_URI") or source_env.get("LOCATION")
    chunk_size = sink_env.get("CHUNK_SIZE")
    dir_location = sink_env.get("DIR_LOCATION")
    replay_config = mounted_source_for_container("c2-poc-replay-service", "/opt/etc/config.json")
    return {
        "schema_version": SCHEMA_VERSION,
        "input_type": "rtsp" if rtsp_url and str(rtsp_url).startswith("rtsp://") else "unknown",
        "source_id": source_env.get("SOURCE_ID") or savant_env.get("SOURCE_ID"),
        "camera_id": source_env.get("SOURCE_ID") or savant_env.get("SOURCE_ID"),
        "rtsp_url_redacted": c2_13.redact_url(str(rtsp_url)) if rtsp_url else None,
        "ring_root": str(ring_root),
        "containers": containers,
        "source_adapter": {
            "source_id": source_env.get("SOURCE_ID"),
            "zmq_endpoint": source_env.get("ZMQ_ENDPOINT"),
            "rtsp_transport": source_env.get("RTSP_TRANSPORT"),
        },
        "savant": {
            "source_id": savant_env.get("SOURCE_ID"),
            "sink_endpoint": savant_env.get("ZMQ_SINK_ENDPOINT"),
            "module_file": savant_env.get("SAVANT_MODULE_FILE"),
        },
        "replay_service": {
            "config_mount_source": replay_config,
            "db_path": replay_env.get("DB_PATH"),
            "out_stream_to_video_file_sink_expected": True,
        },
        "video_file_sink": {
            "running": bool((containers.get("c2-poc-video-file-sink") or {}).get("running")),
            "chunk_size": chunk_size,
            "chunk_size_ready": chunk_size_is_ring_ready(chunk_size),
            "expected_chunk_size": expected_chunk_size,
            "dir_location": dir_location,
            "expected_dir_location": expected_dir_location,
            "dir_location_ready": dir_location_looks_ring_ready(dir_location),
            "supports_source_id_template": "%source_id" in str(dir_location or ""),
            "supports_chunk_idx_template": "%chunk_idx" in str(dir_location or ""),
        },
        "rtsp_input_verified": bool(rtsp_url and str(rtsp_url).startswith("rtsp://") and (source_env.get("SOURCE_ID") == source_id or savant_env.get("SOURCE_ID") == source_id)),
    }


def mounted_source_for_container(container_name: str, destination: str) -> str | None:
    try:
        proc = subprocess.run(
            ["docker", "inspect", container_name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    for mount in ((payload[0] or {}).get("Mounts") or []) if payload else []:
        if mount.get("Destination") == destination:
            return mount.get("Source")
    return None


def chunk_size_is_ring_ready(value: Any) -> bool:
    parsed = ring.int_or_none(value)
    return parsed is not None and parsed > 0


def dir_location_looks_ring_ready(value: Any) -> bool:
    text = str(value or "")
    return "/rtsp-ring/" in text and "%source_id" in text and "%chunk_idx" in text


def validate_writer_pass_requirements(*, input_type: str, source_id: str, expected_source_id: str, chunk_size: Any, segment_count: int, indexed_rows: int, source_mismatch_count: int) -> dict[str, Any]:
    failures: list[str] = []
    if input_type != "rtsp":
        failures.append("input_not_rtsp")
    if source_id != expected_source_id:
        failures.append("source_id_mismatch")
    if not chunk_size_is_ring_ready(chunk_size):
        failures.append("chunk_size_not_ring_ready")
    if segment_count <= 0:
        failures.append("no_compatible_segments")
    if indexed_rows <= 0:
        failures.append("no_index_rows")
    if source_mismatch_count > 0 and indexed_rows <= 0:
        failures.append("only_source_id_mismatch_segments")
    return {
        "passed": not failures,
        "failure_reasons": failures,
        "input_type": input_type,
        "source_id": source_id,
        "expected_source_id": expected_source_id,
        "chunk_size": chunk_size,
        "segment_count": segment_count,
        "indexed_rows": indexed_rows,
        "source_mismatch_count": source_mismatch_count,
    }


def validate_clip_candidate(
    *,
    event: dict[str, Any] | None,
    window: dict[str, Any] | None,
    clip_result: dict[str, Any] | None,
) -> dict[str, Any]:
    failures: list[str] = []
    if not event:
        failures.append("no_event_in_window")
    if window is None or window.get("status") != "pass":
        failures.append("event_window_not_fully_covered")
    if clip_result is None:
        failures.append("clip_not_attempted")
    else:
        summary = clip_result.get("summary") if isinstance(clip_result.get("summary"), dict) else {}
        if not summary.get("raw_clip"):
            failures.append("raw_clip_missing")
        if not bool(summary.get("video_integrity_pass")):
            failures.append("video_integrity_failed")
        if summary.get("event_style_replay_job_passed") is not False:
            failures.append("event_style_replay_claimed")
        if summary.get("db_window_fallback_used") is not False:
            failures.append("db_window_fallback_used")
        if summary.get("legacy_used_for_visual_binding") is not False:
            failures.append("legacy_used_for_visual_binding")
    return {"passed": not failures, "failure_reasons": failures}


def select_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [event for event in events if event.get("event_type") in EVENT_TYPES and event_has_time_anchor(event)]
    intrusions = [event for event in usable if event.get("event_type") == "intrusion"]
    watchlist = [event for event in usable if event.get("event_type") == "watchlist_hit"]
    return (intrusions or watchlist or usable or [None])[0]


def ordered_event_candidates(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    usable = [event for event in events if event.get("event_type") in EVENT_TYPES and event_has_time_anchor(event)]
    intrusions = [event for event in usable if event.get("event_type") == "intrusion"]
    watchlist = [event for event in usable if event.get("event_type") == "watchlist_hit"]
    return intrusions + [event for event in watchlist if event not in intrusions]


def event_has_time_anchor(event: dict[str, Any]) -> bool:
    anchor = clip_builder.event_anchor(event)
    return anchor.get("frame_pts") is not None or anchor.get("event_ts_ms") is not None


def normalize_event(event: dict[str, Any], *, origin: str) -> dict[str, Any]:
    return c2_13v.normalize_event(event, origin=origin)


def capture_events(
    *,
    redis_client: redis.Redis,
    database_url: str,
    source_id: str,
    redis_start_id: str,
    start_time: datetime,
    end_time: datetime,
    max_messages: int,
) -> dict[str, Any]:
    redis_events = [
        normalize_event(event, origin="redis")
        for event in c2_13.parse_security_events(
            c2_13.read_stream_after(redis_client, c2_13.EVENT_STREAM, redis_start_id, max_messages=max_messages)
        )
        if event.get("source_id") == source_id and event.get("event_type") in EVENT_TYPES
    ]
    db_events = [
        normalize_event(event, origin="database")
        for event in fetch_recent_event_rows(database_url, source_id, start_time, end_time)
    ]
    events = c2_13v.dedupe_events(redis_events + db_events)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_id": source_id,
        "capture_window": {
            "started_at": start_time.isoformat(),
            "ended_at": end_time.isoformat(),
            "duration_seconds": (end_time - start_time).total_seconds(),
        },
        "events": events,
        "watchlist_events": [event for event in events if event.get("event_type") == "watchlist_hit"],
        "intrusion_events": [event for event in events if event.get("event_type") == "intrusion"],
        "selected_event": select_event(events),
    }


def fetch_recent_event_rows(database_url: str, source_id: str, start_time: datetime, end_time: datetime) -> list[dict[str, Any]]:
    try:
        with psycopg.connect(database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id::text AS id, source_event_id, event_type,
                           camera_id, source_id, track_id, person_id,
                           confidence, severity, rule_name, event_ts_ms,
                           created_at, payload
                    FROM events
                    WHERE created_at >= %(start_time)s
                      AND created_at <= %(end_time)s
                      AND source_id = %(source_id)s
                      AND event_type = ANY(%(event_types)s)
                    ORDER BY created_at ASC
                    LIMIT 200
                    """,
                    {
                        "start_time": start_time,
                        "end_time": end_time,
                        "source_id": source_id,
                        "event_types": sorted(EVENT_TYPES),
                    },
                )
                return [c2_13.strip_unsafe_payload(c2_13.json_safe(dict(row))) for row in cur.fetchall()]
    except Exception as exc:
        return []


def summarize_ring_segments(*, ring_root: Path, source_id: str, index_report: dict[str, Any]) -> dict[str, Any]:
    seg_root = ring.segments_root(ring_root, source_id)
    rows = ring.read_jsonl(ring.index_path(ring_root, source_id))
    source_mismatch = [
        skipped for skipped in (index_report.get("skipped") or [])
        if skipped.get("reason") == "source_id_mismatch"
    ]
    newest = sorted(
        [path for path in seg_root.glob("*") if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:10] if seg_root.exists() else []
    return {
        "ring_root": str(ring_root),
        "segments_root": str(seg_root),
        "segment_count": len([path for path in seg_root.glob("*") if path.is_dir()]) if seg_root.exists() else 0,
        "indexed_segment_count": len(rows),
        "source_mismatch_skipped_count": len(source_mismatch),
        "first_segment_id": rows[0].get("segment_id") if rows else None,
        "last_segment_id": rows[-1].get("segment_id") if rows else None,
        "first_frame_pts": rows[0].get("first_frame_pts") if rows else None,
        "last_frame_pts": rows[-1].get("last_frame_pts") if rows else None,
        "first_timestamp_ms": rows[0].get("first_timestamp_ms") if rows else None,
        "last_timestamp_ms": rows[-1].get("last_timestamp_ms") if rows else None,
        "total_ring_bytes": ring.directory_size(ring.source_ring_root(ring_root, source_id)),
        "newest_segment_paths": [str(path) for path in newest],
        "sample_segment_paths": [str(row.get("segment_dir")) for row in rows[:5]],
    }


def decide_marker(
    *,
    writer_validation: dict[str, Any],
    event_capture: dict[str, Any],
    window: dict[str, Any] | None,
    clip_result: dict[str, Any] | None,
    retention: dict[str, Any],
    unsafe_scan: dict[str, Any],
) -> str:
    if not unsafe_scan.get("passed", False):
        return MARKER_FAIL
    if retention.get("unsafe_deletion_target_count") not in (0, None):
        return MARKER_FAIL
    if retention.get("deleted_count") not in (0, None):
        return MARKER_FAIL
    if not writer_validation.get("passed"):
        return MARKER_WRITER_GAP
    selected_event = event_capture.get("selected_event")
    if not selected_event:
        return MARKER_NO_EVENT
    if window is None or window.get("status") != "pass":
        return MARKER_WINDOW_GAP
    if clip_result and clip_result.get("result_marker") == clip_builder.RESULT_PASS:
        return MARKER_EVENT_CLIP_READY
    return MARKER_VIDEO_GAP


def render_html(summary: dict[str, Any]) -> str:
    rows = [
        ("Marker", summary.get("result_marker")),
        ("Input type", summary.get("input_type")),
        ("Source", summary.get("source_id")),
        ("Ring root", summary.get("ring_root")),
        ("Segments written", (summary.get("ring_output") or {}).get("segment_count")),
        ("Indexed rows", (summary.get("ring_output") or {}).get("indexed_segment_count")),
        ("Event selected", bool(summary.get("selected_event"))),
        ("Evidence bundle", summary.get("evidence_bundle_path")),
        ("Video integrity", summary.get("video_integrity_status")),
    ]
    body = "\n".join(f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows)
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>C2.14B RTSP Segment Writer</title></head>
<body>
  <h1>C2.14B RTSP Segment Writer</h1>
  <table>{body}</table>
</body>
</html>
"""


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    if args.runtime_seconds < 0 or args.runtime_seconds > args.max_runtime_seconds:
        raise ValueError(f"runtime_seconds must be between 0 and {args.max_runtime_seconds}")
    if args.chunk_size <= 0:
        raise ValueError("chunk_size must be > 0 for C2.14B")
    output_dir = args.output_dir or args.evidence_root / f"c2_14b_rtsp_segment_writer_{run_stamp()}"
    output_dir.mkdir(parents=True, exist_ok=True)
    expected_dir_location = args.expected_dir_location

    runtime_before = inspect_runtime(
        source_id=args.source_id,
        ring_root=args.ring_root,
        expected_chunk_size=args.chunk_size,
        expected_dir_location=expected_dir_location,
    )
    redis_client = redis.Redis.from_url(
        args.redis_url,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    redis_before = c2_13.inspect_redis(redis_client)
    event_start_id = str(redis_before["streams"][c2_13.EVENT_STREAM].get("last_id") or "0-0")
    start_time = utc_now()
    time.sleep(args.runtime_seconds)
    end_time = utc_now()

    index_report = ring.index_existing(
        source_dir=ring.segments_root(args.ring_root, args.source_id),
        ring_root=args.ring_root,
        source_id=args.source_id,
        ttl_seconds=args.ttl_seconds,
        completed_age_seconds=args.completed_age_seconds,
        overwrite_index=True,
    )
    retention_plan = ring.retention_plan(
        ring_root=args.ring_root,
        source_id=args.source_id,
        ttl_seconds=args.ttl_seconds,
        max_bytes=args.max_bytes,
        min_keep_seconds=args.min_keep_seconds,
    )
    retention = ring.apply_retention_plan(retention_plan, dry_run=True)
    event_capture = capture_events(
        redis_client=redis_client,
        database_url=args.database_url,
        source_id=args.source_id,
        redis_start_id=event_start_id,
        start_time=start_time,
        end_time=end_time,
        max_messages=args.max_messages,
    )
    runtime_after = inspect_runtime(
        source_id=args.source_id,
        ring_root=args.ring_root,
        expected_chunk_size=args.chunk_size,
        expected_dir_location=expected_dir_location,
    )
    ring_output = summarize_ring_segments(ring_root=args.ring_root, source_id=args.source_id, index_report=index_report)
    writer_validation = validate_writer_pass_requirements(
        input_type=runtime_after.get("input_type") or "unknown",
        source_id=str(runtime_after.get("source_id") or ""),
        expected_source_id=args.source_id,
        chunk_size=(runtime_after.get("video_file_sink") or {}).get("chunk_size"),
        segment_count=int(index_report.get("compatible_segments_found") or 0),
        indexed_rows=int(index_report.get("index_row_count") or 0),
        source_mismatch_count=ring_output["source_mismatch_skipped_count"],
    )

    selected_event = None
    window: dict[str, Any] | None = None
    clip_result: dict[str, Any] | None = None
    event_attempts: list[dict[str, Any]] = []
    for candidate_event in ordered_event_candidates(event_capture.get("events") or []):
        anchor = clip_builder.event_anchor(candidate_event)
        candidate_window = ring.find_window(
            ring_root=args.ring_root,
            source_id=args.source_id,
            center_pts=anchor.get("frame_pts"),
            center_timestamp_ms=None if anchor.get("frame_pts") is not None else anchor.get("event_ts_ms"),
            pre_seconds=args.pre_seconds,
            post_seconds=args.post_seconds,
        )
        candidate_clip_result: dict[str, Any] | None = None
        if candidate_window.get("status") == "pass":
            bundle_dir = args.evidence_root / f"c2_14b_rtsp_event_clip_{run_stamp()}"
            candidate_clip_result = clip_builder.build_clip_from_ring(
                event=candidate_event,
                ring_root=args.ring_root,
                source_id=args.source_id,
                pre_seconds=args.pre_seconds,
                post_seconds=args.post_seconds,
                output_dir=bundle_dir,
            )
        event_attempts.append(
            {
                "event_identity": clip_builder.event_identity(candidate_event),
                "event_type": candidate_event.get("event_type"),
                "frame_pts": anchor.get("frame_pts"),
                "window_status": candidate_window.get("status"),
                "window_reason": candidate_window.get("reason"),
                "selected_segment_count": candidate_window.get("selected_segment_count"),
                "clip_status": (candidate_clip_result or {}).get("status"),
                "clip_marker": (candidate_clip_result or {}).get("result_marker"),
                "bundle_path": (candidate_clip_result or {}).get("bundle_path"),
            }
        )
        if candidate_clip_result and candidate_clip_result.get("result_marker") == clip_builder.RESULT_PASS:
            selected_event = candidate_event
            window = candidate_window
            clip_result = candidate_clip_result
            break
        if selected_event is None:
            selected_event = candidate_event
            window = candidate_window
            clip_result = candidate_clip_result

    video_summary = (clip_result or {}).get("summary") if isinstance((clip_result or {}).get("summary"), dict) else {}
    clip_validation = validate_clip_candidate(event=selected_event, window=window, clip_result=clip_result)
    unsafe_scan = ring.scan_for_unsafe_payload(
        {
            "runtime_before": runtime_before,
            "runtime_after": runtime_after,
            "index_report": index_report,
            "retention": retention,
            "event_capture": event_capture,
            "event_attempts": event_attempts,
            "clip_result": clip_result,
            "writer_validation": writer_validation,
        }
    )
    selected_event_capture = {**event_capture, "selected_event": selected_event}
    marker = decide_marker(
        writer_validation=writer_validation,
        event_capture=selected_event_capture,
        window=window,
        clip_result=clip_result,
        retention=retention,
        unsafe_scan=unsafe_scan,
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "result_marker": marker,
        "input_type": runtime_after.get("input_type"),
        "source_id": args.source_id,
        "camera_id": runtime_after.get("camera_id"),
        "ring_root": str(args.ring_root),
        "chunk_size": (runtime_after.get("video_file_sink") or {}).get("chunk_size"),
        "dir_location": (runtime_after.get("video_file_sink") or {}).get("dir_location"),
        "runtime_before": runtime_before,
        "runtime_after": runtime_after,
        "runtime_restart_summary": {
            "restarted_by_tool": False,
            "expected_restarted_by_smoke_script": ["c2-poc-video-file-sink", "c2-poc-replay-service"],
        },
        "index_report": index_report,
        "ring_output": ring_output,
        "retention": retention,
        "event_capture": event_capture,
        "event_attempts": event_attempts,
        "selected_event": selected_event,
        "event_window": window,
        "clip_validation": clip_validation,
        "evidence_bundle_path": (clip_result or {}).get("bundle_path"),
        "raw_clip_path": video_summary.get("raw_clip"),
        "metadata_path": video_summary.get("sink_metadata"),
        "sidecar_path": video_summary.get("sidecar"),
        "video_integrity_status": video_summary.get("video_integrity_status") or "not_run",
        "video_integrity_pass": bool(video_summary.get("video_integrity_pass")),
        "event_style_replay_job_passed": False,
        "db_window_fallback_used": False,
        "legacy_used_for_visual_binding": False,
        "writer_validation": writer_validation,
        "unsafe_payload_scan": unsafe_scan,
        "reports": {
            "summary": str(output_dir / "decision_summary.json"),
            "runtime_before": str(output_dir / "runtime_before.json"),
            "runtime_after": str(output_dir / "runtime_after.json"),
            "index_report": str(output_dir / "segment_index_report.json"),
            "retention": str(output_dir / "retention_dry_run_report.json"),
            "events": str(output_dir / "rtsp_events_captured.json"),
        },
    }
    write_json(output_dir / "runtime_before.json", runtime_before)
    write_json(output_dir / "runtime_after.json", runtime_after)
    write_json(output_dir / "segment_index_report.json", index_report)
    write_json(output_dir / "retention_dry_run_report.json", retention)
    write_json(output_dir / "rtsp_events_captured.json", event_capture)
    write_json(output_dir / "event_window_report.json", window or {"status": "not_run", "reason": "no_event_selected"})
    write_json(output_dir / "video_evidence_result.json", clip_result or {"status": "not_run"})
    write_json(output_dir / "unsafe_payload_scan.json", unsafe_scan)
    write_json(output_dir / "decision_summary.json", summary)
    (output_dir / "operator_rtsp_segment_writer_event_clip_report.html").write_text(render_html(summary), encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
    parser.add_argument("--ring-root", type=Path, default=DEFAULT_RING_ROOT)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--redis-url", default=os.getenv("C2_14B_REDIS_URL", DEFAULT_REDIS_URL))
    parser.add_argument("--runtime-seconds", type=float, default=DEFAULT_RUNTIME_SECONDS)
    parser.add_argument("--max-runtime-seconds", type=float, default=DEFAULT_MAX_RUNTIME_SECONDS)
    parser.add_argument("--max-messages", type=int, default=300)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--expected-dir-location", default="/media/rtsp-ring/%source_id/segments/%chunk_idx")
    parser.add_argument("--completed-age-seconds", type=float, default=3.0)
    parser.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--min-keep-seconds", type=int, default=DEFAULT_MIN_KEEP_SECONDS)
    parser.add_argument("--pre-seconds", type=float, default=5.0)
    parser.add_argument("--post-seconds", type=float, default=5.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_smoke(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2 if str(result.get("result_marker") or "").startswith("FAIL_") else 0
    except Exception as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "result_marker": MARKER_FAIL,
            "status": "error",
            "error": str(exc),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
