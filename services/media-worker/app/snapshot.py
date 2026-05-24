"""Snapshot extraction — extract a frame from a Replay clip at pre_seconds offset."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_FFMPEG_EXE = None


def _get_ffmpeg() -> str:
    """Return path to ffmpeg binary, preferring imageio-ffmpeg if installed."""
    global _FFMPEG_EXE
    if _FFMPEG_EXE is not None:
        return _FFMPEG_EXE
    try:
        import imageio_ffmpeg
        _FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        _FFMPEG_EXE = "ffmpeg"
    return _FFMPEG_EXE


def _ffmpeg_duration(filepath: str) -> Optional[float]:
    """Return duration in seconds using ffmpeg stderr parsing, or None on failure."""
    ffmpeg = _get_ffmpeg()
    try:
        result = subprocess.run(
            [ffmpeg, "-i", filepath],
            capture_output=True, text=True, timeout=30,
        )
        # ffmpeg writes info to stderr; look for "Duration: HH:MM:SS.ms"
        match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", result.stderr)
        if match:
            h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
            return h * 3600 + m * 60 + s
        return None
    except Exception:
        logger.exception("ffmpeg duration detection failed for %s", filepath)
        return None


def _ffmpeg_extract(filepath: str, offset_seconds: float, output_path: str) -> bool:
    """Extract a single frame at *offset_seconds* using ffmpeg. Returns True on success."""
    ffmpeg = _get_ffmpeg()
    try:
        result = subprocess.run(
            [
                ffmpeg, "-y",
                "-ss", str(offset_seconds),
                "-i", filepath,
                "-frames:v", "1",
                "-q:v", "2",
                output_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.error(
                "ffmpeg extraction failed clip=%s offset=%.2f stderr=%s",
                filepath, offset_seconds, result.stderr[-500:],
            )
        return result.returncode == 0 and os.path.isfile(output_path)
    except FileNotFoundError:
        logger.error("ffmpeg not found on PATH (tried: %s)", ffmpeg)
        return False
    except Exception:
        logger.exception("ffmpeg failed for %s offset=%.2f", filepath, offset_seconds)
        return False


def generate_snapshot(
    event_id: str,
    clip_path: str,
    pre_seconds: float,
    output_dir: str,
) -> dict:
    """Extract a snapshot frame from *clip_path* at offset *pre_seconds*.

    Args:
        event_id: Event UUID, used for the output filename.
        clip_path: Path to the ready clip file.
        pre_seconds: Desired offset in seconds from clip start (the pre-event window).
        output_dir: Directory to write snapshot JPEG files.

    Returns dict with keys:
        snapshot_path: Absolute path to the generated snapshot file.
        snapshot_status: "ready" or "failed".
        snapshot_offset_seconds: Actual offset used for extraction.
        snapshot_fallback_reason: Present only if fallback occurred.
        error_message: Present only if extraction failed.
    """
    # 1. Check ffmpeg availability
    try:
        _get_ffmpeg()
    except Exception:
        return {
            "snapshot_path": None,
            "snapshot_status": "failed",
            "snapshot_offset_seconds": None,
            "error_message": "ffmpeg not available (install imageio-ffmpeg or ffmpeg)",
        }

    # 2. Check clip exists
    if not clip_path or not os.path.isfile(clip_path):
        return {
            "snapshot_path": None,
            "snapshot_status": "failed",
            "snapshot_offset_seconds": None,
            "error_message": f"clip file not found: {clip_path or '(empty)'}",
        }

    # 3. Determine snapshot offset
    duration = _ffmpeg_duration(clip_path)
    offset = float(pre_seconds)
    fallback_reason = None

    if duration is not None and duration > 0:
        if duration > pre_seconds:
            offset = float(pre_seconds)
        else:
            offset = max(0.0, duration / 2.0)
            fallback_reason = (
                f"clip_duration_shorter_than_pre_seconds "
                f"(duration={duration:.2f}s pre_seconds={pre_seconds}s)"
            )
    else:
        logger.warning(
            "ffprobe could not read duration for %s, attempting pre_seconds=%.2f",
            clip_path, pre_seconds,
        )

    # 4. Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{event_id}.jpg")

    # 5. Extract frame
    if _ffmpeg_extract(clip_path, offset, output_path):
        logger.info(
            "snapshot_extracted event_id=%s offset=%.2f fallback=%s path=%s",
            event_id, offset, fallback_reason, output_path,
        )
        result = {
            "snapshot_path": output_path,
            "snapshot_status": "ready",
            "snapshot_offset_seconds": offset,
        }
        if fallback_reason:
            result["snapshot_fallback_reason"] = fallback_reason
        return result

    # 6. ffmpeg extraction failed — try fallback if not already tried
    if duration is not None and duration > 0 and offset != max(0.0, duration / 2.0):
        fallback_offset = max(0.0, duration / 2.0)
        logger.warning(
            "retrying snapshot with fallback offset=%.2f for event_id=%s",
            fallback_offset, event_id,
        )
        if _ffmpeg_extract(clip_path, fallback_offset, output_path):
            return {
                "snapshot_path": output_path,
                "snapshot_status": "ready",
                "snapshot_offset_seconds": fallback_offset,
                "snapshot_fallback_reason": (
                    f"ffmpeg_failed_at_pre_seconds_retried_at_mid "
                    f"(pre_seconds={pre_seconds}s offset={fallback_offset:.2f}s)"
                ),
            }

    return {
        "snapshot_path": None,
        "snapshot_status": "failed",
        "snapshot_offset_seconds": offset if duration else None,
        "error_message": "ffmpeg frame extraction failed",
    }
