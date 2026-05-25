"""FaceObservation event schema — Redis Stream wire format (draft).

Redis Streams carry **only structured data**, never image bytes.
If a snapshot or crop is needed, the event carries a filesystem path
reference (``snapshot_path`` / ``crop_path``).

Schema version ``1.0``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

FACE_OBSERVATION_SCHEMA_VERSION = "1.0"


def build_face_source_observation_id(
    source_id: str,
    track_id: int,
    timestamp_ms: int,
) -> str:
    """Generate a deterministic, idempotent ``source_observation_id``.

    Format: ``face:{source_id}:{track_id_or_no_track}:{timestamp_ms}``

    When ``track_id <= 0`` the track segment is replaced with ``"no_track"``.
    The timestamp ensures uniqueness for untracked detections at the same
    source.
    """
    track_part = str(track_id) if track_id > 0 else "no_track"
    return f"face:{source_id}:{track_part}:{timestamp_ms}"


@dataclass
class FaceObservationEventDraft:
    """Draft event for the ``security.face_observations`` Redis Stream.

    Carries structured face metadata — no image bytes.
    """

    source_observation_id: str = ""
    producer: str = ""
    message_type: str = "face_observation"
    camera_id: str = ""
    source_id: str = ""
    track_id: int = 0
    timestamp_ms: int = 0
    person_bbox: Optional[List[float]] = None
    face_bbox: Optional[List[float]] = None
    landmarks: Optional[List[List[float]]] = None
    quality: float = 0.0
    model_name: str = ""
    model_version: Optional[str] = None
    snapshot_path: Optional[str] = None
    crop_path: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": FACE_OBSERVATION_SCHEMA_VERSION,
            "source_observation_id": self.source_observation_id,
            "producer": self.producer,
            "message_type": self.message_type,
            "camera_id": self.camera_id,
            "source_id": self.source_id,
            "track_id": str(self.track_id) if self.track_id > 0 else None,
            "timestamp_ms": self.timestamp_ms,
            "person_bbox": self.person_bbox,
            "face_bbox": self.face_bbox,
            "landmarks": self.landmarks,
            "quality": self.quality,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "snapshot_path": self.snapshot_path,
            "crop_path": self.crop_path,
            "payload": self.payload,
        }

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), **kwargs)

    @classmethod
    def from_draft(
        cls,
        draft: Any,
        producer: str = "",
    ) -> "FaceObservationEventDraft":
        """Create an event from a ``FaceObservationDraft`` and producer name."""
        return cls(
            source_observation_id=draft.source_observation_id,
            producer=producer,
            camera_id=draft.camera_id,
            source_id=draft.source_id,
            track_id=draft.track_id,
            timestamp_ms=draft.timestamp_ms,
            person_bbox=draft.person_bbox,
            face_bbox=draft.face_bbox,
            landmarks=draft.landmarks,
            quality=draft.quality,
            model_name=draft.model_name,
            model_version=draft.model_version,
            snapshot_path=draft.snapshot_path,
            crop_path=draft.crop_path,
            payload=draft.payload,
        )
