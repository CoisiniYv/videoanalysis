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


def _positive_track_id_text(value: Any) -> Optional[str]:
    try:
        text = str(value)
        if int(text) > 0:
            return text
    except (TypeError, ValueError):
        return None
    return None


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
    frame_num: Optional[int] = None
    person_bbox: Optional[List[float]] = None
    face_bbox: Optional[Dict[str, Any] | List[float]] = None
    person_track_id: Optional[str] = None
    face_track_id: Optional[str] = None
    track_id_semantics: str = "person_track_id"
    landmarks: Optional[List[float]] = None
    face_confidence: float = 0.0
    quality: float = 0.0
    detector_model: str = "yolov8_face"
    embedding_model: str = "adaface"
    embedding_dim: int = 0
    embedding: Optional[List[float]] = None
    embedding_norm: float = 0.0
    reid_allowed: bool = False
    reid_throttle_key: str = ""
    association_score: float = 0.0
    association_method: str = ""
    model_version: Optional[str] = None
    snapshot_path: Optional[str] = None
    crop_path: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        track_id_semantics = self.track_id_semantics or "person_track_id"
        if track_id_semantics == "person_track_id":
            person_track_id = (
                _positive_track_id_text(self.person_track_id)
                or _positive_track_id_text(self.track_id)
            )
            track_id = person_track_id
        else:
            track_id = _positive_track_id_text(self.track_id)
            person_track_id = _positive_track_id_text(self.person_track_id)
        return {
            "schema_version": FACE_OBSERVATION_SCHEMA_VERSION,
            "source_observation_id": self.source_observation_id,
            "producer": self.producer,
            "message_type": self.message_type,
            "camera_id": self.camera_id,
            "source_id": self.source_id,
            "track_id": track_id,
            "person_track_id": person_track_id,
            "face_track_id": self.face_track_id,
            "track_id_semantics": track_id_semantics,
            "timestamp_ms": self.timestamp_ms,
            "frame_num": self.frame_num,
            "person_bbox": self.person_bbox,
            "face_bbox": self.face_bbox,
            "landmarks": self.landmarks,
            "face_confidence": self.face_confidence,
            "quality": self.quality,
            "detector_model": self.detector_model,
            "model_name": self.detector_model,
            "embedding_model": self.embedding_model,
            "embedding_dim": self.embedding_dim,
            "embedding": self.embedding,
            "embedding_norm": self.embedding_norm,
            "reid_allowed": self.reid_allowed,
            "reid_throttle_key": self.reid_throttle_key,
            "association_score": self.association_score,
            "association_method": self.association_method,
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
            person_track_id=getattr(draft, "person_track_id", None),
            face_track_id=getattr(draft, "face_track_id", None),
            track_id_semantics=getattr(
                draft,
                "track_id_semantics",
                "person_track_id",
            ),
            timestamp_ms=draft.timestamp_ms,
            frame_num=getattr(draft, "frame_num", None),
            person_bbox=draft.person_bbox,
            face_bbox=draft.face_bbox,
            landmarks=draft.landmarks,
            face_confidence=getattr(draft, "face_confidence", 0.0),
            quality=draft.quality,
            detector_model=getattr(
                draft,
                "detector_model",
                getattr(draft, "model_name", "yolov8_face"),
            ),
            embedding_model=getattr(draft, "embedding_model", "adaface"),
            embedding_dim=getattr(draft, "embedding_dim", 0),
            embedding=getattr(draft, "embedding", None),
            embedding_norm=getattr(draft, "embedding_norm", 0.0),
            reid_allowed=getattr(draft, "reid_allowed", False),
            reid_throttle_key=getattr(draft, "reid_throttle_key", ""),
            association_score=getattr(draft, "association_score", 0.0),
            association_method=getattr(draft, "association_method", ""),
            model_version=getattr(draft, "model_version", None),
            snapshot_path=draft.snapshot_path,
            crop_path=draft.crop_path,
            payload=draft.payload if hasattr(draft, "payload") else {},
        )
