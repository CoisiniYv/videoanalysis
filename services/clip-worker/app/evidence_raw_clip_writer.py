"""R3.2B raw clip writer for controlled event evidence."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


def _clip_seconds(task: dict[str, Any] | None) -> int:
    if not task:
        return 15
    pre = int(task.get("pre_seconds", 5) or 5)
    post = int(task.get("post_seconds", 10) or 10)
    return max(1, pre + post)


def _copy_with_ffmpeg(input_path: Path, output_path: Path, duration_s: int) -> tuple[bool, str | None]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False, "ffmpeg_not_available_for_explicit_raw_mp4_fallback"

    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "0",
        "-i",
        str(input_path),
        "-t",
        str(duration_s),
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        str(output_path),
    ]
    completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "ffmpeg_copy_failed"
        return False, stderr[:500]
    if not output_path.exists() or output_path.stat().st_size <= 0:
        return False, "ffmpeg_copy_produced_empty_raw_clip"
    return True, None


def process_raw_clip(
    *,
    event: dict[str, Any],
    task: dict[str, Any] | None,
    output_root: str,
    raw_mp4_path: str | None = None,
) -> dict[str, Any]:
    """Generate or explicitly decline R3.2B raw_clip.mp4.

    Production replay/NVR materialization is not available yet in this worker.
    Live RTSP current recording is intentionally not used as a production
    fallback because it would not represent the event pre/post window.
    """
    clip_required = bool(event.get("clip_required", False) or (task or {}).get("clip_required", False))
    pre_seconds = int((task or {}).get("pre_seconds", 5) or 5)
    post_seconds = int((task or {}).get("post_seconds", 10) or 10)
    base = {
        "clip_status": "not_implemented",
        "clip_error_message": None,
        "raw_clip_path": None,
        "annotated_clip_path": None,
        "clip_capture_mode": "not_available",
        "pre_seconds": pre_seconds,
        "post_seconds": post_seconds,
        "exact_event_clip": False,
        "remux_or_copy": True,
        "reencoded": False,
        "fallback_used": False,
        "fallback_reason": None,
        "clip_required": clip_required,
    }

    if not clip_required:
        base["clip_error_message"] = "clip_required=false"
        return base

    if not raw_mp4_path:
        base["clip_error_message"] = (
            "no reliable replay/raw clip source available; R3.2B does not use "
            "live RTSP current recording as production evidence"
        )
        return base

    source = Path(raw_mp4_path)
    if not source.is_absolute():
        base.update(
            {
                "clip_status": "failed",
                "clip_error_message": "raw_mp4_fallback_path_must_be_absolute",
                "clip_capture_mode": "raw_mp4_fallback",
                "fallback_used": True,
                "fallback_reason": "explicit_debug_raw_mp4_fallback",
            }
        )
        return base
    if not source.exists() or not source.is_file():
        base.update(
            {
                "clip_status": "failed",
                "clip_error_message": f"raw_mp4_fallback_not_found: {source}",
                "clip_capture_mode": "raw_mp4_fallback",
                "fallback_used": True,
                "fallback_reason": "explicit_debug_raw_mp4_fallback",
            }
        )
        return base

    output = Path(output_root) / "raw_clip.mp4"
    ok, error = _copy_with_ffmpeg(source, output, _clip_seconds(task))
    if not ok:
        base.update(
            {
                "clip_status": "failed",
                "clip_error_message": error,
                "clip_capture_mode": "raw_mp4_fallback",
                "fallback_used": True,
                "fallback_reason": "explicit_debug_raw_mp4_fallback",
            }
        )
        return base

    base.update(
        {
            "clip_status": "ready",
            "clip_error_message": None,
            "raw_clip_path": str(output),
            "clip_capture_mode": "raw_mp4_fallback",
            "exact_event_clip": False,
            "fallback_used": True,
            "fallback_reason": "explicit_debug_raw_mp4_fallback",
        }
    )
    return base
