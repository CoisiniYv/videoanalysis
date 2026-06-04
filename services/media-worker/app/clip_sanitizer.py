"""Sanitize Replay sink video before publishing an evidence raw_clip.

The video-file-sink output may be a direct MOV remux of the RTSP H.264
bitstream. For evidence playback we normalize that file at finalization time
instead of copying it byte-for-byte.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_DECODE_ERROR_MARKERS = (
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


def _ffmpeg_exe() -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        logger.warning("ffmpeg unavailable for raw_clip sanitize")
        return None


def _decode_ok(ffmpeg: str, path: str) -> tuple[bool, int, list[str]]:
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-v", "error", "-i", path, "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except Exception as exc:
        logger.warning("raw_clip decode validation failed path=%s error=%s", path, exc)
        return False, -1, [str(exc)]

    errors = [
        line.strip()
        for line in (proc.stderr or "").splitlines()
        if line.strip()
        and any(marker in line.lower() for marker in _DECODE_ERROR_MARKERS)
    ]
    if proc.returncode != 0 and not errors:
        errors = [line.strip() for line in (proc.stderr or "").splitlines() if line.strip()]
    return proc.returncode == 0 and not errors, len(errors), errors[:5]


def _run_ffmpeg(cmd: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except Exception as exc:
        logger.warning("ffmpeg sanitize command failed cmd=%s error=%s", cmd[:8], exc)
        return False, str(exc)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        logger.warning(
            "ffmpeg sanitize command returned status=%s stderr=%s",
            proc.returncode,
            stderr[-500:],
        )
        return False, stderr[-500:]
    return True, ""


def _remux_cmd(ffmpeg: str, src: str, dst: str) -> list[str]:
    return [
        ffmpeg,
        "-y",
        "-fflags",
        "+genpts",
        "-i",
        src,
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        dst,
    ]


def _reencode_cmd(
    ffmpeg: str,
    src: str,
    dst: str,
    *,
    fps: int,
    crf: int,
    preset: str,
) -> list[str]:
    return [
        ffmpeg,
        "-y",
        "-fflags",
        "+genpts",
        "-err_detect",
        "ignore_err",
        "-i",
        src,
        "-map",
        "0:v:0",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(fps),
        "-keyint_min",
        str(fps),
        "-bf",
        "0",
        "-vsync",
        "cfr",
        "-r",
        str(fps),
        "-movflags",
        "+faststart",
        "-an",
        dst,
    ]


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except ValueError:
        return default


def _tmp_path(dst: Path, label: str) -> Path:
    return dst.with_name(f".{dst.stem}.{os.getpid()}.{label}{dst.suffix}")


def _copy_fallback(src: Path, dst: Path, result: dict, error: str) -> dict:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    result.update(
        method="copy",
        fallback_used=True,
        sanitize_error=error,
    )
    return result


def _decode_result(ffmpeg: str, path: Path, result: dict, method: str) -> dict:
    ok, count, sample = _decode_ok(ffmpeg, str(path))
    result.update(
        method=method,
        decode_ok=ok,
        decode_error_count=count,
        decode_error_sample=sample,
    )
    return result


def sanitize_raw_clip(src_video: str, dst_clip: str) -> dict:
    """Create a playable evidence raw clip.

    The function always leaves ``dst_clip`` populated when ``src_video`` exists.
    If remux/reencode fails, it falls back to byte-for-byte copy so evidence
    generation is not blocked by sanitizer failures.
    """

    result: dict = {
        "method": "",
        "decode_ok": None,
        "decode_error_count": 0,
        "decode_error_sample": [],
        "fallback_used": False,
        "sanitize_error": "",
    }
    src = Path(src_video)
    dst = Path(dst_clip)
    if not src.is_file():
        result.update(method="none", sanitize_error="source video missing")
        return result

    mode = (os.getenv("RAW_CLIP_SANITIZE_MODE", "auto") or "auto").strip().lower()
    if mode not in {"auto", "copy", "remux", "reencode"}:
        result["sanitize_error"] = f"invalid sanitize mode {mode}; using auto"
        mode = "auto"

    ffmpeg = _ffmpeg_exe()
    if mode == "copy" or ffmpeg is None:
        shutil.copy2(src, dst)
        result["method"] = "copy"
        if ffmpeg is None:
            result.update(
                fallback_used=True,
                sanitize_error="ffmpeg unavailable; copied original",
            )
            return result
        return _decode_result(ffmpeg, dst, result, "copy")

    fps = max(1, _int_env("RAW_CLIP_SANITIZE_FPS", 30))
    crf = _int_env("RAW_CLIP_SANITIZE_CRF", 20)
    preset = os.getenv("RAW_CLIP_SANITIZE_PRESET", "veryfast") or "veryfast"
    dst.parent.mkdir(parents=True, exist_ok=True)

    remux_error = ""
    if mode in {"auto", "remux"}:
        remux_tmp = _tmp_path(dst, "remux")
        remux_ok, remux_error = _run_ffmpeg(_remux_cmd(ffmpeg, str(src), str(remux_tmp)))
        if remux_ok and remux_tmp.is_file():
            remux_tmp.replace(dst)
            _decode_result(ffmpeg, dst, result, "remux")
            if mode == "remux" or result["decode_ok"] is True:
                return result
            logger.warning(
                "raw_clip remux decode validation failed path=%s errors=%s",
                dst,
                result.get("decode_error_count"),
            )
        remux_tmp.unlink(missing_ok=True)
        if mode == "remux":
            return _copy_fallback(src, dst, result, f"remux failed: {remux_error}")

    reencode_tmp = _tmp_path(dst, "reencode")
    reencode_ok, reencode_error = _run_ffmpeg(
        _reencode_cmd(
            ffmpeg,
            str(src),
            str(reencode_tmp),
            fps=fps,
            crf=crf,
            preset=preset,
        )
    )
    if reencode_ok and reencode_tmp.is_file():
        reencode_tmp.replace(dst)
        if remux_error:
            result["sanitize_error"] = f"remux failed before reencode: {remux_error}"
        return _decode_result(ffmpeg, dst, result, "reencode")
    reencode_tmp.unlink(missing_ok=True)

    error = "remux/reencode failed"
    if remux_error or reencode_error:
        error = f"remux_error={remux_error}; reencode_error={reencode_error}"
    return _copy_fallback(src, dst, result, error)
