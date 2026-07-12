"""Media worker — monitor sink output, update events table with clip paths and snapshots."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, local

import psycopg
from psycopg.rows import dict_row

from libs.evidence_lifecycle import (
    ACTIVE_COMPATIBILITY_TASK_STATUSES,
    ACTIVE_MATERIALIZATION_STATUSES,
    CLAIMABLE_MATERIALIZATION_STATUSES,
    MATERIALIZATION_STATE_CONTRACT_VERSION,
    MaterializationPhase,
    TERMINAL_MATERIALIZATION_STATUSES,
    classify_reason,
    retry_delay_seconds,
)

from app.annotated_snapshot import generate_annotated_snapshot
from app.config import Config, load_config
from app.evidence_db_index import upsert_evidence_bundle_index
from app.frame_cache_sidecar_writer import write_frame_cache_identity_sidecar
from app.legacy_observability import (
    guard_failure_attribution,
    materialization_correlation,
)
from app.materialization_repository import (
    MaterializationLease,
    claim_finalizer_task,
    claim_rolling_task,
    complete_finalizer_task,
    current_lease,
    defer_terminal_task,
    fail_unclaimed_task,
    fail_rolling_task,
    persist_finalizer_handoff,
    recover_and_expire_rolling_tasks,
    retry_finalizer_handoff,
    retry_rolling_task,
    schedule_unclaimed_retry,
    supports_lifecycle_v2,
)
from app.post_savant_evidence_bundle import (
    EVIDENCE_TOPOLOGY as POST_SAVANT_REPLAY_EVIDENCE_TOPOLOGY,
    RAW_CLIP_FILE,
    SINK_METADATA_FILE,
    SUMMARY_FILE,
    _copy_or_crop_video,
    _select_time_domain_frames,
    _write_jsonl as _write_metadata_jsonl,
    build_post_savant_evidence_bundle,
    load_native_metadata,
    read_decoded_video_frame_count,
)
from app.post_savant_metadata_annotation_builder import (
    PRODUCTION_TIMELINE_DOMAIN,
    SIDECAR_ANNOTATIONS_FILE,
    SIDECAR_SUMMARY_FILE,
)
from app.production_sidecar_policy import load_frame_cache_sidecar_config
from app.rolling_cache import (
    RollingCacheCoverageMiss,
    RollingSegment,
    find_segments,
    materialize_window,
)
from app.snapshot import generate_snapshot

logger = logging.getLogger(__name__)

shutdown_requested = False
DEFAULT_EVIDENCE_MAX_DURATION_SLACK_SEC = 10.0
DEFAULT_POST_SAVANT_DURATION_GUARD_SLACK_SEC = 1.0
DEFAULT_POST_SAVANT_WINDOW_EDGE_SLACK_SEC = 0.75
DEFAULT_POST_SAVANT_MAX_PTS_GAP_SEC = 2.0
DEFAULT_RUNTIME_EPOCH_STATE_PATH = (
    "/media/replay-sink-output/midterm/.current_epoch.json"
)
DEFAULT_MEDIA_WORKER_STATE_PATH = (
    "/media/replay-sink-output/midterm/.media-worker.processed.json"
)
DEFAULT_SINK_SCAN_MAX_METADATA_FILES = 20000
DEFAULT_MEDIA_PROBE_TIMEOUT_S = 30.0
DEFAULT_MEDIA_DECODE_TIMEOUT_S = 120.0
DEFAULT_MATERIALIZATION_MAX_ACTIVE = 1
DEFAULT_MATERIALIZATION_TIMEOUT_S = 0.0
DEFAULT_MATERIALIZATION_MAX_BACKLOG = 0
DEFAULT_MATERIALIZATION_MAX_PER_POLL = 0
DEFAULT_MATERIALIZATION_FINALIZER_WORKERS = 1
DEFAULT_MATERIALIZATION_THROTTLE_SLEEP_S = 0.0
DEFAULT_MATERIALIZATION_THROTTLE_DEADLINE_GUARD_S = 0.0
DEFAULT_MATERIALIZATION_CPU_THREAD_LIMIT = 0
DEFAULT_FRAME_CACHE_ANCHOR_WAIT_MAX_S = 120.0
DEFAULT_FRAME_CACHE_ANCHOR_WAIT_FACTOR = 1.5
DEFAULT_INVALID_SINK_OUTPUT_MAX_RETRIES = 3
DEFAULT_CLEANUP_REPLAY_SINK_OUTPUT_STATUSES = ("ready",)
IMAGE_EVIDENCE_NEAREST_FRAME_TOLERANCE_NS = 500_000_000
INVALID_SINK_OUTPUT_MARKER = ".media-worker.invalid.json"
ANNOTATION_STATUS_UNAVAILABLE = "unavailable"
BUNDLE_STATUS_DURATION_GUARD_FAILED = "duration_guard_failed"
BUNDLE_STATUS_GENERATED_ANNOTATION_FAILED = "generated_annotation_failed"
MATERIALIZATION_STATUS_DEFERRED = "materialization_deferred"
MATERIALIZATION_STATUS_FAILED = "materialization_failed"
EPOCH_SUPERSEDED_INCOMPLETE_REASON = "epoch_superseded_incomplete"
MATERIALIZATION_BACKLOG_STATUSES = (
    *sorted(ACTIVE_COMPATIBILITY_TASK_STATUSES),
)
POST_SAVANT_FINALIZER_ENV = "EVIDENCE_TOPOLOGY"
SNAPSHOT_ELIGIBLE_CLIP_STATUSES = ("ready", "generated")
SNAPSHOT_INELIGIBLE_CLIP_STATUSES = (
    "generated_corrupt",
    BUNDLE_STATUS_DURATION_GUARD_FAILED,
)
DB_BACKED_EVIDENCE_SIDECARS_TO_PRUNE = (
    "annotations.frame_cache.identity.jsonl",
    "annotations.frame_cache.identity.dropped.debug.jsonl",
    "sink_metadata.json",
    "summary.json",
    "summary.frame_cache.identity.json",
    "metadata.json",
)
SUCCESS_EVIDENCE_FILES_TO_PRUNE = (
    *DB_BACKED_EVIDENCE_SIDECARS_TO_PRUNE,
    "video_crop_ffmpeg.log",
)
PERMANENT_INVALID_SINK_OUTPUT_REASONS = {"video_duration_unavailable"}
_PROBE_METRICS = {
    "ffprobe_invocation_count": 0,
    "ffprobe_duration_ms": 0,
    "ffmpeg_invocation_count": 0,
    "ffmpeg_duration_ms": 0,
    "imageio_ffmpeg_fallback_count": 0,
    "imageio_ffmpeg_fallback_duration_ms": 0,
}
_PROBE_METRICS_LOCK = Lock()
_PROBE_METRICS_LOCAL = local()
_SINK_PHASES: dict[str, dict[str, object]] = {}
_SINK_PHASES_LOCK = Lock()


def _frame_cache_anchor_lag_seconds(summary: dict) -> float | None:
    if str(summary.get("annotation_status") or "") != "missing_frame_metadata":
        return None
    reader = summary.get("frame_cache_reader_summary")
    if not isinstance(reader, dict):
        return None
    anchor_pts = _to_int(
        summary.get("trigger_face_row_frame_pts") or summary.get("frame_pts")
    )
    latest_pts = _to_int(reader.get("latest_frame_pts"))
    if anchor_pts is None or latest_pts is None:
        return None
    return max(0.0, (anchor_pts - latest_pts) / 1_000_000_000.0)


def _write_frame_cache_sidecar_after_anchor(
    **kwargs,
) -> tuple[dict, dict]:
    """Wait only when the annotation stream is measurably behind the event frame."""
    max_wait_s = max(
        0.0,
        float(
            os.getenv(
                "FRAME_CACHE_ANCHOR_WAIT_MAX_S",
                str(DEFAULT_FRAME_CACHE_ANCHOR_WAIT_MAX_S),
            )
        ),
    )
    wait_factor = max(
        1.0,
        float(
            os.getenv(
                "FRAME_CACHE_ANCHOR_WAIT_FACTOR",
                str(DEFAULT_FRAME_CACHE_ANCHOR_WAIT_FACTOR),
            )
        ),
    )
    started = time.monotonic()
    attempts = 0
    waited_s = 0.0
    initial_lag_s = None
    cache_bypassed_for_retry = False
    writer_kwargs = dict(kwargs)
    while True:
        summary, result = write_frame_cache_identity_sidecar(**writer_kwargs)
        lag_s = _frame_cache_anchor_lag_seconds(summary)
        if initial_lag_s is None:
            initial_lag_s = lag_s
        elapsed_s = time.monotonic() - started
        remaining_s = max_wait_s - elapsed_s
        if lag_s is None or lag_s <= 0.0 or remaining_s <= 0.0:
            break
        sleep_s = min(max(1.0, lag_s * wait_factor), remaining_s)
        logger.info(
            "frame_cache_anchor_wait source_id=%s event_id=%s "
            "lag_s=%.3f sleep_s=%.3f attempt=%d",
            (kwargs.get("event") or {}).get("source_id", ""),
            (kwargs.get("event") or {}).get("event_id", ""),
            lag_s,
            sleep_s,
            attempts + 1,
        )
        if not cache_bypassed_for_retry:
            retry_config = dict(writer_kwargs.get("config") or {})
            retry_config["range_cache_ttl_s"] = 0.0
            retry_config["range_cache_max_entries"] = 0
            retry_config["stream_id_range_unbounded"] = True
            writer_kwargs["config"] = retry_config
            cache_bypassed_for_retry = True
        time.sleep(sleep_s)
        waited_s += sleep_s
        attempts += 1
    summary["annotation_anchor_wait"] = {
        "attempts": attempts,
        "waited_s": round(waited_s, 3),
        "max_wait_s": max_wait_s,
        "initial_lag_s": (
            round(initial_lag_s, 3) if initial_lag_s is not None else None
        ),
        "final_lag_s": (
            round(_frame_cache_anchor_lag_seconds(summary), 3)
            if _frame_cache_anchor_lag_seconds(summary) is not None
            else None
        ),
        "status": str(summary.get("annotation_status") or ""),
        "range_cache_bypassed_for_retry": cache_bypassed_for_retry,
    }
    return summary, result


def _build_person_bbox_db_annotation_rows(
    sink_metadata_rows: list[dict],
    observations: list[dict],
) -> list[dict]:
    """Build displayable person-context rows from exact DB frame-PTS matches."""
    timeline_by_pts: dict[int, tuple[int, dict]] = {}
    first_pts: int | None = None
    for index, metadata in enumerate(sink_metadata_rows):
        frame_pts = _to_int(metadata.get("frame_pts") or metadata.get("pts"))
        if frame_pts is None:
            continue
        timeline_by_pts[frame_pts] = (index, metadata)
        first_pts = frame_pts if first_pts is None else min(first_pts, frame_pts)
    if first_pts is None:
        return []

    rows_by_frame: dict[int, dict] = {}
    for observation in observations:
        frame_pts = _to_int(observation.get("frame_pts"))
        timeline = timeline_by_pts.get(frame_pts) if frame_pts is not None else None
        bbox = observation.get("person_bbox")
        if timeline is None or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        clip_frame_index, metadata = timeline
        confidence = _to_float(observation.get("person_confidence"))
        obj = {
            "object_type": "person",
            "annotation_role": "person_context",
            "track_id": str(observation.get("track_id") or ""),
            "frame_num": _to_int(observation.get("frame_num")),
            "source_frame_num": _to_int(observation.get("frame_num")),
            "source_message_id": str(
                observation.get("source_observation_id") or ""
            ),
            "match_type": "frame_pts_exact",
            "label": {"kind": "person"},
            "style": {"reason": "person_detection"},
            "quality": {
                "quality_status": "ok",
                "person_confidence": confidence,
            },
            "bbox": {
                "xyxy": [float(value) for value in bbox],
                "format": "xyxy",
                "coordinate_space": "pixel",
                "confidence": confidence,
            },
        }
        row = rows_by_frame.setdefault(
            clip_frame_index,
            {
                "schema_version": "1.0",
                "source": "person_bbox_observations_db",
                "displayable": True,
                "clip_frame_index": clip_frame_index,
                "frame_uuid": str(
                    metadata.get("frame_uuid") or metadata.get("uuid") or ""
                ),
                "frame_pts": frame_pts,
                "t_ms": int(round((frame_pts - first_pts) / 1_000_000.0)),
                "objects": [],
            },
        )
        row["objects"].append(obj)
    return [rows_by_frame[index] for index in sorted(rows_by_frame)]


def _write_person_bbox_db_sidecar_fallback(
    pg_conn: psycopg.Connection,
    *,
    event_context: dict,
    output_dir: Path,
    sink_metadata_rows: list[dict],
) -> tuple[dict, dict] | None:
    """Recover from retained YOLO observations when the Redis window is gone."""
    frame_pts_values = [
        value
        for row in sink_metadata_rows
        if (value := _to_int(row.get("frame_pts") or row.get("pts"))) is not None
    ]
    source_id = str(event_context.get("source_id") or "")
    if not source_id or not frame_pts_values:
        return None
    with pg_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT source_observation_id, track_id, frame_pts, frame_num,
                   person_bbox, person_confidence
            FROM person_bbox_observations
            WHERE source_id = %s
              AND gate_status = 'accepted'
              AND frame_pts BETWEEN %s AND %s
            ORDER BY frame_pts, track_id
            """,
            (source_id, min(frame_pts_values), max(frame_pts_values)),
        )
        observations = [dict(row) for row in cur.fetchall()]
    annotations = _build_person_bbox_db_annotation_rows(
        sink_metadata_rows,
        observations,
    )
    if not annotations:
        return None
    annotations_path = output_dir / SIDECAR_ANNOTATIONS_FILE
    summary_path = output_dir / SIDECAR_SUMMARY_FILE
    person_count = sum(len(row.get("objects") or []) for row in annotations)
    summary = {
        "annotation_source": "person_bbox_observations_db",
        "annotation_status": "complete",
        "annotations_written": len(annotations),
        "rows_written": len(annotations),
        "rows_total_input": len(annotations),
        "person_context_rows": person_count,
        "person_objects_count": person_count,
        "face_objects_count": 0,
        "known_face_count": 0,
        "unknown_face_count": 0,
        "embedding_vectors_in_output": 0,
        "image_bytes_in_output": 0,
        "production_ready": True,
        "production_ready_failures": [],
        "frontend_overlay_required": True,
        "fallback_used": False,
        "db_person_context_recovery": {
            "match": "frame_pts_exact",
            "observation_rows": len(observations),
            "annotation_frames": len(annotations),
        },
    }
    _write_metadata_jsonl(annotations_path, annotations)
    _atomic_write_json(summary_path, summary)
    logger.warning(
        "frame_cache_db_person_context_recovered source_id=%s event_id=%s "
        "annotation_frames=%d person_objects=%d",
        source_id,
        event_context.get("event_id", ""),
        len(annotations),
        person_count,
    )
    return summary, {
        "written": True,
        "annotations_path": str(annotations_path),
        "summary_path": str(summary_path),
    }


def request_shutdown(signum: int, _frame: object) -> None:
    global shutdown_requested
    logger.info("shutdown requested by signal=%s", signum)
    shutdown_requested = True


def _parse_ndjson(filepath: Path) -> dict | None:
    """Parse an NDJSON (JSON Lines) file, returning the first valid JSON object."""
    try:
        if filepath.stat().st_size <= 0:
            return None
    except OSError:
        return None
    try:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    continue
    except Exception:
        logger.exception("failed to read %s", filepath)
    return None


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _path_tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        try:
            return int(path.lstat().st_size)
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() or child.is_symlink():
                total += int(child.lstat().st_size)
        except OSError:
            continue
    return total


def _cleanup_processed_sink_output(
    *,
    meta_dir: str,
    sink_root: str,
    event_id: str,
    clip_status: str,
    enabled: bool,
    allowed_statuses: tuple[str, ...] = DEFAULT_CLEANUP_REPLAY_SINK_OUTPUT_STATUSES,
) -> dict[str, object]:
    """Delete a finalized video-file-sink output directory after evidence is published."""
    if not enabled:
        return {"status": "disabled", "deleted_bytes": 0}
    if not meta_dir:
        return {"status": "skipped", "reason": "missing_meta_dir", "deleted_bytes": 0}
    if clip_status not in set(allowed_statuses):
        return {
            "status": "skipped",
            "reason": "clip_status_not_allowed",
            "deleted_bytes": 0,
        }

    root = Path(sink_root).resolve(strict=False)
    target = Path(meta_dir).resolve(strict=False)
    if target == root or not _path_is_relative_to(target, root):
        logger.warning(
            "replay_sink_cleanup_skipped_unsafe event_id=%s meta_dir=%s root=%s",
            event_id,
            meta_dir,
            sink_root,
        )
        return {"status": "skipped", "reason": "unsafe_path", "deleted_bytes": 0}
    if not target.is_dir():
        return {"status": "missing", "deleted_bytes": 0}

    deleted_bytes = _path_tree_size(target)
    shutil.rmtree(target)
    logger.info(
        "replay_sink_output_cleaned event_id=%s meta_dir=%s deleted_bytes=%s",
        event_id,
        meta_dir,
        deleted_bytes,
    )
    return {"status": "deleted", "deleted_bytes": deleted_bytes}


def _cleanup_orphan_sink_output(
    *,
    meta_dir: str,
    sink_root: str,
    event_id: str,
) -> dict[str, object]:
    """Delete a sink output whose event/task row no longer exists."""
    try:
        return _cleanup_processed_sink_output(
            meta_dir=meta_dir,
            sink_root=sink_root,
            event_id=event_id,
            clip_status="orphan",
            enabled=True,
            allowed_statuses=("orphan",),
        )
    except OSError as exc:
        logger.warning(
            "orphan_sink_output_cleanup_failed event_id=%s meta_dir=%s error=%s",
            event_id,
            meta_dir,
            exc,
        )
        return {"status": "failed", "deleted_bytes": 0, "error": str(exc)}


def _metadata_scan_limit(limit: int | None = None) -> int:
    if limit is not None:
        return max(int(limit), 1)
    try:
        return max(int(os.getenv("MEDIA_SINK_SCAN_MAX_METADATA_FILES", "")), 1)
    except ValueError:
        return DEFAULT_SINK_SCAN_MAX_METADATA_FILES


def _load_scan_metadata_payload(meta_file: Path) -> dict | None:
    data = _parse_ndjson(meta_file)
    if isinstance(data, dict):
        return data
    try:
        with open(meta_file, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        logger.debug("metadata file not finalized yet path=%s", meta_file)
    except Exception:
        logger.exception("failed to parse %s", meta_file)
    return None


def _incremental_metadata_paths(
    sink_path: Path,
    *,
    processed_dirs: set[str] | None,
) -> tuple[list[Path], bool]:
    paths: list[Path] = []
    root_metadata = sink_path / "metadata.json"
    if root_metadata.is_file():
        paths.append(root_metadata)
    try:
        children = sorted(
            sink_path.iterdir(),
            key=lambda path: _path_mtime_ns(path),
            reverse=True,
        )
    except FileNotFoundError:
        return [], False
    except OSError:
        logger.exception("failed to list sink output dir path=%s", sink_path)
        return [], False

    direct_child_candidates_seen = False
    for child in children:
        if not child.is_dir():
            continue
        meta_file = child / "metadata.json"
        if not meta_file.is_file():
            continue
        direct_child_candidates_seen = True
        if processed_dirs is not None and str(child) in processed_dirs:
            continue
        paths.append(meta_file)
    return paths, bool(paths or direct_child_candidates_seen)


def _path_mtime_ns(path: Path) -> int:
    try:
        return int(path.stat().st_mtime_ns)
    except OSError:
        return 0


def _scan_metadata_files(
    sink_dir: str,
    *,
    processed_dirs: set[str] | None = None,
    max_metadata_files: int | None = None,
) -> tuple[list[dict], dict]:
    """Scan active sink output for metadata files with bounded fallback."""
    started = time.monotonic()
    sink_path = Path(sink_dir)
    limit = _metadata_scan_limit(max_metadata_files)
    stats = {
        "sink_dir": str(sink_path),
        "scan_mode": "missing",
        "scan_duration_ms": 0,
        "metadata_files_visited": 0,
        "metadata_rows_loaded": 0,
        "metadata_files_parsed": 0,
        "metadata_files_truncated": False,
        "rglob_fallback_used": False,
        "processed_dirs_known": len(processed_dirs or set()),
        "active_runtime_epoch_id": _current_runtime_epoch_id(sink_path),
    }
    results: list[dict] = []
    if not sink_path.exists():
        stats["scan_duration_ms"] = int((time.monotonic() - started) * 1000)
        logger.info(
            "media_sink_scan sink_dir=%s scan_mode=%s duration_ms=%s "
            "metadata_files_visited=0 metadata_files_parsed=0 "
            "rglob_fallback_used=false",
            stats["sink_dir"],
            stats["scan_mode"],
            stats["scan_duration_ms"],
        )
        return results, stats

    paths, incremental_available = _incremental_metadata_paths(
        sink_path,
        processed_dirs=processed_dirs,
    )
    if incremental_available:
        stats["scan_mode"] = "active_epoch_incremental"
    else:
        stats["scan_mode"] = "fallback_rglob"
        stats["rglob_fallback_used"] = True
        paths = [
            path
            for path in sink_path.rglob("metadata.json")
            if processed_dirs is None or str(path.parent) not in processed_dirs
        ]
        paths.sort(key=lambda path: _path_mtime_ns(path), reverse=True)

    if len(paths) > limit:
        stats["metadata_files_truncated"] = True
        paths = paths[:limit]

    stats["metadata_files_visited"] = len(paths)
    for meta_file in paths:
        data = _load_scan_metadata_payload(meta_file)
        if data is None:
            continue
        data["_meta_dir"] = str(meta_file.parent)
        results.append(data)
        stats["metadata_files_parsed"] += 1
        stats["metadata_rows_loaded"] += 1
        logger.info(
            "media_metadata_parsed path=%s source_id=%s event_id=%s",
            str(meta_file.parent),
            data.get("source_id", ""),
            data.get("labels", {}).get("event_id", data.get("event_id", "")),
        )

    stats["scan_duration_ms"] = int((time.monotonic() - started) * 1000)
    logger.info(
        "media_sink_scan sink_dir=%s scan_mode=%s active_runtime_epoch_id=%s "
        "duration_ms=%s metadata_files_visited=%s metadata_files_parsed=%s "
        "metadata_files_truncated=%s rglob_fallback_used=%s "
        "processed_dirs_known=%s",
        stats["sink_dir"],
        stats["scan_mode"],
        stats["active_runtime_epoch_id"],
        stats["scan_duration_ms"],
        stats["metadata_files_visited"],
        stats["metadata_files_parsed"],
        stats["metadata_files_truncated"],
        stats["rglob_fallback_used"],
        stats["processed_dirs_known"],
    )
    return results, stats


def _find_metadata_files(sink_dir: str) -> list[dict]:
    """Scan *sink_dir* for metadata.json files and return parsed contents."""
    results, _stats = _scan_metadata_files(sink_dir)
    return results


def _find_video_file(meta_dir: str) -> str | None:
    """Find the first video file (*.mkv, *.mov, *.webm) in *meta_dir*."""
    for ext in ("*.mkv", "*.mov", "*.webm", "*.mp4"):
        for f in Path(meta_dir).glob(ext):
            return str(f)
    return None


def _sink_output_ready_for_finalizer(
    *,
    video_file: str,
    metadata_file: str,
    known_duration_s: float | None = None,
) -> tuple[bool, str]:
    """Return whether video-file-sink output is safe to publish as evidence."""
    video_path = Path(video_file)
    metadata_path = Path(metadata_file)
    try:
        if video_path.stat().st_size <= 0:
            return False, "video_file_empty"
    except OSError:
        return False, "video_file_missing"
    try:
        if metadata_path.stat().st_size <= 0:
            return False, "metadata_file_empty"
    except OSError:
        return False, "metadata_file_missing"

    if known_duration_s is not None and known_duration_s > 0:
        return True, "ready"

    if _probe_video_duration_seconds(video_file) is None:
        return False, "video_duration_unavailable"
    return True, "ready"


def _known_sink_output_duration_seconds(meta: dict | None) -> float | None:
    """Return a trusted duration already produced by the sink/materializer."""
    if not isinstance(meta, dict):
        return None
    rolling_cache = meta.get("rolling_cache")
    if isinstance(rolling_cache, dict):
        duration = _to_float(rolling_cache.get("output_duration_s"))
        if duration is not None and duration > 0:
            return duration
    duration = _to_float(meta.get("output_duration_s") or meta.get("duration_s"))
    return duration if duration is not None and duration > 0 else None


def _invalid_sink_output_max_retries() -> int:
    value = _to_int(os.getenv("MEDIA_INVALID_SINK_OUTPUT_MAX_RETRIES"))
    if value is None:
        return DEFAULT_INVALID_SINK_OUTPUT_MAX_RETRIES
    return max(int(value), 1)


def _invalid_sink_output_marker_path(meta_dir: str) -> Path:
    return Path(meta_dir) / INVALID_SINK_OUTPUT_MARKER


def _write_invalid_sink_output_marker(
    *,
    meta_dir: str,
    event_id: str,
    video_file: str,
    reason: str,
    attempts: int,
) -> None:
    marker = _invalid_sink_output_marker_path(meta_dir)
    payload = {
        "event_id": event_id,
        "video_file": video_file,
        "reason": reason,
        "attempts": attempts,
        "marked_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _atomic_write_json(marker, payload)
    except Exception:
        logger.exception("failed to write invalid sink output marker path=%s", marker)


_UUID_RE = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)


def _extract_uuid(s: str) -> str | None:
    """Extract the first UUID from a string like 'replay-event-{uuid}' or '{uuid}-00000000'."""
    m = _UUID_RE.search(s)
    return m.group(0) if m else None


def _extract_event_id(meta: dict) -> str | None:
    """Extract event_id from metadata JSON, trying multiple paths."""
    # 1. Direct labels.event_id
    labels = meta.get("labels", {})
    if isinstance(labels, dict):
        eid = labels.get("event_id")
        if eid:
            return str(eid)

    # 2. Top-level event_id
    eid = meta.get("event_id")
    if eid:
        return str(eid)

    # 3. source_id with UUID (format: "replay-event-{uuid}" or just "{uuid}")
    source_id = str(meta.get("source_id", ""))
    eid = _extract_uuid(source_id)
    if eid:
        return eid

    # 4. resulting_stream_id (format: "replay-event-{uuid}")
    rsi = str(meta.get("resulting_stream_id", ""))
    eid = _extract_uuid(rsi)
    if eid:
        return eid

    # 5. Directory basename (format: "replay-event-{uuid}-00000000")
    dirname = str(meta.get("_meta_dir", ""))
    eid = _extract_uuid(dirname)
    if eid:
        return eid

    # 6. configuration.labels.event_id
    cfg = meta.get("configuration", {})
    if isinstance(cfg, dict):
        cfg_labels = cfg.get("labels", {})
        if isinstance(cfg_labels, dict):
            eid = cfg_labels.get("event_id")
            if eid:
                return str(eid)

    return None


def _is_already_ready(pg_conn: psycopg.Connection, event_id: str) -> bool:
    """Check if an event already has final clip and annotation outputs."""
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload->'media'->>'clip_status',
                       payload->'media'->>'annotations_jsonl_path',
                       payload->'media'->>'summary_json_path'
                FROM events
                WHERE id = %s::uuid
                """,
                (event_id,),
            )
            row = cur.fetchone()
            has_annotation_outputs = (
                len(row) < 3 or (bool(row[1]) and bool(row[2]))
            ) if row is not None else False
            return row is not None and row[0] in (
                "ready",
                "generated",
                "generated_corrupt",
                "generated_unverified",
                BUNDLE_STATUS_DURATION_GUARD_FAILED,
                BUNDLE_STATUS_GENERATED_ANNOTATION_FAILED,
            ) and has_annotation_outputs
    except Exception:
        return False


def _evidence_state_for_clip_status(clip_status: str) -> str:
    if clip_status in {"ready", "generated", "generated_unverified"}:
        return "materialized"
    if clip_status in {
        BUNDLE_STATUS_DURATION_GUARD_FAILED,
        BUNDLE_STATUS_GENERATED_ANNOTATION_FAILED,
        "generated_corrupt",
        "failed",
    }:
        return "materialization_failed"
    if clip_status == "replay_job_created":
        return "materializing"
    return "materializing"


def _set_event_evidence_state(
    pg_conn: psycopg.Connection,
    event_id: str,
    *,
    state: str,
    reason: str = "",
) -> None:
    if not event_id:
        return
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET media_status = %(state)s,
                    payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'evidence_state', %(state)s::text,
                                'evidence_reason', NULLIF(%(reason)s::text, ''),
                                'evidence_state_updated_at', now(),
                                'materialization_status', %(state)s::text,
                                'materialization_reason',
                                    NULLIF(%(reason)s::text, '')
                            ))
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {"event_id": event_id, "state": state, "reason": reason},
            )
            if cur.rowcount and cur.rowcount > 0:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = %(state)s,
                        materialization_status = %(state)s,
                        error_message = CASE
                            WHEN %(reason)s::text != '' THEN %(reason)s::text
                            ELSE error_message
                        END,
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                    """,
                    {"event_id": event_id, "state": state, "reason": reason},
                )
    except Exception:
        logger.exception(
            "failed to set evidence state event_id=%s state=%s", event_id, state
        )


def _set_event_db_index_status(
    pg_conn: psycopg.Connection,
    event_id: str,
    *,
    status: str,
    error: str = "",
) -> None:
    if not event_id:
        return
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET payload = COALESCE(payload, '{}'::jsonb)
                    || jsonb_build_object(
                        'media',
                        COALESCE(payload->'media', '{}'::jsonb)
                        || jsonb_strip_nulls(jsonb_build_object(
                            'db_index_status', %(status)s::text,
                            'db_index_updated_at', now(),
                            'db_index_error', NULLIF(%(error)s::text, '')
                        ))
                    ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {"event_id": event_id, "status": status, "error": error},
            )
    except Exception:
        logger.exception(
            "failed to set evidence DB index status event_id=%s status=%s",
            event_id,
            status,
        )


def _evidence_event_links_table_exists(pg_conn: psycopg.Connection) -> bool:
    try:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.evidence_event_links')")
            row = cur.fetchone()
            return bool(row and row[0])
    except Exception:
        logger.exception("failed to check evidence_event_links table")
        return False


def _upsert_covered_event_aliases(
    pg_conn: psycopg.Connection,
    *,
    bundle_event_id: str,
) -> int:
    """Publish DB aliases for events covered by *bundle_event_id* evidence."""

    if not bundle_event_id or not _evidence_event_links_table_exists(pg_conn):
        return 0
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                WITH parent AS (
                    SELECT *
                    FROM evidence_bundles
                    WHERE event_id = %(bundle_event_id)s::uuid
                ),
                linked AS (
                    SELECT
                        l.event_id,
                        l.bundle_event_id,
                        l.relation,
                        l.reason,
                        l.metadata AS link_metadata,
                        e.source_event_id,
                        e.camera_id::text AS camera_id,
                        e.source_id,
                        e.event_type,
                        e.created_at AS event_created_at,
                        c.name AS camera_name
                    FROM evidence_event_links l
                    JOIN events e ON e.id = l.event_id
                    LEFT JOIN cameras c
                      ON c.id::text = e.camera_id::text
                      OR c.source_id = e.source_id
                    WHERE l.bundle_event_id = %(bundle_event_id)s::uuid
                      AND l.relation = 'covered_by'
                ),
                inserted AS (
                    INSERT INTO evidence_bundles (
                        event_id, source_event_id, camera_id, source_id,
                        camera_name, event_type, event_created_at,
                        alarm_machine_time, media_status, evidence_state,
                        evidence_reason, raw_clip_uri, raw_clip_size_bytes,
                        raw_clip_duration_seconds, raw_clip_sha256,
                        raw_clip_content_type, annotation_status,
                        annotation_count, matched_objects, unknown_objects,
                        visual_evidence_status, frontend_overlay_required,
                        summary, materialization
                    )
                    SELECT
                        linked.event_id,
                        linked.source_event_id,
                        linked.camera_id,
                        linked.source_id,
                        linked.camera_name,
                        linked.event_type,
                        linked.event_created_at,
                        linked.event_created_at,
                        parent.media_status,
                        parent.evidence_state,
                        'covered_by_event:' || linked.bundle_event_id::text,
                        parent.raw_clip_uri,
                        parent.raw_clip_size_bytes,
                        parent.raw_clip_duration_seconds,
                        parent.raw_clip_sha256,
                        parent.raw_clip_content_type,
                        parent.annotation_status,
                        parent.annotation_count,
                        parent.matched_objects,
                        parent.unknown_objects,
                        parent.visual_evidence_status,
                        parent.frontend_overlay_required,
                        COALESCE(parent.summary, '{}'::jsonb)
                            || jsonb_build_object(
                                'coverage_relation', linked.relation,
                                'covered_by_event_id', linked.bundle_event_id::text,
                                'coverage_reason', linked.reason,
                                'coverage_metadata', linked.link_metadata
                            ),
                        COALESCE(parent.materialization, '{}'::jsonb)
                            || jsonb_build_object(
                                'coverage_relation', linked.relation,
                                'covered_by_event_id', linked.bundle_event_id::text,
                                'coverage_reason', linked.reason
                            )
                    FROM linked, parent
                    ON CONFLICT (event_id) DO UPDATE SET
                        source_event_id = EXCLUDED.source_event_id,
                        camera_id = EXCLUDED.camera_id,
                        source_id = EXCLUDED.source_id,
                        camera_name = EXCLUDED.camera_name,
                        event_type = EXCLUDED.event_type,
                        event_created_at = EXCLUDED.event_created_at,
                        alarm_machine_time = EXCLUDED.alarm_machine_time,
                        media_status = EXCLUDED.media_status,
                        evidence_state = EXCLUDED.evidence_state,
                        evidence_reason = EXCLUDED.evidence_reason,
                        raw_clip_uri = EXCLUDED.raw_clip_uri,
                        raw_clip_size_bytes = EXCLUDED.raw_clip_size_bytes,
                        raw_clip_duration_seconds = EXCLUDED.raw_clip_duration_seconds,
                        raw_clip_sha256 = EXCLUDED.raw_clip_sha256,
                        raw_clip_content_type = EXCLUDED.raw_clip_content_type,
                        annotation_status = EXCLUDED.annotation_status,
                        annotation_count = EXCLUDED.annotation_count,
                        matched_objects = EXCLUDED.matched_objects,
                        unknown_objects = EXCLUDED.unknown_objects,
                        visual_evidence_status = EXCLUDED.visual_evidence_status,
                        frontend_overlay_required = EXCLUDED.frontend_overlay_required,
                        summary = EXCLUDED.summary,
                        materialization = EXCLUDED.materialization,
                        updated_at = now()
                    RETURNING event_id
                )
                SELECT COUNT(*)::int FROM inserted
                """,
                {"bundle_event_id": bundle_event_id},
            )
            row = cur.fetchone()
            alias_count = int(row[0] or 0) if row else 0
            if alias_count <= 0:
                return 0
            cur.execute(
                """
                INSERT INTO evidence_artifacts (
                    event_id, artifact_type, uri, storage_backend, content_type,
                    compression, size_bytes, sha256, status, metadata
                )
                SELECT
                    l.event_id, ea.artifact_type, ea.uri, ea.storage_backend,
                    ea.content_type, ea.compression, ea.size_bytes, ea.sha256,
                    ea.status,
                    COALESCE(ea.metadata, '{}'::jsonb)
                        || jsonb_build_object(
                            'coverage_relation', l.relation,
                            'covered_by_event_id', l.bundle_event_id::text
                        )
                FROM evidence_event_links l
                JOIN evidence_artifacts ea
                  ON ea.event_id = l.bundle_event_id
                WHERE l.bundle_event_id = %(bundle_event_id)s::uuid
                  AND l.relation = 'covered_by'
                ON CONFLICT (event_id, artifact_type) DO UPDATE SET
                    uri = EXCLUDED.uri,
                    storage_backend = EXCLUDED.storage_backend,
                    content_type = EXCLUDED.content_type,
                    compression = EXCLUDED.compression,
                    size_bytes = EXCLUDED.size_bytes,
                    sha256 = EXCLUDED.sha256,
                    status = EXCLUDED.status,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """,
                {"bundle_event_id": bundle_event_id},
            )
            cur.execute(
                """
                INSERT INTO evidence_frame_timeline (
                    event_id, clip_frame_index, frame_uuid, frame_pts, frame_dts,
                    duration_ns, timestamp_ms, width, height, source_id,
                    camera_id, stream_session_id, keyframe_uuid, metadata
                )
                SELECT
                    l.event_id, eft.clip_frame_index, eft.frame_uuid,
                    eft.frame_pts, eft.frame_dts, eft.duration_ns,
                    eft.timestamp_ms, eft.width, eft.height, eft.source_id,
                    eft.camera_id, eft.stream_session_id, eft.keyframe_uuid,
                    COALESCE(eft.metadata, '{}'::jsonb)
                        || jsonb_build_object(
                            'coverage_relation', l.relation,
                            'covered_by_event_id', l.bundle_event_id::text
                        )
                FROM evidence_event_links l
                JOIN evidence_frame_timeline eft
                  ON eft.event_id = l.bundle_event_id
                WHERE l.bundle_event_id = %(bundle_event_id)s::uuid
                  AND l.relation = 'covered_by'
                ON CONFLICT (event_id, clip_frame_index) DO UPDATE SET
                    frame_uuid = EXCLUDED.frame_uuid,
                    frame_pts = EXCLUDED.frame_pts,
                    frame_dts = EXCLUDED.frame_dts,
                    duration_ns = EXCLUDED.duration_ns,
                    timestamp_ms = EXCLUDED.timestamp_ms,
                    width = EXCLUDED.width,
                    height = EXCLUDED.height,
                    source_id = EXCLUDED.source_id,
                    camera_id = EXCLUDED.camera_id,
                    stream_session_id = EXCLUDED.stream_session_id,
                    keyframe_uuid = EXCLUDED.keyframe_uuid,
                    metadata = EXCLUDED.metadata
                """,
                {"bundle_event_id": bundle_event_id},
            )
            cur.execute(
                """
                INSERT INTO evidence_overlay_segments (
                    event_id, clip_frame_index, frame_uuid, frame_pts, t_ms,
                    object_count, objects, record
                )
                SELECT
                    l.event_id, eos.clip_frame_index, eos.frame_uuid,
                    eos.frame_pts, eos.t_ms, eos.object_count, eos.objects,
                    COALESCE(eos.record, '{}'::jsonb)
                        || jsonb_build_object(
                            'coverage_relation', l.relation,
                            'covered_by_event_id', l.bundle_event_id::text
                        )
                FROM evidence_event_links l
                JOIN evidence_overlay_segments eos
                  ON eos.event_id = l.bundle_event_id
                WHERE l.bundle_event_id = %(bundle_event_id)s::uuid
                  AND l.relation = 'covered_by'
                ON CONFLICT (event_id, clip_frame_index) DO UPDATE SET
                    frame_uuid = EXCLUDED.frame_uuid,
                    frame_pts = EXCLUDED.frame_pts,
                    t_ms = EXCLUDED.t_ms,
                    object_count = EXCLUDED.object_count,
                    objects = EXCLUDED.objects,
                    record = EXCLUDED.record
                """,
                {"bundle_event_id": bundle_event_id},
            )
            cur.execute(
                """
                UPDATE events e
                SET media_status = 'materialized',
                    payload = COALESCE(e.payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(e.payload->'media', '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'evidence_state', 'materialized',
                                'evidence_reason',
                                    'covered_by_event:' || l.bundle_event_id::text,
                                'materialization_status', 'materialized',
                                'materialization_reason',
                                    'covered_by_event:' || l.bundle_event_id::text,
                                'covered_by_event_id', l.bundle_event_id::text,
                                'coverage_relation', l.relation,
                                'coverage_reason', l.reason,
                                'evidence_state_updated_at', now()
                            ))
                        ),
                    updated_at = now()
                FROM evidence_event_links l
                WHERE e.id = l.event_id
                  AND l.bundle_event_id = %(bundle_event_id)s::uuid
                  AND l.relation = 'covered_by'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM evidence_tasks et
                      WHERE et.event_id = e.id
                        AND COALESCE(et.materialization_failure_reason, '') = %(superseded_reason)s
                  )
                """,
                {
                    "bundle_event_id": bundle_event_id,
                    "superseded_reason": EPOCH_SUPERSEDED_INCOMPLETE_REASON,
                },
            )
            cur.execute(
                """
                UPDATE evidence_tasks et
                SET status = 'materialized',
                    materialization_status = 'materialized',
                    last_materialization_at = now(),
                    materialization_defer_reason = NULL,
                    error_message = NULL,
                    materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                        || jsonb_build_object(
                            'coverage',
                            jsonb_build_object(
                                'status', 'materialized_alias',
                                'covered_by_event_id', l.bundle_event_id::text,
                                'relation', l.relation,
                                'updated_at', now()
                            )
                        ),
                    updated_at = now()
                FROM evidence_event_links l
                WHERE et.event_id = l.event_id
                  AND l.bundle_event_id = %(bundle_event_id)s::uuid
                  AND l.relation = 'covered_by'
                  AND COALESCE(et.materialization_failure_reason, '') <> %(superseded_reason)s
                """,
                {
                    "bundle_event_id": bundle_event_id,
                    "superseded_reason": EPOCH_SUPERSEDED_INCOMPLETE_REASON,
                },
            )
            logger.info(
                "evidence_event_aliases_upserted bundle_event_id=%s count=%s",
                bundle_event_id,
                alias_count,
            )
            return alias_count
    except Exception:
        logger.exception(
            "failed to upsert covered evidence aliases bundle_event_id=%s",
            bundle_event_id,
        )
        return 0


def _reconcile_covered_event_aliases(
    pg_conn: psycopg.Connection,
    *,
    limit: int = 100,
) -> int:
    """Publish aliases for covered events linked after their parent was finalized."""

    if not _evidence_event_links_table_exists(pg_conn):
        return 0
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT l.bundle_event_id::text
                FROM evidence_event_links l
                JOIN evidence_bundles eb ON eb.event_id = l.bundle_event_id
                LEFT JOIN evidence_tasks child_task ON child_task.event_id = l.event_id
                LEFT JOIN evidence_bundles child_bundle ON child_bundle.event_id = l.event_id
                WHERE l.relation = 'covered_by'
                  AND child_bundle.event_id IS NULL
                  AND COALESCE(child_task.materialization_status, '') <> 'materialized'
                ORDER BY l.bundle_event_id::text
                LIMIT %(limit)s
                """,
                {"limit": max(1, int(limit))},
            )
            bundle_event_ids = [str(row[0]) for row in cur.fetchall()]
    except Exception:
        logger.exception("failed to list covered evidence aliases for reconcile")
        return 0

    updated = 0
    for bundle_event_id in bundle_event_ids:
        updated += _upsert_covered_event_aliases(
            pg_conn,
            bundle_event_id=bundle_event_id,
        )
    return updated


def _prune_success_evidence_sidecars(bundle_dir: str | Path) -> dict[str, int | list[str]]:
    return _prune_evidence_files(bundle_dir, SUCCESS_EVIDENCE_FILES_TO_PRUNE)


def _prune_db_backed_evidence_sidecars(bundle_dir: str | Path) -> dict[str, int | list[str]]:
    return _prune_evidence_files(bundle_dir, DB_BACKED_EVIDENCE_SIDECARS_TO_PRUNE)


def _evidence_db_index_expanded_rows_enabled() -> bool:
    return os.getenv("EVIDENCE_DB_INDEX_EXPANDED_ROWS_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _prune_evidence_files(
    bundle_dir: str | Path,
    filenames: tuple[str, ...],
) -> dict[str, int | list[str]]:
    root = Path(bundle_dir)
    deleted: list[str] = []
    errors = 0
    for name in filenames:
        path = root / name
        try:
            if path.is_file():
                path.unlink()
                deleted.append(name)
        except OSError:
            errors += 1
            logger.exception("evidence_sidecar_prune_failed path=%s", path)
    return {"deleted": deleted, "errors": errors}


class _MaterializationGuard:
    def __init__(self, max_active: int) -> None:
        self.max_active = max(0, int(max_active))
        self.active = 0
        self._lock = Lock()

    def acquire(self) -> bool:
        with self._lock:
            if self.max_active <= 0:
                return False
            if self.active >= self.max_active:
                return False
            self.active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self.active > 0:
                self.active -= 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "max_active": self.max_active,
                "active": self.active,
            }


class _MaterializationPacer:
    def __init__(
        self,
        *,
        max_per_poll: int = 0,
        throttle_sleep_s: float = 0.0,
        deadline_guard_s: float = 0.0,
        sleeper: object = time.sleep,
    ) -> None:
        self.max_per_poll = max(0, int(max_per_poll or 0))
        self.throttle_sleep_s = max(0.0, float(throttle_sleep_s or 0.0))
        self.deadline_guard_s = max(0.0, float(deadline_guard_s or 0.0))
        self._sleeper = sleeper
        self.processed_this_poll = 0
        self.throttled_this_poll = 0
        self.skipped_sleep_this_poll = 0

    def reset_poll(self) -> None:
        self.processed_this_poll = 0
        self.throttled_this_poll = 0
        self.skipped_sleep_this_poll = 0

    def can_start(
        self,
        *,
        deadline_at: datetime | None = None,
        now: datetime | None = None,
    ) -> bool:
        if self.max_per_poll <= 0 or self.processed_this_poll < self.max_per_poll:
            return True
        if deadline_at is None or self.deadline_guard_s <= 0:
            return False
        current = now or datetime.now(timezone.utc)
        if deadline_at.tzinfo is None:
            deadline_at = deadline_at.replace(tzinfo=timezone.utc)
        slack_s = (
            deadline_at.astimezone(timezone.utc) - current.astimezone(timezone.utc)
        ).total_seconds()
        return slack_s <= self.deadline_guard_s

    def record_start(self) -> None:
        self.processed_this_poll += 1

    def decision_after_finalize(
        self,
        *,
        deadline_at: datetime | None,
        now: datetime | None = None,
    ) -> dict:
        return _materialization_throttle_decision(
            throttle_sleep_s=self.throttle_sleep_s,
            deadline_guard_s=self.deadline_guard_s,
            deadline_at=deadline_at,
            now=now,
        )

    def apply_decision(self, decision: dict) -> None:
        if decision["sleep_s"] > 0:
            self.throttled_this_poll += 1
            self._sleeper(decision["sleep_s"])
        else:
            self.skipped_sleep_this_poll += 1

    def snapshot(self) -> dict[str, int | float]:
        return {
            "max_per_poll": self.max_per_poll,
            "processed_this_poll": self.processed_this_poll,
            "throttled_this_poll": self.throttled_this_poll,
            "skipped_sleep_this_poll": self.skipped_sleep_this_poll,
            "throttle_sleep_s": self.throttle_sleep_s,
            "deadline_guard_s": self.deadline_guard_s,
        }


def _materialization_throttle_decision(
    *,
    throttle_sleep_s: float,
    deadline_guard_s: float,
    deadline_at: datetime | None,
    now: datetime | None = None,
) -> dict:
    sleep_s = max(0.0, float(throttle_sleep_s or 0.0))
    guard_s = max(0.0, float(deadline_guard_s or 0.0))
    if sleep_s <= 0:
        return {
            "enabled": False,
            "sleep_s": 0.0,
            "reason": "disabled",
            "deadline_slack_s": None,
            "deadline_guard_s": guard_s,
        }
    slack_s: float | None = None
    if deadline_at is not None:
        current = now or datetime.now(timezone.utc)
        if deadline_at.tzinfo is None:
            deadline_at = deadline_at.replace(tzinfo=timezone.utc)
        slack_s = (deadline_at.astimezone(timezone.utc) - current.astimezone(timezone.utc)).total_seconds()
        if slack_s <= guard_s:
            return {
                "enabled": True,
                "sleep_s": 0.0,
                "reason": "deadline_guard",
                "deadline_slack_s": round(slack_s, 3),
                "deadline_guard_s": guard_s,
            }
    return {
        "enabled": True,
        "sleep_s": sleep_s,
        "reason": "paced",
        "deadline_slack_s": round(slack_s, 3) if slack_s is not None else None,
        "deadline_guard_s": guard_s,
    }


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _apply_materialization_cpu_thread_limit(limit: int) -> dict[str, object]:
    thread_limit = max(0, int(limit or 0))
    applied_env: dict[str, str] = {}
    cv2_threads_set = False
    cv2_error = ""
    if thread_limit <= 0:
        return {
            "enabled": False,
            "thread_limit": 0,
            "applied_env": applied_env,
            "cv2_threads_set": cv2_threads_set,
            "cv2_error": cv2_error,
        }

    value = str(thread_limit)
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        if not os.getenv(name):
            os.environ[name] = value
            applied_env[name] = value

    try:
        import cv2  # type: ignore[import-not-found]

        cv2.setNumThreads(thread_limit)
        cv2_threads_set = True
    except Exception as exc:
        cv2_error = f"{type(exc).__name__}:{exc}"

    return {
        "enabled": True,
        "thread_limit": thread_limit,
        "applied_env": applied_env,
        "cv2_threads_set": cv2_threads_set,
        "cv2_error": cv2_error,
    }


def _materialization_backlog_depth(pg_conn: psycopg.Connection) -> int:
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM evidence_tasks
                WHERE status IN (
                    'pending',
                    'replaying',
                    'finalizing',
                    'materialization_deferred'
                )
                """
            )
            row = cur.fetchone()
            return int(row[0] or 0) if row else 0
    except Exception:
        logger.exception("failed to query materialization backlog depth")
        return 0


def _high_priority_event_types() -> set[str]:
    raw = os.getenv("EVIDENCE_HIGH_PRIORITY_EVENT_TYPES", "watchlist_hit,live_search_hit")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _materialization_schedule_rows(
    pg_conn: psycopg.Connection,
    event_ids: list[str],
) -> dict[str, dict]:
    if not event_ids:
        return {}
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.id::text, e.event_type, et.materialization_deadline_at,
                       e.source_id,
                       COALESCE(
                           et.materialization_audit->>'replay_shard_id',
                           e.payload->'media'->>'replay_shard_id',
                           e.payload->'media'->>'replay_job_shard_id',
                           ''
                       ) AS replay_shard_id
                FROM events e
                LEFT JOIN evidence_tasks et ON et.event_id = e.id
                WHERE e.id = ANY(%(event_ids)s::uuid[])
                """,
                {"event_ids": event_ids},
            )
            rows = cur.fetchall() or []
    except Exception:
        logger.exception("failed to query materialization schedule rows")
        return {}
    result: dict[str, dict] = {}
    priority_types = _high_priority_event_types()
    for row in rows:
        event_id = row[0]
        event_type = row[1] if len(row) > 1 else ""
        deadline_at = row[2] if len(row) > 2 else None
        source_id = row[3] if len(row) > 3 else ""
        replay_shard_id = row[4] if len(row) > 4 else ""
        parsed_deadline = _parse_datetime(deadline_at)
        result[str(event_id)] = {
            "event_type": event_type or "",
            "materialization_deadline_at": parsed_deadline,
            "source_id": str(source_id or ""),
            "replay_shard_id": str(replay_shard_id or ""),
            "priority_rank": 0 if (event_type or "") in priority_types else 1,
        }
    return result


def _metadata_labels(meta: dict) -> dict:
    labels = meta.get("labels")
    if isinstance(labels, dict):
        return labels
    configuration = meta.get("configuration")
    if isinstance(configuration, dict):
        nested = configuration.get("labels")
        if isinstance(nested, dict):
            return nested
    return {}


def _metadata_source_id(meta: dict, schedule: dict | None = None) -> str:
    schedule = schedule or {}
    labels = _metadata_labels(meta)
    for value in (
        schedule.get("source_id"),
        meta.get("source_id"),
        meta.get("stored_stream_id"),
        labels.get("stored_stream_id"),
        labels.get("source_id"),
    ):
        if value:
            return str(value)
    resulting_stream_id = str(meta.get("resulting_stream_id") or labels.get("resulting_stream_id") or "")
    match = re.search(r"event-[0-9a-fA-F-]{36}", resulting_stream_id)
    if resulting_stream_id and not match:
        return resulting_stream_id
    return "unknown"


def _metadata_replay_shard_id(meta: dict, schedule: dict | None = None) -> str:
    schedule = schedule or {}
    labels = _metadata_labels(meta)
    for value in (
        schedule.get("replay_shard_id"),
        meta.get("replay_shard_id"),
        labels.get("replay_shard_id"),
        labels.get("replay_shard"),
    ):
        if value:
            return str(value)
    sink = meta.get("sink")
    if isinstance(sink, dict):
        url = str(sink.get("url") or "")
        if "video-file-sink-a" in url:
            return "replay-a"
        if "video-file-sink-b" in url:
            return "replay-b"
    return "default"


def _sort_metadata_for_materialization(
    metadata_files: list[dict],
    schedule_rows: dict[str, dict],
) -> list[dict]:
    max_deadline = datetime.max.replace(tzinfo=timezone.utc)

    def key(meta: dict) -> tuple[int, datetime, str, str]:
        event_id = _extract_event_id(meta)
        schedule = schedule_rows.get(event_id or "", {})
        deadline = schedule.get("materialization_deadline_at")
        if not isinstance(deadline, datetime):
            deadline = max_deadline
        elif deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        source_id = _metadata_source_id(meta, schedule)
        return (
            int(schedule.get("priority_rank", 1)),
            deadline.astimezone(timezone.utc),
            source_id,
            str(meta.get("_meta_dir", "")),
        )

    ordered = sorted(metadata_files, key=key)
    buckets: dict[tuple[int, str], list[dict]] = {}
    for meta in ordered:
        event_id = _extract_event_id(meta)
        schedule = schedule_rows.get(event_id or "", {})
        bucket_key = (
            int(schedule.get("priority_rank", 1)),
            _metadata_source_id(meta, schedule),
        )
        buckets.setdefault(bucket_key, []).append(meta)

    result: list[dict] = []
    while buckets:
        for bucket_key in sorted(
            list(buckets),
            key=lambda item: key(buckets[item][0]),
        ):
            bucket = buckets[bucket_key]
            result.append(bucket.pop(0))
            if not bucket:
                del buckets[bucket_key]
    return result


def _materialization_backlog_limit_exceeded(
    *,
    backlog_depth: int | None,
    max_backlog: int,
) -> bool:
    return (
        max_backlog > 0
        and backlog_depth is not None
        and backlog_depth >= max_backlog
    )


def _directory_usage_bytes(path: str | Path) -> int:
    root = Path(path)
    if not root.exists():
        return 0
    total = 0
    for item in root.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _storage_quota_decision(
    *,
    evidence_output_dir: str | None,
    sink_output_dir: str,
    incoming_dir: str | None = None,
    evidence_final_root_max_bytes: int = 0,
    evidence_incoming_root_max_bytes: int = 0,
    replay_sink_output_max_bytes: int = 0,
    warning_ratio: float = 0.80,
    critical_ratio: float = 0.90,
    hard_ratio: float = 1.00,
) -> dict:
    areas = []
    for name, path, limit in (
        ("evidence_final_root", evidence_output_dir, evidence_final_root_max_bytes),
        ("evidence_incoming_root", incoming_dir, evidence_incoming_root_max_bytes),
        ("replay_sink_output", sink_output_dir, replay_sink_output_max_bytes),
    ):
        if not path or limit <= 0:
            continue
        used = _directory_usage_bytes(path)
        ratio = float(used) / float(limit) if limit > 0 else 0.0
        if ratio >= hard_ratio:
            level = "hard"
        elif ratio >= critical_ratio:
            level = "critical"
        elif ratio >= warning_ratio:
            level = "warning"
        else:
            level = "normal"
        areas.append(
            {
                "area": name,
                "path": str(path),
                "used_bytes": used,
                "max_bytes": int(limit),
                "used_ratio": round(ratio, 6),
                "level": level,
            }
        )
    order = {"normal": 0, "warning": 1, "critical": 2, "hard": 3}
    overall = max((area["level"] for area in areas), key=lambda value: order[value], default="normal")
    return {
        "schema_version": "phase4-storage-quota-v1",
        "overall_level": overall,
        "areas": areas,
    }


def _materialization_guardrails(
    *,
    guard: _MaterializationGuard | None,
    pacer: _MaterializationPacer | None = None,
    timeout_s: float,
    max_backlog: int,
    backlog_depth: int | None,
    admission_status: str,
    reason: str = "",
) -> dict:
    snapshot = guard.snapshot() if guard is not None else {}
    pacer_snapshot = pacer.snapshot() if pacer is not None else {}
    return {
        "schema_version": "phase1a-materialization-guardrails-v1",
        "mode": "bounded_crop",
        "admission_status": admission_status,
        "reason": reason,
        "max_active": snapshot.get("max_active"),
        "active_at_decision": snapshot.get("active"),
        "timeout_s": float(timeout_s or 0.0),
        "max_backlog": int(max_backlog or 0),
        "backlog_depth_at_decision": backlog_depth,
        "max_per_poll": pacer_snapshot.get("max_per_poll"),
        "processed_this_poll": pacer_snapshot.get("processed_this_poll"),
        "throttle_sleep_s": pacer_snapshot.get("throttle_sleep_s"),
        "deadline_guard_s": pacer_snapshot.get("deadline_guard_s"),
    }


def _mark_media_materialization_deferred(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    sink_path: str,
    reason: str,
    guardrails: dict,
) -> bool:
    lease = current_lease(
        pg_conn,
        event_id=event_id,
        fallback_owner="media-finalizer",
        fallback_phase=MaterializationPhase.FINALIZER_PENDING.value,
    )
    return _mark_media_materialization_terminal(
        pg_conn,
        event_id=event_id,
        sink_path=sink_path,
        state=MATERIALIZATION_STATUS_DEFERRED,
        reason=reason,
        guardrails=guardrails,
        lease=lease,
    )


def _mark_media_materialization_failed(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    sink_path: str,
    reason: str,
    guardrails: dict,
    lease: MaterializationLease | None = None,
) -> bool:
    return _mark_media_materialization_terminal(
        pg_conn,
        event_id=event_id,
        sink_path=sink_path,
        state=MATERIALIZATION_STATUS_FAILED,
        reason=reason,
        guardrails=guardrails,
        lease=lease,
    )


def _mark_media_materialization_retry(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    sink_path: str,
    reason: str,
    guardrails: dict,
    retry_after_s: float = 1.0,
) -> bool:
    lease = current_lease(
        pg_conn,
        event_id=event_id,
        fallback_owner="media-finalizer",
        fallback_phase=MaterializationPhase.FINALIZER_PENDING.value,
    )
    if lease is not None and lease.phase in {
        MaterializationPhase.FINALIZER_PENDING.value,
        MaterializationPhase.FINALIZING.value,
    }:
        changed = retry_finalizer_handoff(
            pg_conn,
            lease,
            reason=reason,
            retry_hint_s=retry_after_s,
        )
    elif lease is not None:
        changed = retry_rolling_task(
            pg_conn,
            lease,
            reason=reason,
            retry_hint_s=retry_after_s,
        )
    else:
        changed = schedule_unclaimed_retry(
            pg_conn,
            event_id=event_id,
            reason=reason,
            retry_hint_s=retry_after_s,
            sink_output_path=sink_path,
        )
    if changed:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE evidence_tasks
                SET materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                        || jsonb_build_object(
                            'last_guardrails', %(guardrails)s::jsonb,
                            'last_reason_detail', %(reason)s::text,
                            'last_sink_output_path', %(sink_path)s::text
                        ),
                    updated_at = now()
                WHERE event_id = %(event_id)s::uuid
                """,
                {
                    "event_id": event_id,
                    "reason": reason,
                    "sink_path": sink_path,
                    "guardrails": json.dumps(guardrails),
                },
            )
    return changed


def _mark_media_materialization_terminal(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    sink_path: str,
    state: str,
    reason: str,
    guardrails: dict,
    lease: MaterializationLease | None = None,
) -> bool:
    try:
        if state == MATERIALIZATION_STATUS_DEFERRED:
            changed = defer_terminal_task(
                pg_conn,
                event_id=event_id,
                reason=reason,
                sink_output_path=sink_path,
                lease=lease,
            )
        elif state == MATERIALIZATION_STATUS_FAILED:
            lease = lease or current_lease(
                pg_conn,
                event_id=event_id,
                fallback_owner="media-finalizer",
                fallback_phase=MaterializationPhase.FINALIZING.value,
            )
            if lease is not None:
                changed = fail_rolling_task(pg_conn, lease, reason=reason)
            else:
                changed = fail_unclaimed_task(
                    pg_conn,
                    event_id=event_id,
                    reason=reason,
                    sink_output_path=sink_path,
                )
        else:
            raise ValueError(f"unsupported terminal materialization state: {state}")
        if not changed:
            return False
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_build_object(
                                'materialization_guardrails', %(guardrails)s::jsonb
                            )
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {
                    "event_id": event_id,
                    "sink_path": sink_path,
                    "state": state,
                    "reason": reason,
                    "guardrails": json.dumps(guardrails),
                },
            )
            cur.execute(
                """
                UPDATE evidence_tasks
                SET materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                        || jsonb_build_object(
                            'last_guardrails', %(guardrails)s::jsonb,
                            'last_reason_detail', %(reason)s::text,
                            'last_sink_output_path', %(sink_path)s::text
                        ),
                    updated_at = now()
                WHERE event_id = %(event_id)s::uuid
                  AND materialization_status = %(state)s
                """,
                {
                    "event_id": event_id,
                    "state": state,
                    "reason": reason,
                    "sink_path": sink_path,
                    "guardrails": json.dumps(guardrails),
                },
            )
        return True
    except Exception:
        logger.exception(
            "failed to mark materialization state event_id=%s state=%s",
            event_id,
            state,
        )
        return False


def _claim_media_finalization(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    sink_path: str,
    worker_id: str,
) -> dict[str, object]:
    try:
        return claim_finalizer_task(
            pg_conn,
            event_id=event_id,
            sink_output_path=sink_path,
            worker_id=worker_id,
            lease_seconds=max(
                1.0,
                _materialization_timeout_s()
                or float(os.getenv("ROLLING_CACHE_MATERIALIZATION_PROCESSING_DEADLINE_SECONDS", "120")),
            ),
        )
    except Exception as exc:
        logger.exception("media_finalization_claim_failed event_id=%s", event_id)
        return {
            "status": "claim_error",
            "claimed": False,
            "error": f"{type(exc).__name__}:{exc}",
        }


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _env_positive_float(name: str, default: float) -> float:
    value = _to_float(os.getenv(name))
    if value is None or value <= 0:
        return float(default)
    return float(value)


def _media_probe_timeout_s() -> float:
    return _env_positive_float("MEDIA_PROBE_TIMEOUT_S", DEFAULT_MEDIA_PROBE_TIMEOUT_S)


def _media_decode_timeout_s() -> float:
    return _env_positive_float("MEDIA_DECODE_TIMEOUT_S", DEFAULT_MEDIA_DECODE_TIMEOUT_S)


def _materialization_timeout_s() -> float:
    return max(
        0.0,
        _to_float(os.getenv("MEDIA_WORKER_MATERIALIZATION_TIMEOUT_S"))
        or DEFAULT_MATERIALIZATION_TIMEOUT_S,
    )


def _probe_metrics_snapshot() -> dict[str, int]:
    metrics = getattr(_PROBE_METRICS_LOCAL, "metrics", None)
    if metrics is None:
        metrics = {key: 0 for key in _PROBE_METRICS}
        _PROBE_METRICS_LOCAL.metrics = metrics
    return dict(metrics)


def _probe_metrics_delta(before: dict[str, int]) -> dict[str, int]:
    metrics = _probe_metrics_snapshot()
    return {
        key: int(metrics.get(key, 0)) - int(before.get(key, 0))
        for key in _PROBE_METRICS
    }


def _record_probe_metric(tool: str, duration_s: float) -> None:
    duration_ms = int(max(duration_s, 0.0) * 1000)
    if tool == "ffprobe":
        updates = {
            "ffprobe_invocation_count": 1,
            "ffprobe_duration_ms": duration_ms,
        }
    elif tool == "imageio_ffmpeg":
        updates = {
            "imageio_ffmpeg_fallback_count": 1,
            "imageio_ffmpeg_fallback_duration_ms": duration_ms,
        }
    else:
        updates = {
            "ffmpeg_invocation_count": 1,
            "ffmpeg_duration_ms": duration_ms,
        }
    with _PROBE_METRICS_LOCK:
        for key, value in updates.items():
            _PROBE_METRICS[key] += value
    thread_metrics = _probe_metrics_snapshot()
    for key, value in updates.items():
        thread_metrics[key] += value
    _PROBE_METRICS_LOCAL.metrics = thread_metrics


def _decoded_frame_count_with_timing(
    raw_clip_path: Path,
    *,
    raw_clip_available: bool,
    known_frame_count: int | None = None,
) -> tuple[int, int]:
    if known_frame_count is not None and known_frame_count >= 0:
        return int(known_frame_count), 0
    started = time.monotonic()
    decoded_frame_count = (
        read_decoded_video_frame_count(raw_clip_path)
        if raw_clip_available and raw_clip_path.is_file()
        else 0
    )
    return int(decoded_frame_count), int((time.monotonic() - started) * 1000)


def _event_context_from_row(event_id: str, row: tuple) -> dict:
    payload = row[6] if len(row) > 6 else {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        payload = {}
    media = payload.get("media", {}) if isinstance(payload, dict) else {}
    if not isinstance(media, dict):
        media = {}

    return {
        "event_id": event_id,
        "event_type": row[0] if len(row) > 0 else "",
        "camera_id": row[1] if len(row) > 1 else "",
        "source_id": row[2] if len(row) > 2 else "",
        "track_id": row[3] if len(row) > 3 else "",
        "event_ts_ms": row[4] if len(row) > 4 else 0,
        "frame_uuid": row[5] if len(row) > 5 else "",
        "payload": payload,
        "confidence": row[7] if len(row) > 7 else 0.0,
        "source_event_id": row[8] if len(row) > 8 else payload.get("source_event_id", ""),
        "keyframe_uuid": row[9] if len(row) > 9 else payload.get("keyframe_uuid", ""),
        "severity": row[10] if len(row) > 10 else "",
        "evidence_policy": row[11] if len(row) > 11 else {},
        "created_at": row[12] if len(row) > 12 else None,
        "previous_keyframe_uuid": media.get("previous_keyframe_uuid", ""),
    }

def _load_event_context(pg_conn: psycopg.Connection, event_id: str) -> dict:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_type, camera_id, source_id, track_id, event_ts_ms,
                   frame_uuid, payload, confidence, source_event_id, keyframe_uuid,
                   severity, evidence_policy, created_at
            FROM events
            WHERE id = %s::uuid
            """,
            (event_id,),
        )
        row = cur.fetchone()

    if not row:
        raise ValueError(f"event not found: {event_id}")

    return _event_context_from_row(event_id, row)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes")


def _load_sink_metadata_file(metadata_file: str) -> dict:
    path = Path(metadata_file)
    data = _parse_ndjson(path)
    if isinstance(data, dict):
        return data
    try:
        with open(path, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("failed to load sink metadata file=%s", metadata_file)
        return {}


def _runtime_epoch_strict_enabled() -> bool:
    return _env_bool("EVIDENCE_RUNTIME_EPOCH_STRICT", default=True)


def _runtime_epoch_base_dir(sink_dir: str | Path) -> Path:
    path = Path(sink_dir)
    for candidate in (path, *path.parents):
        if candidate.name == "midterm":
            return candidate
    return path


def _runtime_epoch_state_path(sink_dir: str | Path | None = None) -> Path:
    configured = os.getenv("RUNTIME_EPOCH_STATE_PATH")
    if configured:
        return Path(configured)
    if sink_dir is not None:
        return _runtime_epoch_base_dir(sink_dir) / ".current_epoch.json"
    return Path(DEFAULT_RUNTIME_EPOCH_STATE_PATH)


def _read_current_runtime_epoch_state(sink_dir: str | Path | None = None) -> dict:
    path = _runtime_epoch_state_path(sink_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("failed to read runtime epoch state path=%s", path)
        return {}


def _current_runtime_epoch_id(sink_dir: str | Path | None = None) -> str:
    env_value = os.getenv("RUNTIME_EPOCH_ID") or os.getenv("VIDEO_ANALYTICS_RUNTIME_EPOCH_ID")
    if env_value:
        return str(env_value)
    state = _read_current_runtime_epoch_state(sink_dir)
    return str(state.get("runtime_epoch_id") or "")


def _active_epoch_sink_output_dir(sink_dir: str) -> str:
    runtime_epoch_id = _current_runtime_epoch_id(sink_dir)
    if not runtime_epoch_id:
        return sink_dir
    return str(_runtime_epoch_base_dir(sink_dir) / "epochs" / runtime_epoch_id)


def _runtime_epoch_from_path(path: str | Path) -> str:
    parts = Path(path).parts
    for index, part in enumerate(parts[:-1]):
        if part == "epochs" and index + 1 < len(parts):
            return parts[index + 1]
    return ""


def _metadata_labels(sink_metadata: dict) -> dict:
    labels = sink_metadata.get("labels")
    if isinstance(labels, dict):
        return labels
    configuration = sink_metadata.get("configuration")
    if isinstance(configuration, dict):
        labels = configuration.get("labels")
        if isinstance(labels, dict):
            return labels
    return {}


def _runtime_epoch_from_event_context(event_context: dict) -> str:
    payload = event_context.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    media = payload.get("media")
    media = media if isinstance(media, dict) else {}
    return str(
        event_context.get("runtime_epoch_id")
        or payload.get("runtime_epoch_id")
        or media.get("runtime_epoch_id")
        or ""
    )


def _runtime_epoch_guard(
    *,
    event_context: dict,
    replay_labels: dict,
    sink_metadata: dict,
    meta_dir: str,
) -> dict:
    labels = _metadata_labels(sink_metadata)
    current_epoch_id = _current_runtime_epoch_id(meta_dir)
    fields = {
        "event_payload_runtime_epoch_id": _runtime_epoch_from_event_context(event_context),
        "record_request_runtime_epoch_id": str(replay_labels.get("runtime_epoch_id") or ""),
        "replay_labels_runtime_epoch_id": str(replay_labels.get("runtime_epoch_id") or ""),
        "sink_metadata_runtime_epoch_id": str(labels.get("runtime_epoch_id") or ""),
        "sink_path_runtime_epoch_id": _runtime_epoch_from_path(meta_dir),
        "current_runtime_epoch_id": current_epoch_id,
    }
    strict = _runtime_epoch_strict_enabled()
    # The official video-file-sink writes native frame metadata and does not
    # preserve Replay labels. Treat sink metadata epoch as optional, but still
    # fail closed when it is present and disagrees with the active epoch.
    required_keys = tuple(
        key for key in fields.keys() if key != "sink_metadata_runtime_epoch_id"
    )
    missing = [key for key in fields.keys() if not fields.get(key)]
    required_missing = [key for key in required_keys if not fields.get(key)]
    nonempty_values = [str(value) for value in fields.values() if value]
    expected = current_epoch_id or (nonempty_values[0] if nonempty_values else "")
    mismatched = [
        key
        for key, value in fields.items()
        if value and expected and str(value) != expected
    ]
    failed = bool(mismatched) or (strict and bool(required_missing))
    reason = ""
    if mismatched:
        reason = "missing_or_mismatched_runtime_epoch"
    elif strict and required_missing:
        reason = "missing_or_mismatched_runtime_epoch"
    return {
        "runtime_epoch_id": expected,
        "epoch_guard_status": "failed" if failed else "passed",
        "epoch_guard_failed": failed,
        "epoch_guard_reason": reason,
        "runtime_epoch_strict": strict,
        "runtime_epoch_fields": fields,
        "runtime_epoch_missing_fields": missing,
        "runtime_epoch_required_missing_fields": required_missing,
        "runtime_epoch_mismatched_fields": mismatched,
    }


def _merge_runtime_epoch_guard(summary: dict, epoch_guard: dict) -> dict:
    summary.update(epoch_guard)
    return summary


def _relax_rolling_cache_epoch_guard(epoch_guard: dict) -> dict:
    """Rolling-cache materialization has no per-event Replay labels to verify."""

    fields = epoch_guard.get("runtime_epoch_fields")
    if not isinstance(fields, dict):
        return epoch_guard
    required_missing = set(epoch_guard.get("runtime_epoch_required_missing_fields") or [])
    replay_only_missing = {
        "record_request_runtime_epoch_id",
        "replay_labels_runtime_epoch_id",
    }
    if not required_missing or not required_missing.issubset(replay_only_missing):
        return epoch_guard
    mismatched = list(epoch_guard.get("runtime_epoch_mismatched_fields") or [])
    if mismatched:
        return epoch_guard
    available_keys = (
        "event_payload_runtime_epoch_id",
        "sink_path_runtime_epoch_id",
        "sink_metadata_runtime_epoch_id",
        "current_runtime_epoch_id",
    )
    values = {str(fields.get(key) or "") for key in available_keys if fields.get(key)}
    if len(values) != 1:
        return epoch_guard
    return {
        **epoch_guard,
        "epoch_guard_status": "relaxed",
        "epoch_guard_failed": False,
        "epoch_guard_reason": "rolling_cache_no_replay_labels",
        "runtime_epoch_required_missing_fields": sorted(required_missing),
    }


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _media_worker_state_path(sink_dir: str | Path, configured: str | None = None) -> Path:
    if configured:
        return Path(configured)
    env_value = os.getenv("MEDIA_WORKER_STATE_PATH")
    if env_value:
        return Path(env_value)
    if sink_dir:
        return _runtime_epoch_base_dir(sink_dir) / ".media-worker.processed.json"
    return Path(DEFAULT_MEDIA_WORKER_STATE_PATH)


def _load_processed_sink_state(path: str | Path | None) -> set[str]:
    if path is None:
        return set()
    state_path = Path(path)
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except Exception:
        logger.exception("failed to read media worker state path=%s", state_path)
        return set()
    raw_dirs = data.get("processed_dirs") if isinstance(data, dict) else data
    if not isinstance(raw_dirs, list):
        return set()
    return {str(item) for item in raw_dirs if item}


def _save_processed_sink_state(path: str | Path | None, processed_dirs: set[str]) -> None:
    if path is None:
        return
    state_path = Path(path)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            state_path,
            {
                "schema_version": "1.0",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "processed_dir_count": len(processed_dirs),
                "processed_dirs": sorted(processed_dirs),
            },
        )
    except Exception:
        logger.exception("failed to write media worker state path=%s", state_path)


def _json_isoformat(value: object) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        return normalized.isoformat()
    return str(value)


def _datetime_or_none(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _elapsed_ms_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return int(max(0.0, (end - start).total_seconds()) * 1000)


def _datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _datetime_from_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _elapsed_ms_between_iso(start: object, end: object) -> int | None:
    return _elapsed_ms_between(_datetime_from_iso(start), _datetime_from_iso(end))


def _phase_elapsed_ms(start: object, end: object) -> int | None:
    try:
        if start is None or end is None:
            return None
        return int(max(0.0, float(end) - float(start)) * 1000)
    except (TypeError, ValueError):
        return None


def _mark_sink_phase(meta_dir: str, event_id: str, phase: str) -> dict[str, object]:
    if not meta_dir:
        return {}
    now = datetime.now(timezone.utc)
    monotonic_now = time.monotonic()
    with _SINK_PHASES_LOCK:
        entry = _SINK_PHASES.setdefault(meta_dir, {})
        entry.setdefault("event_id", event_id)
        entry.setdefault(f"{phase}_at", now.isoformat())
        entry.setdefault(f"{phase}_monotonic", monotonic_now)
        return dict(entry)


def _sink_phase_snapshot(meta_dir: str) -> dict[str, object]:
    if not meta_dir:
        return {}
    with _SINK_PHASES_LOCK:
        return dict(_SINK_PHASES.get(meta_dir) or {})


def _clear_sink_phase(meta_dir: str) -> None:
    if not meta_dir:
        return
    with _SINK_PHASES_LOCK:
        _SINK_PHASES.pop(meta_dir, None)


def _release_replay_slot_for_sink_stable(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    phase_diagnostics: dict[str, object],
) -> bool:
    """Release completion-aware Replay admission once sink video is stable."""
    if not event_id:
        return False
    sink_video_to_stable_ms = _elapsed_ms_between_iso(
        phase_diagnostics.get("sink_video_first_seen_at"),
        phase_diagnostics.get("sink_video_stable_at"),
    )
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return False
    try:
        with cursor_factory() as cur:
            cur.execute(
                """
                WITH released AS (
                    UPDATE evidence_tasks
                    SET replay_slot_status = 'released',
                        replay_slot_released_at = now(),
                        replay_slot_release_reason = 'sink_video_stable',
                        replay_slot_active_age_s = EXTRACT(
                            EPOCH FROM (now() - replay_slot_acquired_at)
                        ),
                        sink_video_to_stable_ms = COALESCE(
                            %(sink_video_to_stable_ms)s::int,
                            sink_video_to_stable_ms
                        ),
                        materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                            || jsonb_build_object(
                                'replay_slot',
                                jsonb_strip_nulls(jsonb_build_object(
                                    'status', 'released',
                                    'release_reason', 'sink_video_stable',
                                    'released_at', now(),
                                    'active_age_s', EXTRACT(
                                        EPOCH FROM (now() - replay_slot_acquired_at)
                                    ),
                                    'sink_video_to_stable_ms',
                                        %(sink_video_to_stable_ms)s::int,
                                    'replay_job_id', replay_job_id,
                                    'resulting_stream_id', replay_resulting_stream_id
                                ))
                            ),
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                      AND replay_slot_status = 'active'
                    RETURNING event_id,
                              replay_job_id,
                              replay_resulting_stream_id,
                              replay_slot_active_age_s
                )
                UPDATE events e
                SET payload = COALESCE(e.payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(e.payload->'media', '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'replay_slot_status', 'released',
                                'replay_slot_released_at', now(),
                                'replay_slot_release_reason', 'sink_video_stable',
                                'replay_slot_active_age_s',
                                    released.replay_slot_active_age_s,
                                'sink_video_to_stable_ms',
                                    %(sink_video_to_stable_ms)s::int,
                                'replay_job_id', released.replay_job_id,
                                'resulting_stream_id',
                                    released.replay_resulting_stream_id
                            ))
                        ),
                    updated_at = now()
                FROM released
                WHERE e.id = released.event_id
                """,
                {
                    "event_id": event_id,
                    "sink_video_to_stable_ms": sink_video_to_stable_ms,
                },
            )
            released = bool(getattr(cur, "rowcount", 0) or 0)
            if released:
                logger.info(
                    "replay_slot_released event_id=%s release_reason=sink_video_stable "
                    "sink_video_to_stable_ms=%s",
                    event_id,
                    sink_video_to_stable_ms,
                )
            else:
                logger.info(
                    "replay_slot_release_transition event_id=%s "
                    "release_reason=sink_video_stable result=noop "
                    "sink_video_to_stable_ms=%s",
                    event_id,
                    sink_video_to_stable_ms,
                )
            return released
    except Exception:
        logger.exception("release_replay_slot_for_sink_stable failed event_id=%s", event_id)
        return False


def _record_replay_slot_finalization_duration(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    finalization_duration_ms: int,
) -> bool:
    if not event_id:
        return False
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return False
    try:
        with cursor_factory() as cur:
            cur.execute(
                """
                UPDATE evidence_tasks
                SET finalization_duration_ms = %(finalization_duration_ms)s::int,
                    materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                        || jsonb_build_object(
                            'replay_slot_finalization',
                            jsonb_build_object(
                                'finalization_duration_ms',
                                    %(finalization_duration_ms)s::int,
                                'recorded_at', now()
                            )
                        ),
                    updated_at = now()
                WHERE event_id = %(event_id)s::uuid
                  AND replay_slot_status IN ('active', 'released', 'timeout')
                """,
                {
                    "event_id": event_id,
                    "finalization_duration_ms": int(finalization_duration_ms),
                },
            )
            task_updated = bool(getattr(cur, "rowcount", 0) or 0)
            cur.execute(
                """
                UPDATE events
                SET payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_build_object(
                                'finalization_duration_ms',
                                    %(finalization_duration_ms)s::int
                            )
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {
                    "event_id": event_id,
                    "finalization_duration_ms": int(finalization_duration_ms),
                },
            )
            return task_updated or bool(getattr(cur, "rowcount", 0) or 0)
    except Exception:
        logger.exception(
            "record_replay_slot_finalization_duration failed event_id=%s",
            event_id,
        )
        return False


def _event_evidence_diagnostics(event_context: dict) -> dict:
    payload = event_context.get("payload", {})
    media = payload.get("media", {}) if isinstance(payload, dict) else {}
    if not isinstance(media, dict):
        return {}
    diagnostics = media.get("evidence_diagnostics")
    return diagnostics if isinstance(diagnostics, dict) else {}


def _post_savant_phase_latency_metrics(
    *,
    event_context: dict,
    phase_diagnostics: dict | None,
    started_at: datetime,
) -> dict[str, object]:
    diagnostics = _event_evidence_diagnostics(event_context)
    phase = phase_diagnostics if isinstance(phase_diagnostics, dict) else {}
    started_iso = started_at.astimezone(timezone.utc).isoformat()
    replay_job_created_at = diagnostics.get("replay_job_created_at")
    sink_metadata_first_seen_at = phase.get("sink_metadata_first_seen_at")
    sink_video_first_seen_at = phase.get("sink_video_first_seen_at")
    sink_video_stable_at = phase.get("sink_video_stable_at")
    sink_ffprobe_ready_at = phase.get("sink_ffprobe_ready_at")
    finalizer_submitted_at = phase.get("finalizer_submitted_at")
    finalizer_started_at = phase.get("finalizer_started_at") or started_iso
    return {
        "proof_wait_ms": diagnostics.get("proof_wait_ms"),
        "record_request_pending_ms": diagnostics.get("record_request_pending_ms"),
        "replay_job_create_ms": diagnostics.get("replay_job_create_ms"),
        "replay_slot_hold_ms": diagnostics.get("replay_slot_hold_ms"),
        "replay_active_global_count_before_create": diagnostics.get(
            "replay_active_global_count_before_create"
        ),
        "replay_active_shard_count_before_create": diagnostics.get(
            "replay_active_shard_count_before_create"
        ),
        "replay_active_source_count_before_create": diagnostics.get(
            "replay_active_source_count_before_create"
        ),
        "replay_job_created_at": replay_job_created_at,
        "sink_metadata_first_seen_at": sink_metadata_first_seen_at,
        "sink_video_first_seen_at": sink_video_first_seen_at,
        "sink_video_stable_at": sink_video_stable_at,
        "sink_ffprobe_ready_at": sink_ffprobe_ready_at,
        "finalizer_submitted_at": finalizer_submitted_at,
        "finalizer_started_at": finalizer_started_at,
        "replay_to_sink_metadata_ms": _elapsed_ms_between_iso(
            replay_job_created_at,
            sink_metadata_first_seen_at,
        ),
        "sink_metadata_to_video_ms": _elapsed_ms_between_iso(
            sink_metadata_first_seen_at,
            sink_video_first_seen_at,
        ),
        "sink_video_to_stable_ms": _elapsed_ms_between_iso(
            sink_video_first_seen_at,
            sink_video_stable_at,
        ),
        "sink_stable_to_ffprobe_ready_ms": _elapsed_ms_between_iso(
            sink_video_stable_at,
            sink_ffprobe_ready_at,
        ),
        "sink_ffprobe_ready_to_finalizer_start_ms": _elapsed_ms_between_iso(
            sink_ffprobe_ready_at,
            finalizer_started_at,
        ),
        "finalizer_pool_wait_ms": _phase_elapsed_ms(
            phase.get("finalizer_submitted_monotonic"),
            phase.get("finalizer_started_monotonic"),
        ),
    }


def _post_savant_materialization_metrics(
    *,
    summary: dict,
    event_context: dict,
    started_at: datetime,
    finished_at: datetime,
    finalization_duration_ms: int,
    finalization_process_cpu_seconds: float | None = None,
    finalization_thread_cpu_seconds: float | None = None,
    job_probe_metrics: dict[str, int] | None = None,
    materialization_guardrails: dict | None = None,
    phase_diagnostics: dict | None = None,
) -> dict:
    video_crop = summary.get("video_crop") if isinstance(summary, dict) else {}
    video_crop = video_crop if isinstance(video_crop, dict) else {}
    event_created_at = _datetime_or_none(event_context.get("created_at"))
    started_at = started_at.astimezone(timezone.utc)
    finished_at = finished_at.astimezone(timezone.utc)
    queue_wait_ms = _elapsed_ms_between(event_created_at, started_at)
    lifecycle_elapsed_ms = _elapsed_ms_between(event_created_at, finished_at)
    phase_latency_ms = _post_savant_phase_latency_metrics(
        event_context=event_context,
        phase_diagnostics=phase_diagnostics,
        started_at=started_at,
    )
    return {
        "measurement_schema_version": "phase0-materialization-v2",
        "materialization_mode": (
            video_crop.get("materialization_mode")
            or ("baseline_crop" if video_crop.get("crop_video_to_time_window") else "copy")
        ),
        "method": video_crop.get("method"),
        "crop_video_to_time_window": bool(video_crop.get("crop_video_to_time_window")),
        "guardrail_mode": (
            (materialization_guardrails or {}).get("mode") or "baseline_crop"
        ),
        "guardrails": materialization_guardrails or {},
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "finalization_elapsed_ms": int(finalization_duration_ms),
        "queue_wait_ms": queue_wait_ms,
        "lifecycle_elapsed_ms": lifecycle_elapsed_ms,
        "phase_latency_ms": phase_latency_ms,
        **{
            key: value
            for key, value in phase_latency_ms.items()
            if key.endswith("_ms") or key.startswith("replay_active_")
        },
        "input_bytes": video_crop.get("input_bytes"),
        "input_duration_seconds": video_crop.get("input_duration_seconds"),
        "output_bytes": video_crop.get("output_bytes"),
        "source_metadata_frame_count": video_crop.get("source_metadata_frame_count"),
        "source_metadata_duration_seconds": video_crop.get(
            "source_metadata_duration_seconds"
        ),
        "ffmpeg_returncode": video_crop.get("ffmpeg_returncode"),
        "ffmpeg_timeout_s": video_crop.get("ffmpeg_timeout_s"),
        "ffmpeg_elapsed_ms": video_crop.get("ffmpeg_elapsed_ms"),
        "ffmpeg_child_cpu_seconds": video_crop.get("ffmpeg_child_cpu_seconds"),
        "finalization_process_cpu_seconds": finalization_process_cpu_seconds,
        "finalization_process_cpu_seconds_scope": (
            "legacy_process_wide_delta_not_job_attributable"
        ),
        "finalization_thread_cpu_seconds": finalization_thread_cpu_seconds,
        "job_probe_metrics_scope": "thread_local_job_delta",
        "job_probe_metrics": dict(job_probe_metrics or {}),
        **dict(job_probe_metrics or {}),
        "correlation": materialization_correlation(
            event_context,
            phase_diagnostics=phase_diagnostics,
        ),
        "ffmpeg_stderr_bytes": video_crop.get("ffmpeg_stderr_bytes"),
        "decoded_frame_count": video_crop.get("decoded_frame_count"),
        "decoded_frame_count_probe_elapsed_ms": video_crop.get(
            "decoded_frame_count_probe_elapsed_ms"
        ),
    }


def _probe_duration_with_imageio_ffmpeg(path: str) -> float | None:
    try:
        import imageio_ffmpeg  # type: ignore

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        started = time.monotonic()
        result = subprocess.run(
            [ffmpeg, "-i", path],
            check=False,
            capture_output=True,
            text=True,
            timeout=_media_probe_timeout_s(),
        )
        _record_probe_metric("imageio_ffmpeg", time.monotonic() - started)
        match = re.search(
            r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
            result.stderr or "",
        )
        if match:
            hours = float(match.group(1))
            minutes = float(match.group(2))
            seconds = float(match.group(3))
            duration_value = hours * 3600 + minutes * 60 + seconds
            if duration_value > 0:
                logger.warning(
                    "ffprobe unavailable; duration probed via imageio_ffmpeg "
                    "ffmpeg path=%s duration=%.6f",
                    path,
                    duration_value,
                )
                return duration_value

        logger.warning(
            "ffprobe unavailable; bounded imageio_ffmpeg duration fallback "
            "did not return duration path=%s timeout_s=%.1f",
            path,
            _media_probe_timeout_s(),
        )
    except Exception:
        logger.exception("imageio_ffmpeg duration fallback failed path=%s", path)
    return None


def _probe_video_duration_seconds(path: str) -> float | None:
    """Return video duration in seconds using ffprobe when available."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            started = time.monotonic()
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    path,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=_media_probe_timeout_s(),
            )
            _record_probe_metric("ffprobe", time.monotonic() - started)
            if result.returncode == 0:
                data = json.loads(result.stdout or "{}")
                duration = _to_float((data.get("format") or {}).get("duration"))
                if duration is not None and duration > 0:
                    return duration
            logger.warning(
                "ffprobe duration probe failed path=%s returncode=%s stderr=%s",
                path,
                result.returncode,
                result.stderr.strip(),
            )
        except Exception:
            logger.exception("ffprobe duration probe failed path=%s", path)
    else:
        logger.warning("ffprobe not found for raw clip duration path=%s", path)

    return _probe_duration_with_imageio_ffmpeg(path)


_DECODE_ERROR_MARKERS = (
    "corrupt",
    "concealing",
    "decode_slice",
    "error while decoding",
    "invalid data",
    "missing reference",
    "non-existing",
    "no frame",
)


def _ffmpeg_exe() -> str | None:
    """Resolve an ffmpeg binary for whole-clip decode validation."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        logger.warning("ffmpeg not found for raw clip decode validation")
        return None


def _stderr_sample(stderr: str, limit: int = 5) -> list[str]:
    return [line.strip() for line in stderr.splitlines() if line.strip()][:limit]


def _probe_clip_decode(path: str) -> dict:
    """Decode the whole clip once and count decoder-error lines."""
    result = {
        "decode_error_count": 0,
        "decode_error_sample": [],
        "decode_ok": None,
        "probe_tool": None,
        "probe_error": "",
    }
    if not Path(path).is_file():
        result["probe_error"] = "clip file not found"
        return result

    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        result["probe_error"] = "ffmpeg unavailable"
        return result
    result["probe_tool"] = ffmpeg

    try:
        started = time.monotonic()
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", path, "-f", "null", "-"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_media_decode_timeout_s(),
        )
        _record_probe_metric("ffmpeg", time.monotonic() - started)
    except Exception as exc:
        logger.warning("clip decode validation failed path=%s error=%s", path, exc)
        result["probe_error"] = str(exc)
        return result

    stderr = proc.stderr or ""
    error_lines = [
        line.strip()
        for line in stderr.splitlines()
        if any(marker in line.lower() for marker in _DECODE_ERROR_MARKERS)
    ]
    if proc.returncode != 0 and not error_lines:
        error_lines = _stderr_sample(stderr)
    result["decode_error_count"] = len(error_lines)
    result["decode_error_sample"] = error_lines[:5]
    result["decode_ok"] = proc.returncode == 0 and not error_lines
    if proc.returncode != 0 and not result["probe_error"]:
        result["probe_error"] = f"ffmpeg exited with status {proc.returncode}"
    return result


def _duration_spec_seconds(spec: object) -> float | None:
    if not isinstance(spec, dict):
        return None
    secs = _to_float(spec.get("secs"))
    nanos = _to_float(spec.get("nanos"))
    if secs is None and nanos is None:
        return None
    return float(secs or 0.0) + float(nanos or 0.0) / 1_000_000_000.0


def _expected_clip_seconds(
    stop_condition: dict,
    configuration: dict,
    offset_seconds: object,
) -> float:
    """Best-effort expected raw clip duration from the Replay job request."""
    if isinstance(stop_condition, dict):
        ts_delta = stop_condition.get("ts_delta_sec")
        if isinstance(ts_delta, dict):
            max_delta = _to_float(ts_delta.get("max_delta_sec"))
            if max_delta is not None and max_delta > 0:
                return max_delta

        frame_count = _to_float(stop_condition.get("frame_count"))
        min_duration = (
            configuration.get("min_duration")
            if isinstance(configuration, dict)
            else None
        )
        frame_duration = _duration_spec_seconds(min_duration)
        if frame_count is not None and frame_count > 0:
            if frame_duration is not None and frame_duration > 0:
                return frame_count * frame_duration
            return frame_count / 30.0

    offset = _to_float(offset_seconds)
    return float(offset or 0.0)


def _evidence_duration_slack_seconds() -> float:
    value = _to_float(os.getenv("EVIDENCE_MAX_DURATION_SLACK_SEC"))
    if value is None:
        return DEFAULT_EVIDENCE_MAX_DURATION_SLACK_SEC
    return max(0.0, value)


def _post_savant_duration_guard_slack_seconds() -> float:
    value = _to_float(os.getenv("POST_SAVANT_DURATION_GUARD_SLACK_SEC"))
    if value is None:
        value = _to_float(os.getenv("EVIDENCE_PRODUCTION_DURATION_SLACK_SEC"))
    if value is None:
        return DEFAULT_POST_SAVANT_DURATION_GUARD_SLACK_SEC
    return max(0.0, value)


def _post_savant_window_edge_slack_ns() -> int:
    value = _to_float(os.getenv("POST_SAVANT_WINDOW_EDGE_SLACK_SEC"))
    if value is None:
        return int(DEFAULT_POST_SAVANT_WINDOW_EDGE_SLACK_SEC * 1_000_000_000)
    return int(max(0.0, value) * 1_000_000_000)


def _post_savant_max_pts_gap_ns() -> int:
    value = _to_float(os.getenv("POST_SAVANT_MAX_PTS_GAP_SEC"))
    if value is None:
        return int(DEFAULT_POST_SAVANT_MAX_PTS_GAP_SEC * 1_000_000_000)
    return int(max(0.0, value) * 1_000_000_000)


def _duration_guard(
    actual: float | None,
    expected: float,
    slack_seconds: float | None = None,
) -> dict:
    slack = (
        _evidence_duration_slack_seconds()
        if slack_seconds is None
        else max(0.0, float(slack_seconds))
    )
    expected = float(expected or 0.0)
    min_allowed = max(0.0, expected - slack) if expected > 0 else None
    max_allowed = expected + slack if expected > 0 else None
    if actual is None or actual <= 0:
        return {
            "duration_guard_status": "unavailable",
            "duration_guard_failed": False,
            "min_allowed_duration_seconds": min_allowed,
            "max_allowed_duration_seconds": max_allowed,
            "duration_guard_reason": "raw_clip_duration_unavailable",
            "duration_guard_slack_seconds": slack,
        }
    if expected <= 0 or max_allowed is None:
        return {
            "duration_guard_status": "not_applicable",
            "duration_guard_failed": False,
            "min_allowed_duration_seconds": min_allowed,
            "max_allowed_duration_seconds": max_allowed,
            "duration_guard_reason": "expected_duration_unavailable",
            "duration_guard_slack_seconds": slack,
        }
    too_short = actual < (min_allowed or 0.0)
    too_long = actual > max_allowed
    failed = too_short or too_long
    return {
        "duration_guard_status": "failed" if failed else "passed",
        "duration_guard_failed": failed,
        "min_allowed_duration_seconds": round(min_allowed or 0.0, 3),
        "max_allowed_duration_seconds": round(max_allowed, 3),
        "duration_guard_reason": (
            "raw_clip_duration_below_expected_minus_slack"
            if too_short
            else "raw_clip_duration_exceeds_expected_plus_slack"
            if too_long
            else ""
        ),
        "duration_guard_slack_seconds": slack,
    }


def _stop_condition_mode(stop_condition: dict) -> str:
    if "ts_delta_sec" in stop_condition:
        return "ts_delta_sec"
    if "frame_count" in stop_condition:
        return "frame_count_fallback"
    return "unknown"


def _env_text(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value


def _evidence_version(default: str = "") -> str:
    return _env_text("EVIDENCE_VERSION", default)


def _evidence_schema_version(default: str) -> str:
    return _env_text("EVIDENCE_SCHEMA_VERSION", default)


def _include_legacy_metadata_fields() -> bool:
    return _env_bool("EVIDENCE_INCLUDE_LEGACY_METADATA_FIELDS", default=False)


def _legacy_metadata_fields(default_version: str) -> dict:
    if not _include_legacy_metadata_fields():
        return {}
    return {"legacy_project_version": _evidence_version(default_version)}


def _post_savant_finalizer_enabled() -> bool:
    return _env_text(POST_SAVANT_FINALIZER_ENV).strip().lower() == (
        POST_SAVANT_REPLAY_EVIDENCE_TOPOLOGY
    )


def _post_savant_fast_raw_clip_enabled() -> bool:
    return _env_bool("POST_SAVANT_FAST_RAW_CLIP_ENABLED", default=False)


def _post_savant_fps_gating_applied() -> bool | None:
    value = os.getenv("POST_SAVANT_FPS_GATING_APPLIED")
    if value is None:
        value = os.getenv("INGRESS_FPS_GATE_ENABLED")
    if value is None:
        value = os.getenv("MAX_FPS_CONTROL")
    if value is None:
        return None
    return value.strip().lower() in ("1", "true", "yes", "on")


def _bundle_clip_status_from_post_savant_summary(summary: dict) -> str:
    if summary.get("production_ready") is True:
        return "ready"
    if summary.get("annotation_status") == "timeline_reconciliation_unverified":
        return "generated_unverified"
    return BUNDLE_STATUS_GENERATED_ANNOTATION_FAILED


def _summary_clip_status(summary: dict) -> str:
    if (
        summary.get("duration_guard_failed") is True
        or summary.get("duration_guard_status") == "failed"
        or summary.get("epoch_guard_failed") is True
        or summary.get("epoch_guard_status") == "failed"
        or summary.get("sink_window_guard_failed") is True
        or summary.get("sink_window_guard_status") == "failed"
        or (summary.get("time_window") or {}).get("time_domain_crop_failed") is True
        or summary.get("time_domain_crop_failed") is True
    ):
        return BUNDLE_STATUS_DURATION_GUARD_FAILED
    if summary.get("production_ready") is True:
        return "ready"
    status = str(summary.get("annotation_status") or "")
    if status in {
        "complete",
        "partial",
        "missing_frame_metadata",
        "no_post_savant_objects",
        "timeline_reconciliation_unverified",
    }:
        return "generated_unverified"
    return BUNDLE_STATUS_GENERATED_ANNOTATION_FAILED


def _evidence_reason_for_bundle(clip_status: str, bundle: dict | None) -> str:
    """Keep the legacy clip status while attributing the actual failed guard."""

    if _evidence_state_for_clip_status(clip_status) == "materialized":
        return ""
    if clip_status != BUNDLE_STATUS_DURATION_GUARD_FAILED or not bundle:
        return clip_status
    attribution = bundle.get("materialization_guard_attribution")
    if not isinstance(attribution, dict):
        return clip_status
    return str(attribution.get("primary_reason") or clip_status)


def _path_for_metadata(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _requested_duration_from_time_window(time_window: dict | None) -> float | None:
    time_window = time_window or {}
    requested_start_pts = _to_int(time_window.get("requested_start_pts"))
    requested_end_pts = _to_int(time_window.get("requested_end_pts"))
    if requested_start_pts is not None and requested_end_pts is not None:
        if requested_end_pts <= requested_start_pts:
            return None
        return (requested_end_pts - requested_start_pts) / 1_000_000_000.0
    requested = _to_float(time_window.get("requested_duration_s"))
    if requested is not None and requested > 0:
        return requested
    return None


def _post_savant_duration_guard(
    raw_clip_path: Path | None,
    time_window: dict | None,
    *,
    actual_duration: float | None = None,
) -> dict:
    expected_duration = _requested_duration_from_time_window(time_window)
    actual_duration = (
        actual_duration
        if actual_duration is not None
        else (
            _probe_video_duration_seconds(str(raw_clip_path))
            if raw_clip_path is not None and raw_clip_path.is_file()
            else None
        )
    )
    guard = _duration_guard(
        actual_duration,
        expected_duration or 0.0,
        _post_savant_duration_guard_slack_seconds(),
    )
    return {
        "raw_clip_duration": actual_duration,
        "expected_duration_seconds": (
            round(float(expected_duration), 3)
            if expected_duration is not None
            else None
        ),
        **guard,
    }


def _sink_metadata_window_guard(rows: list[dict], time_window: dict | None) -> dict:
    time_window = time_window or {}
    requested_start_pts = _to_int(time_window.get("requested_start_pts"))
    requested_end_pts = _to_int(time_window.get("requested_end_pts"))
    event_frame_pts = _to_int(time_window.get("event_frame_pts"))
    pts_values = [
        value
        for value in (_to_int(row.get("pts") or row.get("frame_pts")) for row in rows)
        if value is not None
    ]
    summary = {
        "sink_window_guard_status": "not_applicable",
        "sink_window_guard_failed": False,
        "sink_window_guard_reason": "",
        "sink_metadata_first_pts": pts_values[0] if pts_values else None,
        "sink_metadata_last_pts": pts_values[-1] if pts_values else None,
        "sink_metadata_frame_count_for_guard": len(pts_values),
        "event_pts_inside_clip": False,
        "event_projected_t_s": None,
        "event_centered_in_clip": False,
        "max_sink_metadata_pts_gap_ns": None,
    }
    if requested_start_pts is None or requested_end_pts is None or event_frame_pts is None:
        summary.update(
            {
                "sink_window_guard_status": "unavailable",
                "sink_window_guard_reason": "requested_or_event_pts_unavailable",
            }
        )
        return summary
    if requested_end_pts <= requested_start_pts:
        summary.update(
            {
                "sink_window_guard_status": "failed",
                "sink_window_guard_failed": True,
                "sink_window_guard_reason": "requested_pts_window_invalid",
            }
        )
        return summary
    if not pts_values:
        summary.update(
            {
                "sink_window_guard_status": "failed",
                "sink_window_guard_failed": True,
                "sink_window_guard_reason": "sink_metadata_empty_for_requested_window",
            }
        )
        return summary

    first_pts = int(pts_values[0])
    last_pts = int(pts_values[-1])
    edge_slack_ns = _post_savant_window_edge_slack_ns()
    max_gap_ns = _post_savant_max_pts_gap_ns()
    gaps = [int(b) - int(a) for a, b in zip(pts_values, pts_values[1:])]
    positive_gaps = [gap for gap in gaps if gap > 0]
    largest_gap = max(positive_gaps) if positive_gaps else 0
    summary["max_sink_metadata_pts_gap_ns"] = largest_gap
    event_projected = (event_frame_pts - first_pts) / 1_000_000_000.0
    expected_event_t = _to_float(time_window.get("expected_event_t_s"))
    if expected_event_t is None:
        expected_event_t = (
            (event_frame_pts - requested_start_pts) / 1_000_000_000.0
        )
    event_center_tolerance = _to_float(
        os.getenv("FRAME_CACHE_CANONICAL_EVENT_CENTER_TOLERANCE_SECONDS")
    )
    if event_center_tolerance is None:
        event_center_tolerance = 0.75

    summary.update(
        {
            "event_pts_inside_clip": first_pts <= event_frame_pts <= last_pts,
            "event_projected_t_s": round(float(event_projected), 6),
            "event_centered_in_clip": (
                abs(float(event_projected) - float(expected_event_t))
                <= float(event_center_tolerance)
            ),
        }
    )
    reason = ""
    if any(gap <= 0 for gap in gaps):
        reason = "sink_metadata_pts_not_strictly_increasing"
    elif largest_gap > max_gap_ns:
        reason = "sink_metadata_pts_gap_exceeds_limit"
    elif first_pts > requested_start_pts + edge_slack_ns:
        reason = "sink_metadata_starts_after_requested_window"
    elif last_pts < requested_end_pts - edge_slack_ns:
        reason = "sink_metadata_ends_before_requested_window"
    elif not summary["event_pts_inside_clip"]:
        reason = "event_pts_outside_sink_metadata_window"
    elif not summary["event_centered_in_clip"]:
        reason = "event_not_centered_in_sink_metadata_window"

    if reason:
        summary.update(
            {
                "sink_window_guard_status": "failed",
                "sink_window_guard_failed": True,
                "sink_window_guard_reason": reason,
            }
        )
    else:
        summary["sink_window_guard_status"] = "passed"
    return summary


def _merge_sink_window_guard(summary: dict, sink_window_guard: dict) -> dict:
    summary.update(sink_window_guard)
    return summary


def _merge_post_savant_duration_guard(summary: dict, duration_guard: dict) -> dict:
    summary.update(
        {
            "raw_clip_duration": duration_guard.get("raw_clip_duration"),
            "expected_duration_seconds": duration_guard.get(
                "expected_duration_seconds"
            ),
            "duration_guard_status": duration_guard.get("duration_guard_status"),
            "duration_guard_failed": bool(
                duration_guard.get("duration_guard_failed")
            ),
            "duration_guard_reason": duration_guard.get("duration_guard_reason", ""),
            "duration_guard_slack_seconds": duration_guard.get(
                "duration_guard_slack_seconds"
            ),
            "min_allowed_duration_seconds": duration_guard.get(
                "min_allowed_duration_seconds"
            ),
            "max_allowed_duration_seconds": duration_guard.get(
                "max_allowed_duration_seconds"
            ),
        }
    )
    return summary


def _relax_post_savant_fast_raw_clip_guards(
    *,
    summary: dict,
    duration_guard: dict,
    sink_window_guard: dict,
) -> tuple[dict, dict]:
    """Keep fast-copied evidence playable even when it is not an exact time crop."""
    limitations = list(summary.get("limitations") or [])
    failures = list(summary.get("production_ready_failures") or [])
    for limitation in (
        "fast_raw_clip_time_precision_relaxed",
        "fast_raw_clip_not_canonical_time_crop",
    ):
        if limitation not in limitations:
            limitations.append(limitation)
    summary.update(
        {
            "fast_raw_clip_enabled": True,
            "raw_clip_time_precision": "replay_or_gop_window",
            "production_ready": False,
            "canonical_clip": False,
            "visual_binding_status": "unverified",
            "visual_binding_reason": "fast_raw_clip_time_precision_relaxed",
            "visual_evidence_status": "unverified",
            "evidence_visual_status": "unverified",
            "production_ready_failures": failures,
            "limitations": limitations,
        }
    )
    video_crop = summary.get("video_crop")
    if isinstance(video_crop, dict):
        video_crop.setdefault("method", "copy")
        video_crop["materialization_mode"] = "fast_raw_copy"
        video_crop["fast_raw_clip_enabled"] = True
        video_crop["crop_video_to_time_window"] = False
    time_window = summary.get("time_window")
    if isinstance(time_window, dict):
        time_window["fast_raw_clip_enabled"] = True
        time_window["time_domain_crop_applied"] = False
        time_window["time_precision"] = "replay_or_gop_window"
    if duration_guard.get("duration_guard_failed") is True:
        duration_guard = {
            **duration_guard,
            "duration_guard_status": "relaxed",
            "duration_guard_failed": False,
            "duration_guard_reason": "fast_raw_clip_duration_relaxed",
        }
        _merge_post_savant_duration_guard(summary, duration_guard)
    if sink_window_guard.get("sink_window_guard_failed") is True:
        sink_window_guard = {
            **sink_window_guard,
            "sink_window_guard_status": "relaxed",
            "sink_window_guard_failed": False,
            "sink_window_guard_reason": "fast_raw_clip_window_relaxed",
        }
        _merge_sink_window_guard(summary, sink_window_guard)
    return duration_guard, sink_window_guard


def _apply_post_savant_failure_status(
    summary: dict,
    *,
    reason: str,
    duration_guard: dict | None = None,
) -> dict:
    duration_guard = dict(duration_guard or {})
    if reason in {"time_domain_crop_failed", "runtime_epoch_guard_failed"}:
        duration_guard["duration_guard_status"] = "failed"
        duration_guard["duration_guard_failed"] = True
        duration_guard["duration_guard_reason"] = (
            "time_domain_crop_failed"
            if reason == "time_domain_crop_failed"
            else "missing_or_mismatched_runtime_epoch"
        )
    failures = list(summary.get("production_ready_failures") or [])
    if reason not in failures:
        failures.append(reason)
    limitations = list(summary.get("limitations") or [])
    if reason not in limitations:
        limitations.append(reason)
    summary.update(
        {
            "production_ready": False,
            "canonical_clip": False,
            "visual_binding_status": "unverified",
            "visual_binding_reason": reason,
            "visual_evidence_status": "unverified",
            "evidence_visual_status": "unverified",
            "production_ready_failures": failures,
            "limitations": limitations,
            "raw_clip_path": None,
        }
    )
    if reason == "time_domain_crop_failed":
        summary["time_domain_crop_failed"] = True
    if reason == "runtime_epoch_guard_failed":
        summary["epoch_guard_failed"] = True
        summary["epoch_guard_status"] = "failed"
        summary["epoch_guard_reason"] = "missing_or_mismatched_runtime_epoch"
    if duration_guard:
        _merge_post_savant_duration_guard(summary, duration_guard)
    if reason == "sink_window_guard_failed":
        summary["sink_window_guard_failed"] = True
    return summary


def _rewrite_post_savant_summary_files(bundle_result: object) -> None:
    summary = getattr(bundle_result, "summary")
    summary_path = Path(getattr(bundle_result, "summary_path"))
    _atomic_write_json(summary_path, summary)
    sidecar_summary_path = getattr(bundle_result, "sidecar_summary_path", None)
    if sidecar_summary_path is None:
        sidecar_summary_path = Path(getattr(bundle_result, "output_dir")) / SIDECAR_SUMMARY_FILE
    _atomic_write_json(Path(sidecar_summary_path), summary)


def _without_published_raw_clip(bundle_result: object) -> _EvidenceBundleView:
    raw_clip_path = getattr(bundle_result, "raw_clip_path", None)
    if raw_clip_path is not None:
        try:
            Path(raw_clip_path).unlink(missing_ok=True)
        except OSError:
            logger.warning("failed_to_remove_invalid_raw_clip path=%s", raw_clip_path)
    return _EvidenceBundleView(
        output_dir=Path(getattr(bundle_result, "output_dir")),
        raw_clip_path=None,
        sink_metadata_path=Path(getattr(bundle_result, "sink_metadata_path")),
        production_sidecar_path=Path(getattr(bundle_result, "production_sidecar_path")),
        summary_path=Path(getattr(bundle_result, "summary_path")),
        summary=getattr(bundle_result, "summary"),
    )


def _event_for_frame_cache_sidecar(event_context: dict) -> dict:
    payload = event_context.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    media = payload.get("media")
    media = media if isinstance(media, dict) else {}
    return {
        "id": event_context.get("event_id", ""),
        "event_id": event_context.get("event_id", ""),
        "created_at": event_context.get("created_at"),
        "source_event_id": event_context.get("source_event_id", ""),
        "event_type": event_context.get("event_type", ""),
        "source_id": event_context.get("source_id", ""),
        "camera_id": event_context.get("camera_id", ""),
        "track_id": event_context.get("track_id", ""),
        "event_ts_ms": event_context.get("event_ts_ms"),
        "timestamp_ms": event_context.get("event_ts_ms"),
        "frame_uuid": event_context.get("frame_uuid") or media.get("frame_uuid"),
        "frame_pts": event_context.get("frame_pts") or media.get("frame_pts"),
        "payload": payload,
    }


def _watchlist_identity_continuations(
    pg_conn: psycopg.Connection,
    event_context: dict,
    *,
    post_seconds: float,
) -> dict[str, dict]:
    """Return same-track watchlist identities that also pass the event threshold."""

    if not _env_bool("WATCHLIST_EVIDENCE_CONTINUATION_ENABLED", default=True):
        return {}
    if str(event_context.get("event_type") or "") not in {"watchlist_hit", "live_search_hit"}:
        return {}
    payload = event_context.get("payload")
    if not isinstance(payload, dict):
        return {}
    match = payload.get("match")
    observation = payload.get("observation")
    matched_person = payload.get("matched_person")
    if not isinstance(match, dict) or not isinstance(observation, dict):
        return {}
    if not isinstance(matched_person, dict):
        matched_person = {}

    gallery_embedding_id = _to_int(match.get("gallery_embedding_id"))
    threshold = _to_float(match.get("threshold") or payload.get("threshold"))
    trigger_source_observation_id = str(match.get("source_observation_id") or "").strip()
    trigger_timestamp_ms = _to_int(observation.get("timestamp_ms"))
    source_id = str(observation.get("source_id") or event_context.get("source_id") or "").strip()
    camera_id = str(observation.get("camera_id") or event_context.get("camera_id") or "").strip()
    track_id = str(
        observation.get("person_track_id")
        or observation.get("track_id")
        or event_context.get("track_id")
        or ""
    ).strip()
    if (
        gallery_embedding_id is None
        or threshold is None
        or not trigger_source_observation_id
        or trigger_timestamp_ms is None
        or not source_id
        or not camera_id
        or not track_id
    ):
        return {}

    post_ms = max(0, int(float(post_seconds) * 1000))
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT
                        fo.source_observation_id,
                        fo.timestamp_ms,
                        pge.id AS gallery_embedding_id,
                        pge.person_id,
                        p.external_person_id,
                        p.name AS person_name,
                        1 - (fo.embedding <=> pge.embedding) AS similarity
                    FROM face_observations fo
                    JOIN person_gallery_embeddings pge
                      ON pge.id = %(gallery_embedding_id)s
                     AND pge.is_active IS TRUE
                    JOIN persons p
                      ON p.id = pge.person_id
                     AND p.is_active IS TRUE
                    WHERE fo.embedding IS NOT NULL
                      AND fo.source_id = %(source_id)s
                      AND fo.camera_id = %(camera_id)s
                      AND fo.track_id = %(track_id)s
                      AND fo.timestamp_ms BETWEEN %(start_ms)s AND %(end_ms)s
                )
                SELECT source_observation_id, timestamp_ms, gallery_embedding_id,
                       person_id, external_person_id, person_name, similarity
                FROM candidates
                WHERE similarity >= %(threshold)s
                ORDER BY timestamp_ms ASC
                LIMIT 200
                """,
                {
                    "gallery_embedding_id": gallery_embedding_id,
                    "source_id": source_id,
                    "camera_id": camera_id,
                    "track_id": track_id,
                    "start_ms": trigger_timestamp_ms,
                    "end_ms": trigger_timestamp_ms + post_ms,
                    "threshold": threshold,
                },
            )
            rows = cur.fetchall()
    except Exception:
        logger.exception(
            "watchlist_identity_continuation_query_failed event_id=%s "
            "source_observation_id=%s",
            event_context.get("event_id", ""),
            trigger_source_observation_id,
        )
        return {}

    continuations: dict[str, dict] = {}
    for row in rows:
        source_observation_id = str(row[0] or "").strip()
        if not source_observation_id:
            continue
        continuations[source_observation_id] = {
            "source_observation_id": source_observation_id,
            "person_id": int(row[3]) if row[3] is not None else matched_person.get("person_id"),
            "external_person_id": row[4] or matched_person.get("external_person_id"),
            "display_name": row[5] or matched_person.get("name"),
            "gallery_embedding_id": int(row[2]) if row[2] is not None else gallery_embedding_id,
            "similarity": float(row[6]) if row[6] is not None else None,
            "threshold": float(threshold),
            "match_status": "above_threshold",
        }
    return continuations


def _is_sink_video_frame(row: dict) -> bool:
    return row.get("type") == "VideoFrame"


def _object_counts_from_sidecar_summary(summary: dict) -> dict:
    known = int(summary.get("known_face_count") or summary.get("known_face_objects_count") or 0)
    unknown = int(summary.get("unknown_face_count") or 0)
    person = int(
        summary.get("person_context_rows")
        or summary.get("person_annotations")
        or summary.get("person_objects_count")
        or 0
    )
    face = int(summary.get("face_objects_count") or known + unknown)
    return {
        "person": person,
        "face": face,
        "known_face": known,
    }


def _uuid_first_anchor_metadata(
    *,
    replay_labels: dict,
    replay_job_request: dict,
    time_window: dict | None = None,
) -> dict:
    time_window = time_window or {}
    anchor_keyframe_uuid = (
        replay_labels.get("anchor_keyframe_uuid")
        or replay_job_request.get("anchor_keyframe")
    )
    event_frame_uuid = (
        replay_labels.get("event_frame_uuid")
        or replay_labels.get("frame_uuid")
    )
    metadata = {
        "evidence_anchor_strategy": "uuid_first_pts_verified",
        "event_frame_uuid": event_frame_uuid,
        "event_frame_pts": replay_labels.get("event_frame_pts"),
        "anchor_keyframe_uuid": anchor_keyframe_uuid,
        "anchor_keyframe_pts": replay_labels.get("anchor_keyframe_pts"),
        "original_requested_start_pts": replay_labels.get(
            "original_requested_start_pts"
        )
        or time_window.get("original_requested_start_pts"),
        "effective_start_pts": replay_labels.get("effective_start_pts")
        or time_window.get("effective_start_pts"),
        "pre_window_truncated": replay_labels.get("pre_window_truncated")
        or time_window.get("pre_window_truncated"),
        "pre_window_policy": replay_labels.get("pre_window_policy")
        or time_window.get("pre_window_policy"),
        "requested_pre_window_seconds": replay_labels.get(
            "requested_pre_window_seconds"
        )
        or time_window.get("requested_pre_window_seconds"),
        "effective_pre_window_seconds": replay_labels.get(
            "effective_pre_window_seconds"
        )
        or time_window.get("effective_pre_window_seconds"),
        "pre_window_truncated_seconds": replay_labels.get(
            "pre_window_truncated_seconds"
        )
        or time_window.get("pre_window_truncated_seconds"),
        "requested_start_pts": (
            replay_labels.get("effective_start_pts")
            or time_window.get("effective_start_pts")
            or replay_labels.get("requested_start_pts")
            or time_window.get("requested_start_pts")
        ),
        "requested_end_pts": (
            replay_labels.get("requested_end_pts")
            or time_window.get("requested_end_pts")
        ),
        "actual_start_pts": time_window.get("actual_start_pts"),
        "actual_end_pts": time_window.get("actual_end_pts"),
        "post_window_frame_uuid": replay_labels.get("post_window_frame_uuid"),
        "post_window_frame_pts": replay_labels.get("post_window_frame_pts"),
        "post_window_cross_session_proof_used": replay_labels.get(
            "post_window_cross_session_proof_used"
        ),
        "frame_domain_session_policy": replay_labels.get(
            "frame_domain_session_policy"
        ),
        "frame_domain_proof_method": replay_labels.get("frame_domain_proof_method"),
        "start_window_frame_uuid": replay_labels.get("start_window_frame_uuid"),
        "start_window_frame_pts": replay_labels.get("start_window_frame_pts"),
        "start_window_stream_session_id": replay_labels.get(
            "start_window_stream_session_id"
        ),
        "post_window_stream_session_id": replay_labels.get(
            "post_window_stream_session_id"
        ),
        "time_domain_crop_applied": bool(time_window.get("time_domain_crop_applied")),
        "crop_reason": (
            "requested_pts_window"
            if time_window.get("time_domain_crop_applied")
            else "not_applied"
        ),
    }
    metadata["post_window_proof_used"] = bool(metadata.get("post_window_frame_uuid"))
    metadata["start_window_coverage_used"] = bool(
        metadata.get("start_window_frame_uuid")
    )
    return {
        key: value
        for key, value in metadata.items()
        if value is not None and value != ""
    }


def _build_frame_cache_summary(
    *,
    sidecar_summary: dict,
    sink_metadata_rows: list[dict],
    decoded_video_frame_count: int,
    raw_clip_path: Path,
    sink_metadata_path: Path,
    event_context: dict,
    time_window: dict | None = None,
    video_crop: dict | None = None,
) -> dict:
    object_counts = _object_counts_from_sidecar_summary(sidecar_summary)
    frame_count = int(sidecar_summary.get("annotations_written") or sidecar_summary.get("rows_written") or 0)
    rows_total_input = int(sidecar_summary.get("rows_total_input") or sidecar_summary.get("annotations_input") or frame_count)
    production_ready_failures = list(
        sidecar_summary.get("production_ready_failures")
        or sidecar_summary.get("limitations")
        or []
    )
    if (time_window or {}).get("time_domain_crop_failed") is True:
        production_ready_failures.append("time_domain_crop_failed")
    production_ready = bool(
        frame_count > 0
        and sidecar_summary.get("embedding_vectors_in_output") in (None, 0)
        and sidecar_summary.get("image_bytes_in_output") in (None, 0)
        and sidecar_summary.get("production_ready") is not False
        and not production_ready_failures
    )
    time_window = time_window or {}
    replay_labels = time_window.get("replay_labels")
    replay_labels = replay_labels if isinstance(replay_labels, dict) else {}
    replay_job_request = time_window.get("replay_job_request")
    replay_job_request = replay_job_request if isinstance(replay_job_request, dict) else {}
    anchor_metadata = _uuid_first_anchor_metadata(
        replay_labels=replay_labels,
        replay_job_request=replay_job_request,
        time_window=time_window,
    )
    video_crop = video_crop or {
        "method": "copy",
        "crop_video_to_time_window": False,
        "source_video_path": str(raw_clip_path),
    }
    summary = {
        **sidecar_summary,
        "schema_version": _evidence_schema_version("2.0-midterm"),
        "project_version": _evidence_version(""),
        "evidence_topology": POST_SAVANT_REPLAY_EVIDENCE_TOPOLOGY,
        "sidecar_type": "production",
        "timeline_domain": PRODUCTION_TIMELINE_DOMAIN,
        "annotation_source": str(
            sidecar_summary.get("annotation_source") or "frame_annotation_cache"
        ),
        "annotation_status": "complete" if production_ready else sidecar_summary.get("annotation_status", "partial"),
        "production_ready": production_ready,
        "canonical_clip": production_ready,
        "visual_binding_status": "verified" if production_ready else "unverified",
        "visual_binding_reason": "frame_annotation_cache_aligned_to_replay_metadata" if production_ready else sidecar_summary.get("visual_binding_reason", "frame_annotation_cache_partial"),
        "visual_evidence_status": "verified" if production_ready else "unverified",
        "evidence_visual_status": "verified" if production_ready else "unverified",
        "fallback_used": False,
        "raw_video_binding": "continuous_replay_video",
        "annotation_binding": "frame_annotation_cache_pts_sidecar",
        "raw_clip_path": RAW_CLIP_FILE,
        "sink_metadata_path": SINK_METADATA_FILE,
        "production_sidecar_path": SIDECAR_ANNOTATIONS_FILE,
        "source_metadata_frame_count": len(sink_metadata_rows),
        "original_metadata_frame_count": rows_total_input,
        "decoded_video_frame_count": decoded_video_frame_count,
        "sidecar_frame_count": frame_count,
        "frame_count": frame_count,
        "sidecar_trimmed": False,
        "trim_occurred": False,
        "timeline_reconciliation_status": "metadata_time_aligned_sparse_sidecar",
        "time_domain_crop_applied": bool(time_window.get("time_domain_crop_applied")),
        "video_integrity_required": False,
        "video_integrity": None,
        "video_crop": video_crop,
        "time_window": time_window,
        "fps": {
            "source_input_fps_estimate": _to_float(os.getenv("SOURCE_INPUT_FPS_ESTIMATE")),
            "metadata_fps_estimate": None,
            "decoded_video_fps_estimate": None,
            "max_fps": os.getenv("MAX_FPS") or "8/1",
            "min_fps": os.getenv("MIN_FPS") or "2/1",
            "fps_gating_applied": _post_savant_fps_gating_applied(),
        },
        "object_counts": object_counts,
        "person_objects_count": object_counts["person"],
        "face_objects_count": object_counts["face"],
        "known_face_objects_count": object_counts["known_face"],
        "production_ready_failures": production_ready_failures,
        "limitations": production_ready_failures,
        "metadata_path_used": str(sink_metadata_path),
        **anchor_metadata,
    }
    if _include_legacy_metadata_fields():
        summary.update(
            {
                "legacy_event_type": event_context.get("event_type", ""),
                "legacy_camera_id": event_context.get("camera_id", ""),
                "legacy_source_id": event_context.get("source_id", ""),
            }
        )
    return summary


def _frame_cache_time_domain_window(
    *,
    event_context: dict,
    replay_labels: dict,
) -> dict:
    payload = event_context.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    media = payload.get("media")
    media = media if isinstance(media, dict) else {}
    pre_seconds = _to_float(os.getenv("DEFAULT_PRE_SECONDS", "5")) or 5.0
    post_seconds = _to_float(os.getenv("DEFAULT_POST_SECONDS", "5")) or 5.0
    event_frame_pts = _to_int(
        replay_labels.get("event_frame_pts")
        or replay_labels.get("frame_pts")
        or event_context.get("frame_pts")
        or media.get("event_frame_pts")
        or media.get("frame_pts")
    )
    requested_start_pts = _to_int(
        replay_labels.get("effective_start_pts")
        or replay_labels.get("requested_start_pts")
        or media.get("requested_start_pts")
    )
    original_requested_start_pts = _to_int(
        replay_labels.get("original_requested_start_pts")
    )
    if original_requested_start_pts is None:
        original_requested_start_pts = _to_int(
            replay_labels.get("requested_start_pts")
            or media.get("requested_start_pts")
        )
    requested_end_pts = _to_int(
        replay_labels.get("requested_end_pts")
        or media.get("requested_end_pts")
    )
    if event_frame_pts is not None:
        if requested_start_pts is None:
            requested_start_pts = max(0, int(event_frame_pts - pre_seconds * 1_000_000_000))
        if requested_end_pts is None:
            requested_end_pts = int(event_frame_pts + post_seconds * 1_000_000_000)
    return {
        "original_requested_start_pts": original_requested_start_pts,
        "effective_start_pts": requested_start_pts,
        "requested_start_pts": requested_start_pts,
        "requested_end_pts": requested_end_pts,
        "event_frame_pts": event_frame_pts,
        "pre_seconds": pre_seconds,
        "post_seconds": post_seconds,
        "expected_event_t_s": (
            round((event_frame_pts - requested_start_pts) / 1_000_000_000.0, 9)
            if event_frame_pts is not None and requested_start_pts is not None
            else pre_seconds
        ),
        "pre_window_truncated": str(
            replay_labels.get("pre_window_truncated") or ""
        ).strip().lower() in {"1", "true", "yes"},
        "pre_window_policy": replay_labels.get("pre_window_policy"),
        "requested_pre_window_seconds": _to_float(
            replay_labels.get("requested_pre_window_seconds")
        ),
        "effective_pre_window_seconds": _to_float(
            replay_labels.get("effective_pre_window_seconds")
        ),
        "pre_window_truncated_seconds": _to_float(
            replay_labels.get("pre_window_truncated_seconds")
        ),
        "requested_duration_s": (
            round((requested_end_pts - requested_start_pts) / 1_000_000_000.0, 9)
            if requested_start_pts is not None and requested_end_pts is not None
            else None
        ),
    }


class _EvidenceBundleView:
    def __init__(
        self,
        *,
        output_dir: Path,
        raw_clip_path: Path | None,
        sink_metadata_path: Path,
        production_sidecar_path: Path,
        summary_path: Path,
        summary: dict,
    ) -> None:
        self.output_dir = output_dir
        self.raw_clip_path = raw_clip_path
        self.sink_metadata_path = sink_metadata_path
        self.production_sidecar_path = production_sidecar_path
        self.summary_path = summary_path
        self.summary = summary


def _build_event_metadata(
    *,
    event_context: dict,
    replay_job_id: str,
    replay_job_request: dict,
    sink_output_dir: str,
    bundle_result: object,
) -> dict:
    summary = getattr(bundle_result, "summary")
    raw_clip_path = _path_for_metadata(getattr(bundle_result, "raw_clip_path", None))
    sink_metadata_path = _path_for_metadata(getattr(bundle_result, "sink_metadata_path", None))
    production_sidecar_path = _path_for_metadata(getattr(bundle_result, "production_sidecar_path", None))
    summary_path = _path_for_metadata(getattr(bundle_result, "summary_path", None))
    output_dir = _path_for_metadata(getattr(bundle_result, "output_dir", None))
    stop_condition = replay_job_request.get("stop_condition") or {}
    configuration = replay_job_request.get("configuration") or {}
    replay_labels = configuration.get("labels") or {}
    if not isinstance(replay_labels, dict):
        replay_labels = {}
    anchor_metadata = _uuid_first_anchor_metadata(
        replay_labels=replay_labels,
        replay_job_request=replay_job_request,
        time_window=summary.get("time_window") if isinstance(summary, dict) else {},
    )
    offset = replay_job_request.get("offset") or {}
    raw_clip_size = 0
    if raw_clip_path:
        try:
            raw_clip_size = Path(raw_clip_path).stat().st_size
        except OSError:
            raw_clip_size = 0
    object_counts = summary.get("object_counts") if isinstance(summary, dict) else {}
    if not isinstance(object_counts, dict):
        object_counts = {}
    event_created_at = _json_isoformat(event_context.get("created_at"))

    return {
        "schema_version": _evidence_schema_version("2.0-midterm"),
        "project_version": _evidence_version("midterm"),
        **_legacy_metadata_fields("midterm"),
        "run_id": os.getenv("EVIDENCE_RUN_ID", ""),
        "runtime_epoch_id": summary.get("runtime_epoch_id", ""),
        "evidence_type": "security_event_post_savant_replay_clip",
        "evidence_topology": POST_SAVANT_REPLAY_EVIDENCE_TOPOLOGY,
        "recording_strategy": "savant_replay",
        "event": {
            "event_id": event_context.get("event_id", ""),
            "source_event_id": event_context.get("source_event_id", ""),
            "event_type": event_context.get("event_type", ""),
            "camera_id": event_context.get("camera_id", ""),
            "source_id": event_context.get("source_id", ""),
            "track_id": event_context.get("track_id", ""),
            "created_at": event_created_at,
            "alarm_machine_time": event_created_at,
            "alarm_machine_time_source": "events.created_at" if event_created_at else "",
            "event_ts_ms": event_context.get("event_ts_ms", 0),
            "frame_uuid": (
                anchor_metadata.get("event_frame_uuid")
                or event_context.get("frame_uuid", "")
            ),
            "event_frame_uuid": (
                anchor_metadata.get("event_frame_uuid")
                or event_context.get("event_frame_uuid", "")
                or event_context.get("frame_uuid", "")
            ),
            "event_frame_pts": anchor_metadata.get("event_frame_pts", ""),
            "keyframe_uuid": event_context.get("keyframe_uuid", ""),
            "previous_keyframe_uuid": event_context.get("previous_keyframe_uuid", ""),
            "runtime_epoch_id": summary.get("runtime_epoch_id", ""),
        },
        "replay": {
            "replay_job_id": replay_job_id,
            "runtime_epoch_id": replay_labels.get("runtime_epoch_id", ""),
            "replay_source_kind": (
                replay_labels.get("replay_source_kind", "")
            ),
            "anchor_keyframe_uuid": replay_job_request.get("anchor_keyframe", ""),
            "anchor_keyframe_pts": anchor_metadata.get("anchor_keyframe_pts", ""),
            "evidence_anchor_strategy": anchor_metadata.get(
                "evidence_anchor_strategy",
                "uuid_first_pts_verified",
            ),
            "post_window_proof_used": bool(
                anchor_metadata.get("post_window_proof_used")
            ),
            "start_window_coverage_used": bool(
                anchor_metadata.get("start_window_coverage_used")
            ),
            "offset_seconds": offset.get("seconds", 0),
            "stop_condition": stop_condition,
            "stop_condition_mode": _stop_condition_mode(stop_condition),
            "fallback_reason": replay_job_request.get("fallback_reason", ""),
            "stored_stream_id": configuration.get("stored_stream_id", ""),
            "resulting_stream_id": configuration.get("resulting_stream_id", ""),
        },
        "media": {
            "evidence_dir": output_dir,
            "sink_output_dir": sink_output_dir,
            "sink_metadata_path": sink_metadata_path,
            "raw_clip_path": raw_clip_path,
            "production_sidecar_path": production_sidecar_path,
            "summary_json_path": summary_path,
            "materialization_metrics": summary.get("materialization_metrics") or {},
            "raw_clip_size": raw_clip_size,
            "raw_clip_duration": summary.get("raw_clip_duration"),
            "expected_duration_seconds": summary.get("expected_duration_seconds"),
            "duration_guard_status": summary.get("duration_guard_status"),
            "duration_guard_failed": bool(summary.get("duration_guard_failed")),
            "duration_guard_reason": summary.get("duration_guard_reason", ""),
            "duration_guard_slack_seconds": summary.get(
                "duration_guard_slack_seconds"
            ),
            "max_allowed_duration_seconds": summary.get(
                "max_allowed_duration_seconds"
            ),
            "sink_window_guard_status": summary.get("sink_window_guard_status"),
            "sink_window_guard_failed": bool(
                summary.get("sink_window_guard_failed")
            ),
            "sink_window_guard_reason": summary.get("sink_window_guard_reason", ""),
            "sink_metadata_first_pts": summary.get("sink_metadata_first_pts"),
            "sink_metadata_last_pts": summary.get("sink_metadata_last_pts"),
            "max_sink_metadata_pts_gap_ns": summary.get(
                "max_sink_metadata_pts_gap_ns"
            ),
            "event_pts_inside_clip": bool(summary.get("event_pts_inside_clip")),
            "event_projected_t_s": summary.get("event_projected_t_s"),
            "event_centered_in_clip": bool(summary.get("event_centered_in_clip")),
            "runtime_epoch_id": summary.get("runtime_epoch_id", ""),
            "epoch_guard_status": summary.get("epoch_guard_status"),
            "epoch_guard_failed": bool(summary.get("epoch_guard_failed")),
            "epoch_guard_reason": summary.get("epoch_guard_reason", ""),
            "runtime_epoch_strict": bool(summary.get("runtime_epoch_strict")),
            "runtime_epoch_fields": summary.get("runtime_epoch_fields", {}),
            "annotated_clip_path": None,
            "annotated_clip_status": "not_generated",
        },
        "annotations": {
            "annotation_source": summary.get("annotation_source"),
            "annotation_source_kind": "production_sidecar",
            "annotations_jsonl_path": production_sidecar_path,
            "summary_json_path": summary_path,
            "annotation_status": summary.get("annotation_status"),
            "production_ready": bool(summary.get("production_ready")),
            "visual_evidence_status": summary.get("visual_evidence_status"),
            "frame_count": int(summary.get("frame_count") or 0),
            "sidecar_frame_count": int(summary.get("sidecar_frame_count") or 0),
            "object_counts": object_counts,
            "person_count": int(object_counts.get("person") or 0),
            "face_count": int(object_counts.get("face") or 0),
            "known_face_count": int(object_counts.get("known_face") or 0),
            "annotation_mode": "post_savant_sink_metadata_sidecar",
        },
        "status": {
            "clip_status": _summary_clip_status(summary),
        },
        "anchor": anchor_metadata,
        "limitations": list(summary.get("limitations") or []),
    }


def _finalize_post_savant_evidence_bundle(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    meta_dir: str,
    metadata_file: str,
    evidence_output_dir: str,
    materialization_timeout_s: float = 0.0,
    materialization_guardrails: dict | None = None,
    phase_diagnostics: dict | None = None,
) -> dict:
    """Package post-Savant sink output as a production evidence bundle."""
    finalize_started = time.monotonic()
    finalization_process_cpu_started = time.process_time()
    finalization_thread_cpu_started = (
        time.thread_time() if hasattr(time, "thread_time") else None
    )
    materialization_started_at = datetime.now(timezone.utc)
    probe_before = _probe_metrics_snapshot()
    metadata_rows_loaded = 0
    decoded_frame_count_duration_ms = 0
    event_context = _load_event_context(pg_conn, event_id)
    payload = event_context.get("payload", {})
    media = payload.get("media", {}) if isinstance(payload, dict) else {}
    if not isinstance(media, dict):
        media = {}
    sink_metadata = _load_sink_metadata_file(metadata_file)
    rolling_cache_info = (
        sink_metadata.get("rolling_cache")
        if isinstance(sink_metadata.get("rolling_cache"), dict)
        else {}
    )
    replay_job_id = (
        media.get("replay_job_id")
        or sink_metadata.get("job_id")
        or sink_metadata.get("new_job")
        or ""
    )
    replay_job_request = media.get("replay_job_request") or {}
    if not isinstance(replay_job_request, dict):
        replay_job_request = {}
    replay_configuration = replay_job_request.get("configuration") or {}
    if not isinstance(replay_configuration, dict):
        replay_configuration = {}
    replay_labels = replay_configuration.get("labels") or {}
    if not isinstance(replay_labels, dict):
        replay_labels = {}
    output_dir = Path(evidence_output_dir) / event_id
    source_video = _find_video_file(meta_dir)
    if not source_video:
        raise FileNotFoundError(f"video file not found in {meta_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_clip_path = output_dir / RAW_CLIP_FILE
    sink_metadata_path = output_dir / SINK_METADATA_FILE
    frame_cache_window = _frame_cache_time_domain_window(
        event_context=event_context,
        replay_labels=replay_labels,
    )
    source_metadata_rows = [
        row
        for row in load_native_metadata(Path(metadata_file))
        if isinstance(row, dict) and _is_sink_video_frame(row)
    ]
    metadata_rows_loaded += len(source_metadata_rows)
    sink_metadata_rows_for_guard: list[dict] | None = None
    frame_cache_time_window = {
        "time_domain_crop_applied": False,
        **frame_cache_window,
        "replay_labels": replay_labels,
        "replay_job_request": replay_job_request,
    }
    fast_raw_clip_enabled = _post_savant_fast_raw_clip_enabled()
    if fast_raw_clip_enabled:
        frame_cache_time_window.update(
            {
                "fast_raw_clip_enabled": True,
                "time_precision": "replay_or_gop_window",
            }
        )
    if rolling_cache_info.get("time_domain_crop_applied") is True:
        rc_requested_start_pts = _to_int(rolling_cache_info.get("requested_start_pts"))
        rc_requested_end_pts = _to_int(rolling_cache_info.get("requested_end_pts"))
        rc_actual_start_pts = _to_int(rolling_cache_info.get("actual_start_pts"))
        rc_actual_end_pts = _to_int(rolling_cache_info.get("actual_end_pts"))
        rc_event_frame_pts = _to_int(frame_cache_time_window.get("event_frame_pts"))
        rc_requested_duration_s = _to_float(
            rolling_cache_info.get("requested_duration_s")
        )
        if (
            rc_requested_duration_s is None
            and rc_requested_start_pts is not None
            and rc_requested_end_pts is not None
            and rc_requested_end_pts > rc_requested_start_pts
        ):
            rc_requested_duration_s = (
                rc_requested_end_pts - rc_requested_start_pts
            ) / 1_000_000_000.0
        rc_actual_duration_s = None
        if (
            rc_actual_start_pts is not None
            and rc_actual_end_pts is not None
            and rc_actual_end_pts > rc_actual_start_pts
        ):
            rc_actual_duration_s = (
                rc_actual_end_pts - rc_actual_start_pts
            ) / 1_000_000_000.0
        frame_cache_time_window.update(
            {
                "time_domain_crop_applied": True,
                "effective_start_pts": rc_actual_start_pts,
                "actual_start_pts": rc_actual_start_pts,
                "actual_end_pts": rc_actual_end_pts,
                "requested_start_pts": rc_requested_start_pts,
                "requested_end_pts": rc_requested_end_pts,
                "requested_duration_s": (
                    round(float(rc_requested_duration_s), 9)
                    if rc_requested_duration_s is not None
                    else None
                ),
                "actual_duration_s": (
                    round(float(rc_actual_duration_s), 9)
                    if rc_actual_duration_s is not None
                    else None
                ),
                "pre_window_truncated": bool(
                    rolling_cache_info.get("pre_window_truncated")
                ),
                "post_window_truncated": bool(
                    rolling_cache_info.get("post_window_truncated")
                ),
            }
        )
        if rc_event_frame_pts is not None:
            if rc_requested_start_pts is not None:
                frame_cache_time_window["expected_event_t_s"] = round(
                    (rc_event_frame_pts - rc_requested_start_pts)
                    / 1_000_000_000.0,
                    9,
                )
                frame_cache_time_window["pre_seconds"] = round(
                    max(0.0, (rc_event_frame_pts - rc_requested_start_pts) / 1_000_000_000.0),
                    9,
                )
            if rc_requested_end_pts is not None:
                frame_cache_time_window["post_seconds"] = round(
                    max(0.0, (rc_requested_end_pts - rc_event_frame_pts) / 1_000_000_000.0),
                    9,
                )
    rolling_cache_selected_frame_count = _to_int(
        rolling_cache_info.get("selected_frame_count")
    )
    rolling_cache_output_duration_s = _to_float(
        rolling_cache_info.get("output_duration_s")
    )
    raw_clip_duration = (
        rolling_cache_output_duration_s
        if rolling_cache_output_duration_s is not None
        and rolling_cache_output_duration_s > 0
        else None
    )
    frame_cache_video_crop: dict = {
        "method": "copy",
        "crop_video_to_time_window": False,
        "source_video_path": source_video,
        "materialization_mode": (
            "rolling_cache_copy"
            if rolling_cache_info
            else "fast_raw_copy"
            if fast_raw_clip_enabled
            else "copy"
        ),
        "fast_raw_clip_enabled": fast_raw_clip_enabled,
        "rolling_cache_enabled": bool(rolling_cache_info),
    }
    raw_clip_available = True
    metadata_has_objects = any(
        bool(row.get("objects"))
        for row in source_metadata_rows
        if isinstance(row, dict) and _is_sink_video_frame(row)
    )
    if (
        metadata_has_objects
        and not fast_raw_clip_enabled
        and _env_bool("FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED", default=True)
        and frame_cache_window.get("requested_start_pts") is not None
        and frame_cache_window.get("requested_end_pts") is not None
    ):
        frame_cache_time_window = {
            **frame_cache_window,
            "time_domain_crop_applied": True,
            "replay_labels": replay_labels,
            "replay_job_request": replay_job_request,
        }
    elif (
        not fast_raw_clip_enabled
        and _env_bool("FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED", default=True)
        and frame_cache_window.get("requested_start_pts") is not None
        and frame_cache_window.get("requested_end_pts") is not None
    ):
        try:
            selected_rows, selected_time_window = _select_time_domain_frames(
                source_metadata_rows,
                requested_start_pts=_to_int(frame_cache_window.get("requested_start_pts")),
                requested_end_pts=_to_int(frame_cache_window.get("requested_end_pts")),
                event_frame_pts=_to_int(frame_cache_window.get("event_frame_pts")),
                event_frame_uuid=str(replay_labels.get("event_frame_uuid") or ""),
                start_window_frame_uuid=str(replay_labels.get("start_window_frame_uuid") or ""),
                post_window_frame_uuid=str(replay_labels.get("post_window_frame_uuid") or ""),
                enabled=True,
            )
            frame_cache_time_window = {**frame_cache_window, **selected_time_window}
            frame_cache_time_window["replay_labels"] = replay_labels
            frame_cache_time_window["replay_job_request"] = replay_job_request
            frame_cache_video_crop = _copy_or_crop_video(
                source_video_path=Path(source_video),
                output_video_path=raw_clip_path,
                source_frames=source_metadata_rows,
                time_window=frame_cache_time_window,
                copy_video=True,
                crop_video_to_time_window=True,
                materialization_timeout_s=materialization_timeout_s,
            )
            _write_metadata_jsonl(sink_metadata_path, selected_rows)
            sink_metadata_rows_for_guard = selected_rows
        except Exception as exc:
            logger.warning(
                "frame_cache_time_domain_crop_failed event_id=%s error=%s",
                event_id,
                exc,
            )
            frame_cache_time_window = {
                **frame_cache_time_window,
                "time_domain_crop_applied": False,
                "time_domain_crop_failed": True,
                "time_domain_crop_error": f"{type(exc).__name__}:{exc}",
                "replay_labels": replay_labels,
                "replay_job_request": replay_job_request,
            }
            raw_clip_path.unlink(missing_ok=True)
            raw_clip_available = False
            frame_cache_video_crop = {
                "method": "failed_time_domain_crop",
                "crop_video_to_time_window": True,
                "source_video_path": source_video,
                "error": f"{type(exc).__name__}:{exc}",
                "published_raw_clip": False,
            }
            sink_metadata_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(metadata_file, sink_metadata_path)
            sink_metadata_rows_for_guard = source_metadata_rows
    else:
        if not raw_clip_path.exists():
            frame_cache_video_crop = _copy_or_crop_video(
                source_video_path=Path(source_video),
                output_video_path=raw_clip_path,
                source_frames=source_metadata_rows,
                time_window=frame_cache_time_window,
                copy_video=True,
                crop_video_to_time_window=False,
                materialization_timeout_s=materialization_timeout_s,
            )
            if fast_raw_clip_enabled:
                frame_cache_video_crop["materialization_mode"] = (
                    "rolling_cache_copy" if rolling_cache_info else "fast_raw_copy"
                )
                frame_cache_video_crop["fast_raw_clip_enabled"] = True
            if rolling_cache_info:
                frame_cache_video_crop["materialization_mode"] = "rolling_cache_copy"
                frame_cache_video_crop["rolling_cache_enabled"] = True
        sink_metadata_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(metadata_file, sink_metadata_path)
        sink_metadata_rows_for_guard = source_metadata_rows
    sink_metadata_rows = (
        []
        if metadata_has_objects
        else list(sink_metadata_rows_for_guard or load_native_metadata(sink_metadata_path))
    )
    decoded_frame_count = None
    if not metadata_has_objects:
        decoded_frame_count, decoded_frame_count_duration_ms = (
            _decoded_frame_count_with_timing(
                raw_clip_path,
                raw_clip_available=raw_clip_available,
                known_frame_count=rolling_cache_selected_frame_count,
            )
        )

    if metadata_has_objects:
        builder_event_metadata = {
            **_uuid_first_anchor_metadata(
                replay_labels=replay_labels,
                replay_job_request=replay_job_request,
                time_window=frame_cache_time_window,
            ),
            "replay_source_kind": replay_labels.get("replay_source_kind"),
            "requested_start_pts": frame_cache_time_window.get(
                "requested_start_pts"
            ),
            "original_requested_start_pts": frame_cache_time_window.get(
                "original_requested_start_pts"
            ),
            "effective_start_pts": frame_cache_time_window.get("effective_start_pts"),
            "requested_end_pts": frame_cache_time_window.get("requested_end_pts"),
            "requested_duration_s": frame_cache_time_window.get(
                "requested_duration_s"
            ),
            "actual_start_pts": frame_cache_time_window.get("actual_start_pts"),
            "actual_end_pts": frame_cache_time_window.get("actual_end_pts"),
            "actual_duration_s": frame_cache_time_window.get("actual_duration_s"),
            "event_frame_pts": frame_cache_time_window.get("event_frame_pts"),
            "pre_window_truncated": frame_cache_time_window.get(
                "pre_window_truncated"
            ),
            "pre_window_policy": replay_labels.get("pre_window_policy"),
            "requested_pre_window_seconds": replay_labels.get(
                "requested_pre_window_seconds"
            ),
            "effective_pre_window_seconds": replay_labels.get(
                "effective_pre_window_seconds"
            ),
            "pre_window_truncated_seconds": replay_labels.get(
                "pre_window_truncated_seconds"
            ),
            "replay_offset_seconds": (replay_job_request.get("offset") or {}).get("seconds"),
            "replay_stop_strategy": replay_labels.get("replay_stop_strategy"),
            "time_domain_crop_applied": bool(
                frame_cache_time_window.get("time_domain_crop_applied")
            ),
            "annotation_source_policy": replay_labels.get("annotation_source_policy"),
            "replay_stored_stream_id": replay_configuration.get("stored_stream_id"),
            "replay_resulting_stream_id": replay_configuration.get("resulting_stream_id"),
        }
        if _include_legacy_metadata_fields():
            builder_event_metadata.update(
                {
                    "legacy_record_request_id": replay_labels.get("request_id"),
                    "legacy_source_event_id": (
                        replay_labels.get("source_event_id")
                        or event_context.get("source_event_id", "")
                    ),
                    "legacy_event_type": event_context.get("event_type", ""),
                    "legacy_camera_id": event_context.get("camera_id", ""),
                    "legacy_source_id": event_context.get("source_id", ""),
                    "legacy_frame_pts": replay_labels.get("frame_pts"),
                    "legacy_frame_num": replay_labels.get("frame_num"),
                }
            )
        result = build_post_savant_evidence_bundle(
            input_dir=Path(meta_dir),
            output_dir=output_dir,
            copy_video=True,
            trim_sidecar_to_video=True,
            overwrite=True,
            max_fps=os.getenv("MAX_FPS"),
            min_fps=os.getenv("MIN_FPS"),
            fps_gating_applied=_post_savant_fps_gating_applied(),
            source_input_fps_estimate=_to_float(os.getenv("SOURCE_INPUT_FPS_ESTIMATE")),
            requested_start_pts=_to_int(frame_cache_time_window.get("requested_start_pts")),
            requested_end_pts=_to_int(frame_cache_time_window.get("requested_end_pts")),
            event_frame_pts=_to_int(frame_cache_time_window.get("event_frame_pts")),
            event_frame_uuid=str(replay_labels.get("event_frame_uuid") or ""),
            start_window_frame_uuid=str(replay_labels.get("start_window_frame_uuid") or ""),
            post_window_frame_uuid=str(replay_labels.get("post_window_frame_uuid") or ""),
            time_domain_crop_applied=bool(
                frame_cache_time_window.get("time_domain_crop_applied")
            ),
            crop_video_to_time_window=False
            if fast_raw_clip_enabled
            else bool(frame_cache_time_window.get("time_domain_crop_applied")),
            event_metadata=builder_event_metadata,
            video_integrity_required=False,
            materialization_timeout_s=materialization_timeout_s,
            decoded_frame_count_reader=(
                (lambda _path: int(rolling_cache_selected_frame_count or len(source_metadata_rows)))
                if rolling_cache_info
                else None
            ),
            decoded_video_duration_s=(
                rolling_cache_output_duration_s
                if rolling_cache_info and rolling_cache_output_duration_s
                else None
            ),
        )
    else:
        sidecar_config = load_frame_cache_sidecar_config()
        sidecar_config.update(
            {
                "enabled": True,
                "event_types": {"intrusion", "watchlist_hit"},
                "require_trigger_face": False,
                "pre_seconds": float(os.getenv("DEFAULT_PRE_SECONDS", "5")),
                "post_seconds": float(os.getenv("DEFAULT_POST_SECONDS", "5")),
            }
        )
        identity_continuations = _watchlist_identity_continuations(
            pg_conn,
            event_context,
            post_seconds=float(sidecar_config.get("post_seconds") or 5.0),
        )
        if raw_clip_duration is None:
            raw_clip_duration = (
                _probe_video_duration_seconds(str(raw_clip_path))
                if raw_clip_available
                else None
            )
        sidecar_summary, sidecar_result = _write_frame_cache_sidecar_after_anchor(
            event=_event_for_frame_cache_sidecar(event_context),
            evidence_dir=str(output_dir),
            raw_clip_path=str(raw_clip_path) if raw_clip_available else None,
            metadata_path=str(sink_metadata_path),
            redis_client=None,
            config=sidecar_config,
            identity_continuations=identity_continuations,
            final_clip_context={
                "raw_clip_path": str(raw_clip_path) if raw_clip_available else None,
                "sink_metadata_path": str(sink_metadata_path),
                "raw_clip_duration": raw_clip_duration,
                "expected_duration_seconds": _requested_duration_from_time_window(
                    frame_cache_time_window
                ),
                "expected_event_t_s": frame_cache_time_window.get("expected_event_t_s"),
                "event_projected_t_s": None,
                "event_pts_inside_clip": None,
                "event_position_ratio": None,
            },
        )
        if (
            str(sidecar_summary.get("annotation_status") or "")
            == "missing_frame_metadata"
            and int(sidecar_summary.get("annotations_written") or 0) <= 0
        ):
            db_recovery = _write_person_bbox_db_sidecar_fallback(
                pg_conn,
                event_context=event_context,
                output_dir=output_dir,
                sink_metadata_rows=sink_metadata_rows,
            )
            if db_recovery is not None:
                sidecar_summary, sidecar_result = db_recovery
        summary = _build_frame_cache_summary(
            sidecar_summary=sidecar_summary,
            sink_metadata_rows=sink_metadata_rows,
            decoded_video_frame_count=int(decoded_frame_count or 0),
            raw_clip_path=raw_clip_path,
            sink_metadata_path=sink_metadata_path,
            event_context=event_context,
            time_window=frame_cache_time_window,
            video_crop=frame_cache_video_crop,
        )
        summary_path = output_dir / SUMMARY_FILE
        sidecar_summary_path = output_dir / SIDECAR_SUMMARY_FILE
        _atomic_write_json(summary_path, summary)
        _atomic_write_json(sidecar_summary_path, summary)
        result = _EvidenceBundleView(
            output_dir=output_dir,
            raw_clip_path=raw_clip_path if raw_clip_available else None,
            sink_metadata_path=sink_metadata_path,
            production_sidecar_path=Path(
                sidecar_result.get("annotations_path") or output_dir / SIDECAR_ANNOTATIONS_FILE
            ),
            summary_path=summary_path,
            summary=summary,
        )
    duration_guard = _post_savant_duration_guard(
        getattr(result, "raw_clip_path", None),
        result.summary.get("time_window") if isinstance(result.summary, dict) else None,
        actual_duration=raw_clip_duration,
    )
    _merge_post_savant_duration_guard(result.summary, duration_guard)
    sink_window_rows = (
        sink_metadata_rows_for_guard
        if sink_metadata_rows_for_guard is not None
        else load_native_metadata(Path(result.sink_metadata_path))
    )
    sink_window_guard = _sink_metadata_window_guard(
        sink_window_rows,
        result.summary.get("time_window") if isinstance(result.summary, dict) else None,
    )
    if rolling_cache_info:
        result.summary.update(
            {
                "materialization_mode": "rolling_cache_copy",
                "rolling_cache_enabled": True,
                "rolling_cache": rolling_cache_info,
                "canonical_clip": bool(rolling_cache_info.get("canonical_clip")),
                "requested_start_pts": rolling_cache_info.get("requested_start_pts"),
                "requested_end_pts": rolling_cache_info.get("requested_end_pts"),
                "actual_start_pts": rolling_cache_info.get("actual_start_pts"),
                "actual_end_pts": rolling_cache_info.get("actual_end_pts"),
                "segment_ids": rolling_cache_info.get("segment_ids") or [],
            }
        )
        video_crop = result.summary.get("video_crop")
        if isinstance(video_crop, dict):
            video_crop["materialization_mode"] = "rolling_cache_copy"
            video_crop["rolling_cache_enabled"] = True
    _merge_sink_window_guard(result.summary, sink_window_guard)
    if fast_raw_clip_enabled and not rolling_cache_info:
        duration_guard, sink_window_guard = _relax_post_savant_fast_raw_clip_guards(
            summary=result.summary,
            duration_guard=duration_guard,
            sink_window_guard=sink_window_guard,
        )
    epoch_guard = _runtime_epoch_guard(
        event_context=event_context,
        replay_labels=replay_labels,
        sink_metadata=sink_metadata,
        meta_dir=meta_dir,
    )
    if rolling_cache_info:
        epoch_guard = _relax_rolling_cache_epoch_guard(epoch_guard)
    _merge_runtime_epoch_guard(result.summary, epoch_guard)
    if frame_cache_time_window.get("time_domain_crop_failed") is True:
        _apply_post_savant_failure_status(
            result.summary,
            reason="time_domain_crop_failed",
            duration_guard=duration_guard,
        )
        result = _without_published_raw_clip(result)
    elif duration_guard.get("duration_guard_failed") is True:
        _apply_post_savant_failure_status(
            result.summary,
            reason=duration_guard.get("duration_guard_reason")
            or "raw_clip_duration_exceeds_requested_window",
            duration_guard=duration_guard,
        )
        result = _without_published_raw_clip(result)
    elif epoch_guard.get("epoch_guard_failed") is True:
        _apply_post_savant_failure_status(
            result.summary,
            reason="runtime_epoch_guard_failed",
            duration_guard=duration_guard,
        )
        result.summary["epoch_guard_reason"] = (
            epoch_guard.get("epoch_guard_reason")
            or "missing_or_mismatched_runtime_epoch"
        )
        result = _without_published_raw_clip(result)
    elif sink_window_guard.get("sink_window_guard_failed") is True:
        _apply_post_savant_failure_status(
            result.summary,
            reason="sink_window_guard_failed",
            duration_guard=duration_guard,
        )
        result.summary["sink_window_guard_reason"] = (
            sink_window_guard.get("sink_window_guard_reason")
            or "sink_metadata_window_guard_failed"
        )
        result = _without_published_raw_clip(result)
    result.summary["clip_status"] = _summary_clip_status(result.summary)
    result.summary["materialization_guard_attribution"] = (
        guard_failure_attribution(result.summary)
    )
    materialization_finished_at = datetime.now(timezone.utc)
    finalization_duration_ms = int((time.monotonic() - finalize_started) * 1000)
    finalization_process_cpu_seconds = round(
        max(0.0, time.process_time() - finalization_process_cpu_started),
        6,
    )
    finalization_thread_cpu_seconds = (
        round(max(0.0, time.thread_time() - finalization_thread_cpu_started), 6)
        if finalization_thread_cpu_started is not None
        else None
    )
    job_probe_metrics = _probe_metrics_delta(probe_before)
    result.summary["materialization_metrics"] = _post_savant_materialization_metrics(
        summary=result.summary,
        event_context=event_context,
        started_at=materialization_started_at,
        finished_at=materialization_finished_at,
        finalization_duration_ms=finalization_duration_ms,
        finalization_process_cpu_seconds=finalization_process_cpu_seconds,
        finalization_thread_cpu_seconds=finalization_thread_cpu_seconds,
        job_probe_metrics=job_probe_metrics,
        materialization_guardrails=materialization_guardrails,
        phase_diagnostics=phase_diagnostics,
    )
    result.summary["media_worker_perf"] = {
        "finalization_duration_ms": finalization_duration_ms,
        "metadata_rows_loaded": metadata_rows_loaded,
        "sink_metadata_rows_for_guard": len(sink_window_rows),
        "decoded_frame_count_duration_ms": decoded_frame_count_duration_ms,
        **job_probe_metrics,
    }
    _rewrite_post_savant_summary_files(result)
    business_metadata = _build_event_metadata(
        event_context=event_context,
        replay_job_id=replay_job_id,
        replay_job_request=replay_job_request,
        sink_output_dir=meta_dir,
        bundle_result=result,
    )
    metadata_out = result.output_dir / "metadata.json"
    _atomic_write_json(metadata_out, business_metadata)
    summary = result.summary
    object_counts = summary.get("object_counts") or {}
    if not isinstance(object_counts, dict):
        object_counts = {}
    return {
        "evidence_dir": str(result.output_dir),
        "raw_clip": _path_for_metadata(result.raw_clip_path),
        "metadata": str(metadata_out),
        "sink_metadata": str(result.sink_metadata_path),
        "annotations_jsonl": str(result.production_sidecar_path),
        "summary": str(result.summary_path),
        "sink_output_path": meta_dir,
        "clip_status": business_metadata.get("status", {}).get(
            "clip_status", "generated_unverified"
        ),
        "annotation_status": summary.get("annotation_status"),
        "annotation_lines": int(summary.get("sidecar_frame_count") or 0),
        "annotation_empty_reason": "",
        "annotation_unavailable_reason": "",
        "overlay_available": bool(summary.get("production_ready")),
        "frontend_overlay_required": bool(summary.get("production_ready")),
        "duration_guard_status": summary.get("duration_guard_status"),
        "duration_guard_failed": bool(summary.get("duration_guard_failed")),
        "max_allowed_duration_seconds": summary.get("max_allowed_duration_seconds"),
        "sink_window_guard_status": summary.get("sink_window_guard_status"),
        "sink_window_guard_failed": bool(summary.get("sink_window_guard_failed")),
        "sink_window_guard_reason": summary.get("sink_window_guard_reason", ""),
        "materialization_guard_attribution": summary.get(
            "materialization_guard_attribution"
        )
        or {},
        "runtime_epoch_id": summary.get("runtime_epoch_id", ""),
        "epoch_guard_status": summary.get("epoch_guard_status"),
        "epoch_guard_failed": bool(summary.get("epoch_guard_failed")),
        "epoch_guard_reason": summary.get("epoch_guard_reason", ""),
        "raw_clip_sanitize_method": "post_savant_evidence_bundle",
        "raw_clip_sanitize_decode_ok": None,
        "raw_clip_sanitize_decode_error_count": 0,
        "raw_clip_sanitize_fallback_used": False,
        "raw_clip_sanitize_error": "",
        "evidence_topology": POST_SAVANT_REPLAY_EVIDENCE_TOPOLOGY,
        "annotation_source": summary.get("annotation_source"),
        "production_ready": bool(summary.get("production_ready")),
        "person_count": int(object_counts.get("person") or 0),
        "face_count": int(object_counts.get("face") or 0),
        "known_face_count": int(object_counts.get("known_face") or 0),
        "materialization_metrics": summary.get("materialization_metrics") or {},
    }


def _process_sink_output(
    pg_conn: psycopg.Connection,
    sink_dir: str,
    processed_dirs: set[str],
    *,
    evidence_output_dir: str | None = None,
    candidate_dirs: dict[str, tuple[int, int]] | None = None,
    invalid_output_failures: dict[str, int] | None = None,
    midterm_sink_stability_checks: int = 2,
    processed_state_path: str | Path | None = None,
    sink_scan_max_metadata_files: int | None = None,
    materialization_guard: _MaterializationGuard | None = None,
    materialization_pacer: _MaterializationPacer | None = None,
    materialization_timeout_s: float = 0.0,
    materialization_max_backlog: int = 0,
    evidence_final_root_max_bytes: int = 0,
    evidence_incoming_root_max_bytes: int = 0,
    replay_sink_output_max_bytes: int = 0,
    evidence_storage_warning_ratio: float = 0.80,
    evidence_storage_critical_ratio: float = 0.90,
    evidence_storage_hard_ratio: float = 1.00,
    cleanup_replay_sink_output_enabled: bool = False,
    cleanup_replay_sink_output_statuses: tuple[str, ...] = DEFAULT_CLEANUP_REPLAY_SINK_OUTPUT_STATUSES,
    materialization_finalizer_workers: int = DEFAULT_MATERIALIZATION_FINALIZER_WORKERS,
    materialization_finalizer_max_per_source_per_poll: int = 1,
    materialization_finalizer_source_serial: bool = True,
    materialization_database_url: str | None = None,
    finalizer_worker_id: str = "main",
    metadata_files_override: list[dict] | None = None,
    scan_stats_override: dict | None = None,
    finalizer_phase: dict[str, object] | None = None,
) -> int:
    """Process new sink outputs and update events table. Returns count of updates."""
    updated = 0
    if processed_state_path is not None:
        processed_dirs.update(_load_processed_sink_state(processed_state_path))
    processed_dirs_before = set(processed_dirs)
    if metadata_files_override is None:
        metadata_files, scan_stats = _scan_metadata_files(
            sink_dir,
            processed_dirs=processed_dirs,
            max_metadata_files=sink_scan_max_metadata_files,
        )
    else:
        metadata_files = list(metadata_files_override)
        scan_stats = scan_stats_override or {
            "scan_duration_ms": 0,
            "metadata_files_visited": len(metadata_files),
            "metadata_files_parsed": len(metadata_files),
        }
    post_savant_finalizer_enabled = _post_savant_finalizer_enabled()
    if materialization_pacer is not None:
        materialization_pacer.reset_poll()
    schedule_rows: dict[str, dict] = {}
    if post_savant_finalizer_enabled:
        event_ids = [
            event_id
            for event_id in (_extract_event_id(meta) for meta in metadata_files)
            if event_id
        ]
        schedule_rows = _materialization_schedule_rows(pg_conn, event_ids)
        metadata_files = _sort_metadata_for_materialization(metadata_files, schedule_rows)
    if (
        post_savant_finalizer_enabled
        and materialization_finalizer_workers > 1
        and materialization_database_url
    ):
        return _process_sink_output_with_finalizer_pool(
            pg_conn,
            sink_dir,
            processed_dirs,
            metadata_files=metadata_files,
            schedule_rows=schedule_rows,
            scan_stats=scan_stats,
            evidence_output_dir=evidence_output_dir,
            candidate_dirs=candidate_dirs,
            invalid_output_failures=invalid_output_failures,
            midterm_sink_stability_checks=midterm_sink_stability_checks,
            processed_state_path=processed_state_path,
            sink_scan_max_metadata_files=sink_scan_max_metadata_files,
            materialization_timeout_s=materialization_timeout_s,
            materialization_max_backlog=materialization_max_backlog,
            materialization_max_per_poll=(
                materialization_pacer.max_per_poll
                if materialization_pacer is not None
                else 0
            ),
            materialization_throttle_sleep_s=(
                materialization_pacer.throttle_sleep_s
                if materialization_pacer is not None
                else 0.0
            ),
            materialization_throttle_deadline_guard_s=(
                materialization_pacer.deadline_guard_s
                if materialization_pacer is not None
                else 0.0
            ),
            materialization_finalizer_workers=materialization_finalizer_workers,
            materialization_finalizer_max_per_source_per_poll=(
                materialization_finalizer_max_per_source_per_poll
            ),
            materialization_finalizer_source_serial=(
                materialization_finalizer_source_serial
            ),
            materialization_database_url=materialization_database_url,
            evidence_final_root_max_bytes=evidence_final_root_max_bytes,
            evidence_incoming_root_max_bytes=evidence_incoming_root_max_bytes,
            replay_sink_output_max_bytes=replay_sink_output_max_bytes,
            evidence_storage_warning_ratio=evidence_storage_warning_ratio,
            evidence_storage_critical_ratio=evidence_storage_critical_ratio,
            evidence_storage_hard_ratio=evidence_storage_hard_ratio,
            cleanup_replay_sink_output_enabled=cleanup_replay_sink_output_enabled,
            cleanup_replay_sink_output_statuses=cleanup_replay_sink_output_statuses,
        )
    for meta in metadata_files:
        meta_dir = meta.get("_meta_dir", "")

        # Idempotency: skip already-processed directories
        if meta_dir and meta_dir in processed_dirs:
            continue
        if meta_dir and _invalid_sink_output_marker_path(meta_dir).exists():
            processed_dirs.add(meta_dir)
            logger.debug("media_skip: sink output marked invalid meta_dir=%s", meta_dir)
            continue

        event_id = _extract_event_id(meta)
        if not event_id:
            logger.error(
                "media_failure: no event_id could be extracted from metadata "
                "meta_dir=%s source_id=%s resulting_stream_id=%s",
                meta_dir,
                meta.get("source_id", ""),
                meta.get("resulting_stream_id", ""),
            )
            continue
        phase_diagnostics = (
            dict(finalizer_phase)
            if isinstance(finalizer_phase, dict)
            else _mark_sink_phase(str(meta_dir), event_id, "sink_metadata_first_seen")
        )
        finalizer_lease: MaterializationLease | None = None

        # Idempotency: skip if already marked ready
        if _is_already_ready(pg_conn, event_id):
            if meta_dir:
                processed_dirs.add(meta_dir)
                _clear_sink_phase(str(meta_dir))
            logger.debug("media_skip: event already ready event_id=%s", event_id)
            continue

        finalizer_enabled = post_savant_finalizer_enabled

        video_file = _find_video_file(meta_dir)
        if not video_file:
            continue  # not ready yet
        if finalizer_phase is None:
            phase_diagnostics = _mark_sink_phase(
                str(meta_dir),
                event_id,
                "sink_video_first_seen",
            )

        metadata_file = str(Path(meta_dir) / "metadata.json")

        stable_count = 0
        ready = False
        reason = "finalizer_disabled"
        if finalizer_enabled:
            ready, reason = _sink_output_ready_for_finalizer(
                video_file=video_file,
                metadata_file=metadata_file,
                known_duration_s=_known_sink_output_duration_seconds(meta),
            )

        if ready:
            if finalizer_phase is None:
                phase_diagnostics = _mark_sink_phase(
                    str(meta_dir),
                    event_id,
                    "sink_video_stable",
                )
                _release_replay_slot_for_sink_stable(
                    pg_conn,
                    event_id=event_id,
                    phase_diagnostics=phase_diagnostics,
                )
        elif finalizer_enabled and candidate_dirs is not None:
            try:
                current_size = Path(video_file).stat().st_size
            except OSError:
                continue
            previous_size, stable_count = candidate_dirs.get(meta_dir, (-1, 0))
            stable_count = stable_count + 1 if current_size == previous_size else 0
            candidate_dirs[meta_dir] = (current_size, stable_count)
            if stable_count < max(1, midterm_sink_stability_checks):
                logger.debug(
                    "media_wait_for_stable_sink_output event_id=%s meta_dir=%s "
                    "size=%s previous_size=%s stable_count=%s required=%s",
                    event_id,
                    meta_dir,
                    current_size,
                    previous_size,
                    stable_count,
                    midterm_sink_stability_checks,
                )
                continue
            if finalizer_phase is None:
                phase_diagnostics = _mark_sink_phase(
                    str(meta_dir),
                    event_id,
                    "sink_video_stable",
                )
                _release_replay_slot_for_sink_stable(
                    pg_conn,
                    event_id=event_id,
                    phase_diagnostics=phase_diagnostics,
                )
            ready, reason = _sink_output_ready_for_finalizer(
                video_file=video_file,
                metadata_file=metadata_file,
                known_duration_s=_known_sink_output_duration_seconds(meta),
            )
        elif finalizer_phase is None:
            phase_diagnostics = _mark_sink_phase(
                str(meta_dir),
                event_id,
                "sink_video_stable",
            )
            _release_replay_slot_for_sink_stable(
                pg_conn,
                event_id=event_id,
                phase_diagnostics=phase_diagnostics,
            )

        if finalizer_enabled:
            if not ready:
                invalid_attempts = 0
                if (
                    reason in PERMANENT_INVALID_SINK_OUTPUT_REASONS
                    and invalid_output_failures is not None
                    and stable_count >= max(1, midterm_sink_stability_checks)
                ):
                    invalid_attempts = invalid_output_failures.get(meta_dir, 0) + 1
                    invalid_output_failures[meta_dir] = invalid_attempts
                    if invalid_attempts >= _invalid_sink_output_max_retries():
                        error_message = f"sink_output_invalid:{reason}"
                        logger.warning(
                            "media_sink_output_marked_invalid event_id=%s "
                            "meta_dir=%s video_file=%s reason=%s attempts=%s",
                            event_id,
                            meta_dir,
                            video_file,
                            reason,
                            invalid_attempts,
                        )
                        _write_invalid_sink_output_marker(
                            meta_dir=meta_dir,
                            event_id=event_id,
                            video_file=video_file,
                            reason=reason,
                            attempts=invalid_attempts,
                        )
                        _mark_media_finalize_failed(
                            pg_conn,
                            event_id=event_id,
                            sink_path=meta_dir,
                            error_message=error_message,
                            lease=finalizer_lease,
                        )
                        processed_dirs.add(meta_dir)
                        continue
                logger.info(
                    "media_wait_for_finalized_sink_output event_id=%s meta_dir=%s "
                    "video_file=%s reason=%s invalid_attempts=%s",
                    event_id,
                    meta_dir,
                    video_file,
                    reason,
                    invalid_attempts,
                )
                continue
            if finalizer_phase is None:
                phase_diagnostics = _mark_sink_phase(
                    str(meta_dir),
                    event_id,
                    "sink_ffprobe_ready",
                )

        if (
            post_savant_finalizer_enabled
            and materialization_pacer is not None
            and not materialization_pacer.can_start(
                deadline_at=(
                    schedule_rows.get(event_id, {}).get("materialization_deadline_at")
                    if schedule_rows
                    else None
                )
            )
        ):
            reason = "materialization_max_per_poll_reached"
            guardrails = _materialization_guardrails(
                guard=materialization_guard,
                pacer=materialization_pacer,
                timeout_s=materialization_timeout_s,
                max_backlog=materialization_max_backlog,
                backlog_depth=None,
                admission_status="retry",
                reason=reason,
            )
            _mark_media_materialization_retry(
                pg_conn,
                event_id=event_id,
                sink_path=meta_dir,
                reason=reason,
                guardrails=guardrails,
                retry_after_s=max(
                    1.0,
                    float(os.getenv("MEDIA_POLL_INTERVAL_S", "2")),
                ),
            )
            logger.info(
                "media_materialization_paced event_id=%s meta_dir=%s "
                "reason=max_per_poll_reached max_per_poll=%s processed_this_poll=%s",
                event_id,
                meta_dir,
                materialization_pacer.max_per_poll,
                materialization_pacer.processed_this_poll,
            )
            break

        bundle = None
        finalize_started = time.monotonic()
        probe_before = _probe_metrics_snapshot()
        storage_decision = _storage_quota_decision(
            evidence_output_dir=evidence_output_dir,
            incoming_dir=(
                str(Path(evidence_output_dir) / ".incoming")
                if evidence_output_dir
                else None
            ),
            sink_output_dir=sink_dir,
            evidence_final_root_max_bytes=evidence_final_root_max_bytes,
            evidence_incoming_root_max_bytes=evidence_incoming_root_max_bytes,
            replay_sink_output_max_bytes=replay_sink_output_max_bytes,
            warning_ratio=evidence_storage_warning_ratio,
            critical_ratio=evidence_storage_critical_ratio,
            hard_ratio=evidence_storage_hard_ratio,
        )
        if post_savant_finalizer_enabled and storage_decision["overall_level"] == "hard":
            reason = "storage_hard_limit_exceeded"
            guardrails = _materialization_guardrails(
                guard=materialization_guard,
                pacer=materialization_pacer,
                timeout_s=materialization_timeout_s,
                max_backlog=materialization_max_backlog,
                backlog_depth=None,
                admission_status="deferred",
                reason=reason,
            )
            guardrails["storage_quota_decision"] = storage_decision
            _mark_media_materialization_deferred(
                pg_conn,
                event_id=event_id,
                sink_path=meta_dir,
                reason=reason,
                guardrails=guardrails,
            )
            logger.warning(
                "media_materialization_deferred event_id=%s meta_dir=%s reason=%s",
                event_id,
                meta_dir,
                reason,
            )
            continue
        backlog_depth = (
            _materialization_backlog_depth(pg_conn)
            if post_savant_finalizer_enabled and materialization_max_backlog > 0
            else None
        )
        if (
            post_savant_finalizer_enabled
            and _materialization_backlog_limit_exceeded(
                backlog_depth=backlog_depth,
                max_backlog=materialization_max_backlog,
            )
        ):
            reason = (
                "materialization_backlog_limit_exceeded:"
                f"{backlog_depth}>={materialization_max_backlog}"
            )
            guardrails = _materialization_guardrails(
                guard=materialization_guard,
                pacer=materialization_pacer,
                timeout_s=materialization_timeout_s,
                max_backlog=materialization_max_backlog,
                backlog_depth=backlog_depth,
                admission_status="deferred",
                reason=reason,
            )
            _mark_media_materialization_retry(
                pg_conn,
                event_id=event_id,
                sink_path=meta_dir,
                reason=reason,
                guardrails=guardrails,
                retry_after_s=max(
                    1.0,
                    float(os.getenv("MEDIA_POLL_INTERVAL_S", "2")),
                ),
            )
            logger.info(
                "media_materialization_deferred event_id=%s meta_dir=%s reason=%s",
                event_id,
                meta_dir,
                reason,
            )
            continue
        acquired_materialization_slot = True
        if post_savant_finalizer_enabled and materialization_guard is not None:
            acquired_materialization_slot = materialization_guard.acquire()
            if not acquired_materialization_slot:
                reason = "materialization_concurrency_limit_exceeded"
                guardrails = _materialization_guardrails(
                    guard=materialization_guard,
                    pacer=materialization_pacer,
                    timeout_s=materialization_timeout_s,
                    max_backlog=materialization_max_backlog,
                    backlog_depth=backlog_depth,
                    admission_status="deferred",
                    reason=reason,
                )
                _mark_media_materialization_retry(
                    pg_conn,
                    event_id=event_id,
                    sink_path=meta_dir,
                    reason=reason,
                    guardrails=guardrails,
                    retry_after_s=max(1.0, float(os.getenv("MEDIA_POLL_INTERVAL_S", "2"))),
                )
                logger.info(
                    "media_materialization_deferred event_id=%s meta_dir=%s reason=%s",
                    event_id,
                    meta_dir,
                    reason,
                )
                continue
        guardrails = _materialization_guardrails(
            guard=materialization_guard,
            pacer=materialization_pacer,
            timeout_s=materialization_timeout_s,
            max_backlog=materialization_max_backlog,
            backlog_depth=backlog_depth,
            admission_status="admitted",
        )
        if post_savant_finalizer_enabled and materialization_pacer is not None:
            materialization_pacer.record_start()
        if post_savant_finalizer_enabled:
            if finalizer_phase is None:
                now_iso = datetime.now(timezone.utc).isoformat()
                now_monotonic = time.monotonic()
                phase_diagnostics = {
                    **_sink_phase_snapshot(str(meta_dir)),
                    "finalizer_submitted_at": now_iso,
                    "finalizer_started_at": now_iso,
                    "finalizer_submitted_monotonic": now_monotonic,
                    "finalizer_started_monotonic": now_monotonic,
                }
            else:
                phase_diagnostics = dict(finalizer_phase)
        schedule_row = schedule_rows.get(event_id, {}) if schedule_rows else {}
        source_id = _metadata_source_id(meta, schedule_row)
        replay_shard_id = _metadata_replay_shard_id(meta, schedule_row)
        claim_wait_ms = 0
        claim_result = {"status": "not_required", "claimed": True}
        if post_savant_finalizer_enabled:
            claim_started = time.monotonic()
            claim_result = _claim_media_finalization(
                pg_conn,
                event_id=event_id,
                sink_path=meta_dir,
                worker_id=finalizer_worker_id,
            )
            claim_wait_ms = int((time.monotonic() - claim_started) * 1000)
            claim_status = str(claim_result.get("status") or "")
            finalizer_lease = (
                claim_result.get("lease")
                if isinstance(claim_result.get("lease"), MaterializationLease)
                else None
            )
            if claim_status in {"terminal"}:
                if meta_dir:
                    processed_dirs.add(meta_dir)
                    _clear_sink_phase(str(meta_dir))
                logger.info(
                    "media_finalization_claim_terminal event_id=%s meta_dir=%s "
                    "worker_id=%s source_id=%s replay_shard_id=%s claim_wait_ms=%s",
                    event_id,
                    meta_dir,
                    finalizer_worker_id,
                    source_id,
                    replay_shard_id,
                    claim_wait_ms,
                )
                continue
            if claim_status == "missing":
                cleanup_result = _cleanup_orphan_sink_output(
                    meta_dir=meta_dir,
                    sink_root=sink_dir,
                    event_id=event_id,
                )
                if meta_dir:
                    processed_dirs.add(meta_dir)
                    _clear_sink_phase(str(meta_dir))
                logger.info(
                    "media_finalization_orphan_sink_output event_id=%s meta_dir=%s "
                    "worker_id=%s source_id=%s replay_shard_id=%s "
                    "claim_wait_ms=%s cleanup_status=%s deleted_bytes=%s",
                    event_id,
                    meta_dir,
                    finalizer_worker_id,
                    source_id,
                    replay_shard_id,
                    claim_wait_ms,
                    cleanup_result.get("status"),
                    cleanup_result.get("deleted_bytes"),
                )
                continue
            if claim_status in {"busy", "claim_error"}:
                logger.info(
                    "media_finalization_claim_busy event_id=%s meta_dir=%s "
                    "worker_id=%s source_id=%s replay_shard_id=%s "
                    "claim_status=%s claim_wait_ms=%s",
                    event_id,
                    meta_dir,
                    finalizer_worker_id,
                    source_id,
                    replay_shard_id,
                    claim_status,
                    claim_wait_ms,
                )
                continue
        _set_event_evidence_state(pg_conn, event_id, state="materializing")
        terminal_transition_persisted = False
        evidence_state = "materializing"
        evidence_reason = ""
        try:
            if post_savant_finalizer_enabled:
                if not evidence_output_dir:
                    logger.error("post_savant_finalizer enabled but no evidence_output_dir")
                    continue
                try:
                    bundle = _finalize_post_savant_evidence_bundle(
                        pg_conn,
                        event_id=event_id,
                        meta_dir=meta_dir,
                        metadata_file=metadata_file,
                        evidence_output_dir=evidence_output_dir,
                        materialization_timeout_s=materialization_timeout_s,
                        materialization_guardrails=guardrails,
                        phase_diagnostics=phase_diagnostics,
                    )
                except Exception as exc:
                    error_message = f"{type(exc).__name__}:{exc}"
                    logger.exception(
                        "post_savant_finalizer_failed event_id=%s meta_dir=%s",
                        event_id,
                        meta_dir,
                    )
                    if "timeout" in error_message:
                        failed_guardrails = {
                            **guardrails,
                            "admission_status": "failed",
                            "reason": error_message,
                        }
                        _mark_media_materialization_failed(
                            pg_conn,
                            event_id=event_id,
                            sink_path=meta_dir,
                            reason=error_message,
                            guardrails=failed_guardrails,
                            lease=finalizer_lease,
                        )
                    else:
                        _mark_media_finalize_failed(
                            pg_conn,
                            event_id=event_id,
                            sink_path=meta_dir,
                            error_message=error_message,
                        )
                    if meta_dir:
                        processed_dirs.add(meta_dir)
                        _clear_sink_phase(str(meta_dir))
                    continue
                clip_path = bundle["raw_clip"]
                clip_status = bundle.get("clip_status", "generated_unverified")
                evidence_state = _evidence_state_for_clip_status(clip_status)
                evidence_reason = _evidence_reason_for_bundle(
                    clip_status,
                    bundle if isinstance(bundle, dict) else None,
                )
                if finalizer_lease is None:
                    logger.error(
                        "media_terminal_transition_missing_lease "
                        "event_id=%s worker_id=%s",
                        event_id,
                        finalizer_worker_id,
                    )
                    continue
                terminal_transition_persisted = complete_finalizer_task(
                    pg_conn,
                    finalizer_lease,
                    materialization_status=evidence_state,
                    reason=evidence_reason,
                    clip_path=clip_path,
                    metadata_path=bundle.get("metadata") or metadata_file,
                    output_root=bundle.get("evidence_dir") or "",
                )
                if not terminal_transition_persisted:
                    logger.warning(
                        "media_terminal_transition_fence_lost "
                        "event_id=%s token=%s generation=%s",
                        event_id,
                        finalizer_lease.token,
                        finalizer_lease.generation,
                    )
                    continue
            else:
                clip_path = video_file
                clip_status = "ready"
        finally:
            if (
                post_savant_finalizer_enabled
                and materialization_guard is not None
                and acquired_materialization_slot
            ):
                materialization_guard.release()
        finalize_duration_ms = int((time.monotonic() - finalize_started) * 1000)
        _record_replay_slot_finalization_duration(
            pg_conn,
            event_id=event_id,
            finalization_duration_ms=finalize_duration_ms,
        )
        throttle_decision: dict | None = None
        if post_savant_finalizer_enabled and materialization_pacer is not None:
            deadline_at = (schedule_rows.get(event_id) or {}).get(
                "materialization_deadline_at"
            )
            throttle_decision = materialization_pacer.decision_after_finalize(
                deadline_at=deadline_at if isinstance(deadline_at, datetime) else None,
            )
        probe_delta = _probe_metrics_delta(probe_before)
        materialization_metrics = (
            bundle.get("materialization_metrics")
            if isinstance(bundle, dict)
            else {}
        )
        if not isinstance(materialization_metrics, dict):
            materialization_metrics = {}
        correlation = materialization_metrics.get("correlation")
        correlation = correlation if isinstance(correlation, dict) else {}
        logger.info(
            "media_event_finalized event_id=%s meta_dir=%s "
            "request_id=%s attempt_id=%s lease_token=%s replay_job_id=%s "
            "runtime_epoch_id=%s "
            "worker_id=%s source_id=%s replay_shard_id=%s "
            "claim_status=%s claim_wait_ms=%s "
            "finalization_duration_ms=%s scan_duration_ms=%s "
            "queue_wait_ms=%s lifecycle_elapsed_ms=%s "
            "post_savant_finalization_elapsed_ms=%s "
            "proof_wait_ms=%s replay_job_create_ms=%s "
            "replay_to_sink_metadata_ms=%s sink_metadata_to_video_ms=%s "
            "sink_video_to_stable_ms=%s "
            "sink_stable_to_ffprobe_ready_ms=%s "
            "sink_ffprobe_ready_to_finalizer_start_ms=%s "
            "finalizer_pool_wait_ms=%s "
            "throttle_sleep_s=%s throttle_reason=%s deadline_slack_s=%s "
            "metadata_files_visited=%s ffprobe_invocations=%s "
            "ffprobe_duration_ms=%s ffmpeg_invocations=%s ffmpeg_duration_ms=%s "
            "imageio_ffmpeg_fallback_count=%s imageio_ffmpeg_fallback_duration_ms=%s",
            event_id,
            meta_dir,
            correlation.get("request_id"),
            correlation.get("attempt_id"),
            correlation.get("lease_token"),
            correlation.get("replay_job_id"),
            correlation.get("runtime_epoch_id"),
            finalizer_worker_id,
            source_id,
            replay_shard_id,
            claim_result.get("status"),
            claim_wait_ms,
            finalize_duration_ms,
            scan_stats.get("scan_duration_ms"),
            materialization_metrics.get("queue_wait_ms"),
            materialization_metrics.get("lifecycle_elapsed_ms"),
            materialization_metrics.get("finalization_elapsed_ms"),
            materialization_metrics.get("proof_wait_ms"),
            materialization_metrics.get("replay_job_create_ms"),
            materialization_metrics.get("replay_to_sink_metadata_ms"),
            materialization_metrics.get("sink_metadata_to_video_ms"),
            materialization_metrics.get("sink_video_to_stable_ms"),
            materialization_metrics.get("sink_stable_to_ffprobe_ready_ms"),
            materialization_metrics.get("sink_ffprobe_ready_to_finalizer_start_ms"),
            materialization_metrics.get("finalizer_pool_wait_ms"),
            (throttle_decision or {}).get("sleep_s"),
            (throttle_decision or {}).get("reason"),
            (throttle_decision or {}).get("deadline_slack_s"),
            scan_stats.get("metadata_files_visited"),
            probe_delta["ffprobe_invocation_count"],
            probe_delta["ffprobe_duration_ms"],
            probe_delta["ffmpeg_invocation_count"],
            probe_delta["ffmpeg_duration_ms"],
            probe_delta["imageio_ffmpeg_fallback_count"],
            probe_delta["imageio_ffmpeg_fallback_duration_ms"],
        )
        replay_job_id = meta.get("job_id", "") or meta.get("new_job", "") or ""
        sink_path = meta_dir

        try:
            with pg_conn.cursor() as cur:
                if replay_job_id:
                    cur.execute(
                        """
                        UPDATE events
                        SET clip_path = %(clip_path)s,
                            payload = jsonb_set(
                                jsonb_set(
                                    jsonb_set(
                                        jsonb_set(
                                            COALESCE(payload, '{}'::jsonb),
                                            '{media,clip_status}',
                                            %(clip_status)s::jsonb
                                        ),
                                        '{media,recording_strategy}',
                                        '"savant_replay"'
                                    ),
                                    '{media,replay_job_id}',
                                    %(replay_job_id)s::jsonb
                                ),
                                '{media,sink_output_path}',
                                %(sink_path)s::jsonb
                            ),
                            updated_at = now()
                        WHERE id = %(event_id)s::uuid
                        """,
                        {
                            "clip_path": clip_path,
                            "clip_status": json.dumps(clip_status),
                            "clip_status_text": clip_status,
                            "replay_job_id": json.dumps(replay_job_id),
                            "sink_path": json.dumps(sink_path),
                            "event_id": event_id,
                        },
                    )
                else:
                    cur.execute(
                        """
                        UPDATE events
                        SET clip_path = %(clip_path)s,
                            payload = jsonb_set(
                                jsonb_set(
                                    jsonb_set(
                                        COALESCE(payload, '{}'::jsonb),
                                        '{media,clip_status}',
                                        %(clip_status)s::jsonb
                                    ),
                                    '{media,recording_strategy}',
                                    '"savant_replay"'
                                ),
                                '{media,sink_output_path}',
                                %(sink_path)s::jsonb
                            ),
                            updated_at = now()
                        WHERE id = %(event_id)s::uuid
                        """,
                        {
                            "clip_path": clip_path,
                            "clip_status": json.dumps(clip_status),
                            "clip_status_text": clip_status,
                            "sink_path": json.dumps(sink_path),
                            "event_id": event_id,
                        },
                    )
                if bundle:
                    cur.execute(
                        """
                        UPDATE events
                        SET payload = COALESCE(payload, '{}'::jsonb)
                                || jsonb_build_object(
                                    'media',
                                    COALESCE(payload->'media', '{}'::jsonb)
                                    || jsonb_build_object(
                                        'evidence_dir', %(evidence_dir)s::text,
                                        'metadata_path', %(metadata_path)s::text,
                                        'sink_metadata_path', %(sink_metadata_path)s::text,
                                        'annotations_jsonl_path', %(annotations_path)s::text,
                                        'summary_json_path', %(summary_path)s::text,
                                        'raw_clip_path', %(raw_clip_path)s::text,
                                        'materialization_metrics',
                                            %(materialization_metrics)s::jsonb,
                                        'evidence_diagnostics',
                                            COALESCE(
                                                payload->'media'->'evidence_diagnostics',
                                                '{}'::jsonb
                                            )
                                            || %(evidence_diagnostics)s::jsonb,
                                        'evidence_topology',
                                            %(evidence_topology)s::text,
                                        'annotation_source',
                                            %(annotation_source)s::text,
                                        'production_ready',
                                            %(production_ready)s::boolean,
                                        'person_count',
                                            %(person_count)s::int,
                                        'face_count',
                                            %(face_count)s::int,
                                        'known_face_count',
                                            %(known_face_count)s::int,
                                        'raw_clip_sanitize_method',
                                            %(raw_clip_sanitize_method)s::text,
                                        'raw_clip_sanitize_decode_ok',
                                            %(raw_clip_sanitize_decode_ok)s::boolean,
                                        'raw_clip_sanitize_decode_error_count',
                                            %(raw_clip_sanitize_decode_error_count)s::int,
                                        'raw_clip_sanitize_fallback_used',
                                            %(raw_clip_sanitize_fallback_used)s::boolean,
                                        'raw_clip_sanitize_error',
                                            %(raw_clip_sanitize_error)s::text,
                                        'annotated_clip_path', NULL,
                                        'annotated_clip_status', 'not_generated',
                                        'annotation_status', %(annotation_status)s::text,
                                        'annotation_lines', %(annotation_lines)s::int,
                                        'annotation_empty_reason',
                                            %(annotation_empty_reason)s::text,
                                        'annotation_unavailable_reason',
                                            %(annotation_unavailable_reason)s::text,
                                        'overlay_available',
                                            %(overlay_available)s::boolean,
                                        'frontend_overlay_required',
                                            %(frontend_overlay_required)s::boolean,
                                        'duration_guard_status',
                                            %(duration_guard_status)s::text,
                                        'duration_guard_failed',
                                            %(duration_guard_failed)s::boolean,
                                        'max_allowed_duration_seconds',
                                            %(max_allowed_duration_seconds)s::float,
                                        'sink_window_guard_status',
                                            %(sink_window_guard_status)s::text,
                                        'sink_window_guard_failed',
                                            %(sink_window_guard_failed)s::boolean,
                                        'sink_window_guard_reason',
                                            %(sink_window_guard_reason)s::text,
                                        'materialization_guard_attribution',
                                            %(materialization_guard_attribution)s::jsonb,
                                        'runtime_epoch_id',
                                            %(runtime_epoch_id)s::text,
                                        'epoch_guard_status',
                                            %(epoch_guard_status)s::text,
                                        'epoch_guard_failed',
                                            %(epoch_guard_failed)s::boolean,
                                        'epoch_guard_reason',
                                            %(epoch_guard_reason)s::text
                                    )
                                ),
                            updated_at = now()
                        WHERE id = %(event_id)s::uuid
                        """,
                        {
                            "event_id": event_id,
                            "evidence_dir": bundle["evidence_dir"],
                            "metadata_path": bundle["metadata"],
                            "sink_metadata_path": bundle["sink_metadata"],
                            "annotations_path": bundle["annotations_jsonl"],
                            "summary_path": bundle["summary"],
                            "raw_clip_path": bundle["raw_clip"],
                            "materialization_metrics": json.dumps(
                                bundle.get("materialization_metrics") or {}
                            ),
                            "evidence_diagnostics": json.dumps(
                                (
                                    bundle.get("materialization_metrics")
                                    if isinstance(bundle, dict)
                                    else {}
                                )
                                or {}
                            ),
                            "evidence_topology": bundle.get("evidence_topology", ""),
                            "annotation_source": bundle.get("annotation_source", ""),
                            "production_ready": bool(
                                bundle.get("production_ready", False)
                            ),
                            "person_count": int(bundle.get("person_count") or 0),
                            "face_count": int(bundle.get("face_count") or 0),
                            "known_face_count": int(
                                bundle.get("known_face_count") or 0
                            ),
                            "raw_clip_sanitize_method": bundle.get(
                                "raw_clip_sanitize_method"
                            ),
                            "raw_clip_sanitize_decode_ok": bundle.get(
                                "raw_clip_sanitize_decode_ok"
                            ),
                            "raw_clip_sanitize_decode_error_count": int(
                                bundle.get("raw_clip_sanitize_decode_error_count")
                                or 0
                            ),
                            "raw_clip_sanitize_fallback_used": bool(
                                bundle.get("raw_clip_sanitize_fallback_used")
                            ),
                            "raw_clip_sanitize_error": bundle.get(
                                "raw_clip_sanitize_error"
                            ),
                            "annotation_status": bundle.get("annotation_status"),
                            "annotation_lines": int(bundle.get("annotation_lines") or 0),
                            "annotation_empty_reason": bundle.get(
                                "annotation_empty_reason"
                            ),
                            "annotation_unavailable_reason": bundle.get(
                                "annotation_unavailable_reason"
                            ),
                            "overlay_available": bool(
                                bundle.get("overlay_available")
                            ),
                            "frontend_overlay_required": bool(
                                bundle.get("frontend_overlay_required")
                            ),
                            "duration_guard_status": bundle.get(
                                "duration_guard_status"
                            ),
                            "duration_guard_failed": bool(
                                bundle.get("duration_guard_failed")
                            ),
                            "max_allowed_duration_seconds": bundle.get(
                                "max_allowed_duration_seconds"
                            ),
                            "sink_window_guard_status": bundle.get(
                                "sink_window_guard_status"
                            ),
                            "sink_window_guard_failed": bool(
                                bundle.get("sink_window_guard_failed")
                            ),
                            "sink_window_guard_reason": bundle.get(
                                "sink_window_guard_reason", ""
                            ),
                            "materialization_guard_attribution": json.dumps(
                                bundle.get("materialization_guard_attribution") or {}
                            ),
                            "runtime_epoch_id": bundle.get("runtime_epoch_id", ""),
                            "epoch_guard_status": bundle.get("epoch_guard_status"),
                            "epoch_guard_failed": bool(
                                bundle.get("epoch_guard_failed")
                            ),
                            "epoch_guard_reason": bundle.get(
                                "epoch_guard_reason", ""
                            ),
                        },
                    )
                if cur.rowcount and cur.rowcount > 0:
                    evidence_state = _evidence_state_for_clip_status(clip_status)
                    evidence_reason = _evidence_reason_for_bundle(
                        clip_status,
                        bundle if isinstance(bundle, dict) else None,
                    )
                    lifecycle_fields = ""
                    lifecycle_predicate = ""
                    lifecycle_params: dict[str, object] = {}
                    if finalizer_lease and finalizer_lease.schema_v2:
                        lifecycle_fields = """
                            materialization_phase = 'terminal',
                            materialization_phase_updated_at = now(),
                            materialization_owner = 'terminal',
                            materialization_next_attempt_at = NULL,
                            materialization_retry_reason = NULL,
                            materialization_lease_owner = NULL,
                            materialization_lease_token = NULL,
                            materialization_lease_expires_at = NULL,
                            materialization_lease_heartbeat_at = NULL,
                        """
                        lifecycle_predicate = """
                          AND materialization_status = 'materializing'
                          AND materialization_lease_owner = %(lease_owner)s
                          AND materialization_lease_token = %(lease_token)s
                          AND materialization_lease_generation = %(lease_generation)s
                        """
                        lifecycle_params = {
                            "lease_owner": finalizer_lease.owner,
                            "lease_token": finalizer_lease.token,
                            "lease_generation": finalizer_lease.generation,
                        }
                    if terminal_transition_persisted:
                        # The fenced repository transition already won and
                        # cleared the lease. This legacy enrichment write may
                        # only replay the exact terminal state it committed.
                        lifecycle_fields = ""
                        lifecycle_predicate = """
                          AND materialization_status = %(evidence_state)s
                          AND materialization_phase = 'terminal'
                        """
                        lifecycle_params = {}
                    cur.execute(
                        f"""
                        UPDATE evidence_tasks
                        SET status = %(evidence_state)s,
                            materialization_status = %(evidence_state)s,
                            {lifecycle_fields}
                            clip_path = COALESCE(%(clip_path)s, clip_path),
                            metadata_path = COALESCE(%(metadata_path)s, metadata_path),
                            output_root = COALESCE(%(output_root)s, output_root),
                            last_materialization_at = CASE
                                WHEN %(evidence_state)s::text = 'materialized'
                                    THEN now()
                                ELSE last_materialization_at
                            END,
                            materialization_failure_reason = CASE
                                WHEN %(evidence_state)s::text = 'materialization_failed'
                                    THEN %(evidence_reason)s::text
                                ELSE NULL
                            END,
                            materialization_defer_reason = CASE
                                WHEN %(evidence_state)s::text = 'materialized'
                                    THEN NULL
                                ELSE NULL
                            END,
                            materialization_expired_reason = CASE
                                WHEN %(evidence_state)s::text = 'materialization_expired'
                                    THEN %(evidence_reason)s::text
                                ELSE NULL
                            END,
                            error_message = CASE
                                WHEN %(evidence_reason)s::text != ''
                                    THEN %(evidence_reason)s::text
                                WHEN %(evidence_state)s::text = 'materialized'
                                    THEN NULL
                                ELSE error_message
                            END,
                            updated_at = now()
                        WHERE event_id = %(event_id)s::uuid
                          AND COALESCE(materialization_failure_reason, '') <> %(superseded_reason)s
                          {lifecycle_predicate}
                        """,
                        {
                            "event_id": event_id,
                            "evidence_state": evidence_state,
                            "clip_path": clip_path,
                            "metadata_path": (
                                bundle.get("metadata") if bundle else metadata_file
                            ),
                            "output_root": bundle.get("evidence_dir") if bundle else None,
                            "evidence_reason": evidence_reason,
                            "superseded_reason": EPOCH_SUPERSEDED_INCOMPLETE_REASON,
                            **lifecycle_params,
                        },
                    )
                    if not cur.rowcount:
                        logger.warning(
                            "media_terminal_transition_fence_lost "
                            "event_id=%s token=%s generation=%s",
                            event_id,
                            finalizer_lease.token if finalizer_lease else None,
                            finalizer_lease.generation if finalizer_lease else None,
                        )
                        continue
                    cur.execute(
                        """
                        UPDATE events
                        SET media_status = %(evidence_state)s,
                            payload = COALESCE(payload, '{}'::jsonb)
                                || jsonb_build_object(
                                    'media',
                                    COALESCE(payload->'media', '{}'::jsonb)
                                    || jsonb_strip_nulls(jsonb_build_object(
                                        'evidence_state', %(evidence_state)s::text,
                                        'evidence_reason',
                                            NULLIF(%(evidence_reason)s::text, ''),
                                        'evidence_state_updated_at', now(),
                                        'materialization_status',
                                            %(evidence_state)s::text,
                                        'materialization_phase', 'terminal',
                                        'materialization_reason',
                                            NULLIF(%(evidence_reason)s::text, '')
                                    ))
                                ),
                            updated_at = now()
                        WHERE id = %(event_id)s::uuid
                          AND NOT EXISTS (
                              SELECT 1
                              FROM evidence_tasks et
                              WHERE et.event_id = %(event_id)s::uuid
                                AND COALESCE(et.materialization_failure_reason, '') = %(superseded_reason)s
                          )
                        """,
                        {
                            "event_id": event_id,
                            "evidence_state": evidence_state,
                            "evidence_reason": evidence_reason,
                            "superseded_reason": EPOCH_SUPERSEDED_INCOMPLETE_REASON,
                        },
                    )
                    if (
                        bundle
                        and bundle.get("evidence_dir")
                        and os.getenv("EVIDENCE_DB_INDEX_WRITE_ENABLED", "true").lower()
                        in {"1", "true", "yes", "on"}
                    ):
                        try:
                            db_index_started = time.monotonic()
                            expanded_rows_enabled = (
                                _evidence_db_index_expanded_rows_enabled()
                            )
                            index_result = upsert_evidence_bundle_index(
                                pg_conn,
                                event_id=event_id,
                                bundle_dir=bundle["evidence_dir"],
                                compute_sha256=False,
                                include_timeline=expanded_rows_enabled,
                                include_overlays=expanded_rows_enabled,
                            )
                            db_index_duration_ms = int(
                                (time.monotonic() - db_index_started) * 1000
                            )
                            logger.info(
                                "evidence_db_index_upserted event_id=%s "
                                "expanded_rows_enabled=%s duration_ms=%s result=%s",
                                event_id,
                                expanded_rows_enabled,
                                db_index_duration_ms,
                                index_result,
                            )
                            alias_count = _upsert_covered_event_aliases(
                                pg_conn,
                                bundle_event_id=event_id,
                            )
                            if alias_count:
                                logger.info(
                                    "evidence_covered_aliases_ready event_id=%s "
                                    "alias_count=%s",
                                    event_id,
                                    alias_count,
                                )
                            if (
                                expanded_rows_enabled
                                and evidence_state == "materialized"
                            ):
                                prune_started = time.monotonic()
                                prune_result = _prune_success_evidence_sidecars(
                                    bundle["evidence_dir"]
                                )
                                logger.info(
                                    "evidence_sidecars_pruned event_id=%s "
                                    "duration_ms=%s result=%s",
                                    event_id,
                                    int((time.monotonic() - prune_started) * 1000),
                                    prune_result,
                                )
                            else:
                                logger.info(
                                    "evidence_sidecars_retained event_id=%s "
                                    "reason=%s",
                                    event_id,
                                    "expanded_db_rows_disabled"
                                    if not expanded_rows_enabled
                                    else "non_materialized_diagnostics",
                                )
                            _set_event_db_index_status(
                                pg_conn,
                                event_id,
                                status="ready",
                            )
                        except Exception as exc:
                            logger.exception(
                                "evidence_db_index_upsert_failed event_id=%s",
                                event_id,
                            )
                            _set_event_db_index_status(
                                pg_conn,
                                event_id,
                                status="failed",
                                error=str(exc),
                            )
                    logger.info(
                        "media_event_updated event_id=%s clip_path=%s sink_path=%s",
                        event_id,
                        clip_path,
                        sink_path,
                    )
                    try:
                        cleanup_result = _cleanup_processed_sink_output(
                            meta_dir=meta_dir,
                            sink_root=sink_dir,
                            event_id=event_id,
                            clip_status=str(clip_status),
                            enabled=cleanup_replay_sink_output_enabled,
                            allowed_statuses=cleanup_replay_sink_output_statuses,
                        )
                    except OSError as exc:
                        logger.warning(
                            "replay_sink_output_cleanup_failed event_id=%s "
                            "meta_dir=%s error=%s",
                            event_id,
                            meta_dir,
                            exc,
                        )
                        cleanup_result = {
                            "status": "failed",
                            "deleted_bytes": 0,
                        }
                    if cleanup_result.get("status") == "deleted":
                        cur.execute(
                            """
                            UPDATE events
                            SET payload = COALESCE(payload, '{}'::jsonb)
                                    || jsonb_build_object(
                                        'media',
                                        COALESCE(payload->'media', '{}'::jsonb)
                                        || jsonb_build_object(
                                            'sink_output_cleanup_status',
                                                %(cleanup_status)s::text,
                                            'sink_output_cleanup_deleted_bytes',
                                                %(deleted_bytes)s::bigint,
                                            'sink_output_cleanup_at', now()
                                        )
                                    ),
                                updated_at = now()
                            WHERE id = %(event_id)s::uuid
                            """,
                            {
                                "event_id": event_id,
                                "cleanup_status": "deleted",
                                "deleted_bytes": int(
                                    cleanup_result.get("deleted_bytes") or 0
                                ),
                            },
                        )
                    updated += 1
                if meta_dir:
                    processed_dirs.add(meta_dir)
                    _clear_sink_phase(str(meta_dir))
                if (
                    post_savant_finalizer_enabled
                    and materialization_pacer is not None
                    and throttle_decision is not None
                ):
                    materialization_pacer.apply_decision(throttle_decision)
        except Exception:
            logger.exception("failed to update event_id=%s", event_id)

    if processed_state_path is not None and processed_dirs != processed_dirs_before:
        _save_processed_sink_state(processed_state_path, processed_dirs)
    return updated


@dataclass(frozen=True)
class _FinalizerJob:
    meta: dict
    meta_dir: str
    event_id: str
    source_id: str
    replay_shard_id: str
    worker_id: str
    phase_diagnostics: dict[str, object]


def _process_sink_output_with_finalizer_pool(
    pg_conn: psycopg.Connection,
    sink_dir: str,
    processed_dirs: set[str],
    *,
    metadata_files: list[dict],
    schedule_rows: dict[str, dict],
    scan_stats: dict,
    evidence_output_dir: str | None,
    candidate_dirs: dict[str, tuple[int, int]] | None,
    invalid_output_failures: dict[str, int] | None,
    midterm_sink_stability_checks: int,
    processed_state_path: str | Path | None,
    sink_scan_max_metadata_files: int | None,
    materialization_timeout_s: float,
    materialization_max_backlog: int,
    materialization_max_per_poll: int,
    materialization_throttle_sleep_s: float,
    materialization_throttle_deadline_guard_s: float,
    materialization_finalizer_workers: int,
    materialization_finalizer_max_per_source_per_poll: int = 1,
    materialization_finalizer_source_serial: bool = True,
    materialization_database_url: str,
    evidence_final_root_max_bytes: int,
    evidence_incoming_root_max_bytes: int,
    replay_sink_output_max_bytes: int,
    evidence_storage_warning_ratio: float,
    evidence_storage_critical_ratio: float,
    evidence_storage_hard_ratio: float,
    cleanup_replay_sink_output_enabled: bool,
    cleanup_replay_sink_output_statuses: tuple[str, ...],
) -> int:
    processed_dirs_before = set(processed_dirs)
    workers = max(1, int(materialization_finalizer_workers or 1))
    max_jobs = max(0, int(materialization_max_per_poll or 0))
    jobs: list[_FinalizerJob] = []
    skipped_same_source = 0
    scheduled_source_counts: dict[str, int] = {}
    scheduled_shards: set[str] = set()

    for meta in metadata_files:
        meta_dir = str(meta.get("_meta_dir", ""))
        if meta_dir and meta_dir in processed_dirs:
            continue
        if meta_dir and _invalid_sink_output_marker_path(meta_dir).exists():
            processed_dirs.add(meta_dir)
            logger.debug("media_skip: sink output marked invalid meta_dir=%s", meta_dir)
            continue

        event_id = _extract_event_id(meta)
        if not event_id:
            logger.error(
                "media_failure: no event_id could be extracted from metadata "
                "meta_dir=%s source_id=%s resulting_stream_id=%s",
                meta_dir,
                meta.get("source_id", ""),
                meta.get("resulting_stream_id", ""),
            )
            continue
        finalizer_phase_override = meta.get("_finalizer_phase")
        finalizer_phase = (
            dict(finalizer_phase_override)
            if isinstance(finalizer_phase_override, dict)
            else None
        )
        phase_diagnostics = (
            dict(finalizer_phase)
            if finalizer_phase is not None
            else _mark_sink_phase(
                meta_dir,
                event_id,
                "sink_metadata_first_seen",
            )
        )

        if _is_already_ready(pg_conn, event_id):
            if meta_dir:
                processed_dirs.add(meta_dir)
            logger.debug("media_skip: event already ready event_id=%s", event_id)
            continue

        video_file = _find_video_file(meta_dir)
        if not video_file:
            continue
        if finalizer_phase is None:
            phase_diagnostics = _mark_sink_phase(
                meta_dir,
                event_id,
                "sink_video_first_seen",
            )

        metadata_file = str(Path(meta_dir) / "metadata.json")
        stable_count = 0
        ready, reason = _sink_output_ready_for_finalizer(
            video_file=video_file,
            metadata_file=metadata_file,
            known_duration_s=_known_sink_output_duration_seconds(meta),
        )
        if ready:
            if finalizer_phase is None:
                phase_diagnostics = _mark_sink_phase(
                    meta_dir,
                    event_id,
                    "sink_video_stable",
                )
                _release_replay_slot_for_sink_stable(
                    pg_conn,
                    event_id=event_id,
                    phase_diagnostics=phase_diagnostics,
                )
        elif candidate_dirs is not None:
            try:
                current_size = Path(video_file).stat().st_size
            except OSError:
                continue
            previous_size, stable_count = candidate_dirs.get(meta_dir, (-1, 0))
            stable_count = stable_count + 1 if current_size == previous_size else 0
            candidate_dirs[meta_dir] = (current_size, stable_count)
            if stable_count < max(1, midterm_sink_stability_checks):
                logger.debug(
                    "media_wait_for_stable_sink_output event_id=%s meta_dir=%s "
                    "size=%s previous_size=%s stable_count=%s required=%s",
                    event_id,
                    meta_dir,
                    current_size,
                    previous_size,
                    stable_count,
                    midterm_sink_stability_checks,
                )
                continue
            if finalizer_phase is None:
                phase_diagnostics = _mark_sink_phase(
                    meta_dir,
                    event_id,
                    "sink_video_stable",
                )
                _release_replay_slot_for_sink_stable(
                    pg_conn,
                    event_id=event_id,
                    phase_diagnostics=phase_diagnostics,
                )
            ready, reason = _sink_output_ready_for_finalizer(
                video_file=video_file,
                metadata_file=metadata_file,
                known_duration_s=_known_sink_output_duration_seconds(meta),
            )
        else:
            if finalizer_phase is None:
                phase_diagnostics = _mark_sink_phase(
                    meta_dir,
                    event_id,
                    "sink_video_stable",
                )
                _release_replay_slot_for_sink_stable(
                    pg_conn,
                    event_id=event_id,
                    phase_diagnostics=phase_diagnostics,
                )

        if not ready:
            if (
                reason in PERMANENT_INVALID_SINK_OUTPUT_REASONS
                and invalid_output_failures is not None
                and stable_count >= max(1, midterm_sink_stability_checks)
            ):
                invalid_output_failures[meta_dir] = invalid_output_failures.get(meta_dir, 0) + 1
            logger.info(
                "media_wait_for_finalized_sink_output event_id=%s meta_dir=%s "
                "video_file=%s reason=%s",
                event_id,
                meta_dir,
                video_file,
                reason,
            )
            continue
        if finalizer_phase is None:
            phase_diagnostics = _mark_sink_phase(
                meta_dir,
                event_id,
                "sink_ffprobe_ready",
            )

        schedule_row = schedule_rows.get(event_id, {}) if schedule_rows else {}
        source_id = _metadata_source_id(meta, schedule_row)
        replay_shard_id = _metadata_replay_shard_id(meta, schedule_row)
        source_scheduled = scheduled_source_counts.get(source_id, 0)
        if (
            materialization_finalizer_max_per_source_per_poll > 0
            and source_scheduled >= materialization_finalizer_max_per_source_per_poll
        ):
            skipped_same_source += 1
            continue
        if max_jobs > 0 and len(jobs) >= max_jobs:
            deadline_at = schedule_row.get("materialization_deadline_at")
            if not isinstance(deadline_at, datetime):
                break
            decision = _materialization_throttle_decision(
                throttle_sleep_s=materialization_throttle_sleep_s,
                deadline_guard_s=materialization_throttle_deadline_guard_s,
                deadline_at=deadline_at,
            )
            if decision.get("reason") != "deadline_guard":
                break

        worker_id = f"finalizer-{(len(jobs) % workers) + 1}"
        submitted_at = datetime.now(timezone.utc).isoformat()
        submitted_monotonic = time.monotonic()
        phase_diagnostics = {
            **phase_diagnostics,
            "finalizer_submitted_at": submitted_at,
            "finalizer_submitted_monotonic": submitted_monotonic,
        }
        jobs.append(
            _FinalizerJob(
                meta=meta,
                meta_dir=meta_dir,
                event_id=event_id,
                source_id=source_id,
                replay_shard_id=replay_shard_id,
                worker_id=worker_id,
                phase_diagnostics=phase_diagnostics,
            )
        )
        scheduled_source_counts[source_id] = source_scheduled + 1
        scheduled_shards.add(replay_shard_id)

    if not jobs:
        if processed_state_path is not None and processed_dirs != processed_dirs_before:
            _save_processed_sink_state(processed_state_path, processed_dirs)
        if skipped_same_source:
            logger.info(
                "media_finalizer_pool_no_jobs skipped_same_source=%s workers=%s",
                skipped_same_source,
                workers,
            )
        return 0

    logger.info(
        "media_finalizer_pool_started workers=%s jobs=%s source_count=%s "
        "shard_count=%s skipped_same_source=%s max_per_source_per_poll=%s "
        "source_serial=%s",
        workers,
        len(jobs),
        len(scheduled_source_counts),
        len(scheduled_shards),
        skipped_same_source,
        materialization_finalizer_max_per_source_per_poll,
        materialization_finalizer_source_serial,
    )
    updated = 0
    source_locks: dict[str, Lock] = {
        source_id: Lock() for source_id in scheduled_source_counts
    }
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="media-finalizer") as executor:
        futures = {
            executor.submit(
                _process_single_finalizer_job,
                job,
                sink_dir=sink_dir,
                database_url=materialization_database_url,
                scan_stats=scan_stats,
                source_lock=(
                    source_locks[job.source_id]
                    if materialization_finalizer_source_serial
                    else Lock()
                ),
                evidence_output_dir=evidence_output_dir,
                sink_scan_max_metadata_files=sink_scan_max_metadata_files,
                materialization_timeout_s=materialization_timeout_s,
                materialization_max_backlog=materialization_max_backlog,
                materialization_throttle_sleep_s=materialization_throttle_sleep_s,
                materialization_throttle_deadline_guard_s=materialization_throttle_deadline_guard_s,
                evidence_final_root_max_bytes=evidence_final_root_max_bytes,
                evidence_incoming_root_max_bytes=evidence_incoming_root_max_bytes,
                replay_sink_output_max_bytes=replay_sink_output_max_bytes,
                evidence_storage_warning_ratio=evidence_storage_warning_ratio,
                evidence_storage_critical_ratio=evidence_storage_critical_ratio,
                evidence_storage_hard_ratio=evidence_storage_hard_ratio,
                cleanup_replay_sink_output_enabled=cleanup_replay_sink_output_enabled,
                cleanup_replay_sink_output_statuses=cleanup_replay_sink_output_statuses,
            ): job
            for job in jobs
        }
        for future in as_completed(futures):
            job = futures[future]
            try:
                raw_result = future.result() or 0
            except Exception:
                logger.exception(
                    "media_finalizer_pool_job_failed event_id=%s meta_dir=%s "
                    "worker_id=%s source_id=%s replay_shard_id=%s",
                    job.event_id,
                    job.meta_dir,
                    job.worker_id,
                    job.source_id,
                    job.replay_shard_id,
                )
                continue
            if isinstance(raw_result, dict):
                result = int(raw_result.get("updated") or 0)
                processed = bool(raw_result.get("processed"))
            else:
                result = int(raw_result)
                processed = result > 0
            if result > 0:
                updated += result
            if processed:
                if job.meta_dir:
                    processed_dirs.add(job.meta_dir)
                    _clear_sink_phase(job.meta_dir)

    logger.info(
        "media_finalizer_pool_finished workers=%s jobs=%s updated=%s",
        workers,
        len(jobs),
        updated,
    )
    if processed_state_path is not None and processed_dirs != processed_dirs_before:
        _save_processed_sink_state(processed_state_path, processed_dirs)
    return updated


def _process_single_finalizer_job(
    job: _FinalizerJob,
    *,
    sink_dir: str,
    database_url: str,
    scan_stats: dict,
    source_lock: Lock,
    evidence_output_dir: str | None,
    sink_scan_max_metadata_files: int | None,
    materialization_timeout_s: float,
    materialization_max_backlog: int,
    materialization_throttle_sleep_s: float,
    materialization_throttle_deadline_guard_s: float,
    evidence_final_root_max_bytes: int,
    evidence_incoming_root_max_bytes: int,
    replay_sink_output_max_bytes: int,
    evidence_storage_warning_ratio: float,
    evidence_storage_critical_ratio: float,
    evidence_storage_hard_ratio: float,
    cleanup_replay_sink_output_enabled: bool,
    cleanup_replay_sink_output_statuses: tuple[str, ...],
) -> dict[str, object]:
    finalizer_started_at = datetime.now(timezone.utc).isoformat()
    finalizer_started_monotonic = time.monotonic()
    phase_diagnostics = {
        **(job.phase_diagnostics or {}),
        "finalizer_started_at": finalizer_started_at,
        "finalizer_started_monotonic": finalizer_started_monotonic,
    }
    with source_lock:
        conn = psycopg.connect(database_url, autocommit=True)
        local_processed_dirs: set[str] = set()
        try:
            updated = _process_sink_output(
                conn,
                sink_dir,
                local_processed_dirs,
                evidence_output_dir=evidence_output_dir,
                candidate_dirs=None,
                invalid_output_failures=None,
                midterm_sink_stability_checks=1,
                processed_state_path=None,
                sink_scan_max_metadata_files=sink_scan_max_metadata_files,
                materialization_guard=_MaterializationGuard(1),
                materialization_pacer=_MaterializationPacer(
                    max_per_poll=0,
                    throttle_sleep_s=materialization_throttle_sleep_s,
                    deadline_guard_s=materialization_throttle_deadline_guard_s,
                ),
                materialization_timeout_s=materialization_timeout_s,
                materialization_max_backlog=materialization_max_backlog,
                evidence_final_root_max_bytes=evidence_final_root_max_bytes,
                evidence_incoming_root_max_bytes=evidence_incoming_root_max_bytes,
                replay_sink_output_max_bytes=replay_sink_output_max_bytes,
                evidence_storage_warning_ratio=evidence_storage_warning_ratio,
                evidence_storage_critical_ratio=evidence_storage_critical_ratio,
                evidence_storage_hard_ratio=evidence_storage_hard_ratio,
                cleanup_replay_sink_output_enabled=cleanup_replay_sink_output_enabled,
                cleanup_replay_sink_output_statuses=cleanup_replay_sink_output_statuses,
                materialization_finalizer_workers=1,
                materialization_finalizer_max_per_source_per_poll=1,
                materialization_finalizer_source_serial=True,
                materialization_database_url=None,
                finalizer_worker_id=job.worker_id,
                metadata_files_override=[job.meta],
                scan_stats_override=scan_stats,
                finalizer_phase=phase_diagnostics,
            )
            return {
                "updated": int(updated or 0),
                "processed": bool(job.meta_dir and job.meta_dir in local_processed_dirs),
            }
        finally:
            conn.close()


def _mark_media_finalize_failed(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    sink_path: str,
    error_message: str,
    lease: MaterializationLease | None = None,
) -> bool:
    try:
        lease = lease or current_lease(
            pg_conn,
            event_id=event_id,
            fallback_owner="media-finalizer",
            fallback_phase=MaterializationPhase.FINALIZING.value,
        )
        if lease is not None:
            changed = fail_rolling_task(
                pg_conn,
                lease,
                reason=error_message,
            )
        else:
            changed = fail_unclaimed_task(
                pg_conn,
                event_id=event_id,
                reason=error_message,
                sink_output_path=sink_path,
            )
        if not changed:
            return False
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET media_status = 'failed',
                    payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_build_object(
                                'clip_status', %(clip_status_text)s::text,
                                'sink_output_path', %(sink_path)s::text,
                                'finalizer_error', %(error_message)s::text,
                                'evidence_state', 'failed',
                                'evidence_reason', %(error_message)s::text,
                                'evidence_state_updated_at', now()
                            )
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {
                    "event_id": event_id,
                    "clip_status_text": BUNDLE_STATUS_GENERATED_ANNOTATION_FAILED,
                    "sink_path": sink_path,
                    "error_message": error_message,
                },
            )
        return True
    except Exception:
        logger.exception("failed to mark media finalizer failure event_id=%s", event_id)
        return False


ROLLING_CACHE_TASK_STATUSES = tuple(sorted(CLAIMABLE_MATERIALIZATION_STATUSES))


def _process_rolling_cache_tasks(
    pg_conn: psycopg.Connection,
    cfg: Config,
    runner: "_RollingCacheMaterializationRunner | None" = None,
) -> int:
    if not (cfg.rolling_cache_enabled and cfg.rolling_cache_materialization_enabled):
        return 0

    updated = _expire_overdue_rolling_cache_tasks(pg_conn, cfg)
    updated += _process_rolling_cache_image_tasks(pg_conn, cfg)
    if runner is not None:
        return updated + runner.process(pg_conn, cfg)

    rows = _rolling_cache_candidate_tasks(pg_conn, cfg)
    metadata_overrides: list[dict] = []
    finalizer_chunk_size = max(1, int(cfg.materialization_finalizer_workers or 1) * 2)
    jobs: list[dict[str, object]] = []
    segment_cache: dict[tuple[str, str], list[RollingSegment]] = {}
    for row in rows:
        event_id = str(row.get("event_id") or "")
        source_id = str(row.get("source_id") or row.get("replay_source_id") or "")
        if not event_id or not source_id:
            continue
        if cfg.rolling_cache_sources and source_id not in cfg.rolling_cache_sources:
            continue
        if _is_already_ready(pg_conn, event_id):
            continue
        lease = _claim_rolling_cache_task(
            pg_conn,
            event_id=event_id,
            ready_at=row.get("rolling_cache_ready_at"),
            processing_deadline_s=(
                cfg.rolling_cache_materialization_processing_deadline_seconds
            ),
            phase=MaterializationPhase.REMUX_RUNNING.value,
        )
        if lease is None:
            continue
        try:
            event_context = _load_event_context(pg_conn, event_id)
            window = _rolling_cache_window(row, event_context)
            if window is None:
                _defer_rolling_cache_task(
                    pg_conn,
                    event_id=event_id,
                    reason="rolling_cache_missing_event_frame_pts",
                    lease=lease,
                )
                continue
            requested_start_pts, requested_end_pts, event_frame_pts = window
            runtime_epoch_id = (
                _runtime_epoch_from_event_context(event_context)
                or _current_runtime_epoch_id(cfg.sink_output_dir)
            )
            labels = _rolling_cache_labels(
                row=row,
                event_context=event_context,
                runtime_epoch_id=runtime_epoch_id,
                requested_start_pts=requested_start_pts,
                requested_end_pts=requested_end_pts,
                event_frame_pts=event_frame_pts,
            )
            cache_key = (source_id, runtime_epoch_id)
            segments = segment_cache.get(cache_key)
            if segments is None:
                segments = find_segments(
                    cfg.rolling_cache_root,
                    source_id=source_id,
                    runtime_epoch_id=runtime_epoch_id,
                )
                segment_cache[cache_key] = segments
            jobs.append(
                {
                    "event_id": event_id,
                    "source_id": source_id,
                    "requested_start_pts": requested_start_pts,
                    "requested_end_pts": requested_end_pts,
                    "runtime_epoch_id": runtime_epoch_id,
                    "labels": labels,
                    "segments": segments,
                    "lease": lease,
                }
            )
        except RollingCacheCoverageMiss as exc:
            _defer_rolling_cache_task(
                pg_conn,
                event_id=event_id,
                reason=str(exc) or "rolling_cache_coverage_miss",
                retry_after_s=_rolling_cache_coverage_retry_after_s(exc),
                lease=lease,
            )
        except Exception as exc:
            logger.exception("rolling_cache_materialization_failed event_id=%s", event_id)
            _fail_rolling_cache_task(
                pg_conn,
                event_id=event_id,
                reason=_rolling_cache_failure_reason(exc),
                lease=lease,
            )
    workers = max(1, int(getattr(cfg, "rolling_cache_materialization_workers", 1)))
    if workers <= 1 or len(jobs) <= 1:
        for job in jobs:
            event_id = str(job.get("event_id") or "")
            lease = (
                job.get("lease")
                if isinstance(job.get("lease"), MaterializationLease)
                else None
            )
            try:
                metadata = _materialize_rolling_cache_job(
                    root=cfg.rolling_cache_root,
                    output_root=cfg.rolling_cache_materialized_root,
                    job=job,
                )
                if _persist_rolling_cache_handoff_metadata(pg_conn, cfg, metadata):
                    metadata_overrides.append(metadata)
            except RollingCacheCoverageMiss as exc:
                _defer_rolling_cache_task(
                    pg_conn,
                    event_id=event_id,
                    reason=str(exc) or "rolling_cache_coverage_miss",
                    retry_after_s=_rolling_cache_coverage_retry_after_s(exc),
                    lease=lease,
                )
            except Exception as exc:
                logger.exception(
                    "rolling_cache_materialization_failed event_id=%s",
                    event_id,
                )
                _fail_rolling_cache_task(
                    pg_conn,
                    event_id=event_id,
                    reason=_rolling_cache_failure_reason(exc),
                    lease=lease,
                )
            if len(metadata_overrides) >= finalizer_chunk_size:
                updated += _flush_rolling_cache_finalizer_batch_or_defer(
                    pg_conn,
                    cfg,
                    metadata_overrides,
                )
                metadata_overrides.clear()
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as executor:
            futures = {
                executor.submit(
                    _materialize_rolling_cache_job,
                    root=cfg.rolling_cache_root,
                    output_root=cfg.rolling_cache_materialized_root,
                    job=job,
                ): (
                    str(job.get("event_id") or ""),
                    job.get("lease")
                    if isinstance(job.get("lease"), MaterializationLease)
                    else None,
                )
                for job in jobs
            }
            for future in as_completed(futures):
                event_id, lease = futures[future]
                try:
                    metadata = future.result()
                    if _persist_rolling_cache_handoff_metadata(
                        pg_conn,
                        cfg,
                        metadata,
                    ):
                        metadata_overrides.append(metadata)
                except RollingCacheCoverageMiss as exc:
                    _defer_rolling_cache_task(
                        pg_conn,
                        event_id=event_id,
                        reason=str(exc) or "rolling_cache_coverage_miss",
                        retry_after_s=_rolling_cache_coverage_retry_after_s(exc),
                        lease=lease,
                    )
                except Exception as exc:
                    logger.exception(
                        "rolling_cache_materialization_failed event_id=%s",
                        event_id,
                    )
                    _fail_rolling_cache_task(
                        pg_conn,
                        event_id=event_id,
                        reason=_rolling_cache_failure_reason(exc),
                        lease=lease,
                    )
                if len(metadata_overrides) >= finalizer_chunk_size:
                    updated += _flush_rolling_cache_finalizer_batch_or_defer(
                        pg_conn,
                        cfg,
                        metadata_overrides,
                    )
                    metadata_overrides.clear()
    if not metadata_overrides:
        return updated

    updated += _flush_rolling_cache_finalizer_batch_or_defer(
        pg_conn,
        cfg,
        metadata_overrides,
    )
    metadata_overrides.clear()
    return updated


def _process_rolling_cache_image_tasks(
    pg_conn: psycopg.Connection,
    cfg: Config,
) -> int:
    rows = _rolling_cache_image_candidate_tasks(pg_conn, cfg)
    updated = 0
    segment_cache: dict[tuple[str, str], list[RollingSegment]] = {}
    for row in rows:
        event_id = str(row.get("event_id") or "")
        source_id = str(row.get("source_id") or row.get("replay_source_id") or "")
        if not event_id or not source_id:
            continue
        if cfg.rolling_cache_sources and source_id not in cfg.rolling_cache_sources:
            continue
        lease = _claim_rolling_cache_task(
            pg_conn,
            event_id=event_id,
            ready_at=row.get("rolling_cache_ready_at"),
            processing_deadline_s=(
                cfg.rolling_cache_materialization_processing_deadline_seconds
            ),
            phase=MaterializationPhase.IMAGE_RUNNING.value,
        )
        if lease is None:
            continue
        try:
            event_context = _load_event_context(pg_conn, event_id)
            runtime_epoch_id = (
                _runtime_epoch_from_event_context(event_context)
                or _current_runtime_epoch_id(cfg.sink_output_dir)
            )
            cache_key = (source_id, runtime_epoch_id)
            segments = segment_cache.get(cache_key)
            if segments is None:
                segments = find_segments(
                    cfg.rolling_cache_root,
                    source_id=source_id,
                    runtime_epoch_id=runtime_epoch_id,
                )
                segment_cache[cache_key] = segments
            result = _materialize_face_image_from_rolling_cache(
                cfg=cfg,
                row=row,
                event_context=event_context,
                segments=segments,
                runtime_epoch_id=runtime_epoch_id,
            )
            if _mark_image_evidence_materialized(
                pg_conn,
                event_id=event_id,
                event_context=event_context,
                result=result,
                lease=lease,
            ):
                updated += 1
        except RollingCacheCoverageMiss as exc:
            _mark_image_evidence_failed(
                pg_conn,
                event_id=event_id,
                reason=str(exc) or "face_image_frame_not_found",
                lease=lease,
            )
        except Exception as exc:
            logger.exception("rolling_cache_image_materialization_failed event_id=%s", event_id)
            _mark_image_evidence_failed(
                pg_conn,
                event_id=event_id,
                reason=_image_materialization_failure_reason(exc),
                lease=lease,
            )
    return updated


def _rolling_cache_image_candidate_tasks(
    pg_conn: psycopg.Connection,
    cfg: Config,
    *,
    limit: int | None = None,
) -> list[dict[str, object]]:
    with pg_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH candidates AS (
                SELECT
                    et.event_id, et.source_id, et.replay_source_id,
                    et.camera_id, et.event_type, et.event_ts_ms,
                    et.pre_seconds, et.post_seconds, et.replay_window,
                    et.priority, e.payload, e.frame_uuid, e.created_at,
                    et.materialization_ready_at AS rolling_cache_ready_at
                FROM evidence_tasks et
                JOIN events e ON e.id = et.event_id
                WHERE et.materialization_status = ANY(%(statuses)s)
                  AND COALESCE(et.task_type, '') = 'image_only'
                  AND COALESCE(et.clip_required, false) = false
                  AND (
                      %(sources_empty)s
                      OR COALESCE(et.source_id, et.replay_source_id, '') = ANY(%(sources)s)
                  )
                  AND et.materialization_ready_at IS NOT NULL
                  AND et.materialization_ready_at <= now()
                  AND COALESCE(
                        NULLIF(
                            to_jsonb(et)->>'materialization_next_attempt_at',
                            ''
                        )::timestamptz,
                        et.materialization_ready_at
                      ) <= now()
                  AND COALESCE(
                        NULLIF(to_jsonb(et)->>'materialization_owner', ''),
                        'rolling'
                      ) = 'rolling'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM evidence_artifacts ea
                      WHERE ea.event_id = et.event_id
                        AND ea.artifact_type IN ('face_crop', 'full_frame', 'annotated_frame')
                        AND COALESCE(ea.uri, '') <> ''
                  )
            )
            SELECT
                *,
                GREATEST(
                    0,
                    floor(extract(epoch FROM (now() - rolling_cache_ready_at)) * 1000)
                )::bigint AS rolling_cache_ready_lag_ms
            FROM candidates
            WHERE rolling_cache_ready_at <= now()
            ORDER BY priority DESC, rolling_cache_ready_at ASC, created_at ASC
            LIMIT %(limit)s
            """,
            {
                "statuses": list(ROLLING_CACHE_TASK_STATUSES),
                "sources": list(cfg.rolling_cache_sources),
                "sources_empty": not bool(cfg.rolling_cache_sources),
                "limit": (
                    max(1, int(limit))
                    if limit is not None
                    else cfg.rolling_cache_materialization_max_per_poll
                ),
            },
        )
        return [dict(row) for row in cur.fetchall()]


def _materialize_face_image_from_rolling_cache(
    *,
    cfg: Config,
    row: dict[str, object],
    event_context: dict,
    segments: list[RollingSegment],
    runtime_epoch_id: str,
) -> dict[str, object]:
    event_id = str(event_context.get("event_id") or row.get("event_id") or "")
    if not event_id:
        raise ValueError("face_image_missing_event_id")
    frame = _rolling_cache_frame_for_image_event(event_context, segments)
    segment = frame["segment"]
    frame_pts = int(frame["pts"])
    output_dir = Path(cfg.evidence_output_dir) / event_id
    output_dir.mkdir(parents=True, exist_ok=True)
    full_frame_path = output_dir / "full_frame.jpg"
    face_crop_path = output_dir / "face_crop.jpg"
    annotated_frame_path = output_dir / "annotated_frame.jpg"
    offset_s = max(0.0, (frame_pts - int(segment.first_pts)) / 1_000_000_000)
    _extract_full_frame_image(
        segment.video_path,
        full_frame_path,
        offset_s=offset_s,
    )
    bbox = _face_bbox_from_event(event_context)
    crop_status = "skipped_missing_bbox"
    annotated_status = "skipped_missing_bbox"
    crop_box = None
    annotation_box = None
    if bbox:
        annotation_box = _normalize_face_bbox(
            bbox,
            image_path=full_frame_path,
            padding_ratio=0.0,
        )
        crop_box = _normalize_face_bbox(
            bbox,
            image_path=full_frame_path,
            padding_ratio=0.20,
        )
        if crop_box is not None:
            crop_status = _write_face_crop(full_frame_path, face_crop_path, crop_box)
        if annotation_box is not None:
            annotated_status = _write_annotated_face_frame(
                full_frame_path,
                annotated_frame_path,
                annotation_box,
                event_context=event_context,
            )
    return {
        "event_id": event_id,
        "runtime_epoch_id": runtime_epoch_id,
        "source_observation_id": _source_observation_id_from_event_context(event_context),
        "source_id": event_context.get("source_id") or row.get("source_id") or "",
        "camera_id": event_context.get("camera_id") or row.get("camera_id") or "",
        "event_type": event_context.get("event_type") or row.get("event_type") or "",
        "frame_pts": frame_pts,
        "frame_uuid": frame.get("frame_uuid") or event_context.get("frame_uuid") or "",
        "segment_id": segment.segment_id,
        "segment_path": str(segment.video_path),
        "full_frame_path": str(full_frame_path),
        "face_crop_path": str(face_crop_path) if face_crop_path.is_file() else "",
        "annotated_frame_path": (
            str(annotated_frame_path) if annotated_frame_path.is_file() else ""
        ),
        "face_bbox": bbox,
        "crop_box": crop_box,
        "annotation_box": annotation_box,
        "crop_status": crop_status,
        "annotated_status": annotated_status,
        "materialization_mode": "rolling_cache_image_extract",
    }


def _rolling_cache_frame_for_image_event(
    event_context: dict,
    segments: list[RollingSegment],
) -> dict[str, object]:
    if not segments:
        raise RollingCacheCoverageMiss("face_image_no_rolling_cache_segments")
    payload = event_context.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    media = payload.get("media")
    media = media if isinstance(media, dict) else {}
    frame_uuid = str(
        event_context.get("frame_uuid")
        or media.get("frame_uuid")
        or ""
    )
    frame_pts = _to_int(media.get("frame_pts") or event_context.get("frame_pts"))
    if frame_uuid:
        for segment in segments:
            for record in _safe_load_native_metadata(segment.metadata_path):
                record_uuid = str(record.get("uuid") or record.get("frame_uuid") or "")
                if record_uuid == frame_uuid:
                    pts = _to_int(record.get("pts") or record.get("frame_pts"))
                    if pts is not None:
                        return {
                            "segment": segment,
                            "pts": pts,
                            "frame_uuid": frame_uuid,
                        }
    if frame_pts is None:
        raise RollingCacheCoverageMiss("face_image_missing_frame_pts")
    for segment in segments:
        if segment.first_pts <= frame_pts <= segment.last_pts:
            return {
                "segment": segment,
                "pts": frame_pts,
                "frame_uuid": frame_uuid,
            }
    nearest: dict[str, object] | None = None
    nearest_delta: int | None = None
    for segment in segments:
        for record in _safe_load_native_metadata(segment.metadata_path):
            pts = _to_int(record.get("pts") or record.get("frame_pts"))
            if pts is None:
                continue
            delta = abs(pts - frame_pts)
            if (
                delta <= IMAGE_EVIDENCE_NEAREST_FRAME_TOLERANCE_NS
                and (nearest_delta is None or delta < nearest_delta)
            ):
                nearest_delta = delta
                nearest = {
                    "segment": segment,
                    "pts": pts,
                    "frame_uuid": str(
                        record.get("uuid")
                        or record.get("frame_uuid")
                        or frame_uuid
                    ),
                    "frame_pts_delta_ns": delta,
                    "frame_selection": "nearest_metadata",
                }
    if nearest is not None:
        return nearest
    raise RollingCacheCoverageMiss("face_image_frame_not_found")


def _safe_load_native_metadata(path: Path) -> list[dict]:
    try:
        return load_native_metadata(path)
    except Exception:
        logger.exception("failed to load rolling-cache metadata path=%s", path)
        return []


def _extract_full_frame_image(
    video_path: Path,
    output_path: Path,
    *,
    offset_s: float,
) -> None:
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-i",
        str(video_path),
        "-ss",
        f"{max(0.0, offset_s):.6f}",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(output_path),
    ]
    started = time.monotonic()
    proc = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=_media_probe_timeout_s(),
        check=False,
    )
    _record_probe_metric("ffmpeg", time.monotonic() - started)
    if proc.returncode != 0:
        raise RuntimeError(
            "face_image_ffmpeg_failed:"
            + " ".join((proc.stderr or proc.stdout or "").split())[-500:]
        )
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("face_image_full_frame_missing")


def _face_bbox_from_event(event_context: dict) -> dict | None:
    payload = event_context.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    overlay = payload.get("overlay")
    overlay = overlay if isinstance(overlay, dict) else {}
    observation = payload.get("observation")
    observation = observation if isinstance(observation, dict) else {}
    for value in (
        overlay.get("face_bbox"),
        observation.get("face_bbox"),
        payload.get("face_bbox"),
    ):
        if isinstance(value, dict):
            return value
    return None


def _normalize_face_bbox(
    bbox: dict,
    *,
    image_path: Path,
    padding_ratio: float = 0.0,
) -> tuple[int, int, int, int] | None:
    from PIL import Image

    with Image.open(image_path) as image:
        width, height = image.size
    values = bbox.get("values")
    fmt = str(bbox.get("format") or "").lower()
    if isinstance(values, (list, tuple)) and len(values) >= 4:
        a, b, c, d = [float(v) for v in values[:4]]
        if fmt == "cxcywh":
            x1 = a - c / 2
            y1 = b - d / 2
            x2 = a + c / 2
            y2 = b + d / 2
        elif fmt in {"xyxy", "x1y1x2y2"}:
            x1, y1, x2, y2 = a, b, c, d
        else:
            x1, y1, x2, y2 = a, b, a + c, b + d
    else:
        x = _to_float(bbox.get("x"))
        y = _to_float(bbox.get("y"))
        w = _to_float(bbox.get("width") or bbox.get("w"))
        h = _to_float(bbox.get("height") or bbox.get("h"))
        if x is None or y is None or w is None or h is None:
            return None
        x1, y1, x2, y2 = x, y, x + w, y + h
    if x2 <= x1 or y2 <= y1:
        return None
    pad_x = (x2 - x1) * max(0.0, padding_ratio)
    pad_y = (y2 - y1) * max(0.0, padding_ratio)
    x1 = max(0, int(round(x1 - pad_x)))
    y1 = max(0, int(round(y1 - pad_y)))
    x2 = min(width, int(round(x2 + pad_x)))
    y2 = min(height, int(round(y2 + pad_y)))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _write_face_crop(
    full_frame_path: Path,
    face_crop_path: Path,
    crop_box: tuple[int, int, int, int],
) -> str:
    from PIL import Image

    with Image.open(full_frame_path).convert("RGB") as image:
        image.crop(crop_box).save(face_crop_path, "JPEG", quality=92)
    return "ready" if face_crop_path.is_file() else "failed"


def _write_annotated_face_frame(
    full_frame_path: Path,
    annotated_frame_path: Path,
    crop_box: tuple[int, int, int, int],
    *,
    event_context: dict,
) -> str:
    from PIL import Image, ImageDraw

    with Image.open(full_frame_path).convert("RGB") as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle(crop_box, outline=(220, 30, 30), width=4)
        label = str(
            (
                (event_context.get("payload") or {}).get("matched_person", {})
                if isinstance(event_context.get("payload"), dict)
                else {}
            ).get("name")
            or event_context.get("event_type")
            or "watchlist_hit"
        )
        x1, y1, _x2, _y2 = crop_box
        draw.text((x1, max(0, y1 - 18)), label, fill=(255, 255, 255))
        image.save(annotated_frame_path, "JPEG", quality=92)
    return "ready" if annotated_frame_path.is_file() else "failed"


def _source_observation_id_from_event_context(event_context: dict) -> str:
    payload = event_context.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    match = payload.get("match")
    match = match if isinstance(match, dict) else {}
    return str(match.get("source_observation_id") or payload.get("source_observation_id") or "")


def _image_materialization_summary(
    event_context: dict,
    result: dict[str, object],
) -> dict[str, object]:
    payload = event_context.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    return {
        "schema_version": "face-image-evidence-v1",
        "playback_kind": "image",
        "evidence_mode": "image_only",
        "clip_required": False,
        "clip_status": "not_required",
        "image_status": "image_ready",
        "image_available": True,
        "source_observation_id": result.get("source_observation_id") or "",
        "person_id": event_context.get("person_id") or payload.get("person_id"),
        "matched_person": payload.get("matched_person") if isinstance(payload.get("matched_person"), dict) else {},
        "match": payload.get("match") if isinstance(payload.get("match"), dict) else {},
        "similarity": (
            (payload.get("match") or {}).get("similarity")
            if isinstance(payload.get("match"), dict)
            else event_context.get("confidence")
        ),
        "observation": payload.get("observation") if isinstance(payload.get("observation"), dict) else {},
        "face_bbox": result.get("face_bbox"),
        "crop_box": result.get("crop_box"),
        "face_crop_uri": result.get("face_crop_path") or "",
        "full_frame_uri": result.get("full_frame_path") or "",
        "annotated_frame_uri": result.get("annotated_frame_path") or "",
        "materialization_mode": "rolling_cache_image_extract",
        "runtime_epoch_id": result.get("runtime_epoch_id") or "",
        "frame_pts": result.get("frame_pts"),
        "frame_uuid": result.get("frame_uuid") or "",
        "segment_id": result.get("segment_id") or "",
        "crop_status": result.get("crop_status") or "",
        "annotated_status": result.get("annotated_status") or "",
    }


def _image_materialization_doc(result: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "image-only-v1",
        "created_by": "media-worker",
        "materialization_status": "materialized",
        "materialization_mode": "rolling_cache_image_extract",
        "runtime_epoch_id": result.get("runtime_epoch_id") or "",
        "frame_pts": result.get("frame_pts"),
        "frame_uuid": result.get("frame_uuid") or "",
        "segment_id": result.get("segment_id") or "",
        "segment_path": result.get("segment_path") or "",
        "full_frame_path": result.get("full_frame_path") or "",
        "face_crop_path": result.get("face_crop_path") or "",
        "annotated_frame_path": result.get("annotated_frame_path") or "",
        "crop_status": result.get("crop_status") or "",
        "annotated_status": result.get("annotated_status") or "",
        "materialized_at": datetime.now(timezone.utc).isoformat(),
    }


def _mark_image_evidence_materialized(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    event_context: dict,
    result: dict[str, object],
    lease: MaterializationLease,
) -> bool:
    summary = _image_materialization_summary(event_context, result)
    materialization = _image_materialization_doc(result)
    full_frame_path = str(result.get("full_frame_path") or "")
    face_crop_path = str(result.get("face_crop_path") or "")
    annotated_frame_path = str(result.get("annotated_frame_path") or "")
    with pg_conn.transaction(), pg_conn.cursor() as cur:
        if lease.schema_v2:
            cur.execute(
                """
                SELECT 1
                FROM evidence_tasks
                WHERE event_id = %(event_id)s::uuid
                  AND materialization_status = 'materializing'
                  AND materialization_phase = 'image_running'
                  AND materialization_lease_owner = %(lease_owner)s
                  AND materialization_lease_token = %(lease_token)s
                  AND materialization_lease_generation = %(lease_generation)s
                FOR UPDATE
                """,
                {
                    "event_id": event_id,
                    "lease_owner": lease.owner,
                    "lease_token": lease.token,
                    "lease_generation": lease.generation,
                },
            )
        else:
            cur.execute(
                """
                SELECT 1
                FROM evidence_tasks
                WHERE event_id = %(event_id)s::uuid
                  AND materialization_status = 'materializing'
                  AND COALESCE(task_type, '') = 'image_only'
                FOR UPDATE
                """,
                {"event_id": event_id},
            )
        if not cur.fetchone():
            logger.warning(
                "image_materialization_fence_lost event_id=%s token=%s generation=%s",
                event_id,
                lease.token,
                lease.generation,
            )
            return False
        cur.execute(
            """
            INSERT INTO evidence_bundles (
                event_id, source_event_id, camera_id, source_id, camera_name,
                event_type, event_created_at, alarm_machine_time,
                media_status, evidence_state, evidence_reason, raw_clip_uri,
                annotation_status, annotation_count, matched_objects,
                unknown_objects, visual_evidence_status,
                frontend_overlay_required, summary, materialization
            )
            SELECT
                e.id, e.source_event_id, e.camera_id, e.source_id,
                COALESCE(c.name, e.payload->>'camera_name', e.payload->'camera'->>'name'),
                e.event_type, e.created_at, e.created_at,
                'image_ready', 'image_ready', '', NULL,
                'not_required', 0, 1, 0, 'verified',
                false, %(summary)s::jsonb, %(materialization)s::jsonb
            FROM events e
            LEFT JOIN cameras c
              ON c.id::text = e.camera_id
              OR c.source_id = e.source_id
            WHERE e.id = %(event_id)s::uuid
            ON CONFLICT (event_id) DO UPDATE SET
                media_status = 'image_ready',
                evidence_state = 'image_ready',
                evidence_reason = '',
                raw_clip_uri = NULL,
                annotation_status = 'not_required',
                matched_objects = 1,
                visual_evidence_status = 'verified',
                frontend_overlay_required = false,
                summary = EXCLUDED.summary,
                materialization = EXCLUDED.materialization,
                updated_at = now()
            """,
            {
                "event_id": event_id,
                "summary": json.dumps(summary, ensure_ascii=False),
                "materialization": json.dumps(materialization, ensure_ascii=False),
            },
        )
        for artifact_type, path in (
            ("full_frame", full_frame_path),
            ("face_crop", face_crop_path),
            ("annotated_frame", annotated_frame_path),
        ):
            if not path:
                continue
            cur.execute(
                """
                INSERT INTO evidence_artifacts (
                    event_id, artifact_type, uri, storage_backend, content_type,
                    size_bytes, status, metadata, updated_at
                )
                VALUES (
                    %(event_id)s::uuid, %(artifact_type)s, %(uri)s,
                    'filesystem', 'image/jpeg', %(size_bytes)s, 'ready',
                    %(metadata)s::jsonb, now()
                )
                ON CONFLICT (event_id, artifact_type) DO UPDATE SET
                    uri = EXCLUDED.uri,
                    storage_backend = EXCLUDED.storage_backend,
                    content_type = EXCLUDED.content_type,
                    size_bytes = EXCLUDED.size_bytes,
                    status = 'ready',
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """,
                {
                    "event_id": event_id,
                    "artifact_type": artifact_type,
                    "uri": path,
                    "size_bytes": _file_size_or_none(path),
                    "metadata": json.dumps(
                        {
                            "source_observation_id": result.get("source_observation_id") or "",
                            "storage_semantics": "image_only_face_evidence",
                            "materialization_mode": "rolling_cache_image_extract",
                        },
                        ensure_ascii=False,
                    ),
                },
            )
        cur.execute(
            """
            UPDATE events
            SET snapshot_path = %(full_frame_path)s,
                clip_required = false,
                media_status = 'image_ready',
                payload = COALESCE(payload, '{}'::jsonb)
                    || jsonb_build_object(
                        'media',
                        COALESCE(payload->'media', '{}'::jsonb)
                        || jsonb_build_object(
                            'media_status', 'image_ready',
                            'snapshot_status', 'image_ready',
                            'clip_status', 'not_required',
                            'metadata_status', 'image_ready',
                            'evidence_state', 'image_ready',
                            'evidence_reason', '',
                            'materialization_status', 'materialized',
                            'playback_kind', 'image',
                            'evidence_mode', 'image_only',
                            'clip_required', false,
                            'snapshot_path', %(full_frame_path)s::text,
                            'crop_path', NULLIF(%(face_crop_path)s::text, ''),
                            'annotated_frame_path', NULLIF(%(annotated_frame_path)s::text, '')
                        )
                    ),
                updated_at = now()
            WHERE id = %(event_id)s::uuid
            """,
            {
                "event_id": event_id,
                "full_frame_path": full_frame_path,
                "face_crop_path": face_crop_path,
                "annotated_frame_path": annotated_frame_path,
            },
        )
        source_observation_id = str(result.get("source_observation_id") or "")
        if source_observation_id:
            cur.execute(
                """
                UPDATE face_observations
                SET snapshot_path = COALESCE(NULLIF(%(full_frame_path)s::text, ''), snapshot_path),
                    crop_path = COALESCE(NULLIF(%(face_crop_path)s::text, ''), crop_path),
                    payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_build_object(
                                'snapshot_path', %(full_frame_path)s::text,
                                'crop_path', NULLIF(%(face_crop_path)s::text, ''),
                                'annotated_frame_path', NULLIF(%(annotated_frame_path)s::text, '')
                            )
                        )
                WHERE source_observation_id = %(source_observation_id)s
                """,
                {
                    "source_observation_id": source_observation_id,
                    "full_frame_path": full_frame_path,
                    "face_crop_path": face_crop_path,
                    "annotated_frame_path": annotated_frame_path,
                },
            )
        lifecycle_fields = ""
        lifecycle_predicate = ""
        lifecycle_params: dict[str, object] = {}
        if lease.schema_v2:
            lifecycle_fields = """
                materialization_phase = 'terminal',
                materialization_phase_updated_at = now(),
                materialization_owner = 'terminal',
                materialization_next_attempt_at = NULL,
                materialization_retry_reason = NULL,
                materialization_defer_reason = NULL,
                materialization_failure_reason = NULL,
                materialization_expired_reason = NULL,
                materialization_lease_owner = NULL,
                materialization_lease_token = NULL,
                materialization_lease_expires_at = NULL,
                materialization_lease_heartbeat_at = NULL,
                materialization_handoff = '{}'::jsonb,
            """
            lifecycle_predicate = """
              AND materialization_lease_owner = %(lease_owner)s
              AND materialization_lease_token = %(lease_token)s
              AND materialization_lease_generation = %(lease_generation)s
            """
            lifecycle_params = {
                "lease_owner": lease.owner,
                "lease_token": lease.token,
                "lease_generation": lease.generation,
            }
        cur.execute(
            f"""
            UPDATE evidence_tasks
            SET status = 'materialized',
                materialization_status = 'materialized',
                {lifecycle_fields}
                materialization_audit = COALESCE(materialization_audit, '{{}}'::jsonb)
                    || %(materialization)s::jsonb,
                error_message = '',
                updated_at = now()
            WHERE event_id = %(event_id)s::uuid
              AND materialization_status = 'materializing'
              AND COALESCE(task_type, '') = 'image_only'
              {lifecycle_predicate}
            """,
            {
                "event_id": event_id,
                "materialization": json.dumps(
                    {"image_only": materialization},
                    ensure_ascii=False,
                ),
                **lifecycle_params,
            },
        )
        if not cur.rowcount:
            raise RuntimeError(
                f"image_materialization_terminal_fence_lost:{event_id}"
            )
    return True


def _mark_image_evidence_failed(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    reason: str,
    lease: MaterializationLease,
) -> bool:
    if not fail_rolling_task(pg_conn, lease, reason=reason):
        return False
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE evidence_bundles
            SET media_status = 'image_missing',
                evidence_state = 'image_missing',
                evidence_reason = %(reason)s,
                visual_evidence_status = 'missing',
                summary = jsonb_set(
                    jsonb_set(
                        jsonb_set(COALESCE(summary, '{}'::jsonb), '{image_status}', '"image_missing"'::jsonb, true),
                        '{image_available}', 'false'::jsonb, true
                    ),
                    '{image_missing_reason}', to_jsonb(%(reason)s::text), true
                ),
                updated_at = now()
            WHERE event_id = %(event_id)s::uuid
            """,
            {"event_id": event_id, "reason": reason},
        )
        cur.execute(
            """
            UPDATE events
            SET media_status = 'image_missing',
                payload = COALESCE(payload, '{}'::jsonb)
                    || jsonb_build_object(
                        'media',
                        COALESCE(payload->'media', '{}'::jsonb)
                        || jsonb_build_object(
                            'media_status', 'image_missing',
                            'snapshot_status', 'image_missing',
                            'metadata_status', 'image_missing',
                            'evidence_state', 'image_missing',
                            'evidence_reason', %(reason)s::text,
                            'materialization_status', 'materialization_failed'
                        )
                    ),
                updated_at = now()
            WHERE id = %(event_id)s::uuid
            """,
            {"event_id": event_id, "reason": reason},
        )
    return True


def _file_size_or_none(path: str) -> int | None:
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def _image_materialization_failure_reason(exc: Exception, *, max_chars: int = 900) -> str:
    text = " ".join(str(exc or "").split())
    prefix = f"face_image_extract_failed:{type(exc).__name__}"
    if not text:
        return prefix
    reason = f"{prefix}:{text}"
    return reason if len(reason) <= max_chars else reason[:max_chars]


class _RollingCacheMaterializationRunner:
    """Keep rolling-cache materialization workers hot without blocking polling.

    The old path claimed a full DB batch, ran every remux, and finalized the whole
    batch before the media-worker loop could scan for newly-ready tasks again.
    Under 60-stream pressure that turned a 1s poll interval into a 60-100s
    effective interval. This runner keeps the expensive copy/remux work in a
    persistent pool and lets the main loop keep claiming ready work as capacity
    opens.
    """

    def __init__(self, *, max_workers: int) -> None:
        self.max_workers = max(1, int(max_workers or 1))
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self._futures: dict[object, tuple[str, MaterializationLease | None]] = {}

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def snapshot(self) -> dict[str, int]:
        return {
            "active": len(self._futures),
            "capacity": self.max_workers,
        }

    def process(self, pg_conn: psycopg.Connection, cfg: Config) -> int:
        # Recovery/expiry is owned by _process_rolling_cache_tasks and runs
        # exactly once before either legacy or persistent-runner admission.
        updated = self._drain_completed(pg_conn, cfg)

        available = self.max_workers - len(self._futures)
        if available > 0:
            claim_limit = min(
                max(1, int(getattr(cfg, "rolling_cache_materialization_max_per_poll", 1))),
                available,
            )
            rows = _rolling_cache_candidate_tasks(pg_conn, cfg, limit=claim_limit)
            segment_cache: dict[tuple[str, str], list[RollingSegment]] = {}
            for row in rows:
                if len(self._futures) >= self.max_workers:
                    break
                job = _prepare_rolling_cache_job(
                    pg_conn,
                    cfg,
                    row,
                    segment_cache=segment_cache,
                )
                if job is None:
                    continue
                future = self._executor.submit(
                    _materialize_rolling_cache_job,
                    root=cfg.rolling_cache_root,
                    output_root=cfg.rolling_cache_materialized_root,
                    job=job,
                )
                self._futures[future] = (
                    str(job.get("event_id") or ""),
                    job.get("lease")
                    if isinstance(job.get("lease"), MaterializationLease)
                    else None,
                )

        return updated + self._drain_completed(pg_conn, cfg)

    def _drain_completed(self, pg_conn: psycopg.Connection, cfg: Config) -> int:
        if not self._futures:
            return 0

        metadata_overrides: list[dict] = []
        for future, future_context in list(self._futures.items()):
            if not future.done():
                continue
            self._futures.pop(future, None)
            event_id, lease = future_context
            try:
                metadata = future.result()
                if _persist_rolling_cache_handoff_metadata(
                    pg_conn,
                    cfg,
                    metadata,
                ):
                    metadata_overrides.append(metadata)
            except RollingCacheCoverageMiss as exc:
                _defer_rolling_cache_task(
                    pg_conn,
                    event_id=event_id,
                    reason=str(exc) or "rolling_cache_coverage_miss",
                    lease=lease,
                )
            except Exception as exc:
                logger.exception(
                    "rolling_cache_materialization_failed event_id=%s",
                    event_id,
                )
                _fail_rolling_cache_task(
                    pg_conn,
                    event_id=event_id,
                    reason=_rolling_cache_failure_reason(exc),
                    lease=lease,
                )

        if not metadata_overrides:
            return 0
        return _flush_rolling_cache_finalizer_batch_or_defer(
            pg_conn,
            cfg,
            metadata_overrides,
        )


def _prepare_rolling_cache_job(
    pg_conn: psycopg.Connection,
    cfg: Config,
    row: dict[str, object],
    *,
    segment_cache: dict[tuple[str, str], list[RollingSegment]],
) -> dict[str, object] | None:
    event_id = str(row.get("event_id") or "")
    source_id = str(row.get("source_id") or row.get("replay_source_id") or "")
    if not event_id or not source_id:
        return None
    if cfg.rolling_cache_sources and source_id not in cfg.rolling_cache_sources:
        return None
    if _is_already_ready(pg_conn, event_id):
        return None
    lease = _claim_rolling_cache_task(
        pg_conn,
        event_id=event_id,
        ready_at=row.get("rolling_cache_ready_at"),
        processing_deadline_s=(
            cfg.rolling_cache_materialization_processing_deadline_seconds
        ),
        phase=MaterializationPhase.REMUX_RUNNING.value,
    )
    if lease is None:
        return None
    try:
        event_context = _load_event_context(pg_conn, event_id)
        window = _rolling_cache_window(row, event_context)
        if window is None:
            _defer_rolling_cache_task(
                pg_conn,
                event_id=event_id,
                reason="rolling_cache_missing_event_frame_pts",
                lease=lease,
            )
            return None
        requested_start_pts, requested_end_pts, event_frame_pts = window
        runtime_epoch_id = (
            _runtime_epoch_from_event_context(event_context)
            or _current_runtime_epoch_id(cfg.sink_output_dir)
        )
        labels = _rolling_cache_labels(
            row=row,
            event_context=event_context,
            runtime_epoch_id=runtime_epoch_id,
            requested_start_pts=requested_start_pts,
            requested_end_pts=requested_end_pts,
            event_frame_pts=event_frame_pts,
        )
        cache_key = (source_id, runtime_epoch_id)
        segments = segment_cache.get(cache_key)
        if segments is None:
            segments = find_segments(
                cfg.rolling_cache_root,
                source_id=source_id,
                runtime_epoch_id=runtime_epoch_id,
            )
            segment_cache[cache_key] = segments
        return {
            "event_id": event_id,
            "source_id": source_id,
            "requested_start_pts": requested_start_pts,
            "requested_end_pts": requested_end_pts,
            "runtime_epoch_id": runtime_epoch_id,
            "labels": labels,
            "segments": segments,
            "lease": lease,
        }
    except RollingCacheCoverageMiss as exc:
        _defer_rolling_cache_task(
            pg_conn,
            event_id=event_id,
            reason=str(exc) or "rolling_cache_coverage_miss",
            lease=lease,
        )
    except Exception as exc:
        logger.exception("rolling_cache_materialization_failed event_id=%s", event_id)
        _fail_rolling_cache_task(
            pg_conn,
            event_id=event_id,
            reason=_rolling_cache_failure_reason(exc),
            lease=lease,
        )
    return None


def _materialize_rolling_cache_job(
    *,
    root: str,
    output_root: str,
    job: dict[str, object],
) -> dict:
    event_id = str(job.get("event_id") or "")
    materialized = materialize_window(
        root=root,
        output_root=output_root,
        event_id=event_id,
        source_id=str(job.get("source_id") or ""),
        requested_start_pts=int(job.get("requested_start_pts") or 0),
        requested_end_pts=int(job.get("requested_end_pts") or 0),
        runtime_epoch_id=str(job.get("runtime_epoch_id") or ""),
        labels=job.get("labels") if isinstance(job.get("labels"), dict) else {},
        segments=(
            job.get("segments")
            if isinstance(job.get("segments"), list)
            else None
        ),
    )
    metadata = _load_scan_metadata_payload(materialized.metadata_path)
    if metadata is None:
        raise RuntimeError("rolling_cache_materialized_metadata_unreadable")
    lease = job.get("lease")
    if not isinstance(lease, MaterializationLease):
        raise RuntimeError("rolling_cache_materialization_missing_lease")
    try:
        identity = materialized.video_path.stat()
    except OSError as exc:
        raise RollingCacheCoverageMiss(
            f"segment_disappeared:{materialized.video_path}:{exc}"
        ) from exc
    handoff = {
        "attempt_token": lease.token or f"legacy:{lease.event_id}:{lease.generation}",
        "source_id": str(job.get("source_id") or ""),
        "runtime_epoch_id": str(job.get("runtime_epoch_id") or ""),
        "requested_window": {
            "start_pts": int(job.get("requested_start_pts") or 0),
            "end_pts": int(job.get("requested_end_pts") or 0),
        },
        "selected_segment_ids": list(materialized.segment_ids),
        # Phase 4 moves this to an attempt-specific staging tree.  Phase 1
        # freezes the existing immutable output identity before finalization.
        "staging_path": str(materialized.sink_dir),
        "canonical_path": str(materialized.video_path),
        "device": int(identity.st_dev),
        "inode": int(identity.st_ino),
        "size": int(identity.st_size),
        "mtime_ns": int(identity.st_mtime_ns),
    }
    observed_at = datetime.now(timezone.utc).isoformat()
    return {
        **metadata,
        "_meta_dir": str(materialized.sink_dir),
        "_lifecycle_lease": {
            "event_id": lease.event_id,
            "owner": lease.owner,
            "token": lease.token,
            "generation": lease.generation,
            "phase": lease.phase,
            "schema_v2": lease.schema_v2,
        },
        "_lifecycle_handoff": handoff,
        "_finalizer_phase": {
            "sink_metadata_first_seen_at": observed_at,
            "sink_video_first_seen_at": observed_at,
            "sink_video_stable_at": observed_at,
            "sink_ffprobe_ready_at": observed_at,
            "rolling_cache_materialization_ms": materialized.materialization_ms,
            "rolling_cache_segment_ids": list(materialized.segment_ids),
        },
    }


def _persist_rolling_cache_handoff_metadata(
    pg_conn: psycopg.Connection,
    cfg: Config,
    metadata: dict,
) -> bool:
    lease_payload = metadata.pop("_lifecycle_lease", None)
    handoff = metadata.pop("_lifecycle_handoff", None)
    if not isinstance(lease_payload, dict) or not isinstance(handoff, dict):
        logger.error(
            "rolling_cache_handoff_missing event_id=%s",
            _metadata_override_event_ids([metadata])[:1],
        )
        return False
    lease = MaterializationLease(
        event_id=str(lease_payload.get("event_id") or ""),
        owner=str(lease_payload.get("owner") or ""),
        token=str(lease_payload.get("token") or ""),
        generation=int(lease_payload.get("generation") or 0),
        phase=str(lease_payload.get("phase") or ""),
        schema_v2=bool(lease_payload.get("schema_v2", True)),
    )
    sink_path = str(metadata.get("_meta_dir") or handoff.get("staging_path") or "")
    persisted = persist_finalizer_handoff(
        pg_conn,
        lease,
        sink_output_path=sink_path,
        handoff=handoff,
        lease_seconds=(
            cfg.rolling_cache_materialization_processing_deadline_seconds
        ),
    )
    if not persisted:
        logger.warning(
            "rolling_cache_handoff_fence_lost event_id=%s token=%s generation=%s",
            lease.event_id,
            lease.token,
            lease.generation,
        )
        return False
    finalizer_phase = metadata.get("_finalizer_phase")
    if isinstance(finalizer_phase, dict):
        finalizer_phase.update(
            {
                "lease_token": lease.token or None,
                "lease_generation": lease.generation,
                "handoff_persisted_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return True


def _expire_overdue_rolling_cache_tasks(
    pg_conn: psycopg.Connection,
    cfg: Config,
) -> int:
    result = recover_and_expire_rolling_tasks(
        pg_conn,
        source_ids=tuple(cfg.rolling_cache_sources),
    )
    if result.changed:
        logger.info(
            "rolling_lifecycle_recovery ready_deadline_expired=%s "
            "running_sla_missed=%s handoff_recovered=%s "
            "lease_retry_scheduled=%s lease_deadline_expired=%s",
            result.ready_deadline_expired,
            result.running_sla_missed,
            result.handoff_recovered,
            result.lease_retry_scheduled,
            result.lease_deadline_expired,
        )
    return result.changed


def _flush_rolling_cache_finalizer_batch_or_defer(
    pg_conn: psycopg.Connection,
    cfg: Config,
    metadata_overrides: list[dict],
) -> int:
    try:
        return _flush_rolling_cache_finalizer_batch(pg_conn, cfg, metadata_overrides)
    except Exception as exc:
        logger.exception(
            "rolling_cache_finalizer_batch_failed count=%s",
            len(metadata_overrides),
        )
        for event_id in _metadata_override_event_ids(metadata_overrides):
            _fail_rolling_cache_task(
                pg_conn,
                event_id=event_id,
                reason=f"rolling_cache_finalizer_error:{type(exc).__name__}",
            )
        return 0


def _metadata_override_event_ids(metadata_overrides: list[dict]) -> list[str]:
    event_ids: list[str] = []
    for metadata in metadata_overrides:
        labels = metadata.get("labels")
        labels = labels if isinstance(labels, dict) else {}
        event_id = str(metadata.get("event_id") or labels.get("event_id") or "")
        if event_id:
            event_ids.append(event_id)
    return event_ids


def _flush_rolling_cache_finalizer_batch(
    pg_conn: psycopg.Connection,
    cfg: Config,
    metadata_overrides: list[dict],
) -> int:
    if not metadata_overrides:
        return 0

    local_processed: set[str] = set()
    return int(
        _process_sink_output(
            pg_conn,
            str(Path(cfg.rolling_cache_materialized_root) / "midterm"),
            local_processed,
            evidence_output_dir=cfg.evidence_output_dir,
            candidate_dirs=None,
            invalid_output_failures=None,
            midterm_sink_stability_checks=1,
            processed_state_path=None,
            sink_scan_max_metadata_files=cfg.sink_scan_max_metadata_files,
            materialization_guard=None,
            materialization_pacer=None,
            materialization_timeout_s=cfg.materialization_timeout_s,
            materialization_max_backlog=0,
            evidence_final_root_max_bytes=cfg.evidence_final_root_max_bytes,
            evidence_incoming_root_max_bytes=cfg.evidence_incoming_root_max_bytes,
            replay_sink_output_max_bytes=0,
            evidence_storage_warning_ratio=cfg.evidence_storage_warning_ratio,
            evidence_storage_critical_ratio=cfg.evidence_storage_critical_ratio,
            evidence_storage_hard_ratio=cfg.evidence_storage_hard_ratio,
            cleanup_replay_sink_output_enabled=True,
            cleanup_replay_sink_output_statuses=cfg.cleanup_replay_sink_output_statuses,
            materialization_finalizer_workers=cfg.materialization_finalizer_workers,
            materialization_finalizer_max_per_source_per_poll=0,
            materialization_finalizer_source_serial=(
                cfg.materialization_finalizer_source_serial
            ),
            materialization_database_url=cfg.database_url,
            finalizer_worker_id="rolling-cache",
            metadata_files_override=metadata_overrides,
            scan_stats_override={
                "scan_duration_ms": 0,
                "metadata_files_visited": len(metadata_overrides),
                "metadata_files_parsed": len(metadata_overrides),
                "scan_mode": "rolling_cache",
            },
        )
        or 0
    )


def _rolling_cache_candidate_tasks(
    pg_conn: psycopg.Connection,
    cfg: Config,
    *,
    limit: int | None = None,
) -> list[dict[str, object]]:
    with pg_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH candidates AS (
                SELECT
                    et.event_id, et.source_id, et.replay_source_id,
                    et.camera_id, et.event_type, et.event_ts_ms,
                    et.pre_seconds, et.post_seconds, et.replay_window,
                    et.priority, e.payload, e.frame_uuid, e.created_at,
                    et.materialization_ready_at AS rolling_cache_ready_at
                FROM evidence_tasks et
                JOIN events e ON e.id = et.event_id
                LEFT JOIN evidence_bundles eb ON eb.event_id = et.event_id
                WHERE et.materialization_status = ANY(%(statuses)s)
                  AND COALESCE(et.task_type, '') <> 'image_only'
                  AND COALESCE(et.clip_required, false) = true
                  AND (
                      %(sources_empty)s
                      OR COALESCE(et.source_id, et.replay_source_id, '') = ANY(%(sources)s)
                  )
                  AND et.materialization_ready_at IS NOT NULL
                  AND et.materialization_ready_at <= now()
                  AND COALESCE(
                        NULLIF(
                            to_jsonb(et)->>'materialization_next_attempt_at',
                            ''
                        )::timestamptz,
                        et.materialization_ready_at
                      ) <= now()
                  AND COALESCE(
                        NULLIF(to_jsonb(et)->>'materialization_owner', ''),
                        'rolling'
                      ) = 'rolling'
                  AND COALESCE(et.replay_slot_status, '') <> 'active'
                  AND eb.event_id IS NULL
            )
            SELECT
                *,
                GREATEST(
                    0,
                    floor(extract(epoch FROM (now() - rolling_cache_ready_at)) * 1000)
                )::bigint AS rolling_cache_ready_lag_ms
            FROM candidates
            WHERE rolling_cache_ready_at <= now()
            ORDER BY priority DESC, rolling_cache_ready_at ASC, created_at ASC
            LIMIT %(limit)s
            """,
            {
                "statuses": list(ROLLING_CACHE_TASK_STATUSES),
                "sources": list(cfg.rolling_cache_sources),
                "sources_empty": not bool(cfg.rolling_cache_sources),
                "limit": (
                    max(1, int(limit))
                    if limit is not None
                    else cfg.rolling_cache_materialization_max_per_poll
                ),
            },
        )
        return [dict(row) for row in cur.fetchall()]


def _claim_rolling_cache_task(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    ready_at: object | None = None,
    processing_deadline_s: float = 120.0,
    phase: str = MaterializationPhase.REMUX_RUNNING.value,
    worker_id: str = "",
) -> MaterializationLease | None:
    # ready_at remains accepted for legacy callers/metrics, but event-worker is
    # the sole ready-time producer and the claim never writes it.
    del ready_at
    return claim_rolling_task(
        pg_conn,
        event_id=event_id,
        worker_id=(
            worker_id
            or os.getenv("MEDIA_WORKER_ID")
            or f"media-worker:{os.getenv('HOSTNAME', 'local')}:{os.getpid()}"
        ),
        phase=phase,
        lease_seconds=max(1.0, float(processing_deadline_s)),
    )


def _rolling_cache_coverage_retry_after_s(exc: BaseException) -> float:
    message = str(exc)
    gaps_ns: list[int] = []
    for key in ("pre_gap_ns=", "post_gap_ns="):
        start = message.find(key)
        if start < 0:
            continue
        index = start + len(key)
        digits: list[str] = []
        while index < len(message) and message[index].isdigit():
            digits.append(message[index])
            index += 1
        if digits:
            gaps_ns.append(int("".join(digits)))
    if not gaps_ns:
        return 2.0
    return min(10.0, max(1.0, max(gaps_ns) / 1_000_000_000.0 + 1.0))


def _defer_rolling_cache_task(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    reason: str,
    retry_after_s: float = 2.0,
    lease: MaterializationLease | None = None,
) -> bool:
    lease = lease or current_lease(
        pg_conn,
        event_id=event_id,
        fallback_owner="rolling-cache",
        fallback_phase=MaterializationPhase.REMUX_RUNNING.value,
    )
    if lease is None:
        logger.warning(
            "rolling_retry_ignored_stale_owner event_id=%s reason=%s",
            event_id,
            reason,
        )
        return False
    return retry_rolling_task(
        pg_conn,
        lease,
        reason=reason,
        retry_hint_s=max(0.5, float(retry_after_s or 0.0)),
    )


def _rolling_cache_failure_reason(exc: Exception, *, max_chars: int = 900) -> str:
    detail = " ".join(str(exc or "").split())
    prefix = f"rolling_cache_error:{type(exc).__name__}"
    if not detail:
        return prefix
    reason = f"{prefix}:{detail}"
    if len(reason) <= max_chars:
        return reason
    return f"{prefix}:...{reason[-max_chars + len(prefix) + 4:]}"


def _fail_rolling_cache_task(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    reason: str,
    lease: MaterializationLease | None = None,
) -> bool:
    lease = lease or current_lease(
        pg_conn,
        event_id=event_id,
        fallback_owner="rolling-cache",
        fallback_phase=MaterializationPhase.REMUX_RUNNING.value,
    )
    if lease is None:
        logger.warning(
            "rolling_failure_ignored_stale_owner event_id=%s reason=%s",
            event_id,
            reason,
        )
        return False
    return fail_rolling_task(pg_conn, lease, reason=reason)


def _rolling_cache_window(
    task_row: dict[str, object],
    event_context: dict,
) -> tuple[int, int, int] | None:
    payload = event_context.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    media = payload.get("media")
    media = media if isinstance(media, dict) else {}
    replay_window = task_row.get("replay_window")
    replay_window = replay_window if isinstance(replay_window, dict) else {}
    event_frame_pts = _to_int(
        media.get("event_frame_pts")
        or media.get("frame_pts")
        or replay_window.get("event_frame_pts")
        or replay_window.get("frame_pts")
    )
    if event_frame_pts is None:
        return None
    pre_seconds = (
        _to_float(task_row.get("pre_seconds"))
        or _to_float(replay_window.get("pre_seconds"))
        or float(os.getenv("DEFAULT_PRE_SECONDS", "5"))
    )
    post_seconds = (
        _to_float(task_row.get("post_seconds"))
        or _to_float(replay_window.get("post_seconds"))
        or float(os.getenv("DEFAULT_POST_SECONDS", "5"))
    )
    requested_start_pts = max(0, int(event_frame_pts - pre_seconds * 1_000_000_000))
    requested_end_pts = int(event_frame_pts + post_seconds * 1_000_000_000)
    return requested_start_pts, requested_end_pts, event_frame_pts


def _rolling_cache_labels(
    *,
    row: dict[str, object],
    event_context: dict,
    runtime_epoch_id: str,
    requested_start_pts: int,
    requested_end_pts: int,
    event_frame_pts: int,
) -> dict[str, object]:
    return {
        "event_id": event_context.get("event_id", ""),
        "source_event_id": event_context.get("source_event_id", ""),
        "event_type": event_context.get("event_type", ""),
        "source_id": event_context.get("source_id") or row.get("source_id") or "",
        "camera_id": event_context.get("camera_id", ""),
        "frame_uuid": event_context.get("frame_uuid", ""),
        "event_frame_uuid": event_context.get("frame_uuid", ""),
        "event_frame_pts": event_frame_pts,
        "frame_pts": event_frame_pts,
        "requested_start_pts": requested_start_pts,
        "original_requested_start_pts": requested_start_pts,
        "effective_start_pts": requested_start_pts,
        "requested_end_pts": requested_end_pts,
        "requested_pre_window_seconds": row.get("pre_seconds"),
        "effective_pre_window_seconds": row.get("pre_seconds"),
        "runtime_epoch_id": runtime_epoch_id,
        "replay_source_kind": "rolling_cache",
        "evidence_topology": "replay_raw_rolling_cache",
        "annotation_source_policy": "frame_cache",
        "replay_stop_strategy": "rolling_cache_segment_copy",
        "materialization_mode": "rolling_cache_copy",
    }


def _promote_generated_clips_to_ready(pg_conn: psycopg.Connection) -> int:
    """Promote historical clean generated clips to the current ready status."""
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET media_status = 'ready',
                    payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_build_object(
                                'clip_status', 'ready',
                                'evidence_state', 'ready',
                                'evidence_reason', NULL,
                                'evidence_state_updated_at', now()
                            )
                        ),
                    updated_at = now()
                WHERE payload -> 'media' ->> 'clip_status' = 'generated'
                  AND clip_path IS NOT NULL
                  AND clip_path != ''
                """
            )
            updated = cur.rowcount or 0
            if updated:
                cur.execute(
                    """
                    UPDATE evidence_tasks t
                    SET status = 'ready',
                        clip_path = COALESCE(e.clip_path, t.clip_path),
                        updated_at = now()
                    FROM events e
                    WHERE t.event_id = e.id
                      AND e.payload -> 'media' ->> 'evidence_state' = 'ready'
                      AND t.status <> 'ready'
                      AND COALESCE(t.materialization_failure_reason, '') <> %(superseded_reason)s
                    """
                    ,
                    {
                        "superseded_reason": EPOCH_SUPERSEDED_INCOMPLETE_REASON,
                    }
                )
            return updated
    except Exception:
        logger.exception("_promote_generated_clips_to_ready failed")
        return 0


def _snapshot_needed(pg_conn: psycopg.Connection) -> list[dict]:
    """Return events with clean clip statuses that need snapshot generation.

    Conditions: clip_status is 'ready' or legacy-clean 'generated',
    snap_status is not 'ready' or 'not_required', snapshot_required=true,
    and clip_path is non-null.
    """
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, clip_path,
                       COALESCE(
                           (payload -> 'media' ->> 'pre_seconds')::float,
                           (payload -> 'media' ->> 'pre_seconds')::int::float
                       ) AS media_pre_seconds,
                       COALESCE(payload -> 'media' ->> 'snapshot_required', 'false') = 'true'
                           OR COALESCE((payload ->> 'snapshot_required')::bool, false)
                           AS snapshot_required,
                       payload -> 'media' ->> 'snapshot_status' AS snap_status
                FROM events
                WHERE payload -> 'media' ->> 'clip_status'
                      IN ('ready', 'generated')
                  AND clip_path IS NOT NULL
                  AND clip_path != ''
                """
            )
            rows = []
            for row in cur.fetchall():
                snap_status = row[4] or "not_implemented"
                if snap_status in ("ready", "not_required"):
                    continue
                if not row[3]:  # snapshot_required is false
                    continue
                rows.append({
                    "event_id": row[0],
                    "clip_path": row[1],
                    "pre_seconds": row[2],
                    "snapshot_required": row[3],
                    "snap_status": snap_status,
                })
            return rows
    except Exception:
        logger.exception("failed to query events needing snapshots")
        return []


def _update_snapshot_status(
    pg_conn: psycopg.Connection,
    event_id: str,
    snapshot_path: str | None,
    snapshot_status: str,
    snapshot_offset_seconds: float | None = None,
    snapshot_fallback_reason: str | None = None,
    error_message: str | None = None,
) -> bool:
    """Update snapshot-related fields on an event row."""
    try:
        with pg_conn.cursor() as cur:
            parts = []
            params: dict = {"event_id": event_id}

            if snapshot_status:
                parts.append(
                    "{media,snapshot_status}")
                params["snap_status"] = json.dumps(snapshot_status)

            if snapshot_offset_seconds is not None:
                parts.append(
                    "{media,snapshot_offset_seconds}")
                params["snap_offset"] = json.dumps(snapshot_offset_seconds)

            if snapshot_fallback_reason:
                parts.append(
                    "{media,snapshot_fallback_reason}")
                params["fallback"] = json.dumps(snapshot_fallback_reason)

            if error_message:
                parts.append(
                    "{media,snapshot_error_message}")
                params["err_msg"] = json.dumps(error_message)

            if not parts:
                return False

            # Build nested jsonb_set chain: jsonb_set(jsonb_set(COALESCE(...), ...), ...)
            path_param_map = {
                "{media,snapshot_status}": "snap_status",
                "{media,snapshot_offset_seconds}": "snap_offset",
                "{media,snapshot_fallback_reason}": "fallback",
                "{media,snapshot_error_message}": "err_msg",
            }
            payload_expr = "COALESCE(payload, '{}'::jsonb)"
            for media_path in parts:
                param_name = path_param_map[media_path]
                pg_path = "{" + ",".join(media_path.strip("{}").split(",")) + "}"
                payload_expr = (
                    "jsonb_set(" + payload_expr
                    + ", '" + pg_path + "'"
                    + ", %(" + param_name + ")s::jsonb)"
                )

            set_parts = ["payload = " + payload_expr]
            if snapshot_path:
                set_parts.append("snapshot_path = %(snap_path)s")
                params["snap_path"] = snapshot_path
            set_parts.append("updated_at = now()")

            sql = (
                "UPDATE events SET "
                + ", ".join(set_parts)
                + " WHERE id = %(event_id)s::uuid"
            )

            cur.execute(sql, params)
            return cur.rowcount is not None and cur.rowcount > 0
    except Exception:
        logger.exception(
            "update_snapshot_status failed event_id=%s sql=%s params=%s",
            event_id, sql, params,
        )
        return False


def _mark_not_required(pg_conn: psycopg.Connection) -> int:
    """Mark events with snapshot_required=false && clean clip as not_required."""
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET payload = jsonb_set(
                        COALESCE(payload, '{}'::jsonb),
                        '{media,snapshot_status}',
                        '"not_required"'::jsonb
                    ),
                    updated_at = now()
                WHERE payload -> 'media' ->> 'clip_status'
                      IN ('ready', 'generated')
                  AND (
                      COALESCE(payload -> 'media' ->> 'snapshot_required', 'false') = 'false'
                      OR (payload -> 'media' ? 'snapshot_required'
                          AND payload -> 'media' ->> 'snapshot_required' = 'false')
                  )
                  AND COALESCE(payload -> 'media' ->> 'snapshot_status', 'not_implemented')
                      NOT IN ('ready', 'not_required')
                """
            )
            return cur.rowcount or 0
    except Exception:
        logger.exception("_mark_not_required failed")
        return 0


def _process_pending_snapshots(
    pg_conn: psycopg.Connection,
    snapshot_output_dir: str,
    default_pre_seconds: float,
) -> int:
    """Generate snapshots for events that have clean clips but no snapshot yet.

    Idempotent: skips events whose snapshot_status is already 'ready' or
    'not_required'.  If snapshot_status is 'failed' but the file exists
    from a prior attempt, it promotes the status to 'ready'.

    Also marks snapshot_required=false events as 'not_required' when clip
    is ready.
    """
    _mark_not_required(pg_conn)

    events = _snapshot_needed(pg_conn)
    logger.info("media_snapshot_queue snapshot_queue_size=%s", len(events))
    if not events:
        return 0

    updated = 0
    for ev in events:
        event_id = ev["event_id"]
        clip_path = ev["clip_path"]
        pre_seconds = ev["pre_seconds"] if ev["pre_seconds"] else default_pre_seconds

        # Idempotency: if snapshot file already exists, just update status
        expected_path = os.path.join(snapshot_output_dir, f"{event_id}.jpg")
        if os.path.isfile(expected_path):
            logger.info(
                "snapshot_already_exists event_id=%s path=%s — promoting to ready",
                event_id, expected_path,
            )
            _update_snapshot_status(
                pg_conn, event_id,
                snapshot_path=expected_path,
                snapshot_status="ready",
                snapshot_offset_seconds=float(pre_seconds),
            )
            updated += 1
            continue

        logger.info(
            "generating_snapshot event_id=%s clip=%s pre_seconds=%.2f",
            event_id, clip_path, pre_seconds,
        )

        snapshot_started = time.monotonic()
        result = generate_snapshot(
            event_id=event_id,
            clip_path=clip_path,
            pre_seconds=pre_seconds,
            output_dir=snapshot_output_dir,
        )
        logger.info(
            "media_snapshot_extracted event_id=%s snapshot_status=%s "
            "extraction_duration_ms=%s",
            event_id,
            result.get("snapshot_status"),
            int((time.monotonic() - snapshot_started) * 1000),
        )

        _update_snapshot_status(
            pg_conn, event_id,
            snapshot_path=result.get("snapshot_path"),
            snapshot_status=result["snapshot_status"],
            snapshot_offset_seconds=result.get("snapshot_offset_seconds"),
            snapshot_fallback_reason=result.get("snapshot_fallback_reason"),
            error_message=result.get("error_message"),
        )
        updated += 1

    return updated


def _annotation_needed(pg_conn: psycopg.Connection) -> list[dict]:
    """Return events with snapshot_status=ready that need annotation.

    Conditions: snapshot_status is 'ready', annotated_snapshot_status is not
    'ready', snapshot_path is non-null.
    """
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, snapshot_path, payload, clip_path
                FROM events
                WHERE payload -> 'media' ->> 'snapshot_status' = 'ready'
                  AND snapshot_path IS NOT NULL
                  AND snapshot_path != ''
                  AND COALESCE(
                        payload -> 'media' ->> 'annotated_snapshot_status',
                        'not_implemented'
                      ) NOT IN ('ready')
                """
            )
            rows = []
            for row in cur.fetchall():
                payload = row[2] or {}
                if isinstance(payload, str):
                    import json as _json
                    payload = _json.loads(payload)
                # Defense-in-depth: skip already-annotated events
                media = payload.get("media", {}) if isinstance(payload, dict) else {}
                ann_status = media.get("annotated_snapshot_status", "not_implemented")
                if ann_status == "ready":
                    continue
                rows.append({
                    "event_id": row[0],
                    "snapshot_path": row[1],
                    "payload": payload,
                    "clip_path": row[3],
                })
            return rows
    except Exception:
        logger.exception("failed to query events needing annotation")
        return []


def _update_annotation_status(
    pg_conn: psycopg.Connection,
    event_id: str,
    annotated_snapshot_path: str | None,
    annotated_snapshot_status: str,
    bbox_overlay_status: str | None = None,
    zone_overlay_status: str | None = None,
    error_message: str | None = None,
) -> bool:
    """Update annotation-related fields on an event row (all stored in payload.media)."""
    try:
        with pg_conn.cursor() as cur:
            parts = []
            params: dict = {"event_id": event_id}

            if annotated_snapshot_status:
                parts.append("{media,annotated_snapshot_status}")
                params["ann_status"] = json.dumps(annotated_snapshot_status)

            if annotated_snapshot_path:
                parts.append("{media,annotated_snapshot_path}")
                params["ann_path"] = json.dumps(annotated_snapshot_path)

            if bbox_overlay_status is not None:
                parts.append("{media,bbox_overlay_status}")
                params["bbox_overlay"] = json.dumps(bbox_overlay_status)

            if zone_overlay_status is not None:
                parts.append("{media,zone_overlay_status}")
                params["zone_overlay"] = json.dumps(zone_overlay_status)

            if error_message:
                parts.append("{media,annotated_snapshot_error_message}")
                params["ann_err"] = json.dumps(error_message)

            if not parts:
                return False

            path_param_map = {
                "{media,annotated_snapshot_status}": "ann_status",
                "{media,annotated_snapshot_path}": "ann_path",
                "{media,bbox_overlay_status}": "bbox_overlay",
                "{media,zone_overlay_status}": "zone_overlay",
                "{media,annotated_snapshot_error_message}": "ann_err",
            }
            payload_expr = "COALESCE(payload, '{}'::jsonb)"
            for media_path in parts:
                param_name = path_param_map[media_path]
                pg_path = "{" + ",".join(media_path.strip("{}").split(",")) + "}"
                payload_expr = (
                    "jsonb_set(" + payload_expr
                    + ", '" + pg_path + "'"
                    + ", %(" + param_name + ")s::jsonb)"
                )

            sql = (
                "UPDATE events SET "
                + "payload = " + payload_expr
                + ", updated_at = now()"
                + " WHERE id = %(event_id)s::uuid"
            )

            cur.execute(sql, params)
            return cur.rowcount is not None and cur.rowcount > 0
    except Exception:
        logger.exception(
            "update_annotation_status failed event_id=%s", event_id,
        )
        return False


def _metadata_has_detections(clip_path: str | None) -> bool:
    """Check if the clip's metadata.json contains any non-empty objects frames.

    Returns False if clip_path is None, the metadata file is missing, or
    every frame has ``"objects": []``.
    """
    if not clip_path:
        return False
    meta_path = os.path.join(os.path.dirname(clip_path), "metadata.json")
    try:
        with open(meta_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if '"objects":[' in line and '"objects":[]' not in line:
                    return True
        return False
    except Exception:
        logger.debug("metadata.json unreadable for %s", clip_path)
        return False


def _process_pending_annotations(
    pg_conn: psycopg.Connection,
    annotated_output_dir: str,
) -> int:
    """Generate annotated snapshots for events with ready raw snapshots.

    Determines bbox trust by checking whether the clip's metadata.json
    contains any non-empty ``metadata.objects`` frames (i.e. real Savant
    detection data).  The bbox is only drawn when the source is trusted.

    Idempotent: skips events whose annotated_snapshot_status is already
    'ready'.  If the annotated file already exists on disk, promotes the
    status to 'ready' without re-generation.
    """
    events = _annotation_needed(pg_conn)
    if not events:
        return 0

    updated = 0
    for ev in events:
        event_id = ev["event_id"]
        raw_snapshot_path = ev["snapshot_path"]
        payload = ev["payload"] or {}
        clip_path = ev.get("clip_path")

        # Determine bbox trust from payload marker (set by Savant event export)
        bbox_trusted = payload.get("bbox_source") == "savant_detection"

        # Idempotency: if annotated file already exists, promote to ready
        expected_path = os.path.join(annotated_output_dir, f"{event_id}.jpg")
        if os.path.isfile(expected_path):
            logger.info(
                "annotated_snapshot_already_exists event_id=%s path=%s — promoting to ready",
                event_id, expected_path,
            )
            _update_annotation_status(
                pg_conn, event_id,
                annotated_snapshot_path=expected_path,
                annotated_snapshot_status="ready",
            )
            updated += 1
            continue

        logger.info(
            "generating_annotated_snapshot event_id=%s bbox_trusted=%s",
            event_id, bbox_trusted,
        )

        result = generate_annotated_snapshot(
            event_id=event_id,
            raw_snapshot_path=raw_snapshot_path,
            payload=payload,
            annotated_output_dir=annotated_output_dir,
            bbox_trusted=bbox_trusted,
        )

        _update_annotation_status(
            pg_conn, event_id,
            annotated_snapshot_path=result.get("annotated_snapshot_path"),
            annotated_snapshot_status=result["annotated_snapshot_status"],
            bbox_overlay_status=result.get("bbox_overlay_status"),
            zone_overlay_status=result.get("zone_overlay_status"),
            error_message=result.get("annotated_snapshot_error_message"),
        )
        updated += 1

    return updated


def connect_postgres(cfg: Config) -> psycopg.Connection:
    conn = psycopg.connect(cfg.database_url, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    logger.info("connected to postgres")
    return conn


def run_worker(cfg: Config, pg_conn: psycopg.Connection) -> None:
    cpu_thread_limit = _apply_materialization_cpu_thread_limit(
        cfg.materialization_cpu_thread_limit
    )
    rolling_cache_poll_interval_s = float(
        getattr(cfg, "rolling_cache_materialization_poll_interval_s", 1.0) or 1.0
    )
    logger.info(
        "media-worker started sink_dir=%s snap_dir=%s ann_dir=%s evidence_dir=%s "
        "sink_stability_checks=%d poll_interval=%ds "
        "default_pre_seconds=%.1f evidence_max_duration_slack_sec=%.1f "
        "state_path=%s sink_scan_max_metadata_files=%d "
        "media_probe_timeout_s=%.1f media_decode_timeout_s=%.1f "
        "materialization_max_active=%d materialization_timeout_s=%.1f "
        "materialization_max_backlog=%d materialization_max_per_poll=%d "
        "materialization_finalizer_workers=%d "
        "materialization_finalizer_max_per_source_per_poll=%d "
        "materialization_finalizer_source_serial=%s "
        "materialization_throttle_sleep_s=%.1f "
        "materialization_throttle_deadline_guard_s=%.1f "
        "materialization_cpu_thread_limit=%d cpu_thread_limit_result=%s "
        "cleanup_replay_sink_output=%s cleanup_statuses=%s "
        "rolling_cache_poll_interval_s=%.1f",
        cfg.sink_output_dir,
        cfg.snapshot_output_dir,
        cfg.annotated_output_dir,
        cfg.evidence_output_dir,
        cfg.midterm_sink_stability_checks,
        cfg.poll_interval_s,
        cfg.default_pre_seconds,
        cfg.evidence_max_duration_slack_sec,
        cfg.media_worker_state_path,
        cfg.sink_scan_max_metadata_files,
        cfg.media_probe_timeout_s,
        cfg.media_decode_timeout_s,
        cfg.materialization_max_active,
        cfg.materialization_timeout_s,
        cfg.materialization_max_backlog,
        cfg.materialization_max_per_poll,
        cfg.materialization_finalizer_workers,
        cfg.materialization_finalizer_max_per_source_per_poll,
        cfg.materialization_finalizer_source_serial,
        cfg.materialization_throttle_sleep_s,
        cfg.materialization_throttle_deadline_guard_s,
        cfg.materialization_cpu_thread_limit,
        cpu_thread_limit,
        cfg.cleanup_replay_sink_output_enabled,
        ",".join(cfg.cleanup_replay_sink_output_statuses),
        rolling_cache_poll_interval_s,
    )

    processed_state_path = _media_worker_state_path(
        cfg.sink_output_dir,
        cfg.media_worker_state_path,
    )
    processed_dirs: set[str] = _load_processed_sink_state(processed_state_path)
    candidate_dirs: dict[str, tuple[int, int]] = {}
    invalid_output_failures: dict[str, int] = {}
    materialization_guard = _MaterializationGuard(cfg.materialization_max_active)
    materialization_pacer = _MaterializationPacer(
        max_per_poll=cfg.materialization_max_per_poll,
        throttle_sleep_s=cfg.materialization_throttle_sleep_s,
        deadline_guard_s=cfg.materialization_throttle_deadline_guard_s,
    )
    rolling_cache_runner = (
        _RollingCacheMaterializationRunner(
            max_workers=cfg.rolling_cache_materialization_workers
        )
        if cfg.rolling_cache_enabled and cfg.rolling_cache_materialization_enabled
        else None
    )

    next_rolling_cache_poll_at = 0.0
    next_general_poll_at = 0.0
    last_scheduler_tick_started_at: float | None = None
    scheduler_tick_sequence = 0
    try:
        while not shutdown_requested:
            tick_started_at = time.monotonic()
            tick_gap_ms = (
                int((tick_started_at - last_scheduler_tick_started_at) * 1000)
                if last_scheduler_tick_started_at is not None
                else None
            )
            last_scheduler_tick_started_at = tick_started_at
            scheduler_tick_sequence += 1
            now_monotonic = tick_started_at
            rolling_cache_due = (
                cfg.rolling_cache_enabled
                and cfg.rolling_cache_materialization_enabled
                and now_monotonic >= next_rolling_cache_poll_at
            )
            general_due = now_monotonic >= next_general_poll_at
            try:
                if rolling_cache_due:
                    next_rolling_cache_poll_at = (
                        now_monotonic + rolling_cache_poll_interval_s
                    )
                    rolling_updates = _process_rolling_cache_tasks(
                        pg_conn,
                        cfg,
                        runner=rolling_cache_runner,
                    )
                    if rolling_updates:
                        logger.info(
                            "media_worker: rolling-cache materialized %d events",
                            rolling_updates,
                        )

                if general_due:
                    next_general_poll_at = now_monotonic + max(0.1, cfg.poll_interval_s)
                    active_sink_output_dir = _active_epoch_sink_output_dir(
                        cfg.sink_output_dir
                    )
                    clip_updates = _process_sink_output(
                        pg_conn,
                        active_sink_output_dir,
                        processed_dirs,
                        evidence_output_dir=cfg.evidence_output_dir,
                        candidate_dirs=candidate_dirs,
                        invalid_output_failures=invalid_output_failures,
                        midterm_sink_stability_checks=cfg.midterm_sink_stability_checks,
                        processed_state_path=processed_state_path,
                        sink_scan_max_metadata_files=cfg.sink_scan_max_metadata_files,
                        materialization_guard=materialization_guard,
                        materialization_pacer=materialization_pacer,
                        materialization_timeout_s=cfg.materialization_timeout_s,
                        materialization_max_backlog=cfg.materialization_max_backlog,
                        materialization_finalizer_workers=(
                            cfg.materialization_finalizer_workers
                        ),
                        materialization_finalizer_max_per_source_per_poll=(
                            cfg.materialization_finalizer_max_per_source_per_poll
                        ),
                        materialization_finalizer_source_serial=(
                            cfg.materialization_finalizer_source_serial
                        ),
                        materialization_database_url=cfg.database_url,
                        evidence_final_root_max_bytes=cfg.evidence_final_root_max_bytes,
                        evidence_incoming_root_max_bytes=cfg.evidence_incoming_root_max_bytes,
                        replay_sink_output_max_bytes=cfg.replay_sink_output_max_bytes,
                        evidence_storage_warning_ratio=cfg.evidence_storage_warning_ratio,
                        evidence_storage_critical_ratio=cfg.evidence_storage_critical_ratio,
                        evidence_storage_hard_ratio=cfg.evidence_storage_hard_ratio,
                        cleanup_replay_sink_output_enabled=(
                            cfg.cleanup_replay_sink_output_enabled
                        ),
                        cleanup_replay_sink_output_statuses=(
                            cfg.cleanup_replay_sink_output_statuses
                        ),
                    )
                    if clip_updates:
                        logger.info("media_worker: clip updated %d events", clip_updates)

                    alias_updates = _reconcile_covered_event_aliases(pg_conn)
                    if alias_updates:
                        logger.info(
                            "media_worker: covered evidence aliases updated %d events",
                            alias_updates,
                        )

                    snap_updates = _process_pending_snapshots(
                        pg_conn,
                        cfg.snapshot_output_dir,
                        cfg.default_pre_seconds,
                    )
                    if snap_updates:
                        logger.info(
                            "media_worker: snapshot updated %d events", snap_updates
                        )

                    ann_updates = _process_pending_annotations(
                        pg_conn, cfg.annotated_output_dir,
                    )
                    if ann_updates:
                        logger.info(
                            "media_worker: annotation updated %d events", ann_updates
                        )
            except Exception:
                logger.exception("media worker loop error")

            tick_duration_ms = int((time.monotonic() - tick_started_at) * 1000)
            permit_snapshot = materialization_guard.snapshot()
            remux_snapshot = (
                rolling_cache_runner.snapshot()
                if rolling_cache_runner is not None
                else {"active": 0, "capacity": 0}
            )
            logger.info(
                "media_scheduler_tick schema_version=phase0-scheduler-v1 "
                "scheduler_mode=legacy sequence=%s tick_duration_ms=%s "
                "tick_gap_ms=%s rolling_due=%s general_due=%s "
                "oldest_ready_age_ms=unavailable "
                "image_lane_depth=unavailable remux_lane_depth=%s "
                "finalizer_lane_depth=unavailable "
                "permit_active=%s permit_limit=%s "
                "db_pool_in_use=unavailable db_pool_limit=unavailable "
                "segment_index_mode=legacy_recursive_scan "
                "segment_index_hits=unavailable segment_index_misses=unavailable "
                "segment_index_fallback_scans=unavailable",
                scheduler_tick_sequence,
                tick_duration_ms,
                tick_gap_ms if tick_gap_ms is not None else "unavailable",
                rolling_cache_due,
                general_due,
                remux_snapshot["active"],
                permit_snapshot["active"],
                permit_snapshot["max_active"],
            )

            now_monotonic = time.monotonic()
            next_due_at = next_general_poll_at
            if cfg.rolling_cache_enabled and cfg.rolling_cache_materialization_enabled:
                next_due_at = min(next_due_at, next_rolling_cache_poll_at)
            sleep_s = max(0.1, min(1.0, next_due_at - now_monotonic))
            time.sleep(sleep_s)
    finally:
        if rolling_cache_runner is not None:
            rolling_cache_runner.close()

    logger.info("media-worker stopped (processed %d dirs)", len(processed_dirs))
