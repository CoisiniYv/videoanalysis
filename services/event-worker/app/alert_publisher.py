"""AlertPublisher — publish alert messages to Redis Stream security.alerts."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from redis import Redis

logger = logging.getLogger(__name__)


class AlertPublisher:
    """Publishes structured alert messages to a Redis Stream.

    Each alert has a deterministic id (``alert:{source_event_id}``) so
    re-processing the same event does not create a semantically distinct
    alert entry.

    The publisher is called **only** after a successful event DB insert
    (i.e. the caller checks ``repo.insert_event(...) is True``).
    """

    def __init__(self, client: Redis, stream: str) -> None:
        self._client = client
        self._stream = stream

    def publish(self, event: Dict[str, Any], event_id: str | None = None) -> str:
        """Publish an alert for *event*.

        Args:
            event: The full SecurityEvent dict.
            event_id: The PostgreSQL UUID of the inserted row (optional).

        Returns:
            The Redis message id.
        """
        source_event_id = event.get("source_event_id", "")
        alert_id = f"alert:{source_event_id}"
        payload = event.get("payload", {}) or {}
        media = payload.get("media", {}) if isinstance(payload, dict) else {}

        alert = {
            "alert_id": alert_id,
            "event_id": event_id or "",
            "source_event_id": source_event_id,
            "event_type": event.get("event_type", ""),
            "camera_id": event.get("camera_id", ""),
            "source_id": event.get("source_id", ""),
            "track_id": str(event.get("track_id", "")),
            "severity": event.get("severity", "medium"),
            "confidence": float(event.get("confidence", 0.0)),
            "status": "new",
            "snapshot_url": None,
            "clip_url": None,
            "media": media,
            "created_at": "",
        }

        fields = {
            "alert_id": alert_id,
            "source_event_id": source_event_id,
            "event_type": alert["event_type"],
            "camera_id": alert["camera_id"],
            "data": json.dumps(alert, ensure_ascii=False),
        }

        msg_id = self._client.xadd(self._stream, fields, maxlen=10000, approximate=True)

        logger.info(
            "alert_published alert_id=%s source_event_id=%s stream=%s msg_id=%s",
            alert_id,
            source_event_id,
            self._stream,
            msg_id.decode() if isinstance(msg_id, bytes) else msg_id,
        )
        return msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
