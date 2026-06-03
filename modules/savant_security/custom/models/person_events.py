"""Person bbox observation schema for Redis Stream export.

Carries accepted person bbox metadata only. No keypoints, embeddings, image
bytes, identity, or trajectory data are serialized.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


PERSON_OBSERVATION_SCHEMA_VERSION = "c1i.person_bbox_observation.v1"


def build_person_source_observation_id(
    source_id: str,
    track_id: int | str | None,
    timestamp_ms: int,
    index: int,
) -> str:
    """Generate a deterministic idempotency key for one person bbox."""

    try:
        track_int = int(track_id) if track_id not in (None, "") else 0
    except (TypeError, ValueError):
        track_int = 0
    track_part = str(track_int) if track_int > 0 else "no_track"
    return f"person:{source_id}:{track_part}:{int(timestamp_ms)}:{int(index)}"


@dataclass
class PersonBBoxObservationEventDraft:
    """Accepted person bbox observation for ``security.person_observations``."""

    source_observation_id: str = ""
    producer: str = "savant_security"
    message_type: str = "person_bbox_observation"
    source_id: str = ""
    camera_id: str = ""
    track_id: Optional[str] = None
    timestamp_ms: int = 0
    frame_pts: Optional[int] = None
    frame_num: Optional[int] = None
    person_bbox: list[float] = field(default_factory=list)
    person_confidence: Optional[float] = None
    gate_status: str = "accepted"
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": PERSON_OBSERVATION_SCHEMA_VERSION,
            "source_observation_id": self.source_observation_id,
            "producer": self.producer,
            "message_type": self.message_type,
            "source_id": self.source_id,
            "camera_id": self.camera_id,
            "track_id": self.track_id,
            "timestamp_ms": int(self.timestamp_ms),
            "frame_pts": self.frame_pts,
            "frame_num": self.frame_num,
            "person_bbox": self.person_bbox,
            "person_confidence": self.person_confidence,
            "gate_status": self.gate_status,
            "payload": self.payload,
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)
