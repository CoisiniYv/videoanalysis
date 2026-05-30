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

    return {
        "event_id": event.get("event_id") or event.get("id") or "",
        "source_event_id": event.get("source_event_id") or "",
        "event_type": event.get("event_type") or event.get("message_type") or "",
        "camera_id": event.get("camera_id") or "",
        "source_id": event.get("source_id") or media.get("source_id") or "",
        "track_id": event.get("track_id"),
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


def _bbox_from_dict(value: dict[str, Any]) -> tuple[int, int, int, int] | None:
    if {"x", "y", "width", "height"}.issubset(value):
        x = _safe_float(value.get("x"))
        y = _safe_float(value.get("y"))
        w = _safe_float(value.get("width"))
        h = _safe_float(value.get("height"))
    elif {"xc", "yc", "width", "height"}.issubset(value):
        w = _safe_float(value.get("width"))
        h = _safe_float(value.get("height"))
        x = _safe_float(value.get("xc")) - w / 2.0
        y = _safe_float(value.get("yc")) - h / 2.0
    elif {"left", "top", "right", "bottom"}.issubset(value):
        x = _safe_float(value.get("left"))
        y = _safe_float(value.get("top"))
        w = _safe_float(value.get("right")) - x
        h = _safe_float(value.get("bottom")) - y
    else:
        return None
    if w <= 0 or h <= 0:
        return None
    return int(round(x)), int(round(y)), int(round(w)), int(round(h))


def _bbox_from_list(value: list[Any]) -> tuple[int, int, int, int] | None:
    if len(value) < 4:
        return None
    xc = _safe_float(value[0])
    yc = _safe_float(value[1])
    w = _safe_float(value[2])
    h = _safe_float(value[3])
    if w <= 0 or h <= 0:
        return None
    return int(round(xc - w / 2.0)), int(round(yc - h / 2.0)), int(round(w)), int(round(h))


def normalize_bbox(value: Any) -> tuple[int, int, int, int] | None:
    value = _maybe_json(value)
    if isinstance(value, dict):
        return _bbox_from_dict(value)
    if isinstance(value, list):
        return _bbox_from_list(value)
    return None


def extract_person_bbox(event: dict[str, Any]) -> tuple[int, int, int, int] | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for candidate in (
        event.get("bbox"),
        payload.get("bbox"),
        event.get("person_bbox"),
        payload.get("person_bbox"),
    ):
        bbox = normalize_bbox(candidate)
        if bbox:
            return bbox
    return None


def extract_face_bbox(event: dict[str, Any]) -> tuple[int, int, int, int] | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for candidate in (
        event.get("face_bbox"),
        payload.get("face_bbox"),
        _deep_get(payload, ("face", "bbox")),
    ):
        bbox = normalize_bbox(candidate)
        if bbox:
            return bbox
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
    person_bbox: tuple[int, int, int, int] | None = None,
    face_bbox: tuple[int, int, int, int] | None = None,
    roi_polygon: list[tuple[int, int]] | None = None,
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

    if person_bbox:
        box = clamp_bbox(person_bbox, width, height)
        if box:
            x, y, w, h = box
            cv2.rectangle(image, (x, y), (x + w, y + h), (0, 80, 255), 3)
            cv2.putText(
                image, "person", (x, max(20, y - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                0.65, (0, 80, 255), 2, cv2.LINE_AA,
            )
            statuses["person_bbox_status"] = "generated"

    if face_bbox:
        box = clamp_bbox(face_bbox, width, height)
        if box:
            x, y, w, h = box
            cv2.rectangle(image, (x, y), (x + w, y + h), (40, 220, 40), 2)
            cv2.putText(
                image, "face", (x, max(20, y - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                0.6, (40, 220, 40), 2, cv2.LINE_AA,
            )
            statuses["face_bbox_status"] = "generated"

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


def _annotate_snapshot(
    source_frame: Path,
    output_path: Path,
    source: dict[str, Any],
    person_bbox: tuple[int, int, int, int] | None,
    face_bbox: tuple[int, int, int, int] | None,
    roi_polygon: list[tuple[int, int]] | None,
) -> dict[str, str]:
    if cv2 is None:
        raise RuntimeError("OpenCV is unavailable")
    image = cv2.imread(str(source_frame))
    if image is None:
        raise RuntimeError(f"failed to read source frame: {source_frame}")
    statuses = draw_annotations(
        image, source, person_bbox=person_bbox, face_bbox=face_bbox,
        roi_polygon=roi_polygon, event_frame=True,
    )
    if not cv2.imwrite(str(output_path), image):
        raise RuntimeError(f"failed to write annotated snapshot: {output_path}")
    return statuses


def _annotate_clip(
    source_clip: Path,
    output_path: Path,
    frames_dir: Path,
    source: dict[str, Any],
    person_bbox: tuple[int, int, int, int] | None,
    face_bbox: tuple[int, int, int, int] | None,
    roi_polygon: list[tuple[int, int]] | None,
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
                frame, source, person_bbox=person_bbox, face_bbox=face_bbox,
                roi_polygon=roi_polygon, event_frame=(idx == 0),
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
            frame, source, person_bbox=person_bbox, face_bbox=face_bbox,
            roi_polygon=roi_polygon, event_frame=(idx == 0),
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
    paths: dict[str, str | None],
    annotation_statuses: dict[str, str],
    limitations: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "phase": PHASE,
        "visual_result_type": VISUAL_RESULT_TYPE,
        "debug_only": True,
        "not_production_evidence": True,
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
        },
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

    source_frame = Path(args.source_frame or event.get("_source_frame", ""))
    source_clip = Path(args.source_clip or event.get("_source_clip", ""))
    person_bbox = extract_person_bbox(event)
    face_bbox = extract_face_bbox(event)
    roi_polygon = extract_roi_polygon(event)
    limitations = [
        SOURCE_EXTRACTION_LIMITATION,
        NOT_PRODUCTION_LIMITATION,
        STATIC_CLIP_LIMITATION,
    ]
    if person_bbox is None:
        limitations.append("person bbox unavailable; person box was not drawn")
    if face_bbox is None:
        limitations.append("face bbox unavailable; face box was not drawn")
    if roi_polygon is None:
        limitations.append("ROI polygon unavailable; ROI was not drawn")

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
        "person_bbox_status": "generated" if person_bbox else "unavailable",
        "face_bbox_status": "generated" if face_bbox else "unavailable",
        "roi_status": "generated" if roi_polygon else "unavailable",
        "label_status": "generated",
    }

    if source_frame and source_frame.exists():
        raw_snapshot = output_dir / "raw_snapshot.jpg"
        annotated_snapshot = output_dir / "annotated_snapshot.jpg"
        paths["raw_snapshot_path"] = _copy_if_present(source_frame, raw_snapshot)
        snap_statuses = _annotate_snapshot(
            raw_snapshot, annotated_snapshot, source, person_bbox, face_bbox, roi_polygon
        )
        statuses.update(snap_statuses)
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
            raw_clip, annotated_clip, frames_dir, source, person_bbox, face_bbox, roi_polygon
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

    metadata = build_metadata(
        run_id=run_id,
        output_root=output_root,
        fallback_output_root=fallback,
        fallback_reason=fallback_reason,
        source=source,
        paths=paths,
        annotation_statuses=statuses,
        limitations=limitations,
    )
    metadata_path = output_dir / "metadata.json"
    report_path = output_dir / "report.md"
    metadata["media"]["metadata_path"] = str(metadata_path)
    metadata["media"]["report_path"] = str(report_path)
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    write_report(metadata, report_path)
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
    print(f"raw_snapshot={media.get('raw_snapshot_path') or ''}")
    print(f"annotated_snapshot={media.get('annotated_snapshot_path') or ''}")
    print(f"raw_clip={media.get('raw_clip_path') or ''}")
    print(f"annotated_clip={media.get('annotated_clip_path') or ''}")
    print(f"clip_annotation_status={media.get('clip_annotation_status')}")
    print(f"fallback_output_root={media.get('fallback_output_root')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
