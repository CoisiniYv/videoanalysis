"""RecordRequestPublisher — publish recording requests to Redis Stream."""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Dict

from redis import Redis

logger = logging.getLogger(__name__)

DEFAULT_PRE_SECONDS = 5
DEFAULT_POST_SECONDS = 5


def _resolve_source_id(event: Dict[str, Any]) -> str:
    """Resolve the replay source_id from an event.

    Priority:
    1. payload.media.source_id (if present and not null/empty)
    2. event.source_id (if not "0" or empty)
    3. DEFAULT_REPLAY_SOURCE_ID env var
    4. fallback "default"
    """
    payload = event.get("payload") or {}
    media = payload.get("media", {}) if isinstance(payload, dict) else {}
    media_sid = media.get("source_id", "") if isinstance(media, dict) else ""
    if media_sid and str(media_sid) not in ("", "0", "None", "null"):
        logger.debug("source_id resolved from payload.media.source_id=%s", media_sid)
        return str(media_sid)

    event_sid = str(event.get("source_id", ""))
    if event_sid and event_sid not in ("0", ""):
        logger.debug("source_id resolved from event.source_id=%s", event_sid)
        return event_sid

    default_sid = os.getenv("DEFAULT_REPLAY_SOURCE_ID", "phase3a")
    logger.info(
        "source_id fallback to DEFAULT_REPLAY_SOURCE_ID=%s "
        "(original_source_id=%s media_source_id=%s)",
        default_sid,
        event_sid,
        media_sid,
    )
    return default_sid


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
        source_id = _resolve_source_id(event)

        record = {
            "request_id": request_id,
            "event_id": event_id,
            "source_event_id": source_event_id,
            "camera_id": event.get("camera_id", ""),
            "source_id": source_id,
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
                source_id,
                self._stream,
            )
            return msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
        except Exception:
            logger.exception(
                "record_request publish failed for source_event_id=%s",
                source_event_id,
            )
            return None
