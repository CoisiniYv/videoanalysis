"""PTS-domain frame sampler used before Savant analysis."""

from __future__ import annotations

from fractions import Fraction
from typing import Any


NANOS_PER_SECOND = 1_000_000_000


class AnalysisFrameSampler:
    """Admit at most ``max_fps`` frames per source in the frame PTS domain."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        max_fps: str | float | int = "8/1",
        min_fps: str | float | int | None = "2/1",
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
        self._last_accepted_pts_ns_by_source: dict[str, int] = {}

    def admit(self, video_frame: Any) -> bool:
        source_key = _source_key(video_frame)
        if _content_is_none(video_frame):
            return False
        if not self.enabled or self.min_interval_ns <= 0:
            self._accept(source_key, _frame_pts_ns(video_frame))
            return True

        pts_ns = _frame_pts_ns(video_frame)
        if pts_ns is None:
            self._accept(source_key, None)
            return True
        if _is_keyframe(video_frame):
            self._accept(source_key, pts_ns)
            return True

        last_pts_ns = self._last_accepted_pts_ns_by_source.get(source_key)
        if last_pts_ns is None or pts_ns <= last_pts_ns:
            self._accept(source_key, pts_ns)
            return True
        if pts_ns - last_pts_ns + self.pts_quantization_slack_ns >= self.min_interval_ns:
            self._accept(source_key, pts_ns)
            return True
        return False

    def _accept(self, source_key: str, pts_ns: int | None) -> None:
        if pts_ns is not None:
            self._last_accepted_pts_ns_by_source[source_key] = int(pts_ns)


def _source_key(video_frame: Any) -> str:
    source_id = str(getattr(video_frame, "source_id", "") or "")
    return source_id or "_unknown_source"


def _frame_pts_ns(video_frame: Any) -> int | None:
    pts = _int_or_none(getattr(video_frame, "pts", None))
    if pts is None:
        return None
    scale = _time_base_to_seconds(getattr(video_frame, "time_base", None))
    if scale is None:
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
            return float(Fraction(value.strip()))
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
