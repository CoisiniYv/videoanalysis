"""RedisStreamConsumer — consumer-group based Redis Stream reader."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from redis import Redis

logger = logging.getLogger(__name__)

# ResponseError from redis-py when group already exists
_GROUP_EXISTS_MSG = "BUSYGROUP"


class RedisStreamConsumer:
    """Reads from a Redis Stream using a consumer group.

    Supports:
    - Automatic group creation (MKSTREAM)
    - Reading new messages via ``>``
    - Claiming and processing pending messages
    - Explicit ACK
    """

    def __init__(
        self,
        client: Redis,
        stream: str,
        group: str,
        consumer: str,
    ) -> None:
        self._client = client
        self._stream = stream
        self._group = group
        self._consumer = consumer

    def ensure_group(self) -> None:
        """Create the consumer group if it does not already exist."""
        try:
            self._client.xgroup_create(
                self._stream, self._group, id="$", mkstream=True
            )
            logger.info(
                "created consumer group=%s stream=%s", self._group, self._stream
            )
        except Exception as exc:
            if _GROUP_EXISTS_MSG in str(exc):
                logger.info(
                    "consumer group=%s already exists on stream=%s",
                    self._group,
                    self._stream,
                )
            else:
                raise

    def read_new(
        self, count: int = 10, block_ms: int = 5000
    ) -> List[Tuple[str, Dict[bytes, bytes]]]:
        """Read new messages from the stream (id ``>``).

        Returns a list of ``(msg_id, fields_dict)`` tuples, or an empty list
        on timeout.
        """
        try:
            result = self._client.xreadgroup(
                self._group,
                self._consumer,
                {self._stream: ">"},
                count=count,
                block=block_ms,
            )
        except Exception:
            logger.exception("xreadgroup failed")
            return []

        if not result:
            return []

        # result is [[stream_name, [(msg_id, fields), ...]], ...]
        messages: List[Tuple[str, Dict[bytes, bytes]]] = []
        for _stream_name, entries in result:
            for msg_id, fields in entries:
                messages.append((msg_id.decode(), fields))
        return messages

    def read_pending(
        self, count: int = 10, min_idle_ms: int = 60000
    ) -> List[Tuple[str, Dict[bytes, bytes]]]:
        """Claim and return pending messages idle for at least *min_idle_ms*."""
        try:
            pending = self._client.xpending_range(
                self._stream, self._group, "-", "+", count=count
            )
        except Exception:
            logger.exception("xpending_range failed")
            return []

        if not pending:
            return []

        # Filter by idle time
        stale_ids = [
            p["message_id"]
            for p in pending
            if p.get("time_since_delivered", 0) >= min_idle_ms
        ]

        if not stale_ids:
            return []

        try:
            claimed = self._client.xclaim(
                self._stream,
                self._group,
                self._consumer,
                min_idle_time=min_idle_ms,
                message_ids=stale_ids,
            )
        except Exception:
            logger.exception("xclaim failed")
            return []

        messages: List[Tuple[str, Dict[bytes, bytes]]] = []
        for msg_id, fields in claimed:
            messages.append((msg_id.decode(), fields))
        return messages

    def ack(self, msg_id: str) -> bool:
        """Acknowledge *msg_id* in the consumer group. Returns True on success."""
        try:
            result = self._client.xack(self._stream, self._group, msg_id)
            return bool(result)
        except Exception:
            logger.exception("xack failed for msg_id=%s", msg_id)
            return False

    def trim(self, maxlen: int = 10000) -> None:
        """Trim the stream to approximately *maxlen* entries."""
        try:
            self._client.xtrim(self._stream, maxlen=maxlen, approximate=True)
        except Exception:
            logger.exception("xtrim failed")
