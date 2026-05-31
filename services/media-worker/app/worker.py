"""Media worker — monitor sink output, update events table with clip paths and snapshots."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from ast import literal_eval
from pathlib import Path

import psycopg

from app.annotated_snapshot import generate_annotated_snapshot
from app.config import Config, load_config
from app.snapshot import generate_snapshot

logger = logging.getLogger(__name__)

shutdown_requested = False


def request_shutdown(signum: int, _frame: object) -> None:
    global shutdown_requested
    logger.info("shutdown requested by signal=%s", signum)
    shutdown_requested = True


def _parse_ndjson(filepath: Path) -> dict | None:
    """Parse an NDJSON (JSON Lines) file, returning the first valid JSON object."""
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


def _find_metadata_files(sink_dir: str) -> list[dict]:
    """Scan *sink_dir* for metadata.json files and return parsed contents."""
    results = []
    sink_path = Path(sink_dir)
    if not sink_path.exists():
        return results

    for meta_file in sink_path.rglob("metadata.json"):
        data = _parse_ndjson(meta_file)
        if data is None:
            # Try single JSON object as fallback
            try:
                with open(meta_file, "r") as f:
                    data = json.load(f)
            except Exception:
                logger.exception("failed to parse %s", meta_file)
                continue
        data["_meta_dir"] = str(meta_file.parent)
        results.append(data)
        logger.info(
            "media_metadata_parsed path=%s source_id=%s event_id=%s",
            str(meta_file.parent),
            data.get("source_id", ""),
            data.get("labels", {}).get("event_id", data.get("event_id", "")),
        )
    return results


def _find_video_file(meta_dir: str) -> str | None:
    """Find the first video file (*.mkv, *.mov, *.webm) in *meta_dir*."""
    for ext in ("*.mkv", "*.mov", "*.webm", "*.mp4"):
        for f in Path(meta_dir).glob(ext):
            return str(f)
    return None


import re

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
    """Check if an event already has a final clip_status."""
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT payload->'media'->>'clip_status' FROM events WHERE id = %s::uuid",
                (event_id,),
            )
            row = cur.fetchone()
            return row is not None and row[0] in ("ready", "generated")
    except Exception:
        return False


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
                   frame_uuid, payload, confidence, source_event_id, keyframe_uuid
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
    """Build the P1 event-frame annotation document from event context."""
    payload = context["payload"]
    media = payload.get("media", {}) if isinstance(payload, dict) else {}
    if not isinstance(media, dict):
        media = {}

    overlays = []
    missing = []
    bbox = payload.get("bbox") or payload.get("person_bbox")
    bbox_source = "event.payload.bbox" if payload.get("bbox") else "event.payload.person_bbox"
    if bbox is None:
        bbox = media.get("bbox") or media.get("person_bbox")
        bbox_source = "event.payload.media.bbox" if media.get("bbox") else "event.payload.media.person_bbox"
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
    """Build the P1 event-frame annotation document from the event row."""
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


def _probe_duration_with_imageio_ffmpeg(path: str) -> float | None:
    try:
        import imageio_ffmpeg  # type: ignore

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        result = subprocess.run(
            [ffmpeg, "-i", path],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
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

        _frames, duration = imageio_ffmpeg.count_frames_and_secs(path)
        duration_value = float(duration)
        if duration_value > 0:
            logger.warning(
                "ffprobe unavailable; duration probed via imageio_ffmpeg frame "
                "count path=%s duration=%.6f",
                path,
                duration_value,
            )
            return duration_value
    except Exception:
        logger.exception("imageio_ffmpeg duration fallback failed path=%s", path)
    return None


def _probe_video_duration_seconds(path: str) -> float | None:
    """Return video duration in seconds using ffprobe when available."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
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
                timeout=30,
            )
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
) -> dict:
    payload = event_context.get("payload", {})
    media = payload.get("media", {}) if isinstance(payload, dict) else {}
    if not isinstance(media, dict):
        media = {}
    stop_condition = replay_job_request.get("stop_condition") or {}
    configuration = replay_job_request.get("configuration") or {}
    offset = replay_job_request.get("offset") or {}
    raw_clip_size = 0
    try:
        raw_clip_size = Path(raw_clip_path).stat().st_size
    except OSError:
        raw_clip_size = 0
    raw_clip_duration = _probe_video_duration_seconds(raw_clip_path)
    duration_probe_status = "ok" if raw_clip_duration is not None else "failed"

    return {
        "schema_version": "1.0",
        "phase": os.getenv("EVIDENCE_PHASE", "P1"),
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
            "event_ts_ms": event_context.get("event_ts_ms", 0),
            "frame_uuid": event_context.get("frame_uuid", ""),
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
            "event_annotation_path": event_annotation_path,
            "raw_clip_size": raw_clip_size,
            "raw_clip_duration": raw_clip_duration,
            "duration_probe_status": duration_probe_status,
        },
        "status": {
            "clip_status": "generated",
        },
        "limitations": [
            "single-event evidence POC",
            "not incident coalescing",
            "not continuous recording",
            "no annotated_clip generated",
        ],
    }


def _finalize_p1_evidence_bundle(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    meta_dir: str,
    video_file: str,
    metadata_file: str,
    evidence_output_dir: str,
) -> dict:
    """Copy Replay sink output into the P1 raw evidence bundle."""
    evidence_dir = Path(evidence_output_dir) / event_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    raw_clip = evidence_dir / f"raw_clip{Path(video_file).suffix}"
    metadata_out = evidence_dir / "metadata.json"
    sink_metadata_out = evidence_dir / "sink_metadata.json"
    annotation_out = evidence_dir / "event_annotation.json"

    if not raw_clip.exists():
        shutil.copy2(video_file, raw_clip)
    shutil.copy2(metadata_file, sink_metadata_out)

    event_context = _load_event_context(pg_conn, event_id)
    annotation = _event_annotation_from_context(
        event_context,
        cameras_config_path=os.getenv("CAMERAS_CONFIG_PATH"),
    )
    with open(annotation_out, "w") as f:
        json.dump(annotation, f, ensure_ascii=False, indent=2)
        f.write("\n")

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
    )
    with open(metadata_out, "w") as f:
        json.dump(business_metadata, f, ensure_ascii=False, indent=2)
        f.write("\n")

    return {
        "evidence_dir": str(evidence_dir),
        "raw_clip": str(raw_clip),
        "metadata": str(metadata_out),
        "sink_metadata": str(sink_metadata_out),
        "event_annotation": str(annotation_out),
        "sink_output_path": meta_dir,
    }


def _process_sink_output(
    pg_conn: psycopg.Connection,
    sink_dir: str,
    processed_dirs: set[str],
    *,
    evidence_output_dir: str | None = None,
    p1_raw_clip_finalizer_enabled: bool = False,
    candidate_dirs: dict[str, tuple[int, int]] | None = None,
    p1_sink_stability_checks: int = 2,
) -> int:
    """Process new sink outputs and update events table. Returns count of updates."""
    updated = 0
    for meta in _find_metadata_files(sink_dir):
        meta_dir = meta.get("_meta_dir", "")

        # Idempotency: skip already-processed directories
        if meta_dir and meta_dir in processed_dirs:
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

        video_file = _find_video_file(meta_dir)
        if not video_file:
            continue  # not ready yet

        if p1_raw_clip_finalizer_enabled and candidate_dirs is not None:
            try:
                current_size = Path(video_file).stat().st_size
            except OSError:
                continue
            previous_size, stable_count = candidate_dirs.get(meta_dir, (-1, 0))
            stable_count = stable_count + 1 if current_size == previous_size else 0
            candidate_dirs[meta_dir] = (current_size, stable_count)
            if stable_count < max(1, p1_sink_stability_checks):
                logger.debug(
                    "media_wait_for_stable_sink_output event_id=%s meta_dir=%s "
                    "size=%s previous_size=%s stable_count=%s required=%s",
                    event_id,
                    meta_dir,
                    current_size,
                    previous_size,
                    stable_count,
                    p1_sink_stability_checks,
                )
                continue

        metadata_file = str(Path(meta_dir) / "metadata.json")
        bundle = None
        if p1_raw_clip_finalizer_enabled:
            if not evidence_output_dir:
                logger.error("p1_finalizer enabled but no evidence_output_dir")
                continue
            bundle = _finalize_p1_evidence_bundle(
                pg_conn,
                event_id=event_id,
                meta_dir=meta_dir,
                video_file=video_file,
                metadata_file=metadata_file,
                evidence_output_dir=evidence_output_dir,
            )
            clip_path = bundle["raw_clip"]
            clip_status = "generated"
        else:
            clip_path = video_file
            clip_status = "ready"
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
                            "sink_path": json.dumps(sink_path),
                            "event_id": event_id,
                        },
                    )
                if bundle:
                    cur.execute(
                        """
                        UPDATE events
                        SET payload = jsonb_set(
                                jsonb_set(
                                    jsonb_set(
                                        jsonb_set(
                                            COALESCE(payload, '{}'::jsonb),
                                            '{media,evidence_dir}',
                                            %(evidence_dir)s::jsonb
                                        ),
                                        '{media,metadata_path}',
                                        %(metadata_path)s::jsonb
                                    ),
                                    '{media,event_annotation_path}',
                                    %(annotation_path)s::jsonb
                                ),
                                '{media,sink_metadata_path}',
                                %(sink_metadata_path)s::jsonb
                            ),
                            updated_at = now()
                        WHERE id = %(event_id)s::uuid
                        """,
                        {
                            "event_id": event_id,
                            "evidence_dir": json.dumps(bundle["evidence_dir"]),
                            "metadata_path": json.dumps(bundle["metadata"]),
                            "annotation_path": json.dumps(bundle["event_annotation"]),
                            "sink_metadata_path": json.dumps(bundle["sink_metadata"]),
                        },
                    )
                if cur.rowcount and cur.rowcount > 0:
                    logger.info(
                        "media_event_updated event_id=%s clip_path=%s sink_path=%s",
                        event_id,
                        clip_path,
                        sink_path,
                    )
                    updated += 1
                if meta_dir:
                    processed_dirs.add(meta_dir)
        except Exception:
            logger.exception("failed to update event_id=%s", event_id)

    return updated


def _snapshot_needed(pg_conn: psycopg.Connection) -> list[dict]:
    """Return events with clip_status=ready that need snapshot generation.

    Conditions: clip_status is 'ready', snap_status is not 'ready' or
    'not_required', snapshot_required=true, and clip_path is non-null.
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
                WHERE payload -> 'media' ->> 'clip_status' = 'ready'
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
    """Mark events with snapshot_required=false && clip ready as not_required."""
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
                WHERE payload -> 'media' ->> 'clip_status' = 'ready'
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
    """Generate snapshots for events that have clip_status=ready but no snapshot yet.

    Idempotent: skips events whose snapshot_status is already 'ready' or
    'not_required'.  If snapshot_status is 'failed' but the file exists
    from a prior attempt, it promotes the status to 'ready'.

    Also marks snapshot_required=false events as 'not_required' when clip
    is ready.
    """
    _mark_not_required(pg_conn)

    events = _snapshot_needed(pg_conn)
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

        result = generate_snapshot(
            event_id=event_id,
            clip_path=clip_path,
            pre_seconds=pre_seconds,
            output_dir=snapshot_output_dir,
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
        "p1_finalizer=%s sink_stability_checks=%d poll_interval=%ds "
        "default_pre_seconds=%.1f",
        cfg.sink_output_dir,
        cfg.snapshot_output_dir,
        cfg.annotated_output_dir,
        cfg.evidence_output_dir,
        cfg.p1_raw_clip_finalizer_enabled,
        cfg.p1_sink_stability_checks,
        cfg.poll_interval_s,
        cfg.default_pre_seconds,
    )

    processed_dirs: set[str] = set()
    candidate_dirs: dict[str, tuple[int, int]] = {}

    while not shutdown_requested:
        try:
            clip_updates = _process_sink_output(
                pg_conn,
                cfg.sink_output_dir,
                processed_dirs,
                evidence_output_dir=cfg.evidence_output_dir,
                p1_raw_clip_finalizer_enabled=cfg.p1_raw_clip_finalizer_enabled,
                candidate_dirs=candidate_dirs,
                p1_sink_stability_checks=cfg.p1_sink_stability_checks,
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
