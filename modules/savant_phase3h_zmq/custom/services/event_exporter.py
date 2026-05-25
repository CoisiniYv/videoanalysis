"""EventExporter interface, DryRunEventExporter, and RedisStreamEventExporter.

``DryRunEventExporter`` serialises each ``SecurityEvent`` to a single-line
JSON log.  ``RedisStreamEventExporter`` writes events to a Redis Stream.

Select the exporter via the ``EVENT_EXPORTER`` environment variable::

    EVENT_EXPORTER=dryrun   → DryRunEventExporter (default)
    EVENT_EXPORTER=redis    → RedisStreamEventExporter

Redis configuration::

    REDIS_URL      → default ``redis://redis:6379/0``
    EVENT_STREAM   → default ``security.events``
    EVENT_MAXLEN   → optional max stream length (default 10000)
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

from custom.models.events import SecurityEvent


class EventExporter(ABC):
    """Abstract interface for exporting ``SecurityEvent`` objects."""

    @abstractmethod
    def export(self, event: SecurityEvent) -> None:
        """Export *event* to the configured sink."""


class DryRunEventExporter(EventExporter):
    """Exporter that logs each event as a single-line JSON dry-run entry.

    The output is structured so it can be grepped::

        stage=phase2c_security_event_dry_run
        source_event_id=...
        event_type=intrusion
        security_event_json={...}
    """

    def export(self, event: SecurityEvent) -> None:
        """Print the event as a structured single-line log entry.

        Does not write to Redis, PostgreSQL, or any external system.
        """
        event_json = event.to_json()
        print(
            f"stage=phase2c_security_event_dry_run "
            f"source_event_id={event.source_event_id} "
            f"event_type={event.event_type} "
            f"camera_id={event.camera_id} "
            f"track_id={event.track_id} "
            f"security_event_json={event_json}",
            flush=True,
        )


class RedisStreamEventExporter(EventExporter):
    """Exports ``SecurityEvent`` objects to a Redis Stream.

    Each event is serialised to JSON and written as a single stream entry
    with the event dict flattened into stream field-value pairs::

        XADD security.events MAXLEN ~10000 * type security_event \\
            source_event_id savant:cam_01:3:intrusion:1000 \\
            event_type intrusion camera_id cam_01 track_id 3 \\
            data {...full JSON...}
    """

    def __init__(
        self,
        redis_url: str | None = None,
        stream: str | None = None,
        maxlen: int | None = None,
    ) -> None:
        """Initialise the Redis connection and stream configuration.

        Args:
            redis_url: Redis connection URL.  Defaults to ``REDIS_URL`` env
                or ``redis://redis:6379/0``.
            stream: Redis Stream key.  Defaults to ``EVENT_STREAM`` env or
                ``security.events``.
            maxlen: Approximate max stream length.  Defaults to
                ``EVENT_MAXLEN`` env or 10000.
        """
        _ = 0  # force local scope

        try:
            import redis as _redis
        except ImportError:
            raise ImportError(
                "RedisStreamEventExporter requires redis-py. "
                "Install it with: pip install redis"
            )

        self._redis_url = redis_url or os.environ.get(
            "REDIS_URL", "redis://redis:6379/0"
        )
        self._stream = stream or os.environ.get("EVENT_STREAM", "security.events")
        self._maxlen = (
            maxlen
            if maxlen is not None
            else int(os.environ.get("EVENT_MAXLEN", "10000"))
        )

        self._client: _redis.Redis = _redis.Redis.from_url(self._redis_url)

        print(
            f"stage=phase2c_redis_exporter_init "
            f"redis_url={self._redis_url} "
            f"stream={self._stream} "
            f"maxlen={self._maxlen}",
            flush=True,
        )

    def export(self, event: SecurityEvent) -> None:
        """Write *event* to the configured Redis Stream."""
        event_dict = event.to_dict()
        event_json = event.to_json()

        fields = {
            "type": "security_event",
            "source_event_id": event_dict.get("source_event_id", ""),
            "event_type": event_dict.get("event_type", ""),
            "camera_id": event_dict.get("camera_id", ""),
            "track_id": str(event_dict.get("track_id", "")),
            "start_ts_ms": str(event_dict.get("start_ts_ms", "")),
            "end_ts_ms": str(event_dict.get("end_ts_ms", "")),
            "severity": event_dict.get("severity", ""),
            "data": event_json,
        }

        try:
            self._client.xadd(
                self._stream,
                fields,
                maxlen=self._maxlen,
                approximate=True,
            )
        except Exception:
            import traceback

            print(
                f"stage=phase2c_redis_export_error "
                f"source_event_id={event.source_event_id} "
                f"stream={self._stream} "
                f"traceback={traceback.format_exc().replace(chr(10), ' | ')}",
                flush=True,
            )


# ---------------------------------------------------------------------------
# Exporter factory
# ---------------------------------------------------------------------------


def create_event_exporter() -> EventExporter:
    """Return an ``EventExporter`` selected by the ``EVENT_EXPORTER`` env var.

    ``EVENT_EXPORTER=dryrun`` (or unset) → ``DryRunEventExporter``.
    ``EVENT_EXPORTER=redis`` → ``RedisStreamEventExporter``.
    """
    exporter_type = os.environ.get("EVENT_EXPORTER", "dryrun").strip().lower()

    if exporter_type == "redis":
        return RedisStreamEventExporter()

    return DryRunEventExporter()
