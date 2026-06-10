"""Annotated snapshot — draw bbox, labels, and zone polygon on raw snapshots."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

BBOX_COLOR = (220, 30, 30)        # red
BBOX_WIDTH = 3
POLYGON_COLOR = (50, 200, 50)     # green
POLYGON_WIDTH = 2
TEXT_BG = (0, 0, 0, 160)          # semi-transparent black
TEXT_COLOR = (255, 255, 255)      # white
LABEL_FONT_SIZE = 14
HEADER_FONT_SIZE = 16
LINE_HEIGHT = 18
PADDING_X = 8
PADDING_Y = 6


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
    except Exception:
        pass
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except Exception:
        pass
    return ImageFont.load_default()


def _draw_bbox(draw: ImageDraw.ImageDraw, bbox: Dict[str, Any]) -> None:
    """Draw a detection bounding box rectangle."""
    x = float(bbox.get("x", 0))
    y = float(bbox.get("y", 0))
    w = float(bbox.get("width", 0))
    h = float(bbox.get("height", 0))
    if w <= 0 or h <= 0:
        return
    draw.rectangle([x, y, x + w, y + h], outline=BBOX_COLOR, width=BBOX_WIDTH)


def _draw_polygon(draw: ImageDraw.ImageDraw, polygon: List[Tuple[float, float]]) -> None:
    """Draw a zone/ROI polygon outline."""
    if len(polygon) < 3:
        return
    pts = [(float(p[0]), float(p[1])) for p in polygon]
    draw.polygon(pts, outline=POLYGON_COLOR, width=POLYGON_WIDTH)


def _draw_label_block(
    draw: ImageDraw.ImageDraw,
    img: Image.Image,
    event_id: str,
    event_type: str,
    camera_id: str,
    track_id: str,
    confidence: float,
    event_ts_ms: int,
) -> None:
    """Draw a text label block in the top-left corner."""
    try:
        ts_str = datetime.fromtimestamp(event_ts_ms / 1000.0, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except Exception:
        ts_str = f"ts_ms={event_ts_ms}"

    lines = [
        f"EVENT: {event_id[:8]}...",
        f"Type: {event_type}  |  Camera: {camera_id}",
        f"Track: {track_id}  |  Conf: {confidence:.0%}",
        f"{ts_str}",
    ]

    header_font = _load_font(HEADER_FONT_SIZE)
    label_font = _load_font(LABEL_FONT_SIZE)

    # Measure block dimensions
    line_heights = [HEADER_FONT_SIZE + 4] + [LABEL_FONT_SIZE + 2] * (len(lines) - 1)
    total_height = sum(line_heights) + PADDING_Y * 2
    max_width = 0
    for i, line in enumerate(lines):
        font = header_font if i == 0 else label_font
        bbox = draw.textbbox((0, 0), line, font=font)
        max_width = max(max_width, bbox[2] - bbox[0])

    block_w = max_width + PADDING_X * 4
    block_h = total_height

    # Draw opaque dark background rectangle
    draw.rectangle([0, 0, block_w, block_h], fill=(20, 20, 20))

    # Draw text lines
    y = PADDING_Y
    for i, line in enumerate(lines):
        font = header_font if i == 0 else label_font
        draw.text((PADDING_X * 2, y), line, fill=TEXT_COLOR, font=font)
        y += line_heights[i]


def _extract_polygon(payload: Dict[str, Any]) -> List[Tuple[float, float]] | None:
    """Extract zone polygon from payload, if available."""
    # Check payload.polygon
    poly = payload.get("polygon")
    if isinstance(poly, list) and len(poly) >= 3:
        return [(float(p[0]), float(p[1])) for p in poly]

    # Check payload.zone_polygon
    poly = payload.get("zone_polygon")
    if isinstance(poly, list) and len(poly) >= 3:
        return [(float(p[0]), float(p[1])) for p in poly]

    return None


def generate_annotated_snapshot(
    event_id: str,
    raw_snapshot_path: str,
    payload: Dict[str, Any],
    annotated_output_dir: str,
    bbox_trusted: bool = False,
) -> Dict[str, Any]:
    """Generate an annotated snapshot from a raw snapshot.

    Draws a text label block.  Optionally draws the event bbox (only when
    *bbox_trusted* is True) and the zone polygon (if available).

    Args:
        event_id: Event UUID, used for the output filename.
        raw_snapshot_path: Path to the raw snapshot JPEG.
        payload: The full ``events.payload`` JSONB column.
        annotated_output_dir: Directory for annotated snapshot files.
        bbox_trusted: If True, the payload bbox came from a trusted source
            (e.g. real-time Savant detection) and should be drawn.  If False,
            the bbox is skipped and ``bbox_overlay_status`` records why.

    Returns dict with keys:
        annotated_snapshot_path: Absolute path or None.
        annotated_snapshot_status: ``"ready"`` or ``"failed"``.
        bbox_overlay_status: ``"ready"``, ``"skipped_missing_bbox"``,
            ``"skipped_untrusted_bbox"``, or None.
        zone_overlay_status: ``"ok"``, ``"skipped_missing_polygon"``, or None.
        annotated_snapshot_error_message: Present only on failure.
    """
    # 0. Normalize event_id to str (psycopg returns UUID objects)
    event_id = str(event_id)

    # 1. Check raw snapshot exists
    if not raw_snapshot_path or not os.path.isfile(raw_snapshot_path):
        return {
            "annotated_snapshot_path": None,
            "annotated_snapshot_status": "failed",
            "bbox_overlay_status": None,
            "zone_overlay_status": None,
            "annotated_snapshot_error_message": (
                f"raw snapshot not found: {raw_snapshot_path or '(empty)'}"
            ),
        }

    # 2. Open image
    try:
        img = Image.open(raw_snapshot_path).convert("RGB")
    except Exception:
        logger.exception("failed to open raw snapshot %s", raw_snapshot_path)
        return {
            "annotated_snapshot_path": None,
            "annotated_snapshot_status": "failed",
            "bbox_overlay_status": None,
            "zone_overlay_status": None,
            "annotated_snapshot_error_message": "failed to open raw snapshot image",
        }

    # 3. Draw overlays
    draw = ImageDraw.Draw(img)

    # 3a. Bbox — only when trusted
    bbox_overlay_status = None
    bbox = payload.get("bbox")
    if bbox and isinstance(bbox, dict):
        if bbox_trusted:
            _draw_bbox(draw, bbox)
            bbox_overlay_status = "ready"
        else:
            bbox_overlay_status = "skipped_untrusted_bbox"
    else:
        bbox_overlay_status = "skipped_missing_bbox"

    # 3b. Label block (always drawn)
    event_type = str(payload.get("event_type") or "unknown")
    camera_id = str(payload.get("camera_id") or payload.get("source_id", "unknown"))
    track_id = str(payload.get("track_id", "unknown"))
    confidence = float(payload.get("confidence", 0.0))
    event_ts_ms = int(payload.get("event_ts_ms", 0))

    _draw_label_block(
        draw, img,
        event_id=event_id,
        event_type=event_type,
        camera_id=camera_id,
        track_id=track_id,
        confidence=confidence,
        event_ts_ms=event_ts_ms,
    )

    # 3c. Polygon overlay
    zone_overlay_status = None
    polygon = _extract_polygon(payload)
    if polygon:
        _draw_polygon(draw, polygon)
        zone_overlay_status = "ok"
    elif payload.get("zone_id"):
        zone_overlay_status = "skipped_missing_polygon"

    # 4. Save
    os.makedirs(annotated_output_dir, exist_ok=True)
    output_path = os.path.join(annotated_output_dir, f"{event_id}.jpg")
    try:
        img.save(output_path, "JPEG", quality=92)
    except Exception:
        logger.exception("failed to save annotated snapshot %s", output_path)
        return {
            "annotated_snapshot_path": None,
            "annotated_snapshot_status": "failed",
            "bbox_overlay_status": bbox_overlay_status,
            "zone_overlay_status": zone_overlay_status,
            "annotated_snapshot_error_message": "failed to save annotated snapshot",
        }

    logger.info(
        "annotated_snapshot_generated event_id=%s path=%s bbox_overlay=%s zone_overlay=%s",
        event_id, output_path, bbox_overlay_status, zone_overlay_status,
    )

    result: Dict[str, Any] = {
        "annotated_snapshot_path": output_path,
        "annotated_snapshot_status": "ready",
        "bbox_overlay_status": bbox_overlay_status,
    }
    if zone_overlay_status is not None:
        result["zone_overlay_status"] = zone_overlay_status
    return result
