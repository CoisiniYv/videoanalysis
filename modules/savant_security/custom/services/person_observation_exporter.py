"""Person bbox observation Redis Stream exporter."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Dict, Optional

from custom.models.person_events import PersonBBoxObservationEventDraft


class PersonObservationThrottleMap:
    """In-memory per camera+track throttle for person bbox observations."""

    def __init__(self, min_interval_ms: int = 1000):
        self._min_interval_ms = max(int(min_interval_ms), 0)
        self._last_exported: Dict[str, int] = {}

    def is_allowed(self, throttle_key: str, timestamp_ms: int) -> bool:
        if self._min_interval_ms <= 0:
            return True
        last = self._last_exported.get(throttle_key)
        if last is None:
            return True
        return int(timestamp_ms) - last >= self._min_interval_ms

    def record(self, throttle_key: str, timestamp_ms: int) -> None:
        self._last_exported[throttle_key] = int(timestamp_ms)


class PersonObservationExporter(ABC):
    """Abstract sink for accepted person bbox observations."""

    @abstractmethod
    def export(self, observation: PersonBBoxObservationEventDraft) -> None:
        """Export one observation."""


class DryRunPersonObservationExporter(PersonObservationExporter):
    """Log accepted person bbox observations without writing Redis."""

    def export(self, observation: PersonBBoxObservationEventDraft) -> None:
        print(
            "stage=savant_security_person_observation_dry_run "
            f"source_observation_id={observation.source_observation_id} "
            f"source_id={observation.source_id} "
            f"camera_id={observation.camera_id} "
            f"track_id={observation.track_id} "
            f"confidence={observation.person_confidence}",
            flush=True,
        )


class RedisStreamPersonObservationExporter(PersonObservationExporter):
    """Exports accepted person bbox observations to a Redis Stream via XADD."""

    def __init__(
        self,
        redis_url: Optional[str] = None,
        stream: Optional[str] = None,
        maxlen: Optional[int] = None,
    ) -> None:
        try:
            import redis as _redis
        except ImportError:
            raise ImportError(
                "RedisStreamPersonObservationExporter requires redis-py or the "
                "local Redis shim."
            )

        self._redis_url = redis_url or os.environ.get(
            "REDIS_URL", "redis://redis:6379/0",
        )
        self._stream = stream or os.environ.get(
            "PERSON_OBSERVATION_STREAM", "security.person_observations",
        )
        self._maxlen = (
            maxlen
            if maxlen is not None
            else int(os.environ.get("PERSON_OBSERVATION_MAXLEN", "10000"))
        )
        self._client: _redis.Redis = _redis.Redis.from_url(self._redis_url)
        print(
            "stage=savant_security_person_obs_exporter_init "
            f"redis_url={self._redis_url} "
            f"stream={self._stream} "
            f"maxlen={self._maxlen}",
            flush=True,
        )

    def export(self, observation: PersonBBoxObservationEventDraft) -> None:
        obs_dict = observation.to_dict()
        obs_json = json.dumps(obs_dict, separators=(",", ":"))
        fields = {
            "type": "person_bbox_observation",
            "source_observation_id": obs_dict.get("source_observation_id", ""),
            "source_id": obs_dict.get("source_id", ""),
            "camera_id": obs_dict.get("camera_id", ""),
            "track_id": str(obs_dict.get("track_id") or ""),
            "timestamp_ms": str(obs_dict.get("timestamp_ms", "")),
            "person_confidence": str(obs_dict.get("person_confidence", "")),
            "gate_status": obs_dict.get("gate_status", ""),
            "data": obs_json,
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
                "stage=savant_security_person_obs_export_error "
                f"source_observation_id={observation.source_observation_id} "
                f"stream={self._stream} "
                f"traceback={traceback.format_exc().replace(chr(10), ' | ')}",
                flush=True,
            )


def create_person_observation_exporter() -> PersonObservationExporter:
    enabled = os.environ.get(
        "PERSON_OBSERVATION_EXPORT_ENABLED", "true",
    ).strip().lower()
    if enabled in ("true", "1", "yes"):
        return RedisStreamPersonObservationExporter()
    return DryRunPersonObservationExporter()
