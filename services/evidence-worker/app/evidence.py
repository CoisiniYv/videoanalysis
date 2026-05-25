"""Evidence generation from Phase 3H.2 aligned video + metadata."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def find_metadata_file(video_dir: str, source_id: str) -> str | None:
    """Locate metadata.json under video_dir for the given source_id.

    The video-file-sink writes to {video_dir}/{source_id}%/{filename}%/metadata.json
    where % is URL-encoded, but on filesystem it may appear literally or encoded.
    """
    if not os.path.isdir(video_dir):
        return None
    for entry in os.listdir(video_dir):
        entry_path = os.path.join(video_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        for sub_entry in os.listdir(entry_path):
            sub_path = os.path.join(entry_path, sub_entry)
            if not os.path.isdir(sub_path):
                continue
            candidate = os.path.join(sub_path, "metadata.json")
            if os.path.isfile(candidate):
                return candidate
    return None


def find_event_frame(
    metadata_path: str, track_id: str
) -> Tuple[int, List[Dict[str, Any]]] | None:
    """Scan metadata.json NDJSON for frames containing the given track_id.

    Returns (frame_num, objects_list) for the middle frame where the track appears,
    or None if the track is not found in any frame.
    """
    target_track = int(track_id)
    matching_frames: List[Tuple[int, List[Dict[str, Any]]]] = []

    with open(metadata_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue

            frame_num = frame.get("frame_num")
            meta = frame.get("metadata") or {}
            objects = meta.get("objects") or []

            has_track = any(
                obj.get("object_id") == target_track for obj in objects
            )
            if has_track:
                matching_frames.append((frame_num, objects))

    if not matching_frames:
        return None

    middle = matching_frames[len(matching_frames) // 2]
    logger.info(
        "track_id=%s found in %d frames, selected middle frame=%d",
        track_id, len(matching_frames), middle[0],
    )
    return middle


def extract_snapshot(
    video_path: str, frame_num: int, output_path: str
) -> bool:
    """Extract a single frame from video.mov using ffmpeg."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-i", video_path,
        "-vf", f"select=eq(n\\,{frame_num})",
        "-vframes", "1",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.isfile(output_path):
        logger.error("ffmpeg snapshot failed: %s", result.stderr)
        return False
    logger.info("snapshot extracted: frame=%d -> %s", frame_num, output_path)
    return True


def extract_clip(
    video_path: str,
    frame_num: int,
    pre_seconds: float,
    post_seconds: float,
    fps: float,
    output_path: str,
) -> bool:
    """Extract a short clip around frame_num from video.mov.

    Uses keyframe-aligned copy to avoid re-encoding. The seek point is the
    nearest keyframe before (frame_num/fps - pre_seconds).
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    event_time = frame_num / fps
    start_time = max(0, event_time - pre_seconds)
    duration = pre_seconds + post_seconds

    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-ss", f"{start_time:.3f}",
        "-i", video_path,
        "-t", f"{duration:.3f}",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.isfile(output_path):
        logger.error("ffmpeg clip failed: %s", result.stderr)
        return False
    logger.info(
        "clip extracted: frame=%d start=%.2fs dur=%.2fs -> %s",
        frame_num, start_time, duration, output_path,
    )
    return True
