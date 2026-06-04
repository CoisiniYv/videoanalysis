#!/usr/bin/env python3
"""Diagnose an evidence raw_clip video.

This script distinguishes normal B-frame display timestamp reordering from
actual decode/container problems. It prints a JSON object so smoke scripts and
manual audits can consume the same output.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

DECODE_ERROR_MARKERS = (
    "corrupt",
    "concealing",
    "decode_slice",
    "error while decoding",
    "invalid data",
    "missing reference",
    "non-existing",
    "no frame",
    "mmco",
    "could not find ref",
)


def _resolve(tool: str) -> str | None:
    found = shutil.which(tool)
    if found:
        return found
    if tool == "ffmpeg":
        try:
            import imageio_ffmpeg  # type: ignore

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


def _run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _probe_frames(ffprobe: str, path: str) -> tuple[list[dict[str, Any]], str]:
    proc = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pts_time,pkt_dts_time,pict_type,key_frame,coded_picture_number",
            "-of",
            "json",
            path,
        ]
    )
    if proc.returncode != 0:
        return [], (proc.stderr or "").strip()
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        return [], str(exc)
    frames = data.get("frames")
    return frames if isinstance(frames, list) else [], ""


def _analyze_frames(frames: list[dict[str, Any]]) -> dict[str, Any]:
    pict_types: dict[str, int] = {}
    keyframes: list[int] = []
    pts_values: list[float] = []
    dts_values: list[float] = []
    pts_backward_decode_count = 0
    dts_backward_count = 0

    for index, frame in enumerate(frames):
        pict_type = str(frame.get("pict_type") or "?").upper()
        pict_types[pict_type] = pict_types.get(pict_type, 0) + 1
        if str(frame.get("key_frame")) == "1":
            keyframes.append(index)

        pts = _float_or_none(frame.get("pts_time"))
        if pts is not None:
            if pts_values and pts < pts_values[-1]:
                pts_backward_decode_count += 1
            pts_values.append(pts)

        dts = _float_or_none(frame.get("pkt_dts_time"))
        if dts is not None:
            if dts_values and dts < dts_values[-1]:
                dts_backward_count += 1
            dts_values.append(dts)

    keyframe_gaps = [
        keyframes[index + 1] - keyframes[index]
        for index in range(len(keyframes) - 1)
    ]
    return {
        "total_frames": len(frames),
        "pict_type_distribution": pict_types,
        "has_b_frames": pict_types.get("B", 0) > 0,
        "dts_backward_count": dts_backward_count,
        "pts_backward_decode_count": pts_backward_decode_count,
        "keyframe_count": len(keyframes),
        "first_frame_is_keyframe": keyframes[:1] == [0],
        "keyframe_gaps": keyframe_gaps,
    }


def _decode_check(ffmpeg: str, path: str) -> dict[str, Any]:
    proc = _run(
        [ffmpeg, "-hide_banner", "-v", "error", "-i", path, "-f", "null", "-"]
    )
    errors = [
        line.strip()
        for line in (proc.stderr or "").splitlines()
        if line.strip()
        and any(marker in line.lower() for marker in DECODE_ERROR_MARKERS)
    ]
    if proc.returncode != 0 and not errors:
        errors = [line.strip() for line in (proc.stderr or "").splitlines() if line.strip()]
    return {
        "decode_returncode": proc.returncode,
        "decode_error_count": len(errors),
        "decode_error_sample": errors[:8],
        "decode_ok": proc.returncode == 0 and not errors,
    }


def _likely_status(frame_info: dict[str, Any], decode_info: dict[str, Any]) -> str:
    if decode_info.get("decode_error_count", 0) > 0:
        return "decode_corrupt"
    if frame_info.get("dts_backward_count", 0) > 0:
        return "dts_corrupt"
    if frame_info and not frame_info.get("first_frame_is_keyframe", True):
        return "starts_mid_gop"
    gaps = frame_info.get("keyframe_gaps") or []
    if gaps and max(gaps) > 90:
        return "sparse_gop"
    if frame_info.get("has_b_frames") and frame_info.get("pts_backward_decode_count", 0) > 0:
        return "normal_b_frame_reorder"
    if decode_info.get("decode_ok") is True:
        return "clean"
    return "unknown"


def diagnose(path: str) -> dict[str, Any]:
    video_path = Path(path)
    ffprobe = _resolve("ffprobe")
    ffmpeg = _resolve("ffmpeg")
    frame_error = ""
    frame_info: dict[str, Any] = {
        "total_frames": 0,
        "pict_type_distribution": {},
        "has_b_frames": False,
        "dts_backward_count": 0,
        "pts_backward_decode_count": 0,
        "keyframe_count": 0,
        "first_frame_is_keyframe": False,
        "keyframe_gaps": [],
    }
    if ffprobe:
        frames, frame_error = _probe_frames(ffprobe, str(video_path))
        if frames:
            frame_info = _analyze_frames(frames)
    decode_info = {
        "decode_returncode": None,
        "decode_error_count": 0,
        "decode_error_sample": [],
        "decode_ok": None,
    }
    if ffmpeg:
        decode_info = _decode_check(ffmpeg, str(video_path))

    result = {
        "path": str(video_path),
        "exists": video_path.is_file(),
        "ffprobe": ffprobe or "",
        "ffmpeg": ffmpeg or "",
        "frame_probe_error": frame_error,
        **frame_info,
        **decode_info,
    }
    result["likely_status"] = _likely_status(frame_info, decode_info)
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/tools/diagnose_raw_clip.py /path/to/raw_clip.mov")
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(json.dumps({"path": str(path), "exists": False, "likely_status": "missing"}))
        return 2
    print(json.dumps(diagnose(str(path)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
