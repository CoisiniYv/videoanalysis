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
from ast import literal_eval
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from app.annotated_snapshot import generate_annotated_snapshot
from app.clip_sanitizer import sanitize_raw_clip
from app.config import Config, load_config
from app.continuous_annotation import write_continuous_annotation_bundle
from app.frame_cache_sidecar_writer import write_frame_cache_identity_sidecar
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
DEFAULT_SINK_SCAN_MAX_METADATA_FILES = 2000
DEFAULT_MEDIA_PROBE_TIMEOUT_S = 30.0
DEFAULT_MEDIA_DECODE_TIMEOUT_S = 120.0
DEFAULT_INVALID_SINK_OUTPUT_MAX_RETRIES = 3
DEFAULT_CLEANUP_REPLAY_SINK_OUTPUT_STATUSES = ("ready",)
INVALID_SINK_OUTPUT_MARKER = ".media-worker.invalid.json"
ANNOTATION_STATUS_UNAVAILABLE = "unavailable"
BUNDLE_STATUS_DURATION_GUARD_FAILED = "duration_guard_failed"
BUNDLE_STATUS_GENERATED_ANNOTATION_FAILED = "generated_annotation_failed"
POST_SAVANT_FINALIZER_ENV = "EVIDENCE_TOPOLOGY"
SNAPSHOT_ELIGIBLE_CLIP_STATUSES = ("ready", "generated")
SNAPSHOT_INELIGIBLE_CLIP_STATUSES = (
    "generated_corrupt",
    BUNDLE_STATUS_DURATION_GUARD_FAILED,
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
        children = sorted(sink_path.iterdir(), key=lambda path: path.name)
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

    if _probe_video_duration_seconds(video_file) is None:
        return False, "video_duration_unavailable"
    return True, "ready"


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
    if clip_status in {"ready", "generated"}:
        return "ready"
    if clip_status in {
        BUNDLE_STATUS_DURATION_GUARD_FAILED,
        BUNDLE_STATUS_GENERATED_ANNOTATION_FAILED,
        "generated_corrupt",
        "generated_unverified",
        "failed",
    }:
        return "failed"
    if clip_status == "replay_job_created":
        return "replaying"
    return "finalizing"


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
                                'evidence_state_updated_at', now()
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


def _probe_metrics_snapshot() -> dict[str, int]:
    return dict(_PROBE_METRICS)


def _probe_metrics_delta(before: dict[str, int]) -> dict[str, int]:
    return {
        key: int(_PROBE_METRICS.get(key, 0)) - int(before.get(key, 0))
        for key in _PROBE_METRICS
    }


def _record_probe_metric(tool: str, duration_s: float) -> None:
    duration_ms = int(max(duration_s, 0.0) * 1000)
    if tool == "ffprobe":
        _PROBE_METRICS["ffprobe_invocation_count"] += 1
        _PROBE_METRICS["ffprobe_duration_ms"] += duration_ms
    elif tool == "imageio_ffmpeg":
        _PROBE_METRICS["imageio_ffmpeg_fallback_count"] += 1
        _PROBE_METRICS["imageio_ffmpeg_fallback_duration_ms"] += duration_ms
    else:
        _PROBE_METRICS["ffmpeg_invocation_count"] += 1
        _PROBE_METRICS["ffmpeg_duration_ms"] += duration_ms


def _parse_simple_camera_yaml(path: str) -> dict:
    """Parse the limited camera YAML shape used by POC/dev configs.

    This fallback keeps media-worker independent from PyYAML in no-build
    dev images. It intentionally supports only the camera/zones/points fields
    needed for ROI annotation lookup.
    """
    cameras: dict[str, dict] = {}
    current_camera: str | None = None
    current_zone: str | None = None
    in_zones = False
    in_points = False

    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()

        if indent == 2 and stripped.endswith(":"):
            current_camera = stripped[:-1]
            cameras[current_camera] = {"zones": {}}
            current_zone = None
            in_zones = False
            in_points = False
            continue

        if current_camera is None:
            continue

        camera = cameras[current_camera]
        if indent == 4:
            current_zone = None
            in_points = False
            if stripped == "zones:":
                in_zones = True
                continue
            in_zones = False
            if stripped.startswith("source_id:"):
                camera["source_id"] = stripped.split(":", 1)[1].strip()
            continue

        if in_zones and indent == 6 and stripped.endswith(":"):
            current_zone = stripped[:-1]
            camera.setdefault("zones", {})[current_zone] = {}
            in_points = False
            continue

        if not in_zones or current_zone is None:
            continue

        zone = camera.setdefault("zones", {})[current_zone]
        if indent == 8:
            if stripped == "points:":
                in_points = True
                zone.setdefault("points", [])
            elif stripped.startswith("type:"):
                zone["type"] = stripped.split(":", 1)[1].strip()
            continue

        if in_points and indent >= 10 and stripped.startswith("- "):
            try:
                point = literal_eval(stripped[2:].strip())
            except (SyntaxError, ValueError):
                continue
            if isinstance(point, (list, tuple)) and len(point) == 2:
                zone.setdefault("points", []).append([float(point[0]), float(point[1])])

    return {"cameras": cameras}


def _load_camera_config(path: str | None) -> dict:
    if not path:
        return {}
    config_path = Path(path)
    if not config_path.is_file():
        logger.warning("camera config not found for ROI lookup: %s", path)
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except ImportError:
        return _parse_simple_camera_yaml(str(config_path))
    except Exception:
        logger.exception("failed to load camera config for ROI lookup: %s", path)
        return {}


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


def _lookup_roi_from_camera_config(
    *,
    camera_id: str,
    source_id: str,
    zone_id: str,
    cameras_config_path: str | None,
) -> tuple[list | None, str]:
    if not cameras_config_path:
        return None, "not_configured"
    config = _load_camera_config(cameras_config_path)
    cameras = config.get("cameras", {}) if isinstance(config, dict) else {}
    if not isinstance(cameras, dict):
        return None, "not_found"

    camera = cameras.get(camera_id)
    if not isinstance(camera, dict):
        for candidate in cameras.values():
            if (
                isinstance(candidate, dict)
                and source_id
                and candidate.get("source_id") == source_id
            ):
                camera = candidate
                break
    if not isinstance(camera, dict):
        return None, "not_found"

    zones = camera.get("zones", {})
    if not isinstance(zones, dict):
        return None, "not_found"
    zone = zones.get(zone_id) if zone_id else None
    if not isinstance(zone, dict):
        return None, "not_found"
    points = zone.get("points")
    if isinstance(points, list) and points:
        return points, "found"
    return None, "not_found"


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


def _normalise_person_bbox(
    bbox: object,
    *,
    confidence: float,
    source: str,
    source_format: str | None = None,
) -> dict | None:
    if isinstance(bbox, dict):
        if {"x", "y", "width", "height"}.issubset(bbox):
            x = _to_float(bbox.get("x"))
            y = _to_float(bbox.get("y"))
            width = _to_float(bbox.get("width"))
            height = _to_float(bbox.get("height"))
            if None in (x, y, width, height):
                return None
            return {
                "type": "person_bbox",
                "bbox_format": "xyxy",
                "bbox": [x, y, x + width, y + height],
                "bbox_source_format": "xywh",
                "bbox_raw": bbox,
                "confidence": confidence,
                "source": source,
            }
        if {"x1", "y1", "x2", "y2"}.issubset(bbox):
            x1 = _to_float(bbox.get("x1"))
            y1 = _to_float(bbox.get("y1"))
            x2 = _to_float(bbox.get("x2"))
            y2 = _to_float(bbox.get("y2"))
            if None in (x1, y1, x2, y2):
                return None
            return {
                "type": "person_bbox",
                "bbox_format": "xyxy",
                "bbox": [x1, y1, x2, y2],
                "bbox_source_format": "xyxy",
                "bbox_raw": bbox,
                "confidence": confidence,
                "source": source,
            }
        return None

    if isinstance(bbox, list) and len(bbox) == 4:
        values = [_to_float(item) for item in bbox]
        if any(item is None for item in values):
            return None
        numbers = [float(item) for item in values if item is not None]
        if source_format == "xywh":
            x, y, width, height = numbers
            return {
                "type": "person_bbox",
                "bbox_format": "xyxy",
                "bbox": [x, y, x + width, y + height],
                "bbox_source_format": "xywh",
                "bbox_raw": bbox,
                "confidence": confidence,
                "source": source,
            }
        if source_format == "xyxy":
            return {
                "type": "person_bbox",
                "bbox_format": "xyxy",
                "bbox": numbers,
                "bbox_source_format": "xyxy",
                "bbox_raw": bbox,
                "confidence": confidence,
                "source": source,
            }
        return {
            "type": "person_bbox",
            "bbox_format": "unknown",
            "bbox": numbers,
            "bbox_source_format": "list_unknown",
            "bbox_raw": bbox,
            "confidence": confidence,
            "source": source,
        }

    return None


def _event_annotation_from_context(
    context: dict,
    *,
    cameras_config_path: str | None = None,
) -> dict:
    """Build the midterm event-frame annotation document from event context."""
    payload = context["payload"]
    media = payload.get("media", {}) if isinstance(payload, dict) else {}
    if not isinstance(media, dict):
        media = {}

    overlays = []
    missing = []
    bbox = payload.get("person_bbox")
    bbox_source = "event.payload.person_bbox"
    if bbox is None:
        bbox = payload.get("bbox")
        bbox_source = "event.payload.bbox"
    if bbox is None:
        bbox = media.get("person_bbox")
        bbox_source = "event.payload.media.person_bbox"
    if bbox is None:
        bbox = media.get("bbox")
        bbox_source = "event.payload.media.bbox"
    bbox_source_format = (
        payload.get("bbox_format")
        or payload.get("person_bbox_format")
        or media.get("bbox_format")
        or media.get("person_bbox_format")
    )
    if bbox is not None:
        overlay = _normalise_person_bbox(
            bbox,
            confidence=float(context["confidence"] or 0.0),
            source=bbox_source,
            source_format=bbox_source_format,
        )
        if overlay:
            overlays.append(overlay)
        else:
            missing.append("person_bbox")
    else:
        missing.append("person_bbox")

    roi = payload.get("roi_polygon")
    roi_source = "event.payload.roi_polygon"
    if roi is None:
        roi = payload.get("zone_polygon")
        roi_source = "event.payload.zone_polygon"
    if roi is None:
        roi = media.get("roi_polygon")
        roi_source = "event.payload.media.roi_polygon"
    if roi is None:
        roi = media.get("zone_polygon")
        roi_source = "event.payload.media.zone_polygon"
    zone_id = (
        payload.get("zone_id")
        or payload.get("zone")
        or media.get("zone_id")
        or media.get("zone")
    )
    roi_lookup_status = "payload" if roi else "not_found"
    if roi is None:
        roi, roi_lookup_status = _lookup_roi_from_camera_config(
            camera_id=str(context.get("camera_id", "")),
            source_id=str(context.get("source_id", "")),
            zone_id=str(zone_id or ""),
            cameras_config_path=cameras_config_path,
        )
        if roi is not None:
            roi_source = "camera_config"
    if roi:
        overlays.append({
            "type": "roi_polygon",
            "zone_id": zone_id,
            "points": roi,
            "source": roi_source,
        })
    else:
        missing.append("roi_polygon")

    return {
        "schema_version": "1.0",
        "annotation_type": "event_frame",
        "annotation_status": "partial" if missing else "complete",
        "missing": missing,
        "roi_lookup_status": roi_lookup_status,
        "event": {
            "event_id": context["event_id"],
            "source_event_id": context["source_event_id"],
            "event_type": context["event_type"],
            "camera_id": context["camera_id"],
            "source_id": context["source_id"],
            "track_id": context["track_id"],
            "event_ts_ms": context["event_ts_ms"],
            "frame_uuid": context["frame_uuid"],
            "keyframe_uuid": context["keyframe_uuid"],
            "previous_keyframe_uuid": context["previous_keyframe_uuid"],
        },
        "overlays": overlays,
    }


def _load_event_annotation(pg_conn: psycopg.Connection, event_id: str) -> dict:
    """Build the midterm event-frame annotation document from the event row."""
    return _event_annotation_from_context(
        _load_event_context(pg_conn, event_id),
        cameras_config_path=os.getenv("CAMERAS_CONFIG_PATH"),
    )


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


def _update_summary_with_bundle_validation(
    summary_path: Path,
    business_metadata: dict,
) -> dict:
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if not isinstance(summary, dict):
            summary = {}
    except Exception:
        logger.exception("failed to load annotation summary for validation merge")
        summary = {}

    media = business_metadata.get("media", {})
    status = business_metadata.get("status", {})
    clip_validation = (
        media.get("clip_validation", {}) if isinstance(media, dict) else {}
    )
    if not isinstance(clip_validation, dict):
        clip_validation = {}

    summary.update(
        {
            "raw_clip_duration": media.get("raw_clip_duration"),
            "expected_duration_seconds": media.get("expected_duration_seconds"),
            "max_allowed_duration_seconds": clip_validation.get(
                "max_allowed_duration_seconds"
            ),
            "duration_guard_status": clip_validation.get("duration_guard_status"),
            "duration_guard_failed": bool(
                clip_validation.get("duration_guard_failed")
            ),
            "duration_guard_reason": clip_validation.get("duration_guard_reason", ""),
            "duration_guard_slack_seconds": clip_validation.get(
                "duration_guard_slack_seconds"
            ),
            "clip_status": status.get("clip_status"),
            "decode_error_count": clip_validation.get("decode_error_count", 0),
            "decode_error_sample": clip_validation.get("decode_error_sample", []),
            "raw_clip_sanitize_method": media.get("raw_clip_sanitize_method", ""),
            "raw_clip_sanitize_decode_ok": media.get(
                "raw_clip_sanitize_decode_ok"
            ),
            "raw_clip_sanitize_decode_error_count": media.get(
                "raw_clip_sanitize_decode_error_count", 0
            ),
            "raw_clip_sanitize_fallback_used": bool(
                media.get("raw_clip_sanitize_fallback_used")
            ),
            "raw_clip_sanitize_error": media.get("raw_clip_sanitize_error", ""),
        }
    )
    _atomic_write_json(summary_path, summary)
    return summary


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
    max_allowed = float(expected or 0.0) + slack if expected > 0 else None
    if actual is None or actual <= 0:
        return {
            "duration_guard_status": "unavailable",
            "duration_guard_failed": False,
            "max_allowed_duration_seconds": max_allowed,
            "duration_guard_reason": "raw_clip_duration_unavailable",
            "duration_guard_slack_seconds": slack,
        }
    if expected <= 0 or max_allowed is None:
        return {
            "duration_guard_status": "not_applicable",
            "duration_guard_failed": False,
            "max_allowed_duration_seconds": max_allowed,
            "duration_guard_reason": "expected_duration_unavailable",
            "duration_guard_slack_seconds": slack,
        }
    failed = actual > max_allowed
    return {
        "duration_guard_status": "failed" if failed else "passed",
        "duration_guard_failed": failed,
        "max_allowed_duration_seconds": round(max_allowed, 3),
        "duration_guard_reason": (
            "raw_clip_duration_exceeds_expected_plus_slack" if failed else ""
        ),
        "duration_guard_slack_seconds": slack,
    }


def _duration_ok(
    actual: float | None,
    expected: float,
    slack_seconds: float | None = None,
) -> bool:
    if actual is None or actual <= 0:
        return False
    if expected <= 0:
        return actual > 0
    guard = _duration_guard(actual, expected, slack_seconds)
    lower_bound = max(0.0, expected * 0.8)
    return actual >= lower_bound and not guard["duration_guard_failed"]


def _clip_status_from_validation(clip_validation: dict) -> str:
    if clip_validation.get("duration_guard_failed") is True:
        return BUNDLE_STATUS_DURATION_GUARD_FAILED
    if clip_validation.get("annotation_generation_failed") is True:
        return BUNDLE_STATUS_GENERATED_ANNOTATION_FAILED
    if clip_validation.get("decode_error_count", 0) > 0:
        return "generated_corrupt"
    if clip_validation.get("ok") is True:
        return "ready"
    if clip_validation.get("ok") is False:
        return "generated_corrupt"
    return "generated_unverified"


def _stop_condition_mode(stop_condition: dict) -> str:
    if "ts_delta_sec" in stop_condition:
        return "ts_delta_sec"
    if "frame_count" in stop_condition:
        return "frame_count_fallback"
    return "unknown"


def _build_business_metadata(
    *,
    event_context: dict,
    replay_job_id: str,
    replay_job_request: dict,
    sink_metadata_path: str,
    sink_video_path: str,
    sink_output_dir: str,
    raw_clip_path: str,
    event_annotation_path: str,
    annotations_jsonl_path: str = "",
    summary_json_path: str = "",
    annotation_summary: dict | None = None,
    sanitize_info: dict | None = None,
) -> dict:
    payload = event_context.get("payload", {})
    media = payload.get("media", {}) if isinstance(payload, dict) else {}
    if not isinstance(media, dict):
        media = {}
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
    raw_clip_duration = (
        _probe_video_duration_seconds(raw_clip_path) if raw_clip_path else None
    )
    duration_probe_status = "ok" if raw_clip_duration is not None else "failed"
    expected_duration_seconds = _expected_clip_seconds(
        stop_condition,
        configuration,
        offset.get("seconds", 0),
    )
    decode_probe = _probe_clip_decode(raw_clip_path) if raw_clip_path else {
        "decode_error_count": 0,
        "decode_error_sample": [],
        "decode_ok": None,
        "probe_tool": None,
        "probe_error": "clip file not found",
    }
    sanitize_info = sanitize_info or {}
    duration_guard = _duration_guard(raw_clip_duration, expected_duration_seconds)
    duration_ok = _duration_ok(
        raw_clip_duration,
        expected_duration_seconds,
        duration_guard.get("duration_guard_slack_seconds"),
    )
    annotation_summary = annotation_summary or {}
    annotation_generation_failed = (
        annotation_summary.get("annotation_generation_failed") is True
        or annotation_summary.get("annotation_status") == ANNOTATION_STATUS_UNAVAILABLE
    )
    if decode_probe.get("decode_ok") is None or raw_clip_duration is None:
        validation_ok = None
    else:
        validation_ok = (
            bool(decode_probe.get("decode_ok"))
            and duration_ok
            and not annotation_generation_failed
        )
    clip_validation = {
        "ok": validation_ok,
        "decode_error_count": decode_probe.get("decode_error_count", 0),
        "decode_error_sample": decode_probe.get("decode_error_sample", []),
        "duration_ok": duration_ok,
        **duration_guard,
        "annotation_generation_failed": annotation_generation_failed,
        "probe_tool": decode_probe.get("probe_tool"),
        "probe_error": decode_probe.get("probe_error", ""),
    }
    clip_status = _clip_status_from_validation(clip_validation)
    event_created_at = _json_isoformat(event_context.get("created_at"))

    return {
        "schema_version": "1.0",
        "project_version": _evidence_version("midterm"),
        **_legacy_metadata_fields("midterm"),
        "run_id": os.getenv("EVIDENCE_RUN_ID", ""),
        "evidence_type": "security_event_replay_clip",
        "recording_strategy": "savant_replay",
        "input": {
            "input_type": os.getenv("EVIDENCE_INPUT_TYPE", ""),
            "input_uri": os.getenv("EVIDENCE_INPUT_URI", ""),
            "local_file_used": _env_bool("EVIDENCE_LOCAL_FILE_USED"),
            "test_video_used": _env_bool("EVIDENCE_TEST_VIDEO_USED"),
            "source_extraction_fallback": _env_bool(
                "EVIDENCE_SOURCE_EXTRACTION_FALLBACK"
            ),
            "second_rtsp_pull": _env_bool("EVIDENCE_SECOND_RTSP_PULL"),
        },
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
        },
        "replay": {
            "replay_job_id": replay_job_id,
            "anchor_keyframe_uuid": replay_job_request.get("anchor_keyframe", ""),
            "offset_seconds": offset.get("seconds", 0),
            "stop_condition": stop_condition,
            "stop_condition_mode": _stop_condition_mode(stop_condition),
            "fallback_reason": replay_job_request.get("fallback_reason", ""),
            "stored_stream_id": configuration.get("stored_stream_id", ""),
            "resulting_stream_id": configuration.get("resulting_stream_id", ""),
        },
        "media": {
            "sink_output_dir": sink_output_dir,
            "sink_metadata_path": sink_metadata_path,
            "sink_video_path": sink_video_path,
            "raw_clip_path": raw_clip_path,
            "annotated_clip_path": None,
            "annotated_clip_status": "not_generated",
            "event_annotation_path": event_annotation_path,
            "raw_clip_size": raw_clip_size,
            "raw_clip_duration": raw_clip_duration,
            "expected_duration_seconds": round(expected_duration_seconds, 3),
            "duration_probe_status": duration_probe_status,
            "raw_clip_sanitize_method": sanitize_info.get("method", ""),
            "raw_clip_sanitize_decode_ok": sanitize_info.get("decode_ok"),
            "raw_clip_sanitize_decode_error_count": sanitize_info.get(
                "decode_error_count", 0
            ),
            "raw_clip_sanitize_decode_error_sample": sanitize_info.get(
                "decode_error_sample", []
            ),
            "raw_clip_sanitize_fallback_used": bool(
                sanitize_info.get("fallback_used", False)
            ),
            "raw_clip_sanitize_error": sanitize_info.get("sanitize_error", ""),
            "clip_validation": clip_validation,
        },
        "annotations": {
            "annotations_jsonl_path": annotations_jsonl_path,
            "summary_json_path": summary_json_path,
            "annotation_status": annotation_summary.get("annotation_status"),
            "annotation_lines": annotation_summary.get("annotation_lines"),
            "annotation_empty_reason": annotation_summary.get(
                "annotation_empty_reason"
            ),
            "annotation_unavailable_reason": annotation_summary.get(
                "annotation_unavailable_reason"
            ),
            "overlay_available": bool(annotation_summary.get("overlay_available")),
            "frontend_overlay_required": bool(
                annotation_summary.get("frontend_overlay_required")
            ),
            "annotation_mode": "continuous_jsonl",
        },
        "status": {
            "clip_status": clip_status,
        },
        "limitations": [
            "single-event evidence POC",
            "not incident coalescing",
            "not continuous recording",
            "no annotated_clip generated",
        ],
    }


def _finalize_midterm_evidence_bundle(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    meta_dir: str,
    video_file: str,
    metadata_file: str,
    evidence_output_dir: str,
) -> dict:
    """Copy Replay sink output into the midterm raw evidence bundle."""
    evidence_dir = Path(evidence_output_dir) / event_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    raw_clip = evidence_dir / f"raw_clip{Path(video_file).suffix}"
    metadata_out = evidence_dir / "metadata.json"
    sink_metadata_out = evidence_dir / "sink_metadata.json"
    annotation_out = evidence_dir / "event_annotation.json"
    annotations_jsonl_out = evidence_dir / "annotations.jsonl"
    summary_out = evidence_dir / "summary.json"

    if not raw_clip.exists():
        sanitize_info = sanitize_raw_clip(video_file, str(raw_clip))
        logger.info(
            "raw_clip_sanitized event_id=%s source=%s raw_clip=%s method=%s "
            "decode_ok=%s decode_errors=%s fallback_used=%s sanitize_error=%s",
            event_id,
            video_file,
            raw_clip,
            sanitize_info.get("method"),
            sanitize_info.get("decode_ok"),
            sanitize_info.get("decode_error_count"),
            sanitize_info.get("fallback_used"),
            sanitize_info.get("sanitize_error", ""),
        )
    else:
        sanitize_info = {
            "method": "existing",
            "decode_ok": None,
            "decode_error_count": 0,
            "decode_error_sample": [],
            "fallback_used": False,
            "sanitize_error": "raw_clip already existed; sanitizer skipped",
        }
    shutil.copy2(metadata_file, sink_metadata_out)

    event_context = _load_event_context(pg_conn, event_id)
    annotation = _event_annotation_from_context(
        event_context,
        cameras_config_path=os.getenv("CAMERAS_CONFIG_PATH"),
    )
    with open(annotation_out, "w") as f:
        json.dump(annotation, f, ensure_ascii=False, indent=2)
        f.write("\n")

    annotation_summary = write_continuous_annotation_bundle(
        pg_conn,
        event_context,
        annotations_path=str(annotations_jsonl_out),
        summary_path=str(summary_out),
        replay_metadata_path=str(sink_metadata_out),
    )
    logger.info(
        "continuous_annotations_written event_id=%s lines=%s faces=%s matched=%s",
        event_id,
        annotation_summary.get("annotation_lines", 0),
        annotation_summary.get("face_objects", 0),
        annotation_summary.get("matched_objects", 0),
    )

    payload = event_context.get("payload", {})
    media = payload.get("media", {}) if isinstance(payload, dict) else {}
    if not isinstance(media, dict):
        media = {}
    sink_metadata = _load_sink_metadata_file(metadata_file)
    replay_job_id = (
        media.get("replay_job_id")
        or sink_metadata.get("job_id")
        or sink_metadata.get("new_job")
        or ""
    )
    replay_job_request = media.get("replay_job_request") or {}
    if not isinstance(replay_job_request, dict):
        replay_job_request = {}
    business_metadata = _build_business_metadata(
        event_context=event_context,
        replay_job_id=replay_job_id,
        replay_job_request=replay_job_request,
        sink_metadata_path=str(sink_metadata_out),
        sink_video_path=video_file,
        sink_output_dir=meta_dir,
        raw_clip_path=str(raw_clip),
        event_annotation_path=str(annotation_out),
        annotations_jsonl_path=str(annotations_jsonl_out),
        summary_json_path=str(summary_out),
        annotation_summary=annotation_summary,
        sanitize_info=sanitize_info,
    )
    annotation_summary = _update_summary_with_bundle_validation(
        summary_out,
        business_metadata,
    )
    _atomic_write_json(metadata_out, business_metadata)
    annotations_meta = business_metadata.get("annotations", {})
    clip_validation = business_metadata.get("media", {}).get("clip_validation", {})

    return {
        "evidence_dir": str(evidence_dir),
        "raw_clip": str(raw_clip),
        "metadata": str(metadata_out),
        "sink_metadata": str(sink_metadata_out),
        "event_annotation": str(annotation_out),
        "annotations_jsonl": str(annotations_jsonl_out),
        "summary": str(summary_out),
        "sink_output_path": meta_dir,
        "clip_status": business_metadata.get("status", {}).get(
            "clip_status", "generated_unverified"
        ),
        "annotation_status": annotations_meta.get("annotation_status"),
        "annotation_lines": annotations_meta.get("annotation_lines"),
        "annotation_empty_reason": annotations_meta.get("annotation_empty_reason"),
        "annotation_unavailable_reason": annotations_meta.get(
            "annotation_unavailable_reason"
        ),
        "overlay_available": annotations_meta.get("overlay_available"),
        "frontend_overlay_required": annotations_meta.get(
            "frontend_overlay_required"
        ),
        "duration_guard_status": clip_validation.get("duration_guard_status"),
        "duration_guard_failed": clip_validation.get("duration_guard_failed"),
        "max_allowed_duration_seconds": clip_validation.get(
            "max_allowed_duration_seconds"
        ),
        "raw_clip_sanitize_method": sanitize_info.get("method", ""),
        "raw_clip_sanitize_decode_ok": sanitize_info.get("decode_ok"),
        "raw_clip_sanitize_decode_error_count": sanitize_info.get(
            "decode_error_count", 0
        ),
        "raw_clip_sanitize_fallback_used": bool(
            sanitize_info.get("fallback_used", False)
        ),
        "raw_clip_sanitize_error": sanitize_info.get("sanitize_error", ""),
    }


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
    if status in {"complete", "partial"}:
        return "generated_unverified"
    return BUNDLE_STATUS_GENERATED_ANNOTATION_FAILED


def _path_for_metadata(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _requested_duration_from_time_window(time_window: dict | None) -> float | None:
    time_window = time_window or {}
    requested = _to_float(time_window.get("requested_duration_s"))
    if requested is not None and requested > 0:
        return requested
    requested_start_pts = _to_int(time_window.get("requested_start_pts"))
    requested_end_pts = _to_int(time_window.get("requested_end_pts"))
    if requested_start_pts is None or requested_end_pts is None:
        return None
    if requested_end_pts <= requested_start_pts:
        return None
    return (requested_end_pts - requested_start_pts) / 1_000_000_000.0


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
            "max_allowed_duration_seconds": duration_guard.get(
                "max_allowed_duration_seconds"
            ),
        }
    )
    return summary


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
        summary["duration_guard_reason"] = (
            duration_guard.get("duration_guard_reason") or reason
        )
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
        "annotation_source": "frame_annotation_cache",
        "annotation_status": "complete" if production_ready else sidecar_summary.get("annotation_status", "partial"),
        "production_ready": production_ready,
        "canonical_clip": production_ready,
        "visual_binding_status": "verified" if production_ready else "unverified",
        "visual_binding_reason": "frame_annotation_cache_aligned_to_replay_metadata" if production_ready else sidecar_summary.get("visual_binding_reason", "frame_annotation_cache_partial"),
        "visual_evidence_status": "verified" if production_ready else "unverified",
        "evidence_visual_status": "verified" if production_ready else "unverified",
        "legacy_fallback_allowed": False,
        "legacy_used_for_visual_binding": False,
        "fallback_used": False,
        "allow_db_annotation_fallback": False,
        "allow_legacy_annotation_fallback": False,
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
            "legacy_used_for_visual_binding": bool(
                summary.get("legacy_used_for_visual_binding")
            ),
            "legacy_fallback_allowed": bool(summary.get("legacy_fallback_allowed")),
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
) -> dict:
    """Package post-Savant sink output as a production evidence bundle."""
    finalize_started = time.monotonic()
    probe_before = _probe_metrics_snapshot()
    metadata_rows_loaded = 0
    decoded_frame_count_duration_ms = 0
    event_context = _load_event_context(pg_conn, event_id)
    payload = event_context.get("payload", {})
    media = payload.get("media", {}) if isinstance(payload, dict) else {}
    if not isinstance(media, dict):
        media = {}
    sink_metadata = _load_sink_metadata_file(metadata_file)
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
    frame_cache_video_crop: dict = {
        "method": "copy",
        "crop_video_to_time_window": False,
        "source_video_path": source_video,
    }
    raw_clip_available = True
    metadata_has_objects = any(
        bool(row.get("objects"))
        for row in source_metadata_rows
        if isinstance(row, dict) and _is_sink_video_frame(row)
    )
    if (
        metadata_has_objects
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
        _env_bool("FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED", default=True)
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
            shutil.copy2(metadata_file, sink_metadata_path)
            sink_metadata_rows_for_guard = source_metadata_rows
    else:
        if not raw_clip_path.exists():
            shutil.copy2(source_video, raw_clip_path)
        shutil.copy2(metadata_file, sink_metadata_path)
        sink_metadata_rows_for_guard = source_metadata_rows
    sink_metadata_rows = (
        []
        if metadata_has_objects
        else list(sink_metadata_rows_for_guard or load_native_metadata(sink_metadata_path))
    )
    decoded_frame_count = None
    if not metadata_has_objects:
        decoded_started = time.monotonic()
        decoded_frame_count = (
            read_decoded_video_frame_count(Path(raw_clip_path))
            if raw_clip_available and raw_clip_path.is_file()
            else 0
        )
        decoded_frame_count_duration_ms = int(
            (time.monotonic() - decoded_started) * 1000
        )

    if metadata_has_objects:
        builder_event_metadata = {
            **_uuid_first_anchor_metadata(
                replay_labels=replay_labels,
                replay_job_request=replay_job_request,
                time_window=frame_cache_time_window,
            ),
            "replay_source_kind": replay_labels.get("replay_source_kind"),
            "requested_start_pts": replay_labels.get("requested_start_pts"),
            "original_requested_start_pts": replay_labels.get(
                "original_requested_start_pts"
            ),
            "effective_start_pts": replay_labels.get("effective_start_pts"),
            "requested_end_pts": replay_labels.get("requested_end_pts"),
            "event_frame_pts": replay_labels.get("event_frame_pts"),
            "pre_window_truncated": replay_labels.get("pre_window_truncated"),
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
            "allow_db_annotation_fallback": (
                replay_labels.get("allow_db_annotation_fallback") == "true"
            ),
            "allow_legacy_annotation_fallback": (
                replay_labels.get("allow_legacy_annotation_fallback") == "true"
            ),
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
            crop_video_to_time_window=bool(
                frame_cache_time_window.get("time_domain_crop_applied")
            ),
            event_metadata=builder_event_metadata,
            video_integrity_required=False,
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
        raw_clip_duration = (
            _probe_video_duration_seconds(str(raw_clip_path))
            if raw_clip_available
            else None
        )
        sidecar_summary, sidecar_result = write_frame_cache_identity_sidecar(
            event=_event_for_frame_cache_sidecar(event_context),
            evidence_dir=str(output_dir),
            raw_clip_path=str(raw_clip_path) if raw_clip_available else None,
            metadata_path=str(sink_metadata_path),
            redis_client=None,
            config=sidecar_config,
            final_clip_context={
                "raw_clip_path": str(raw_clip_path) if raw_clip_available else None,
                "sink_metadata_path": str(sink_metadata_path),
                "raw_clip_duration": raw_clip_duration,
                "expected_event_t_s": frame_cache_time_window.get("expected_event_t_s"),
                "event_projected_t_s": None,
                "event_pts_inside_clip": None,
                "event_position_ratio": None,
            },
        )
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
    if metadata_has_objects:
        raw_clip_duration = None
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
    _merge_sink_window_guard(result.summary, sink_window_guard)
    epoch_guard = _runtime_epoch_guard(
        event_context=event_context,
        replay_labels=replay_labels,
        sink_metadata=sink_metadata,
        meta_dir=meta_dir,
    )
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
    result.summary["media_worker_perf"] = {
        "finalization_duration_ms": int((time.monotonic() - finalize_started) * 1000),
        "metadata_rows_loaded": metadata_rows_loaded,
        "sink_metadata_rows_for_guard": len(sink_window_rows),
        "decoded_frame_count_duration_ms": decoded_frame_count_duration_ms,
        **_probe_metrics_delta(probe_before),
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
        "event_annotation": "",
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
        "legacy_used_for_visual_binding": bool(
            summary.get("legacy_used_for_visual_binding")
        ),
        "person_count": int(object_counts.get("person") or 0),
        "face_count": int(object_counts.get("face") or 0),
        "known_face_count": int(object_counts.get("known_face") or 0),
    }


def _process_sink_output(
    pg_conn: psycopg.Connection,
    sink_dir: str,
    processed_dirs: set[str],
    *,
    evidence_output_dir: str | None = None,
    midterm_raw_clip_finalizer_enabled: bool = False,
    candidate_dirs: dict[str, tuple[int, int]] | None = None,
    invalid_output_failures: dict[str, int] | None = None,
    midterm_sink_stability_checks: int = 2,
    processed_state_path: str | Path | None = None,
    sink_scan_max_metadata_files: int | None = None,
    cleanup_replay_sink_output_enabled: bool = False,
    cleanup_replay_sink_output_statuses: tuple[str, ...] = DEFAULT_CLEANUP_REPLAY_SINK_OUTPUT_STATUSES,
) -> int:
    """Process new sink outputs and update events table. Returns count of updates."""
    updated = 0
    if processed_state_path is not None:
        processed_dirs.update(_load_processed_sink_state(processed_state_path))
    processed_dirs_before = set(processed_dirs)
    metadata_files, scan_stats = _scan_metadata_files(
        sink_dir,
        processed_dirs=processed_dirs,
        max_metadata_files=sink_scan_max_metadata_files,
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

        # Idempotency: skip if already marked ready
        if _is_already_ready(pg_conn, event_id):
            if meta_dir:
                processed_dirs.add(meta_dir)
            logger.debug("media_skip: event already ready event_id=%s", event_id)
            continue

        post_savant_finalizer_enabled = _post_savant_finalizer_enabled()
        finalizer_enabled = (
            midterm_raw_clip_finalizer_enabled or post_savant_finalizer_enabled
        )

        video_file = _find_video_file(meta_dir)
        if not video_file:
            continue  # not ready yet

        metadata_file = str(Path(meta_dir) / "metadata.json")

        stable_count = 0
        if finalizer_enabled and candidate_dirs is not None:
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

        if finalizer_enabled:
            ready, reason = _sink_output_ready_for_finalizer(
                video_file=video_file,
                metadata_file=metadata_file,
            )
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

        bundle = None
        finalize_started = time.monotonic()
        probe_before = _probe_metrics_snapshot()
        _set_event_evidence_state(pg_conn, event_id, state="finalizing")
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
                )
            except Exception as exc:
                logger.exception(
                    "post_savant_finalizer_failed event_id=%s meta_dir=%s",
                    event_id,
                    meta_dir,
                )
                _mark_media_finalize_failed(
                    pg_conn,
                    event_id=event_id,
                    sink_path=meta_dir,
                    error_message=f"{type(exc).__name__}:{exc}",
                )
                if meta_dir:
                    processed_dirs.add(meta_dir)
                continue
            clip_path = bundle["raw_clip"]
            clip_status = bundle.get("clip_status", "generated_unverified")
        elif midterm_raw_clip_finalizer_enabled:
            if not evidence_output_dir:
                logger.error("midterm_finalizer enabled but no evidence_output_dir")
                continue
            bundle = _finalize_midterm_evidence_bundle(
                pg_conn,
                event_id=event_id,
                meta_dir=meta_dir,
                video_file=video_file,
                metadata_file=metadata_file,
                evidence_output_dir=evidence_output_dir,
            )
            clip_path = bundle["raw_clip"]
            clip_status = bundle.get("clip_status", "generated_unverified")
        else:
            clip_path = video_file
            clip_status = "ready"
        finalize_duration_ms = int((time.monotonic() - finalize_started) * 1000)
        probe_delta = _probe_metrics_delta(probe_before)
        logger.info(
            "media_event_finalized event_id=%s meta_dir=%s "
            "finalization_duration_ms=%s scan_duration_ms=%s "
            "metadata_files_visited=%s ffprobe_invocations=%s "
            "ffprobe_duration_ms=%s ffmpeg_invocations=%s ffmpeg_duration_ms=%s "
            "imageio_ffmpeg_fallback_count=%s imageio_ffmpeg_fallback_duration_ms=%s",
            event_id,
            meta_dir,
            finalize_duration_ms,
            scan_stats.get("scan_duration_ms"),
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
                            media_status = %(clip_status_text)s,
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
                            media_status = %(clip_status_text)s,
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
                                        'event_annotation_path', %(annotation_path)s::text,
                                        'sink_metadata_path', %(sink_metadata_path)s::text,
                                        'annotations_jsonl_path', %(annotations_path)s::text,
                                        'summary_json_path', %(summary_path)s::text,
                                        'raw_clip_path', %(raw_clip_path)s::text,
                                        'evidence_topology',
                                            %(evidence_topology)s::text,
                                        'annotation_source',
                                            %(annotation_source)s::text,
                                        'production_ready',
                                            %(production_ready)s::boolean,
                                        'legacy_used_for_visual_binding',
                                            %(legacy_used_for_visual_binding)s::boolean,
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
                            "annotation_path": bundle["event_annotation"],
                            "sink_metadata_path": bundle["sink_metadata"],
                            "annotations_path": bundle["annotations_jsonl"],
                            "summary_path": bundle["summary"],
                            "raw_clip_path": bundle["raw_clip"],
                            "evidence_topology": bundle.get("evidence_topology", ""),
                            "annotation_source": bundle.get("annotation_source", ""),
                            "production_ready": bool(
                                bundle.get("production_ready", False)
                            ),
                            "legacy_used_for_visual_binding": bool(
                                bundle.get("legacy_used_for_visual_binding", False)
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
                    evidence_reason = "" if evidence_state == "ready" else clip_status
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
                                        'evidence_state_updated_at', now()
                                    ))
                                ),
                            updated_at = now()
                        WHERE id = %(event_id)s::uuid
                        """,
                        {
                            "event_id": event_id,
                            "evidence_state": evidence_state,
                            "evidence_reason": evidence_reason,
                        },
                    )
                    cur.execute(
                        """
                        UPDATE evidence_tasks
                        SET status = %(evidence_state)s,
                            clip_path = COALESCE(%(clip_path)s, clip_path),
                            metadata_path = COALESCE(%(metadata_path)s, metadata_path),
                            output_root = COALESCE(%(output_root)s, output_root),
                            error_message = CASE
                                WHEN %(evidence_reason)s::text != ''
                                    THEN %(evidence_reason)s::text
                                ELSE error_message
                            END,
                            updated_at = now()
                        WHERE event_id = %(event_id)s::uuid
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
                        },
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
        except Exception:
            logger.exception("failed to update event_id=%s", event_id)

    if processed_state_path is not None and processed_dirs != processed_dirs_before:
        _save_processed_sink_state(processed_state_path, processed_dirs)
    return updated


def _mark_media_finalize_failed(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    sink_path: str,
    error_message: str,
) -> None:
    try:
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
            if cur.rowcount and cur.rowcount > 0:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = 'failed',
                        error_message = %(error_message)s,
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                    """,
                    {"event_id": event_id, "error_message": error_message},
                )
    except Exception:
        logger.exception("failed to mark media finalizer failure event_id=%s", event_id)


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
                    """
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
    logger.info(
        "media-worker started sink_dir=%s snap_dir=%s ann_dir=%s evidence_dir=%s "
        "midterm_finalizer=%s sink_stability_checks=%d poll_interval=%ds "
        "default_pre_seconds=%.1f evidence_max_duration_slack_sec=%.1f "
        "state_path=%s sink_scan_max_metadata_files=%d "
        "media_probe_timeout_s=%.1f media_decode_timeout_s=%.1f "
        "cleanup_replay_sink_output=%s cleanup_statuses=%s",
        cfg.sink_output_dir,
        cfg.snapshot_output_dir,
        cfg.annotated_output_dir,
        cfg.evidence_output_dir,
        cfg.midterm_raw_clip_finalizer_enabled,
        cfg.midterm_sink_stability_checks,
        cfg.poll_interval_s,
        cfg.default_pre_seconds,
        cfg.evidence_max_duration_slack_sec,
        cfg.media_worker_state_path,
        cfg.sink_scan_max_metadata_files,
        cfg.media_probe_timeout_s,
        cfg.media_decode_timeout_s,
        cfg.cleanup_replay_sink_output_enabled,
        ",".join(cfg.cleanup_replay_sink_output_statuses),
    )

    processed_state_path = _media_worker_state_path(
        cfg.sink_output_dir,
        cfg.media_worker_state_path,
    )
    processed_dirs: set[str] = _load_processed_sink_state(processed_state_path)
    candidate_dirs: dict[str, tuple[int, int]] = {}
    invalid_output_failures: dict[str, int] = {}

    while not shutdown_requested:
        try:
            active_sink_output_dir = _active_epoch_sink_output_dir(cfg.sink_output_dir)
            clip_updates = _process_sink_output(
                pg_conn,
                active_sink_output_dir,
                processed_dirs,
                evidence_output_dir=cfg.evidence_output_dir,
                midterm_raw_clip_finalizer_enabled=cfg.midterm_raw_clip_finalizer_enabled,
                candidate_dirs=candidate_dirs,
                invalid_output_failures=invalid_output_failures,
                midterm_sink_stability_checks=cfg.midterm_sink_stability_checks,
                processed_state_path=processed_state_path,
                sink_scan_max_metadata_files=cfg.sink_scan_max_metadata_files,
                cleanup_replay_sink_output_enabled=(
                    cfg.cleanup_replay_sink_output_enabled
                ),
                cleanup_replay_sink_output_statuses=(
                    cfg.cleanup_replay_sink_output_statuses
                ),
            )
            if clip_updates:
                logger.info("media_worker: clip updated %d events", clip_updates)

            snap_updates = _process_pending_snapshots(
                pg_conn,
                cfg.snapshot_output_dir,
                cfg.default_pre_seconds,
            )
            if snap_updates:
                logger.info("media_worker: snapshot updated %d events", snap_updates)

            ann_updates = _process_pending_annotations(
                pg_conn, cfg.annotated_output_dir,
            )
            if ann_updates:
                logger.info("media_worker: annotation updated %d events", ann_updates)
        except Exception:
            logger.exception("media worker loop error")

        time.sleep(cfg.poll_interval_s)

    logger.info("media-worker stopped (processed %d dirs)", len(processed_dirs))
