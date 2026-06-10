"""Draw person bounding boxes on snapshot frames.

Handles Savant center-based (xc, yc, width, height, angle) bbox format,
converting to top-left for PIL drawing.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

BBOX_COLOR = (220, 30, 30)         # red outline
BBOX_WIDTH_DEFAULT = 3
LABEL_BG = (20, 20, 20, 180)      # semi-transparent dark background
LABEL_TEXT = (255, 255, 255)       # white text
LABEL_FONT_SIZE = 14
HEADER_FONT_SIZE = 16
LINE_HEIGHT = 18
PADDING_X = 8
PADDING_Y = 6


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for font_path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _draw_label_block(
    draw: ImageDraw.ImageDraw,
    event_id: str,
    event_type: str,
    track_id: str,
    object_count: int,
    frame_num: int,
    compact: bool = False,
) -> None:
    """Draw info label block in the top-left corner.

    When compact=True, a smaller single-line block is drawn (for video overlays).
    """
    if compact:
        line = f"E:{event_id[:8]} T:{track_id} F:{frame_num} N:{object_count}"
        font = _load_font(13)
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.rectangle([0, 0, tw + 10, th + 6], fill=LABEL_BG)
        draw.text((5, 3), line, fill=LABEL_TEXT, font=font)
        return

    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        f"EVENT: {event_id[:8]}...  frame={frame_num}",
        f"Type: {event_type}  |  Track: {track_id}  |  Objects: {object_count}",
        f"Phase E1 — Dev Path (track_id-matched)",
        f"{ts_str}",
    ]

    header_font = _load_font(HEADER_FONT_SIZE)
    label_font = _load_font(LABEL_FONT_SIZE)

    line_heights = [HEADER_FONT_SIZE + 4] + [LABEL_FONT_SIZE + 2] * (len(lines) - 1)
    total_height = sum(line_heights) + PADDING_Y * 2
    max_width = 0
    for i, line in enumerate(lines):
        font = header_font if i == 0 else label_font
        bbox = draw.textbbox((0, 0), line, font=font)
        max_width = max(max_width, bbox[2] - bbox[0])

    block_w = max_width + PADDING_X * 4
    draw.rectangle([0, 0, block_w, total_height], fill=LABEL_BG)

    y = PADDING_Y
    for i, line in enumerate(lines):
        font = header_font if i == 0 else label_font
        draw.text((PADDING_X * 2, y), line, fill=LABEL_TEXT, font=font)
        y += line_heights[i]


def draw_bboxes_on_image(
    img: Image.Image,
    objects: List[Dict[str, Any]],
    event_id: str = "unknown",
    event_type: str = "intrusion",
    track_id: str = "0",
    frame_num: int = 0,
    bbox_width: int = BBOX_WIDTH_DEFAULT,
    compact: bool = False,
) -> int:
    """Draw person bboxes onto an in-memory PIL Image. Returns count drawn."""
    draw = ImageDraw.Draw(img)
    drawn = 0
    for obj in objects:
        bbox = obj.get("bbox")
        if not bbox or not isinstance(bbox, dict):
            continue
        xc = float(bbox.get("xc", 0))
        yc = float(bbox.get("yc", 0))
        w = float(bbox.get("width", 0))
        h = float(bbox.get("height", 0))
        if w <= 0 or h <= 0:
            continue
        x = xc - w / 2.0
        y = yc - h / 2.0
        draw.rectangle([x, y, x + w, y + h], outline=BBOX_COLOR, width=bbox_width)
        drawn += 1

    _draw_label_block(
        draw,
        event_id=event_id,
        event_type=event_type,
        track_id=track_id,
        object_count=drawn,
        frame_num=frame_num,
        compact=compact,
    )
    return drawn


def draw_bboxes_on_frame(
    input_path: str,
    objects: List[Dict[str, Any]],
    output_path: str,
    event_id: str = "unknown",
    event_type: str = "intrusion",
    track_id: str = "0",
    frame_num: int = 0,
) -> bool:
    """Draw all person bounding boxes from Savant metadata onto a snapshot file.

    Converts center-based (xc, yc, width, height) to top-left for drawing.
    Draws a label block in the top-left corner.
    """
    if not os.path.isfile(input_path):
        logger.error("input snapshot not found: %s", input_path)
        return False

    try:
        img = Image.open(input_path).convert("RGB")
    except Exception:
        logger.exception("failed to open snapshot %s", input_path)
        return False

    drawn = draw_bboxes_on_image(
        img, objects,
        event_id=event_id, event_type=event_type,
        track_id=track_id, frame_num=frame_num,
    )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    try:
        img.save(output_path, "JPEG", quality=92)
    except Exception:
        logger.exception("failed to save annotated snapshot %s", output_path)
        return False

    logger.info("annotated snapshot saved: %d bboxes -> %s", drawn, output_path)
    return True
