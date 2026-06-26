"""Camera preview frame capture helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2


class CameraPreviewError(RuntimeError):
    """Raised when a camera preview frame cannot be captured."""


@dataclass(frozen=True)
class CameraPreviewFrame:
    data: bytes
    width: int
    height: int
    source_width: int
    source_height: int


def capture_camera_preview_jpeg(
    rtsp_url: str,
    *,
    rtsp_transport: str = "tcp",
    timeout_ms: int = 3000,
    max_width: int = 1280,
    jpeg_quality: int = 85,
) -> CameraPreviewFrame:
    """Capture one camera frame and return it as JPEG bytes.

    The 8090 operator UI cannot consume RTSP directly, so the API exposes a
    small HTTP JPEG bridge for ROI drawing. This helper is intentionally
    single-frame and side-effect free.
    """

    url = str(rtsp_url or "").strip()
    if not url:
        raise CameraPreviewError("camera rtsp_url is empty")
    if max_width < 160:
        max_width = 160
    if jpeg_quality < 30 or jpeg_quality > 95:
        jpeg_quality = 85

    cap = cv2.VideoCapture()
    try:
        _set_capture_timeouts(cap, timeout_ms)
        _set_rtsp_transport(cap, rtsp_transport)
        opened = _open_capture(cap, url)
        if not opened:
            raise CameraPreviewError("failed to open camera stream")

        ok, frame = _read_latest_frame(cap)
        if not ok or frame is None:
            raise CameraPreviewError("failed to read camera frame")

        source_height, source_width = frame.shape[:2]
        if source_width <= 0 or source_height <= 0:
            raise CameraPreviewError("camera frame has invalid dimensions")

        output = _resize_frame(frame, max_width=max_width)
        height, width = output.shape[:2]
        ok, encoded = cv2.imencode(
            ".jpg",
            output,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)],
        )
        if not ok:
            raise CameraPreviewError("failed to encode preview frame")
        return CameraPreviewFrame(
            data=encoded.tobytes(),
            width=int(width),
            height=int(height),
            source_width=int(source_width),
            source_height=int(source_height),
        )
    finally:
        cap.release()


def _set_capture_timeouts(cap: Any, timeout_ms: int) -> None:
    timeout = max(int(timeout_ms or 0), 500)
    for prop_name in ("CAP_PROP_OPEN_TIMEOUT_MSEC", "CAP_PROP_READ_TIMEOUT_MSEC"):
        prop = getattr(cv2, prop_name, None)
        if prop is None:
            continue
        try:
            cap.set(prop, timeout)
        except Exception:
            pass


def _set_rtsp_transport(cap: Any, rtsp_transport: str) -> None:
    transport = str(rtsp_transport or "").strip().lower()
    if transport not in {"tcp", "udp"}:
        return
    prop = getattr(cv2, "CAP_PROP_RTSP_TRANSPORT", None)
    if prop is None:
        return
    try:
        cap.set(prop, 1 if transport == "tcp" else 0)
    except Exception:
        pass


def _open_capture(cap: Any, url: str) -> bool:
    try:
        return bool(cap.open(url, cv2.CAP_FFMPEG))
    except TypeError:
        return bool(cap.open(url))


def _read_latest_frame(cap: Any) -> tuple[bool, Any]:
    ok = False
    frame = None
    for _ in range(3):
        ok, candidate = cap.read()
        if ok and candidate is not None:
            frame = candidate
        else:
            break
    return bool(frame is not None), frame


def _resize_frame(frame: Any, *, max_width: int) -> Any:
    height, width = frame.shape[:2]
    if width <= max_width:
        return frame
    scale = max_width / float(width)
    target = (int(max_width), max(1, int(round(height * scale))))
    return cv2.resize(frame, target, interpolation=cv2.INTER_AREA)
