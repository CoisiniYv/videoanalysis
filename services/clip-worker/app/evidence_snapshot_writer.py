"""R3.2A best-effort snapshot writer using OpenCV by default."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _as_bbox(value: Any) -> tuple[int, int, int, int] | None:
    """Normalize bbox list/dict into x1, y1, x2, y2."""
    if value is None:
        return None
    if isinstance(value, dict):
        if all(k in value for k in ("x1", "y1", "x2", "y2")):
            return (
                int(float(value["x1"])),
                int(float(value["y1"])),
                int(float(value["x2"])),
                int(float(value["y2"])),
            )
        if all(k in value for k in ("x", "y", "width", "height")):
            x = int(float(value["x"]))
            y = int(float(value["y"]))
            return (x, y, x + int(float(value["width"])), y + int(float(value["height"])))
        if all(k in value for k in ("left", "top", "width", "height")):
            x = int(float(value["left"]))
            y = int(float(value["top"]))
            return (x, y, x + int(float(value["width"])), y + int(float(value["height"])))
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        vals = [int(float(v)) for v in value[:4]]
        x1, y1, a, b = vals
        # If the third/fourth coordinates look like width/height, convert.
        if a <= x1 or b <= y1:
            return (x1, y1, x1 + max(a, 0), y1 + max(b, 0))
        return (x1, y1, a, b)
    return None


def _draw_label(cv2: Any, image: Any, label: str, x: int, y: int) -> None:
    cv2.putText(
        image,
        label,
        (max(8, x), max(24, y)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 255),
        2,
        cv2.LINE_AA,
    )


def build_overlay(event: dict[str, Any]) -> dict[str, Any]:
    """Extract lightweight overlay instructions from event payload."""
    payload = event.get("payload") or {}
    event_type = event.get("event_type", "")
    items: list[dict[str, Any]] = []
    missing: list[str] = []

    if event_type in ("watchlist_hit", "live_search_hit"):
        overlay = payload.get("overlay") if isinstance(payload, dict) else {}
        matched = payload.get("matched_person", {}) if isinstance(payload, dict) else {}
        match = payload.get("match", {}) if isinstance(payload, dict) else {}
        bbox = _as_bbox((overlay or {}).get("face_bbox"))
        name = (matched or {}).get("name") or "face_match"
        similarity = match.get("similarity")
        label = f"{name} {float(similarity):.2f}" if similarity is not None else str(name)
        if bbox:
            items.append({"kind": "bbox", "target": "face", "bbox": list(bbox), "label": label})
        else:
            missing.append("face_bbox")
        return {
            "type": "face_match",
            "items": items,
            "overlay_missing_reason": ", ".join(missing) if missing else None,
        }

    bbox = None
    for key in ("person_bbox", "bbox"):
        if isinstance(payload, dict):
            bbox = _as_bbox(payload.get(key))
            if bbox:
                break
    polygon = None
    if isinstance(payload, dict):
        polygon = payload.get("roi_polygon") or payload.get("polygon")
    if bbox:
        label = f"{event_type} track={event.get('track_id', '')}".strip()
        items.append({"kind": "bbox", "target": "person", "bbox": list(bbox), "label": label})
    else:
        missing.append("person_bbox")
    if isinstance(polygon, list) and polygon:
        items.append({"kind": "polygon", "target": "roi", "points": polygon, "label": "ROI"})
    else:
        missing.append("roi_polygon")
    return {
        "type": "behavior_event",
        "items": items,
        "overlay_missing_reason": ", ".join(missing) if missing else None,
    }


def capture_rtsp_current_frame_opencv(rtsp_url: str) -> tuple[Any | None, str | None]:
    """Capture one best-effort current frame from RTSP using OpenCV."""
    try:
        import cv2
    except Exception as exc:
        return None, f"opencv_import_failed: {exc}"

    cap = cv2.VideoCapture(rtsp_url)
    try:
        if not cap.isOpened():
            return None, "opencv_capture_open_failed"
        ok, frame = cap.read()
        if not ok or frame is None:
            return None, "opencv_capture_read_failed"
        return frame, None
    except Exception as exc:
        logger.exception("opencv RTSP capture failed")
        return None, f"opencv_capture_exception: {exc}"
    finally:
        cap.release()


def draw_overlay(image: Any, overlay: dict[str, Any], event: dict[str, Any]) -> None:
    """Draw lightweight snapshot overlay in-place."""
    import cv2

    label = (
        f"{event.get('event_type', '')} cam={event.get('camera_id', '')} "
        f"track={event.get('track_id', '')}"
    ).strip()
    _draw_label(cv2, image, label, 12, 28)

    for item in overlay.get("items", []):
        if item.get("kind") == "bbox":
            bbox = _as_bbox(item.get("bbox"))
            if not bbox:
                continue
            x1, y1, x2, y2 = bbox
            cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 0), 2)
            _draw_label(cv2, image, str(item.get("label", "")), x1, max(20, y1 - 8))
        elif item.get("kind") == "polygon":
            points = item.get("points") or []
            if len(points) < 3:
                continue
            pts = [(int(float(p[0])), int(float(p[1]))) for p in points if len(p) >= 2]
            for idx, point in enumerate(pts):
                cv2.line(image, point, pts[(idx + 1) % len(pts)], (255, 200, 0), 2)


def write_snapshot_jpg(
    *,
    rtsp_url: str | None,
    output_path: str,
    event: dict[str, Any],
    overlay: dict[str, Any],
    capture_backend: str = "opencv",
) -> dict[str, Any]:
    """Capture and write snapshot.jpg.

    R3.2A production default is OpenCV/GStreamer-compatible capture. This
    implementation supports OpenCV by default. ffmpeg command-line capture is
    intentionally not used here.
    """
    capture = {
        "capture_backend": capture_backend,
        "snapshot_capture_mode": "rtsp_current" if rtsp_url else "not_available",
        "exact_event_frame": False,
        "fallback_used": False,
        "fallback_reason": None,
        "source_url_redacted": True,
    }

    if capture_backend != "opencv":
        return {
            "snapshot_status": "not_implemented",
            "snapshot_path": None,
            "capture": capture,
            "error_message": f"capture_backend_not_implemented: {capture_backend}",
        }
    if not rtsp_url:
        return {
            "snapshot_status": "not_implemented",
            "snapshot_path": None,
            "capture": capture,
            "error_message": "no RTSP URL or frame source available",
        }

    frame, error = capture_rtsp_current_frame_opencv(rtsp_url)
    if frame is None:
        return {
            "snapshot_status": "failed",
            "snapshot_path": None,
            "capture": capture,
            "error_message": error or "opencv_capture_failed",
        }

    import cv2

    draw_overlay(frame, overlay, event)
    ok = cv2.imwrite(output_path, frame)
    if not ok:
        return {
            "snapshot_status": "failed",
            "snapshot_path": None,
            "capture": capture,
            "error_message": "opencv_imwrite_failed",
        }
    return {
        "snapshot_status": "ready",
        "snapshot_path": output_path,
        "capture": capture,
        "error_message": None,
    }
