"""PTS-domain ingress FPS gate for replay-first Savant inference.

Replay remains the media authority and stores every RTSP frame before Savant.
This filter only throttles frames admitted into the Savant inference graph, using
the Replay/Savant frame PTS domain rather than wall-clock or Redis publish time.
"""

from __future__ import annotations

import os
from fractions import Fraction
from typing import Any

try:
    from savant.base.frame_filter import BaseFrameFilter
except Exception:  # pragma: no cover - local tests run without Savant installed.
    class BaseFrameFilter:  # type: ignore[no-redef]
        """Fallback base for dependency-light unit tests."""

        pass


NANOS_PER_SECOND = 1_000_000_000


class PtsFpsGate(BaseFrameFilter):
    """Allow at most ``max_fps`` frames per source based on frame PTS."""

    def __init__(
        self,
        enabled: bool = True,
        max_fps: str | float | int = "8/1",
        min_fps: str | float | int | None = "2/1",
        log_every_n_frames: int = 300,
        **_kwargs: Any,
    ) -> None:
        self.enabled = _boolish(enabled)
        self.max_fps = _parse_fps(max_fps, default=8.0)
        self.min_fps = _parse_fps(min_fps, default=2.0)
        self.min_interval_ns = (
            int(round(NANOS_PER_SECOND / self.max_fps))
            if self.enabled and self.max_fps > 0
            else 0
        )
        self.pts_quantization_slack_ns = min(1_000_000, self.min_interval_ns // 1000)
        self.log_every_n_frames = max(int(log_every_n_frames), 1)
        self._last_accepted_pts_ns_by_source: dict[str, int] = {}
        self._frames_seen_by_source: dict[str, int] = {}
        self._frames_accepted_by_source: dict[str, int] = {}

        print(
            "component=savant_security_pts_fps_gate_init "
            f"enabled={self.enabled} max_fps={self.max_fps:g} "
            f"min_fps={self.min_fps:g} min_interval_ns={self.min_interval_ns}",
            flush=True,
        )

    def __call__(self, video_frame: Any) -> bool:
        source_id = str(getattr(video_frame, "source_id", "") or "")
        source_key = source_id or "_unknown_source"
        seen = self._frames_seen_by_source.get(source_key, 0) + 1
        self._frames_seen_by_source[source_key] = seen

        if _content_is_none(video_frame):
            self._log_tick(source_key)
            return False
        if not self.enabled or self.min_interval_ns <= 0:
            self._accept(source_key, _frame_pts_ns(video_frame))
            self._log_tick(source_key)
            return True

        pts_ns = _frame_pts_ns(video_frame)
        if pts_ns is None:
            # Missing PTS is not a safe basis for throttling; pass the frame so
            # downstream code can still inspect/report the missing frame domain.
            self._accept(source_key, None)
            self._log_tick(source_key)
            return True

        if _is_keyframe(video_frame):
            self._accept(source_key, pts_ns)
            self._log_tick(source_key)
            return True

        last_pts_ns = self._last_accepted_pts_ns_by_source.get(source_key)
        if last_pts_ns is None or pts_ns <= last_pts_ns:
            self._accept(source_key, pts_ns)
            self._log_tick(source_key)
            return True

        if pts_ns - last_pts_ns + self.pts_quantization_slack_ns >= self.min_interval_ns:
            self._accept(source_key, pts_ns)
            self._log_tick(source_key)
            return True

        self._log_tick(source_key)
        return False

    def _accept(self, source_key: str, pts_ns: int | None) -> None:
        if pts_ns is not None:
            self._last_accepted_pts_ns_by_source[source_key] = int(pts_ns)
        self._frames_accepted_by_source[source_key] = (
            self._frames_accepted_by_source.get(source_key, 0) + 1
        )

    def _log_tick(self, source_key: str) -> None:
        seen = self._frames_seen_by_source.get(source_key, 0)
        if seen != 1 and seen % self.log_every_n_frames != 0:
            return
        accepted = self._frames_accepted_by_source.get(source_key, 0)
        print(
            "component=savant_security_pts_fps_gate_tick "
            f"source_id={source_key} seen={seen} accepted={accepted} "
            f"enabled={self.enabled} max_fps={self.max_fps:g}",
            flush=True,
        )


def _frame_pts_ns(video_frame: Any) -> int | None:
    pts = _int_or_none(getattr(video_frame, "pts", None))
    if pts is None:
        return None
    time_base = getattr(video_frame, "time_base", None)
    scale = _time_base_to_seconds(time_base)
    if scale is None:
        # Savant currently exposes Replay PTS in nanoseconds for this path.
        return int(pts)
    return int(round(float(pts) * scale * NANOS_PER_SECOND))


def _content_is_none(video_frame: Any) -> bool:
    content = getattr(video_frame, "content", None)
    is_none = getattr(content, "is_none", None)
    if callable(is_none):
        try:
            return bool(is_none())
        except Exception:
            return False
    return False


def _is_keyframe(video_frame: Any) -> bool:
    explicit_keyframe = _bool_or_none(getattr(video_frame, "keyframe", None))
    if explicit_keyframe is True:
        return True
    frame_uuid = _text_or_none(getattr(video_frame, "uuid", None))
    keyframe_uuid = _text_or_none(getattr(video_frame, "keyframe_uuid", None))
    return bool(frame_uuid and keyframe_uuid and frame_uuid == keyframe_uuid)


def _parse_fps(value: str | float | int | None, *, default: float) -> float:
    if value in (None, ""):
        return float(default)
    try:
        if isinstance(value, str) and "/" in value:
            parsed = Fraction(value.strip())
            return float(parsed)
        return float(value)  # type: ignore[arg-type]
    except Exception:
        return float(default)


def _time_base_to_seconds(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, str) and "/" in value:
            return float(Fraction(value.strip()))
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return float(Fraction(int(value[0]), int(value[1])))
    except Exception:
        return None
    return None


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() and text not in {"None", "null"} else None


def env_pts_fps_gate() -> PtsFpsGate:
    """Factory useful for direct diagnostics outside Savant config loading."""

    return PtsFpsGate(
        enabled=os.getenv("MAX_FPS_CONTROL", "false"),
        max_fps=os.getenv("MAX_FPS", "8/1"),
        min_fps=os.getenv("MIN_FPS", "2/1"),
    )
