"""RecordRequestPublisher — publish recording requests to Redis Stream."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict

from redis import Redis

logger = logging.getLogger(__name__)

DEFAULT_PRE_SECONDS = 5
DEFAULT_POST_SECONDS = 5


class RecordRequestPublisher:
    """Publishes recording requests to a Redis Stream.

    Called only after successful DB insert, when RECORDING_ENABLED=true.
    Does not block, does not wait for clip completion.
    """

    def __init__(self, client: Redis, stream: str) -> None:
        self._client = client
        self._stream = stream

    def publish(self, event: Dict[str, Any], event_id: str) -> str | None:
        """Publish a record_request for *event*.

        Args:
            event: The full SecurityEvent dict.
            event_id: The PostgreSQL UUID of the inserted row.

        Returns:
            The Redis message id, or None on failure.
        """
        source_event_id = event.get("source_event_id", "")
        request_id = str(uuid.uuid4())

        record = {
            "request_id": request_id,
            "event_id": event_id,
            "source_event_id": source_event_id,
            "camera_id": event.get("camera_id", ""),
            "source_id": event.get("source_id", ""),
            "event_ts_ms": int(event.get("event_ts_ms", 0)),
            "frame_uuid": event.get("frame_uuid"),
            "keyframe_uuid": event.get("keyframe_uuid"),
            "pre_seconds": DEFAULT_PRE_SECONDS,
            "post_seconds": DEFAULT_POST_SECONDS,
            "strategy": "savant_replay",
            "status": "pending",
        }

        fields = {
            "request_id": request_id,
            "event_id": event_id,
            "source_event_id": source_event_id,
            "status": "pending",
            "data": json.dumps(record, ensure_ascii=False),
        }

        try:
            msg_id = self._client.xadd(
                self._stream, fields, maxlen=10000, approximate=True
            )
            logger.info(
                "record_request_published request_id=%s event_id=%s source_event_id=%s "
                "source_id=%s stream=%s",
                request_id,
                event_id,
                source_event_id,
                event.get("source_id", ""),
                self._stream,
            )
            return msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
        except Exception:
            logger.exception(
                "record_request publish failed for source_event_id=%s",
                source_event_id,
            )
            return None
