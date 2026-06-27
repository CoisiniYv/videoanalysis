"""Bounded async Redis Stream writer for Savant hot-path exporters."""

from __future__ import annotations

import os
import queue
import threading
from typing import Any


DEFAULT_SOCKET_TIMEOUT_MS = 50
DEFAULT_CONNECT_TIMEOUT_MS = 50
DEFAULT_QUEUE_MAXSIZE = 1024
_STOP = object()


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return int(default)


class AsyncRedisStreamWriter:
    """Drop-on-full async wrapper around Redis ``XADD``.

    Savant pyfuncs run on the inference path, so Redis jitter must not block
    ``process_frame``. Exporters enqueue bounded work here and let the daemon
    thread perform the actual ``XADD``.
    """

    def __init__(
        self,
        *,
        redis_url: str,
        stream: str,
        maxlen: int,
        component: str,
        socket_timeout_ms: int = DEFAULT_SOCKET_TIMEOUT_MS,
        connect_timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        redis_module: Any | None = None,
        start_worker: bool = True,
    ) -> None:
        if redis_module is None:
            import redis as redis_module

        self.redis_url = redis_url
        self.stream = stream
        self.maxlen = int(maxlen)
        self.component = component
        self.socket_timeout_ms = max(int(socket_timeout_ms), 1)
        self.connect_timeout_ms = max(int(connect_timeout_ms), 1)
        self.queue_maxsize = max(int(queue_maxsize), 1)
        self.enqueued_count = 0
        self.dropped_count = 0
        self.write_error_count = 0
        self._closed = False
        self._queue: queue.Queue[dict[str, Any] | object] = queue.Queue(
            maxsize=self.queue_maxsize
        )
        self._client = redis_module.Redis.from_url(
            self.redis_url,
            socket_timeout=self.socket_timeout_ms / 1000.0,
            socket_connect_timeout=self.connect_timeout_ms / 1000.0,
        )
        self._thread: threading.Thread | None = None
        if start_worker:
            self._thread = threading.Thread(
                target=self._run,
                name=f"{self.component}-redis-stream-writer",
                daemon=True,
            )
            self._thread.start()
        self._log(
            "init",
            stream=self.stream,
            maxlen=self.maxlen,
            queue_maxsize=self.queue_maxsize,
            socket_timeout_ms=self.socket_timeout_ms,
            connect_timeout_ms=self.connect_timeout_ms,
        )

    def enqueue(self, fields: dict[str, Any]) -> bool:
        if self._closed:
            self.dropped_count += 1
            self._log("drop", reason="writer_closed")
            return False
        try:
            self._queue.put_nowait(dict(fields))
        except queue.Full:
            self.dropped_count += 1
            self._log("drop", reason="queue_full", dropped_count=self.dropped_count)
            return False
        self.enqueued_count += 1
        return True

    def flush(self, timeout_s: float = 1.0) -> bool:
        """Best-effort drain helper used by tests."""

        deadline = threading.Event()
        done = False

        def _wait() -> None:
            self._queue.join()
            nonlocal done
            done = True
            deadline.set()

        waiter = threading.Thread(target=_wait, daemon=True)
        waiter.start()
        deadline.wait(max(float(timeout_s), 0.0))
        return done

    def close(self, timeout_s: float = 0.2) -> None:
        self._closed = True
        if self._thread is None:
            return
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            return
        self._thread.join(timeout=max(float(timeout_s), 0.0))

    def _run(self) -> None:
        while True:
            fields = self._queue.get()
            try:
                if fields is _STOP:
                    return
                self._client.xadd(
                    self.stream,
                    fields,
                    maxlen=self.maxlen,
                    approximate=True,
                )
            except Exception as exc:
                self.write_error_count += 1
                self._log(
                    "write_error",
                    error=f"{type(exc).__name__}:{str(exc).replace(chr(10), ' | ')}",
                    write_error_count=self.write_error_count,
                )
            finally:
                self._queue.task_done()

    def _log(self, action: str, **fields: Any) -> None:
        parts = [
            f"component={self.component}",
            f"action={action}",
            f"redis_url={self.redis_url}",
        ]
        for key, value in fields.items():
            parts.append(f"{key}={value}")
        print(" ".join(parts), flush=True)
