"""NoOpFrameCounterPyFunc — C1F.1d.4 minimal ZMQ source isolation.

Counts frames received. Logs every N frames. No model inference.
No event export. No Redis. No database.
"""

from __future__ import annotations

from typing import Any

from savant.deepstream.pyfunc import NvDsPyFuncPlugin


class NoOpFrameCounterPyFunc(NvDsPyFuncPlugin):
    """Count frames and log periodically."""

    def __init__(self, log_every_n_frames: int = 30, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._log_interval = max(int(log_every_n_frames), 1)
        self._frame_count = 0

    def process_frame(self, buffer: Any, frame_meta: Any) -> None:
        self._frame_count += 1
        if self._frame_count % self._log_interval == 1:
            source_id = getattr(frame_meta, "source_id", "?")
            print(
                f"[noop_counter] frame={self._frame_count} source={source_id}",
                flush=True,
            )
