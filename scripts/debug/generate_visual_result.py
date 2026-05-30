#!/usr/bin/env python3
"""Generate debug visual result output from an event/face observation.

This CLI is intentionally a debug/MVP renderer. It reads an existing source
frame/clip and structured event JSON, then writes human-inspectable annotated
media under a debug output root. It is not production evidence generation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import cv2
except Exception:  # pragma: no cover - exercised by environment
    cv2 = None


DEFAULT_OUTPUT_ROOT = Path("/data/video-analytics/media/debug/visual_results")
FALLBACK_OUTPUT_ROOT = Path("tmp/visual_results")
SCHEMA_VERSION = "1.0"
PHASE = "V1"
VISUAL_RESULT_TYPE = "debug_mvp"
SOURCE_EXTRACTION_LIMITATION = "source extraction only; not Replay evidence"
NOT_PRODUCTION_LIMITATION = "not production evidence; not production clip-worker output"
STATIC_CLIP_LIMITATION = "static bbox overlay for clip"
ANNOTATION_KEYS = (
    "frame_uuid",
    "keyframe_uuid",
    "previous_keyframe_uuid",
    "frame_pts",
    "frame_num",
    "ntp_timestamp",
)


class BBoxParseResult:
    def __init__(
        self,
        xyxy: tuple[int, int, int, int] | None,
        bbox_format: str,
        normalized: bool = False,
        clamped: bool = False,
        status: str = "unavailable",
        reason: str | None = None,
    ) -> None:
        self.xyxy = xyxy
        self.bbox_format = bbox_format
        self.normalized = normalized
        self.clamped = clamped
        self.status = status
        self.reason = reason


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short(value: Any, length: int = 12) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= length else text[:length] + "..."


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _json_load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except Exception:
                return value
    return value


def parse_source_event_id_track_id(source_event_id: Any) -> str | None:
    """Parse ``producer:camera_id:track_id:event_type:ts`` when available."""
    if not source_event_id:
        return None
    parts = str(source_event_id).split(":")
    if len(parts) < 5:
        return None
    candidate = parts[-3]
    if candidate and candidate != "none":
        return candidate
    return None


def _deep_get(data: dict[str, Any], path: tuple[str, ...], default: Any = None) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def normalize_input_record(data: dict[str, Any]) -> dict[str, Any]:
    """Return a compatible event/observation dict from common debug inputs."""
    if isinstance(data.get("event"), dict):
        event = deepcopy(data["event"])
        if "inspection_material_path" in data:
            event.setdefault("_source_frame", data["inspection_material_path"])
        if "source_aligned_clip_path" in data:
            event.setdefault("_source_clip", data["source_aligned_clip_path"])
        if "matched_trace_record" in data:
            event.setdefault("_matched_trace_record", data["matched_trace_record"])
        return event

    if isinstance(data.get("data"), dict):
        return deepcopy(data["data"])

    return deepcopy(data)


def load_event_by_id(event_id: str, database_url: str | None = None) -> dict[str, Any]:
    """Best-effort PostgreSQL event loader for manual use."""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("psycopg is required for --event-id") from exc

    url = database_url or os.environ.get(
        "DATABASE_URL", "postgresql://video:video@localhost:5438/video_analytics"
    )
    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text AS event_id, source_event_id, event_type, camera_id,
                       source_id, track_id, event_ts_ms, frame_uuid,
                       keyframe_uuid, confidence, payload
                FROM events
                WHERE id = %s::uuid
                """,
                (event_id,),
            )
            row = cur.fetchone()
    if not row:
        raise ValueError(f"event not found: {event_id}")
    data = dict(row)
    data["payload"] = _maybe_json(data.get("payload")) or {}
    return data


def extract_source_fields(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    event_media = event.get("media") if isinstance(event.get("media"), dict) else {}
    if event_media:
        media = {**event_media, **media}

    track_id = event.get("track_id")
    track_id_source = "event"
    if track_id in (None, "", "None"):
        fallback_track_id = parse_source_event_id_track_id(event.get("source_event_id"))
        if fallback_track_id:
            track_id = fallback_track_id
            track_id_source = "source_event_id_fallback"
        else:
            track_id = None
            track_id_source = "missing"

    return {
        "event_id": event.get("event_id") or event.get("id") or "",
        "source_event_id": event.get("source_event_id") or "",
        "event_type": event.get("event_type") or event.get("message_type") or "",
        "camera_id": event.get("camera_id") or "",
        "source_id": event.get("source_id") or media.get("source_id") or "",
        "track_id": track_id,
        "track_id_source": track_id_source,
        "event_ts_ms": _safe_int(
            event.get("event_ts_ms", event.get("timestamp_ms", media.get("event_ts_ms", 0)))
        ),
        "frame_uuid": event.get("frame_uuid") or media.get("frame_uuid"),
        "keyframe_uuid": event.get("keyframe_uuid") or media.get("keyframe_uuid"),
        "previous_keyframe_uuid": (
            event.get("previous_keyframe_uuid") or media.get("previous_keyframe_uuid")
        ),
        "confidence": event.get("confidence", event.get("face_confidence")),
        "quality": event.get("quality"),
    }


def infer_bbox_format(value: Any, default: str = "auto") -> str:
    value = _maybe_json(value)
    if default and default != "auto":
        return default
    if isinstance(value, dict):
        keys = set(value)
        if {"x", "y", "width", "height"}.issubset(keys):
            return "xywh"
        if {"xc", "yc", "width", "height"}.issubset(keys):
            return "cxcywh"
        if {"cx", "cy", "width", "height"}.issubset(keys):
            return "cxcywh"
        if {"x1", "y1", "x2", "y2"}.issubset(keys):
            return "xyxy"
        if {"left", "top", "right", "bottom"}.issubset(keys):
            return "xyxy"
    return default


def _bbox_values(value: Any, bbox_format: str) -> tuple[float, float, float, float] | None:
    value = _maybe_json(value)
    if isinstance(value, dict):
        if bbox_format == "xywh":
            return (
                _safe_float(value.get("x")),
                _safe_float(value.get("y")),
                _safe_float(value.get("width")),
                _safe_float(value.get("height")),
            )
        if bbox_format in ("cxcywh", "center_xywh"):
            return (
                _safe_float(value.get("xc", value.get("cx"))),
                _safe_float(value.get("yc", value.get("cy"))),
                _safe_float(value.get("width")),
                _safe_float(value.get("height")),
            )
        if bbox_format == "xyxy":
            return (
                _safe_float(value.get("x1", value.get("left"))),
                _safe_float(value.get("y1", value.get("top"))),
                _safe_float(value.get("x2", value.get("right"))),
                _safe_float(value.get("y2", value.get("bottom"))),
            )
    if isinstance(value, list) and len(value) >= 4:
        return tuple(_safe_float(v) for v in value[:4])  # type: ignore[return-value]
    return None


def _looks_normalized(values: tuple[float, float, float, float]) -> bool:
    return all(0.0 <= value <= 1.0 for value in values)


def parse_bbox_to_xyxy(
    value: Any,
    image_width: int,
    image_height: int,
    bbox_format: str = "auto",
) -> BBoxParseResult:
    """Parse explicit bbox formats into pixel xyxy coordinates.

    Supported formats:
    - ``xywh``: x, y, width, height
    - ``xyxy``: x1, y1, x2, y2
    - ``cxcywh`` / ``center_xywh``: center x, center y, width, height
    """
    resolved_format = infer_bbox_format(value, bbox_format)
    if resolved_format == "center_xywh":
        resolved_format = "cxcywh"
    if resolved_format == "auto":
        return BBoxParseResult(None, "auto", status="unavailable", reason="unknown_format")

    values = _bbox_values(value, resolved_format)
    if values is None:
        return BBoxParseResult(None, resolved_format, status="unavailable", reason="missing_values")

    normalized = _looks_normalized(values)
    a, b, c, d = values
    if normalized:
        if resolved_format in ("xywh", "cxcywh"):
            a *= image_width
            c *= image_width
            b *= image_height
            d *= image_height
        elif resolved_format == "xyxy":
            a *= image_width
            c *= image_width
            b *= image_height
            d *= image_height

    if resolved_format == "xywh":
        x1, y1, x2, y2 = a, b, a + c, b + d
    elif resolved_format == "xyxy":
        x1, y1, x2, y2 = a, b, c, d
    elif resolved_format == "cxcywh":
        x1, y1, x2, y2 = a - c / 2.0, b - d / 2.0, a + c / 2.0, b + d / 2.0
    else:
        return BBoxParseResult(
            None, resolved_format, normalized=normalized, status="unavailable",
            reason="unsupported_format",
        )

    if x2 <= x1 or y2 <= y1:
        return BBoxParseResult(
            None, resolved_format, normalized=normalized, status="unavailable",
            reason="non_positive_area",
        )

    raw = (
        int(round(x1)),
        int(round(y1)),
        int(round(x2)),
        int(round(y2)),
    )
    clamped = (
        max(0, min(image_width - 1, raw[0])),
        max(0, min(image_height - 1, raw[1])),
        max(0, min(image_width - 1, raw[2])),
        max(0, min(image_height - 1, raw[3])),
    )
    was_clamped = raw != clamped
    if clamped[2] <= clamped[0] or clamped[3] <= clamped[1]:
        return BBoxParseResult(
            None, resolved_format, normalized=normalized, clamped=was_clamped,
            status="unavailable", reason="outside_image",
        )
    return BBoxParseResult(
        clamped, resolved_format, normalized=normalized, clamped=was_clamped,
        status="generated",
    )


def _first_bbox_candidate(event: dict[str, Any], keys: tuple[str, ...]) -> Any:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for key in keys:
        if key in event:
            return event.get(key)
        if key in payload:
            return payload.get(key)
    return None


def extract_person_bbox_value(event: dict[str, Any]) -> Any:
    return _first_bbox_candidate(event, ("bbox", "person_bbox"))


def extract_face_bbox_value(event: dict[str, Any]) -> Any:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for candidate in (
        event.get("face_bbox"),
        payload.get("face_bbox"),
        _deep_get(payload, ("face", "bbox")),
    ):
        if candidate is not None:
            return candidate
    return None


def normalize_polygon(value: Any) -> list[tuple[int, int]] | None:
    value = _maybe_json(value)
    if isinstance(value, dict):
        value = value.get("points") or value.get("polygon")
    if not isinstance(value, list) or len(value) < 3:
        return None
    points: list[tuple[int, int]] = []
    for point in value:
        if isinstance(point, dict):
            x = point.get("x")
            y = point.get("y")
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            x = point[0]
            y = point[1]
        else:
            return None
        points.append((int(round(_safe_float(x))), int(round(_safe_float(y)))))
    return points if len(points) >= 3 else None


def extract_roi_polygon(event: dict[str, Any]) -> list[tuple[int, int]] | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for candidate in (
        event.get("roi_polygon"),
        event.get("polygon"),
        payload.get("roi_polygon"),
        payload.get("polygon"),
        payload.get("zone_polygon"),
        _deep_get(payload, ("roi", "polygon")),
    ):
        polygon = normalize_polygon(candidate)
        if polygon:
            return polygon
    return None


def load_roi_polygon_from_camera_config(
    config_path: str | None,
    camera_id: str,
    zone_id: str | None,
) -> tuple[list[tuple[int, int]] | None, str | None]:
    """Resolve ROI polygon from generated camera YAML when present."""
    if not config_path or not camera_id:
        return None, None
    path = Path(config_path)
    if not path.exists():
        return None, f"camera_config_missing:{path}"
    try:
        import yaml
    except Exception:
        return None, "pyyaml_unavailable"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return None, f"camera_config_read_error:{type(exc).__name__}"
    cameras = data.get("cameras") if isinstance(data, dict) else {}
    camera = cameras.get(camera_id) if isinstance(cameras, dict) else None
    if not isinstance(camera, dict):
        return None, f"camera_not_found:{camera_id}"
    zones = camera.get("zones") if isinstance(camera.get("zones"), dict) else {}
    if zone_id and zone_id in zones:
        zone = zones.get(zone_id)
        polygon = normalize_polygon(zone.get("points") if isinstance(zone, dict) else None)
        return polygon, f"camera_config:{path}:{camera_id}:{zone_id}" if polygon else None
    if len(zones) == 1:
        name, zone = next(iter(zones.items()))
        polygon = normalize_polygon(zone.get("points") if isinstance(zone, dict) else None)
        return polygon, f"camera_config:{path}:{camera_id}:{name}" if polygon else None
    return None, f"zone_not_found:{zone_id or ''}"


def clamp_bbox(
    bbox: tuple[int, int, int, int], width: int, height: int
) -> tuple[int, int, int, int] | None:
    x, y, w, h = bbox
    x1 = max(0, min(width - 1, x))
    y1 = max(0, min(height - 1, y))
    x2 = max(0, min(width - 1, x + w))
    y2 = max(0, min(height - 1, y + h))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2 - x1, y2 - y1


def _draw_text_box(image: Any, lines: list[str]) -> None:
    if cv2 is None:
        return
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.55
    thickness = 1
    line_h = 21
    max_w = 0
    for line in lines:
        size, _ = cv2.getTextSize(line, font, scale, thickness)
        max_w = max(max_w, size[0])
    box_w = min(max_w + 18, image.shape[1] - 4)
    box_h = min(line_h * len(lines) + 12, image.shape[0] - 4)
    overlay = image.copy()
    cv2.rectangle(overlay, (6, 6), (6 + box_w, 6 + box_h), (10, 10, 10), -1)
    cv2.addWeighted(overlay, 0.68, image, 0.32, 0, image)
    y = 27
    for line in lines:
        cv2.putText(image, line[:110], (15, y), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
        y += line_h


def draw_annotations(
    image: Any,
    source: dict[str, Any],
    person_bbox_xyxy: tuple[int, int, int, int] | None = None,
    face_bbox_xyxy: tuple[int, int, int, int] | None = None,
    roi_polygon: list[tuple[int, int]] | None = None,
    landmarks: list[float] | None = None,
    event_frame: bool = False,
) -> dict[str, str]:
    """Draw static annotations on an OpenCV BGR image and return statuses."""
    if cv2 is None:
        return {
            "person_bbox_status": "unavailable",
            "face_bbox_status": "unavailable",
            "roi_status": "unavailable",
            "label_status": "unavailable",
        }

    height, width = image.shape[:2]
    statuses = {
        "person_bbox_status": "unavailable",
        "face_bbox_status": "unavailable",
        "roi_status": "unavailable",
        "label_status": "generated",
    }

    if roi_polygon:
        pts = []
        for x, y in roi_polygon:
            pts.append([max(0, min(width - 1, x)), max(0, min(height - 1, y))])
        if len(pts) >= 3:
            import numpy as np

            arr = np.array(pts, dtype="int32")
            cv2.polylines(image, [arr], isClosed=True, color=(0, 215, 255), thickness=3)
            statuses["roi_status"] = "generated"

    if person_bbox_xyxy:
        x1, y1, x2, y2 = person_bbox_xyxy
        if x2 > x1 and y2 > y1:
            cv2.rectangle(image, (x1, y1), (x2, y2), (0, 80, 255), 3)
            cv2.putText(
                image, "person", (x1, max(20, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                0.65, (0, 80, 255), 2, cv2.LINE_AA,
            )
            statuses["person_bbox_status"] = "generated"

    if face_bbox_xyxy:
        x1, y1, x2, y2 = face_bbox_xyxy
        if x2 > x1 and y2 > y1:
            cv2.rectangle(image, (x1, y1), (x2, y2), (40, 220, 40), 2)
            cv2.putText(
                image, "face", (x1, max(20, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                0.6, (40, 220, 40), 2, cv2.LINE_AA,
            )
            statuses["face_bbox_status"] = "generated"

    if landmarks and len(landmarks) >= 10:
        for idx in range(0, min(len(landmarks), 10), 2):
            x = max(0, min(width - 1, int(round(_safe_float(landmarks[idx])))))
            y = max(0, min(height - 1, int(round(_safe_float(landmarks[idx + 1])))))
            cv2.circle(image, (x, y), 4, (255, 80, 80), -1)
        statuses["landmarks_status"] = "generated"
    else:
        statuses["landmarks_status"] = "unavailable"

    if event_frame:
        cv2.putText(
            image, "EVENT FRAME", (max(10, width - 270), 36),
            cv2.FONT_HERSHEY_SIMPLEX, 0.85, (0, 255, 255), 2, cv2.LINE_AA,
        )

    lines = [
        f"{source.get('event_type') or 'visual_result'} cam={source.get('camera_id') or '?'}",
        f"source={_short(source.get('source_id'), 28)} track={source.get('track_id') or '?'}",
        f"ts={source.get('event_ts_ms') or 0} frame={_short(source.get('frame_uuid'), 18)}",
    ]
    confidence = source.get("confidence")
    quality = source.get("quality")
    extra = []
    if confidence is not None:
        extra.append(f"conf={_safe_float(confidence):.3f}")
    if quality is not None:
        extra.append(f"quality={_safe_float(quality):.3f}")
    if extra:
        lines.append(" ".join(extra))
    _draw_text_box(image, lines)
    return statuses


def _prepare_output_root(requested: Path) -> tuple[Path, bool, str | None]:
    repo_root = Path(__file__).resolve().parents[2]
    try:
        resolved = requested.resolve()
        relative = resolved.relative_to(repo_root)
        if relative.parts and relative.parts[0] in {
            "docs",
            "harness",
            "scripts",
            "modules",
            "services",
            "db",
            "infra",
            "specs",
        }:
            fallback = FALLBACK_OUTPUT_ROOT
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback, True, f"refusing source-tree output root: {requested}"
    except ValueError:
        pass

    try:
        requested.mkdir(parents=True, exist_ok=True)
        probe = requested / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return requested, False, None
    except Exception as exc:
        fallback = FALLBACK_OUTPUT_ROOT
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback, True, f"{type(exc).__name__}: {exc}"


def _make_run_id(source: dict[str, Any], explicit: str | None) -> str:
    if explicit:
        return explicit
    seed = source.get("event_id") or source.get("source_event_id") or uuid.uuid4().hex
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"v1_{str(seed).replace(':', '_')[:32]}_{suffix}"


def _copy_if_present(src: Path | None, dst: Path) -> str | None:
    if src is None:
        return None
    if not src.exists() or not src.is_file():
        return None
    shutil.copy2(src, dst)
    return str(dst)


def determine_result_type(event: dict[str, Any], source: dict[str, Any], explicit: str) -> str:
    if explicit != "auto":
        return explicit
    if event.get("source_observation_id") or event.get("message_type") == "face_observation":
        return "face_observation"
    if source.get("event_type") == "intrusion":
        return "behavior_intrusion"
    return "generic"


def get_media_dimensions(source_frame: Path, source_clip: Path) -> tuple[int, int]:
    if cv2 is None:
        raise RuntimeError("OpenCV is unavailable")
    if source_frame and source_frame.exists():
        image = cv2.imread(str(source_frame))
        if image is not None:
            height, width = image.shape[:2]
            return int(width), int(height)
    if source_clip and source_clip.exists():
        cap = cv2.VideoCapture(str(source_clip))
        try:
            if cap.isOpened():
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                if width > 0 and height > 0:
                    return width, height
        finally:
            cap.release()
    raise RuntimeError("cannot determine source media dimensions")


def extract_landmarks(event: dict[str, Any]) -> list[float] | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    value = event.get("landmarks")
    if value is None:
        value = payload.get("landmarks")
    value = _maybe_json(value)
    if isinstance(value, list) and len(value) >= 10:
        try:
            return [float(v) for v in value]
        except Exception:
            return None
    return None


def _required_annotation_failures(
    result_type: str,
    statuses: dict[str, str],
    source: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    if result_type == "behavior_intrusion":
        if statuses.get("roi_status") != "generated":
            failures.append("intrusion ROI polygon is required")
        if statuses.get("person_bbox_status") != "generated":
            failures.append("intrusion person bbox is required")
        if statuses.get("label_status") != "generated":
            failures.append("intrusion label is required")
        if not source.get("track_id"):
            failures.append("intrusion track_id is required when available")
        if not source.get("frame_uuid"):
            failures.append("intrusion frame_uuid is required when event has it")
    elif result_type == "face_observation":
        if statuses.get("face_bbox_status") != "generated":
            failures.append("face observation face bbox is required")
        if statuses.get("label_status") != "generated":
            failures.append("face observation label is required")
    return failures


def _annotate_snapshot(
    source_frame: Path,
    output_path: Path,
    source: dict[str, Any],
    person_bbox_xyxy: tuple[int, int, int, int] | None,
    face_bbox_xyxy: tuple[int, int, int, int] | None,
    roi_polygon: list[tuple[int, int]] | None,
    landmarks: list[float] | None,
) -> dict[str, str]:
    if cv2 is None:
        raise RuntimeError("OpenCV is unavailable")
    image = cv2.imread(str(source_frame))
    if image is None:
        raise RuntimeError(f"failed to read source frame: {source_frame}")
    statuses = draw_annotations(
        image, source, person_bbox_xyxy=person_bbox_xyxy,
        face_bbox_xyxy=face_bbox_xyxy, roi_polygon=roi_polygon,
        landmarks=landmarks, event_frame=True,
    )
    if not cv2.imwrite(str(output_path), image):
        raise RuntimeError(f"failed to write annotated snapshot: {output_path}")
    return statuses


def _annotate_clip(
    source_clip: Path,
    output_path: Path,
    frames_dir: Path,
    source: dict[str, Any],
    person_bbox_xyxy: tuple[int, int, int, int] | None,
    face_bbox_xyxy: tuple[int, int, int, int] | None,
    roi_polygon: list[tuple[int, int]] | None,
    landmarks: list[float] | None,
) -> tuple[str, str | None]:
    if cv2 is None:
        return "unavailable", "OpenCV is unavailable"
    cap = cv2.VideoCapture(str(source_clip))
    if not cap.isOpened():
        return "unavailable", f"failed to open source clip: {source_clip}"

    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if width <= 0 or height <= 0:
        cap.release()
        return "unavailable", "source clip has invalid dimensions"

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))
    if not writer.isOpened():
        frames_dir.mkdir(parents=True, exist_ok=True)
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            draw_annotations(
                frame, source, person_bbox_xyxy=person_bbox_xyxy,
                face_bbox_xyxy=face_bbox_xyxy, roi_polygon=roi_polygon,
                landmarks=landmarks, event_frame=(idx == 0),
            )
            cv2.imwrite(str(frames_dir / f"frame_{idx:06d}.jpg"), frame)
            idx += 1
        cap.release()
        if idx:
            return "frames_only", "OpenCV mp4 writer unavailable"
        return "unavailable", "no frames decoded from source clip"

    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        draw_annotations(
            frame, source, person_bbox_xyxy=person_bbox_xyxy,
            face_bbox_xyxy=face_bbox_xyxy, roi_polygon=roi_polygon,
            landmarks=landmarks, event_frame=(idx == 0),
        )
        writer.write(frame)
        idx += 1
    cap.release()
    writer.release()
    if idx <= 0:
        return "unavailable", "no frames decoded from source clip"
    return "generated", None


def build_metadata(
    run_id: str,
    output_root: Path,
    fallback_output_root: bool,
    fallback_reason: str | None,
    source: dict[str, Any],
    result_type: str,
    paths: dict[str, str | None],
    annotation_statuses: dict[str, str],
    bbox_metadata: dict[str, Any],
    limitations: list[str],
) -> dict[str, Any]:
    required_failures = _required_annotation_failures(result_type, annotation_statuses, source)
    return {
        "schema_version": SCHEMA_VERSION,
        "phase": PHASE,
        "visual_result_type": VISUAL_RESULT_TYPE,
        "result_type": result_type,
        "debug_only": True,
        "not_production_evidence": True,
        "required_annotation_status": "pass" if not required_failures else "fail",
        "required_annotation_failures": required_failures,
        "created_at": _utc_now(),
        "run_id": run_id,
        "source": source,
        "media": {
            "output_root": str(output_root),
            "fallback_output_root": fallback_output_root,
            "fallback_reason": fallback_reason,
            "raw_snapshot_path": paths.get("raw_snapshot_path"),
            "annotated_snapshot_path": paths.get("annotated_snapshot_path"),
            "raw_clip_path": paths.get("raw_clip_path"),
            "annotated_clip_path": paths.get("annotated_clip_path"),
            "annotated_frames_dir": paths.get("annotated_frames_dir"),
            "snapshot_annotation_status": annotation_statuses.get(
                "snapshot_annotation_status", "unavailable"
            ),
            "clip_annotation_status": annotation_statuses.get(
                "clip_annotation_status", "unavailable"
            ),
            "clip_annotation_reason": annotation_statuses.get("clip_annotation_reason"),
        },
        "annotations": {
            "person_bbox_status": annotation_statuses.get("person_bbox_status", "unavailable"),
            "face_bbox_status": annotation_statuses.get("face_bbox_status", "unavailable"),
            "roi_status": annotation_statuses.get("roi_status", "unavailable"),
            "label_status": annotation_statuses.get("label_status", "generated"),
            "landmarks_status": annotation_statuses.get("landmarks_status", "unavailable"),
            "roi_source": annotation_statuses.get("roi_source"),
        },
        "bbox": bbox_metadata,
        "limitations": limitations,
    }


def write_report(metadata: dict[str, Any], output_path: Path) -> None:
    source = metadata["source"]
    media = metadata["media"]
    annotations = metadata["annotations"]
    limitations = metadata["limitations"]
    lines = [
        "# V1 Visual Result Output",
        "",
        "This is debug/MVP visual output, not production evidence.",
        "",
        "## Source",
        "",
        f"- event_id: `{source.get('event_id') or ''}`",
        f"- source_event_id: `{source.get('source_event_id') or ''}`",
        f"- event_type: `{source.get('event_type') or ''}`",
        f"- camera_id: `{source.get('camera_id') or ''}`",
        f"- source_id: `{source.get('source_id') or ''}`",
        f"- track_id: `{source.get('track_id') or ''}`",
        f"- event_ts_ms: `{source.get('event_ts_ms') or 0}`",
        f"- frame_uuid: `{source.get('frame_uuid') or ''}`",
        f"- track_id_source: `{source.get('track_id_source') or ''}`",
        f"- result_type: `{metadata.get('result_type') or ''}`",
        f"- required_annotation_status: `{metadata.get('required_annotation_status') or ''}`",
        "",
        "## Output",
        "",
        f"- raw_snapshot: `{media.get('raw_snapshot_path') or ''}`",
        f"- annotated_snapshot: `{media.get('annotated_snapshot_path') or ''}`",
        f"- raw_clip: `{media.get('raw_clip_path') or ''}`",
        f"- annotated_clip: `{media.get('annotated_clip_path') or ''}`",
        f"- annotated_frames: `{media.get('annotated_frames_dir') or ''}`",
        "",
        "## Annotation Status",
        "",
        f"- person_bbox: `{annotations.get('person_bbox_status')}`",
        f"- face_bbox: `{annotations.get('face_bbox_status')}`",
        f"- roi: `{annotations.get('roi_status')}`",
        f"- landmarks: `{annotations.get('landmarks_status')}`",
        f"- labels: `{annotations.get('label_status')}`",
        f"- snapshot: `{media.get('snapshot_annotation_status')}`",
        f"- clip: `{media.get('clip_annotation_status')}`",
        "",
        "## Limitations",
        "",
    ]
    lines.extend(f"- {item}" for item in limitations)
    lines.extend([
        "",
        "## Boundaries",
        "",
        "- No production clip-worker.",
        "- No production media-worker.",
        "- No Replay/cache/sink deployment.",
        "- No DB migration.",
        "- No performance test.",
    ])
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_output_index(metadata: dict[str, Any], output_path: Path) -> None:
    media = metadata["media"]
    title = f"V1 Visual Result - {metadata.get('result_type')}"
    links = []
    for label, key in (
        ("raw snapshot", "raw_snapshot_path"),
        ("annotated snapshot", "annotated_snapshot_path"),
        ("raw clip", "raw_clip_path"),
        ("annotated clip", "annotated_clip_path"),
        ("metadata", "metadata_path"),
        ("report", "report_path"),
    ):
        path = media.get(key)
        if path:
            links.append((label, Path(path).name))
    body = "\n".join(f'<li><a href="{href}">{label}</a></li>' for label, href in links)
    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
<h1>{title}</h1>
<p>Debug/MVP visual output. Not production evidence.</p>
<p>Required annotation status: <strong>{metadata.get('required_annotation_status')}</strong></p>
<ul>
{body}
</ul>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")


def generate_visual_result(args: argparse.Namespace) -> dict[str, Any]:
    if args.event_id:
        event = load_event_by_id(args.event_id, database_url=args.database_url)
    elif args.a2a_summary_json:
        event = normalize_input_record(_json_load(Path(args.a2a_summary_json)))
    elif args.event_json:
        event = normalize_input_record(_json_load(Path(args.event_json)))
    else:
        raise ValueError("one of --event-id, --event-json, or --a2a-summary-json is required")

    source = extract_source_fields(event)
    output_root, fallback, fallback_reason = _prepare_output_root(Path(args.output_root))
    run_id = _make_run_id(source, args.run_id)
    output_dir = output_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    result_type = determine_result_type(event, source, args.result_type)
    source_frame = Path(args.source_frame or event.get("_source_frame", ""))
    source_clip = Path(args.source_clip or event.get("_source_clip", ""))
    image_width, image_height = get_media_dimensions(source_frame, source_clip)
    person_bbox_value = extract_person_bbox_value(event)
    face_bbox_value = extract_face_bbox_value(event)
    person_bbox_result = parse_bbox_to_xyxy(
        person_bbox_value, image_width, image_height, bbox_format=args.person_bbox_format
    ) if person_bbox_value is not None else BBoxParseResult(None, "unavailable")
    face_bbox_result = parse_bbox_to_xyxy(
        face_bbox_value, image_width, image_height, bbox_format=args.face_bbox_format
    ) if face_bbox_value is not None else BBoxParseResult(None, "unavailable")
    landmarks = extract_landmarks(event)
    roi_polygon = extract_roi_polygon(event)
    roi_source = "event_payload" if roi_polygon else None
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    zone_id = event.get("zone_id") or payload.get("zone_id")
    if roi_polygon is None:
        roi_polygon, roi_source = load_roi_polygon_from_camera_config(
            args.camera_config, source.get("camera_id") or "", zone_id
        )
    limitations = [
        SOURCE_EXTRACTION_LIMITATION,
        NOT_PRODUCTION_LIMITATION,
        STATIC_CLIP_LIMITATION,
    ]
    if source.get("track_id_source") == "source_event_id_fallback":
        limitations.append("track_id recovered from source_event_id fallback")
    elif source.get("track_id_source") == "missing":
        limitations.append("track_id_missing")
    if person_bbox_result.xyxy is None:
        limitations.append("person bbox unavailable; person box was not drawn")
    if face_bbox_result.xyxy is None:
        limitations.append("face bbox unavailable; face box was not drawn")
    if roi_polygon is None:
        limitations.append("ROI polygon unavailable; ROI was not drawn")
    if person_bbox_result.clamped or face_bbox_result.clamped:
        limitations.append("bbox was clamped to image bounds")

    paths: dict[str, str | None] = {
        "raw_snapshot_path": None,
        "annotated_snapshot_path": None,
        "raw_clip_path": None,
        "annotated_clip_path": None,
        "annotated_frames_dir": None,
    }
    statuses = {
        "snapshot_annotation_status": "unavailable",
        "clip_annotation_status": "unavailable",
        "person_bbox_status": person_bbox_result.status,
        "face_bbox_status": face_bbox_result.status,
        "roi_status": "generated" if roi_polygon else "unavailable",
        "label_status": "generated",
        "landmarks_status": "generated" if landmarks else "unavailable",
        "roi_source": roi_source,
    }
    if result_type == "behavior_intrusion" and roi_polygon is None:
        statuses["roi_status"] = "missing_required"
    if result_type == "behavior_intrusion" and person_bbox_result.xyxy is None:
        statuses["person_bbox_status"] = "missing_required"
    if result_type == "face_observation" and face_bbox_result.xyxy is None:
        statuses["face_bbox_status"] = "missing_required"

    if source_frame and source_frame.exists():
        raw_snapshot = output_dir / "raw_snapshot.jpg"
        annotated_snapshot = output_dir / "annotated_snapshot.jpg"
        paths["raw_snapshot_path"] = _copy_if_present(source_frame, raw_snapshot)
        snap_statuses = _annotate_snapshot(
            raw_snapshot, annotated_snapshot, source, person_bbox_result.xyxy,
            face_bbox_result.xyxy, roi_polygon, landmarks
        )
        statuses.update(snap_statuses)
        if result_type == "behavior_intrusion" and roi_polygon is None:
            statuses["roi_status"] = "missing_required"
        if result_type == "behavior_intrusion" and person_bbox_result.xyxy is None:
            statuses["person_bbox_status"] = "missing_required"
        if result_type == "face_observation" and face_bbox_result.xyxy is None:
            statuses["face_bbox_status"] = "missing_required"
        statuses["snapshot_annotation_status"] = "generated"
        paths["annotated_snapshot_path"] = str(annotated_snapshot)
    else:
        limitations.append("source frame unavailable; snapshot output was not generated")

    if source_clip and source_clip.exists():
        raw_clip = output_dir / "raw_clip.mp4"
        annotated_clip = output_dir / "annotated_clip.mp4"
        frames_dir = output_dir / "annotated_frames"
        paths["raw_clip_path"] = _copy_if_present(source_clip, raw_clip)
        clip_status, clip_reason = _annotate_clip(
            raw_clip, annotated_clip, frames_dir, source, person_bbox_result.xyxy,
            face_bbox_result.xyxy, roi_polygon, landmarks
        )
        statuses["clip_annotation_status"] = clip_status
        if clip_reason:
            statuses["clip_annotation_reason"] = clip_reason
            limitations.append(clip_reason)
        if clip_status == "generated":
            paths["annotated_clip_path"] = str(annotated_clip)
        elif clip_status == "frames_only":
            paths["annotated_frames_dir"] = str(frames_dir)
    else:
        limitations.append("source clip unavailable; clip output was not generated")

    bbox_metadata = {
        "image_width": image_width,
        "image_height": image_height,
        "person_bbox_format": person_bbox_result.bbox_format,
        "person_bbox_xyxy": list(person_bbox_result.xyxy) if person_bbox_result.xyxy else None,
        "person_bbox_normalized": person_bbox_result.normalized,
        "person_bbox_clamped": person_bbox_result.clamped,
        "person_bbox_reason": person_bbox_result.reason,
        "face_bbox_format": face_bbox_result.bbox_format,
        "face_bbox_xyxy": list(face_bbox_result.xyxy) if face_bbox_result.xyxy else None,
        "face_bbox_normalized": face_bbox_result.normalized,
        "face_bbox_clamped": face_bbox_result.clamped,
        "face_bbox_reason": face_bbox_result.reason,
    }
    metadata = build_metadata(
        run_id=run_id,
        output_root=output_root,
        fallback_output_root=fallback,
        fallback_reason=fallback_reason,
        source=source,
        result_type=result_type,
        paths=paths,
        annotation_statuses=statuses,
        bbox_metadata=bbox_metadata,
        limitations=limitations,
    )
    metadata_path = output_dir / "metadata.json"
    report_path = output_dir / "report.md"
    index_path = output_dir / "index.html"
    metadata["media"]["metadata_path"] = str(metadata_path)
    metadata["media"]["report_path"] = str(report_path)
    metadata["media"]["index_path"] = str(index_path)
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    write_report(metadata, report_path)
    write_output_index(metadata, index_path)
    return metadata


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--event-id", help="PostgreSQL events.id to load")
    source.add_argument("--event-json", help="Path to event/face observation JSON")
    source.add_argument("--a2a-summary-json", help="Path to R3.3A2a identity_summary.json")
    parser.add_argument("--database-url", help="PostgreSQL URL for --event-id")
    parser.add_argument("--source-frame", help="Path to matched/source frame image")
    parser.add_argument("--source-clip", help="Path to source-aligned clip")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--run-id", help="Stable output directory name")
    parser.add_argument("--mode", choices=("snapshot", "clip", "both"), default="both")
    parser.add_argument(
        "--result-type",
        choices=("auto", "behavior_intrusion", "face_observation", "generic"),
        default="auto",
    )
    parser.add_argument(
        "--camera-config",
        default="modules/savant_security/config/cameras.generated.yml",
        help="Camera YAML used to resolve ROI polygons when event payload only has zone_id",
    )
    parser.add_argument(
        "--person-bbox-format",
        choices=("auto", "xywh", "xyxy", "cxcywh", "center_xywh"),
        default="auto",
    )
    parser.add_argument(
        "--face-bbox-format",
        choices=("auto", "xywh", "xyxy", "cxcywh", "center_xywh"),
        default="cxcywh",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        metadata = generate_visual_result(args)
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    media = metadata["media"]
    print(f"output_dir={Path(media['metadata_path']).parent}")
    print(f"metadata_json={media['metadata_path']}")
    print(f"report_md={media['report_path']}")
    print(f"index_html={media.get('index_path')}")
    print(f"raw_snapshot={media.get('raw_snapshot_path') or ''}")
    print(f"annotated_snapshot={media.get('annotated_snapshot_path') or ''}")
    print(f"raw_clip={media.get('raw_clip_path') or ''}")
    print(f"annotated_clip={media.get('annotated_clip_path') or ''}")
    print(f"clip_annotation_status={media.get('clip_annotation_status')}")
    print(f"required_annotation_status={metadata.get('required_annotation_status')}")
    print(f"fallback_output_root={media.get('fallback_output_root')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
