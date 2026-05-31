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


def _resolve_source_id(event: Dict[str, Any], default_source_id: str = "") -> str:
    """Resolve the replay source_id from an event.

    Priority:
    1. payload.media.source_id (if present and not null/empty)
    2. event.source_id (if not "0" or empty)
    3. *default_source_id* (from Config.default_replay_source_id)
    4. fallback "" (no hardcoded default)
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

    if default_source_id:
        logger.info(
            "source_id resolved from Config.default_replay_source_id=%s "
            "(original_source_id=%s media_source_id=%s)",
            default_source_id,
            event_sid,
            media_sid,
        )
        return default_source_id

    logger.warning(
        "source_id could not be resolved — no media.source_id, "
        "event.source_id=%s, and no DEFAULT_REPLAY_SOURCE_ID configured",
        event_sid,
    )
    return ""


class RecordRequestPublisher:
    """Publishes recording requests to a Redis Stream.

    Called only after successful DB insert, when RECORDING_ENABLED=true.
    Does not block, does not wait for clip completion.
    """

    def __init__(
        self, client: Redis, stream: str, default_replay_source_id: str = ""
    ) -> None:
        self._client = client
        self._stream = stream
        self._default_replay_source_id = default_replay_source_id

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
        source_id = _resolve_source_id(event, self._default_replay_source_id)
        payload = event.get("payload") or {}
        media = payload.get("media", {}) if isinstance(payload, dict) else {}
        evidence_policy = event.get("evidence_policy") or {}
        if not isinstance(evidence_policy, dict):
            evidence_policy = {}

        if not source_id:
            logger.error(
                "record_request_skipped: no source_id could be resolved "
                "for source_event_id=%s",
                source_event_id,
            )
            return None

        record = {
            "request_id": request_id,
            "event_id": event_id,
            "source_event_id": source_event_id,
            "camera_id": event.get("camera_id", ""),
            "source_id": source_id,
            "event_ts_ms": int(event.get("event_ts_ms", 0)),
            "frame_uuid": event.get("frame_uuid"),
            "keyframe_uuid": event.get("keyframe_uuid"),
            "previous_keyframe_uuid": (
                event.get("previous_keyframe_uuid")
                or media.get("previous_keyframe_uuid")
            ),
            "pre_seconds": int(evidence_policy.get("pre_seconds", DEFAULT_PRE_SECONDS)),
            "post_seconds": int(evidence_policy.get("post_seconds", DEFAULT_POST_SECONDS)),
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

    def has_request(self, source_event_id: str, recording_strategy: str) -> bool:
        """Return True when a record_request already exists for this event."""
        try:
            stream = self._client.xrange(self._stream, "-", "+")
        except Exception:
            logger.exception(
                "record_request lookup failed for source_event_id=%s", source_event_id
            )
            return False
        for _msg_id, fields in stream:
            data_raw = fields.get(b"data")
            if not data_raw:
                continue
            try:
                data = json.loads(data_raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if (
                data.get("source_event_id") == source_event_id
                and data.get("strategy") == recording_strategy
            ):
                return True
        return False
