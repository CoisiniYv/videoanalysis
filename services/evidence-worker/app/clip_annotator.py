"""Annotated clip generation — render person bboxes onto every frame of a clip.

Pipes raw RGB frames from ffmpeg through PIL bbox drawing into a second
ffmpeg process that encodes the annotated stream to h264 mp4.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Dict

from PIL import Image

from app.bbox_draw import draw_bboxes_on_image

logger = logging.getLogger(__name__)


def extract_annotated_clip(
    video_path: str,
    metadata_path: str,
    event_frame_num: int,
    pre_seconds: float,
    post_seconds: float,
    fps: float,
    output_path: str,
    event_id: str = "unknown",
    event_type: str = "intrusion",
    track_id: str = "0",
) -> bool:
    """Generate an annotated clip with per-frame bbox overlay.

    Uses two ffmpeg processes connected via raw RGB24 pipe:
      ffmpeg decode (video.mov → raw RGB24 frames)
        → Python PIL bbox drawing
        → ffmpeg encode (raw RGB24 → h264 mp4)
    """
    start_frame = max(0, event_frame_num - int(pre_seconds * fps))
    end_frame = event_frame_num + int(post_seconds * fps)
    start_time = start_frame / fps
    n_frames = end_frame - start_frame + 1

    # Build frame→objects lookup for the clip range
    frame_objects: Dict[int, list] = {}
    width, height = 1920, 1080
    with open(metadata_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            fn = frame.get("frame_num", 0)
            if fn == 0:
                width = frame.get("width", width)
                height = frame.get("height", height)
            if start_frame <= fn <= end_frame:
                objs = (frame.get("metadata") or {}).get("objects") or []
                if objs:
                    frame_objects[fn] = objs

    if not frame_objects:
        logger.warning("no frames with objects in clip range [%d, %d]", start_frame, end_frame)
        return False

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    frame_size = width * height * 3

    # Decode: raw RGB24 frames from video.mov
    extract = subprocess.Popen(
        [
            "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
            "-ss", f"{start_time:.3f}",
            "-i", video_path,
            "-frames:v", str(n_frames),
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-",
        ],
        stdout=subprocess.PIPE,
    )

    # Encode: raw RGB24 frames → h264 mp4
    encode = subprocess.Popen(
        [
            "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{width}x{height}",
            "-r", str(fps),
            "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            output_path,
        ],
        stdin=subprocess.PIPE,
    )

    drawn_frames = 0
    try:
        for fn in range(start_frame, end_frame + 1):
            raw = extract.stdout.read(frame_size)
            if len(raw) < frame_size:
                break

            img = Image.frombytes("RGB", (width, height), raw)

            if fn in frame_objects:
                draw_bboxes_on_image(
                    img, frame_objects[fn],
                    event_id=event_id, event_type=event_type,
                    track_id=track_id, frame_num=fn,
                )
                drawn_frames += 1

            encode.stdin.write(img.tobytes())
    except BrokenPipeError:
        logger.error("pipe broken during annotated clip generation")
    finally:
        extract.stdout.close()
        encode.stdin.close()
        extract.wait()
        encode.wait()

    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        logger.error("annotated clip output missing or empty: %s", output_path)
        return False

    logger.info(
        "annotated clip saved: %d/%d frames drawn → %s (%.1f MB)",
        drawn_frames, n_frames, output_path,
        os.path.getsize(output_path) / (1024 * 1024),
    )
    return True
