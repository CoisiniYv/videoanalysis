"""Bounded drop-on-full queue for analysis frames."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from threading import Condition
from typing import Any


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


class BoundedDropQueue:
    """A small condition-protected queue that never blocks producers."""

    def __init__(self, max_size: int) -> None:
        self.max_size = max(int(max_size), 1)
        self._items: deque[ForwarderMessage] = deque()
        self._condition = Condition()

    def push(self, item: ForwarderMessage) -> PushResult:
        with self._condition:
            if len(self._items) < self.max_size:
                self._items.append(item)
                self._condition.notify()
                return PushResult(accepted=True)

            if item.keyframe or not item.video_frame:
                for index, queued in enumerate(self._items):
                    if queued.video_frame and not queued.keyframe:
                        dropped = queued
                        del self._items[index]
                        self._items.append(item)
                        self._condition.notify()
                        return PushResult(
                            accepted=True,
                            dropped=dropped,
                            reason="evicted_non_keyframe",
                        )

            return PushResult(accepted=False, dropped=item, reason="queue_full")

    def pop(self, timeout_s: float = 0.5) -> ForwarderMessage | None:
        deadline = time.monotonic() + max(timeout_s, 0)
        with self._condition:
            while not self._items:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._items.popleft()

    def __len__(self) -> int:
        with self._condition:
            return len(self._items)
