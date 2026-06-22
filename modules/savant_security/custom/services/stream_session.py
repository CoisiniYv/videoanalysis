"""Per-source stream-session tracking for Savant frame-domain evidence.

A runtime epoch changes when the controlled midterm apply path rebuilds the
Savant/video-sink runtime. Source adapters can also restart inside the same
epoch, resetting PTS without changing that epoch. This tracker gives each
source a process-local session id so frame annotations and SecurityEvents can
fail closed instead of mixing PTS domains across adapter restarts.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any


DEFAULT_SESSION_PREFIX = "stream"
NANOS_PER_SECOND = 1_000_000_000
DEFAULT_PTS_ROLLBACK_TOLERANCE_NS = 5 * NANOS_PER_SECOND


@dataclass(frozen=True)
class StreamSessionState:
    source_id: str
    session_id: str
    last_pts: int | None
    bump_count: int


class StreamSessionTracker:
    """Track a process-local stream session per source."""

    def __init__(
        self,
        *,
        session_prefix: str = DEFAULT_SESSION_PREFIX,
        pts_rollback_tolerance_ns: int | None = None,
    ) -> None:
        self._session_prefix = str(session_prefix or DEFAULT_SESSION_PREFIX)
        self._pts_rollback_tolerance_ns = (
            _non_negative_int_or_default(
                os.getenv("STREAM_SESSION_PTS_ROLLBACK_TOLERANCE_NS"),
                DEFAULT_PTS_ROLLBACK_TOLERANCE_NS,
            )
            if pts_rollback_tolerance_ns is None
            else max(int(pts_rollback_tolerance_ns), 0)
        )
        self._state_by_source: dict[str, StreamSessionState] = {}

    def session_id_for_frame(self, source_id: str, frame_pts: Any) -> str:
        source_key = str(source_id or "__unknown_source__")
        pts = _int_or_none(frame_pts)
        state = self._state_by_source.get(source_key)
        if state is None:
            state = StreamSessionState(
                source_id=source_key,
                session_id=self._new_session_id(source_key),
                last_pts=pts,
                bump_count=0,
            )
            self._state_by_source[source_key] = state
            return state.session_id

        last_pts = state.last_pts
        if pts is not None and last_pts is not None and pts < last_pts:
            rollback_delta = last_pts - pts
            if rollback_delta <= self._pts_rollback_tolerance_ns:
                return state.session_id
            state = StreamSessionState(
                source_id=source_key,
                session_id=self._new_session_id(source_key),
                last_pts=pts,
                bump_count=state.bump_count + 1,
            )
            self._state_by_source[source_key] = state
            return state.session_id

        if pts is not None and pts != last_pts:
            state = StreamSessionState(
                source_id=source_key,
                session_id=state.session_id,
                last_pts=pts,
                bump_count=state.bump_count,
            )
            self._state_by_source[source_key] = state
        return state.session_id

    def state_for_source(self, source_id: str) -> StreamSessionState | None:
        return self._state_by_source.get(str(source_id or "__unknown_source__"))

    def _new_session_id(self, source_id: str) -> str:
        safe_source = _safe_session_part(source_id)
        return f"{self._session_prefix}-{safe_source}-{uuid.uuid4().hex}"


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _non_negative_int_or_default(value: Any, default: int) -> int:
    parsed = _int_or_none(value)
    if parsed is None or parsed < 0:
        return int(default)
    return int(parsed)


def _safe_session_part(value: str) -> str:
    text = str(value or "source").strip() or "source"
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in text)
    return safe[:80] or "source"


_GLOBAL_TRACKER = StreamSessionTracker()


def stream_session_id_for_frame(source_id: str, frame_pts: Any) -> str:
    """Return the shared process-local stream session for a source frame."""

    return _GLOBAL_TRACKER.session_id_for_frame(source_id, frame_pts)
