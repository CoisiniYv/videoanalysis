"""Bounded drop-on-full queue for analysis frames."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from threading import Condition
from typing import Any, Callable


@dataclass(frozen=True)
class ForwarderMessage:
    topic: str
    message: Any
    content: bytes
    source_id: str
    keyframe: bool = False
    video_frame: bool = True


@dataclass(frozen=True)
class PushResult:
    accepted: bool
    dropped: ForwarderMessage | None = None
    reason: str = ""


DropCallback = Callable[[ForwarderMessage, str], None]


class BoundedDropQueue:
    """A small condition-protected queue that never blocks producers.

    With ``max_age_s`` > 0 the consumer side also bounds analysis lag: a video
    frame that waited longer than ``max_age_s`` is dropped instead of sent
    ("stale_dropped"), and that source then skips frames until its next
    keyframe ("gop_resync_dropped") so Savant never decodes a GOP whose
    keyframe was dropped. Control messages (EOS, shutdown) are never dropped.
    Evidence uses the full-rate raw branch, so this never removes footage,
    but events visible only in dropped frames are not detected. The cap only
    bounds time spent in this queue, not end-to-end alert latency. It follows
    the runtime principle "process the latest frames, drop expired ones".
    """

    def __init__(
        self,
        max_size: int,
        *,
        max_age_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_size = max(int(max_size), 1)
        self.max_age_s = max(float(max_age_s or 0.0), 0.0)
        self._clock = clock
        self._items: deque[tuple[float, ForwarderMessage]] = deque()
        self._resync_sources: set[str] = set()
        self._condition = Condition()

    def push(self, item: ForwarderMessage) -> PushResult:
        with self._condition:
            entry = (self._clock(), item)
            if len(self._items) < self.max_size:
                self._items.append(entry)
                self._condition.notify()
                return PushResult(accepted=True)

            if item.keyframe or not item.video_frame:
                for index, (_queued_at, queued) in enumerate(self._items):
                    if queued.video_frame and not queued.keyframe:
                        dropped = queued
                        del self._items[index]
                        self._items.append(entry)
                        self._condition.notify()
                        return PushResult(
                            accepted=True,
                            dropped=dropped,
                            reason="evicted_non_keyframe",
                        )

            return PushResult(accepted=False, dropped=item, reason="queue_full")

    def pop(
        self,
        timeout_s: float = 0.5,
        on_drop: DropCallback | None = None,
    ) -> ForwarderMessage | None:
        deadline = time.monotonic() + max(timeout_s, 0)
        dropped: list[tuple[ForwarderMessage, str]] = []
        result: ForwarderMessage | None = None
        with self._condition:
            while result is None:
                if not self._items:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                    continue
                queued_at, item = self._items.popleft()
                reason = self._drop_reason(queued_at, item)
                if reason:
                    dropped.append((item, reason))
                    continue
                result = item
        if on_drop is not None:
            for item, reason in dropped:
                on_drop(item, reason)
        return result

    def _drop_reason(self, queued_at: float, item: ForwarderMessage) -> str:
        if not item.video_frame:
            self._resync_sources.discard(item.source_id)
            return ""
        if self.max_age_s > 0 and self._clock() - queued_at > self.max_age_s:
            self._resync_sources.add(item.source_id)
            return "stale_dropped"
        if item.source_id in self._resync_sources:
            if item.keyframe:
                self._resync_sources.discard(item.source_id)
                return ""
            return "gop_resync_dropped"
        return ""

    def head_age_s(self) -> float:
        """Seconds the oldest queued item has waited (0 when empty)."""
        with self._condition:
            if not self._items:
                return 0.0
            return max(self._clock() - self._items[0][0], 0.0)

    def __len__(self) -> int:
        with self._condition:
            return len(self._items)
