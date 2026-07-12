"""Redis Stream delivery adapter for Clip Coordinator V2."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any

from app.contracts import DeliveryEnvelope


logger = logging.getLogger(__name__)


def _decode_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


@dataclass(frozen=True)
class ConsumerSettings:
    stream: str
    group: str
    consumer: str
    poll_timeout_ms: int
    read_count: int = 10
    pending_claim_min_idle_ms: int = 5000
    pending_claim_count: int = 10


class RequestConsumer:
    """Own Redis read/reclaim/ACK mechanics without business policy."""

    def __init__(self, client: Any, settings: ConsumerSettings) -> None:
        self.client = client
        self.settings = settings

    def ensure_group(self) -> None:
        try:
            self.client.xgroup_create(
                self.settings.stream,
                self.settings.group,
                id="$",
                mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def parse_json(self, delivery: DeliveryEnvelope) -> dict[str, Any] | None:
        fields = delivery.field_map()
        raw = fields.get(b"data", fields.get("data"))
        if not raw:
            return None
        try:
            payload = json.loads(_decode_text(raw))
        except (json.JSONDecodeError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def read_new(self) -> list[DeliveryEnvelope]:
        result = self.client.xreadgroup(
            self.settings.group,
            self.settings.consumer,
            {self.settings.stream: ">"},
            count=max(1, int(self.settings.read_count)),
            block=max(0, int(self.settings.poll_timeout_ms)),
        )
        deliveries: list[DeliveryEnvelope] = []
        for _stream_name, entries in result or []:
            for message_id, fields in entries or []:
                deliveries.append(
                    self._envelope(
                        message_id,
                        fields,
                        delivery_count=1,
                        reclaimed=False,
                    )
                )
        return deliveries

    def reclaim(self) -> list[DeliveryEnvelope]:
        if (
            self.settings.pending_claim_count <= 0
            or self.settings.pending_claim_min_idle_ms < 0
        ):
            return []
        delivery_counts = self._pending_delivery_counts()
        try:
            result = self.client.xautoclaim(
                self.settings.stream,
                self.settings.group,
                self.settings.consumer,
                self.settings.pending_claim_min_idle_ms,
                start_id="0-0",
                count=self.settings.pending_claim_count,
            )
            entries = (
                result[1]
                if isinstance(result, (list, tuple)) and len(result) > 1
                else []
            )
        except AttributeError:
            entries = self._claim_fallback(delivery_counts)
        except Exception as exc:
            logger.warning(
                "clip_consumer_pending_claim_failed stream=%s group=%s "
                "consumer=%s error=%s",
                self.settings.stream,
                self.settings.group,
                self.settings.consumer,
                exc,
            )
            return []
        return [
            self._envelope(
                message_id,
                fields,
                delivery_count=delivery_counts.get(_decode_text(message_id), 1),
                reclaimed=True,
            )
            for message_id, fields in entries or []
        ]

    def ack(self, delivery: DeliveryEnvelope) -> bool:
        result = self.client.xack(
            delivery.stream,
            delivery.group,
            delivery.message_id,
        )
        return bool(result is None or int(result) >= 0)

    def diagnostics(self) -> dict[str, object]:
        diagnostics: dict[str, object] = {"pending": None, "lag": None}
        try:
            pending = self.client.xpending(
                self.settings.stream,
                self.settings.group,
            )
            if isinstance(pending, dict):
                diagnostics["pending"] = pending.get("pending")
            elif isinstance(pending, (list, tuple)) and pending:
                diagnostics["pending"] = pending[0]
        except Exception:
            pass
        try:
            for item in self.client.xinfo_groups(self.settings.stream) or []:
                name = item.get("name") if isinstance(item, dict) else None
                if _decode_text(name) != self.settings.group:
                    continue
                diagnostics["lag"] = item.get("lag")
                diagnostics["pending"] = item.get(
                    "pending",
                    diagnostics["pending"],
                )
                break
        except Exception:
            pass
        return diagnostics

    def _pending_delivery_counts(self) -> dict[str, int]:
        try:
            pending = self.client.xpending_range(
                self.settings.stream,
                self.settings.group,
                min="-",
                max="+",
                count=max(1, int(self.settings.pending_claim_count)),
            )
        except Exception as exc:
            logger.warning(
                "clip_consumer_pending_inspect_failed stream=%s group=%s error=%s",
                self.settings.stream,
                self.settings.group,
                exc,
            )
            return {}
        counts: dict[str, int] = {}
        for item in pending or []:
            if not isinstance(item, dict):
                continue
            message_id = (
                item.get("message_id")
                or item.get("message-id")
                or item.get("id")
            )
            deliveries = (
                item.get("times_delivered")
                or item.get("times-delivered")
                or item.get("delivery_count")
                or 1
            )
            try:
                counts[_decode_text(message_id)] = max(1, int(deliveries))
            except (TypeError, ValueError):
                counts[_decode_text(message_id)] = 1
        return counts

    def _claim_fallback(
        self,
        delivery_counts: dict[str, int],
    ) -> list[tuple[object, object]]:
        if not delivery_counts:
            return []
        message_ids = list(delivery_counts)[: self.settings.pending_claim_count]
        try:
            return list(
                self.client.xclaim(
                    self.settings.stream,
                    self.settings.group,
                    self.settings.consumer,
                    min_idle_time=self.settings.pending_claim_min_idle_ms,
                    message_ids=message_ids,
                )
                or []
            )
        except Exception as exc:
            logger.warning(
                "clip_consumer_pending_claim_fallback_failed stream=%s group=%s "
                "consumer=%s error=%s",
                self.settings.stream,
                self.settings.group,
                self.settings.consumer,
                exc,
            )
            return []

    def _envelope(
        self,
        message_id: object,
        fields: object,
        *,
        delivery_count: int,
        reclaimed: bool,
    ) -> DeliveryEnvelope:
        field_map = fields if isinstance(fields, dict) else {}
        return DeliveryEnvelope(
            stream=self.settings.stream,
            group=self.settings.group,
            consumer=self.settings.consumer,
            message_id=_decode_text(message_id),
            fields=tuple(field_map.items()),
            delivery_count=max(1, int(delivery_count or 1)),
            reclaimed=reclaimed,
        )
