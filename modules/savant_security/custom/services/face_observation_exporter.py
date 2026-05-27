"""Face observation Redis Stream exporter.

Writes FaceObservationEventDraft to Redis Stream ``security.face_observations``.
Follows the same pattern as event_exporter.py (ABC + dry-run + Redis + factory).

No image bytes. Embedding is JSON-encoded list (~2KB).
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Dict, Optional

from custom.models.face_events import FaceObservationEventDraft


class ExportThrottleMap:
    """In-memory per-track throttle for export rate limiting.

    Defensive safety net — primary throttle is in ReIDThrottleMap.
    Tracks the last exported timestamp_ms for each throttle_key.
    """

    def __init__(self, min_interval_ms: int = 1000):
        self._min_interval_ms = max(min_interval_ms, 0)
        self._last_exported: Dict[str, int] = {}

    def is_allowed(self, throttle_key: str, timestamp_ms: int) -> bool:
        """Check if this throttle_key is allowed at timestamp_ms."""
        if self._min_interval_ms <= 0:
            return True
        last = self._last_exported.get(throttle_key)
        if last is None:
            return True
        return (timestamp_ms - last) >= self._min_interval_ms

    def record(self, throttle_key: str, timestamp_ms: int) -> None:
        """Record that this throttle_key was exported at timestamp_ms."""
        self._last_exported[throttle_key] = timestamp_ms

    def clear(self) -> None:
        """Reset all throttle state."""
        self._last_exported.clear()


class FaceObservationExporter(ABC):
    """Abstract interface for exporting face observations."""

    @abstractmethod
    def export(self, observation: FaceObservationEventDraft) -> None:
        """Export a face observation to the configured sink."""


class DryRunFaceObservationExporter(FaceObservationExporter):
    """Logs each observation as a single-line structured entry."""

    def export(self, observation: FaceObservationEventDraft) -> None:
        print(
            f"stage=savant_security_face_observation_dry_run "
            f"source_observation_id={observation.source_observation_id} "
            f"camera_id={observation.camera_id} "
            f"track_id={observation.track_id} "
            f"embedding_dim={observation.embedding_dim} "
            f"quality={observation.quality:.2f}",
            flush=True,
        )


class RedisStreamFaceObservationExporter(FaceObservationExporter):
    """Exports face observations to a Redis Stream via XADD."""

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
                "RedisStreamFaceObservationExporter requires redis-py. "
                "Install it with: pip install redis"
            )

        self._redis_url = redis_url or os.environ.get(
            "REDIS_URL", "redis://redis:6379/0",
        )
        self._stream = stream or os.environ.get(
            "FACE_OBSERVATION_STREAM", "security.face_observations",
        )
        self._maxlen = (
            maxlen
            if maxlen is not None
            else int(os.environ.get("FACE_OBSERVATION_MAXLEN", "10000"))
        )

        self._client: _redis.Redis = _redis.Redis.from_url(self._redis_url)

        print(
            f"stage=savant_security_face_obs_exporter_init "
            f"redis_url={self._redis_url} "
            f"stream={self._stream} "
            f"maxlen={self._maxlen}",
            flush=True,
        )

    def export(self, observation: FaceObservationEventDraft) -> None:
        """Write observation to the configured Redis Stream."""
        obs_dict = observation.to_dict()
        obs_json = json.dumps(obs_dict, separators=(",", ":"))

        # Flat fields for Redis Stream visibility + full JSON in "data"
        fields = {
            "type": "face_observation",
            "source_observation_id": obs_dict.get("source_observation_id", ""),
            "camera_id": obs_dict.get("camera_id", ""),
            "source_id": obs_dict.get("source_id", ""),
            "track_id": str(obs_dict.get("track_id", "")),
            "timestamp_ms": str(obs_dict.get("timestamp_ms", "")),
            "face_confidence": str(obs_dict.get("face_confidence", "")),
            "quality": str(obs_dict.get("quality", "")),
            "embedding_model": obs_dict.get("embedding_model", ""),
            "embedding_dim": str(obs_dict.get("embedding_dim", "")),
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
                f"stage=savant_security_face_obs_export_error "
                f"source_observation_id={observation.source_observation_id} "
                f"stream={self._stream} "
                f"traceback={traceback.format_exc().replace(chr(10), ' | ')}",
                flush=True,
            )


def create_face_observation_exporter() -> FaceObservationExporter:
    """Return an exporter selected by FACE_OBSERVATION_EXPORT_ENABLED.

    FACE_OBSERVATION_EXPORT_ENABLED=true  → RedisStreamFaceObservationExporter
    FACE_OBSERVATION_EXPORT_ENABLED=false (or unset) → DryRunFaceObservationExporter
    """
    enabled = os.environ.get(
        "FACE_OBSERVATION_EXPORT_ENABLED", "true",
    ).strip().lower()

    if enabled in ("true", "1", "yes"):
        return RedisStreamFaceObservationExporter()

    return DryRunFaceObservationExporter()
