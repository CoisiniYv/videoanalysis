"""Bounded Redis Stream contract for aligned AdaFace ROI messages."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any


FACE_ROI_SCHEMA_VERSION = "1.0"
DEFAULT_FACE_ROI_STREAM = "security.face_rois"


@dataclass(frozen=True)
class FaceRoiEnvelope:
    source_id: str
    camera_id: str
    person_track_id: int
    face_index: int
    timestamp_ms: int
    frame_num: int | None
    frame_uuid: str | None
    keyframe_uuid: str | None
    previous_keyframe_uuid: str | None
    frame_pts: int | None
    frame_dts: int | None
    duration: int | None
    time_base: str | None
    ntp_timestamp: int | None
    runtime_epoch_id: str | None
    stream_session_id: str
    face_bbox: dict[str, Any]
    landmarks: list[float]
    face_confidence: float
    quality: float
    association_score: float
    association_method: str
    throttle_key: str
    created_at_ms: int
    expires_at_ms: int

    @property
    def source_observation_id(self) -> str:
        if self.frame_uuid:
            return (
                f"face:{self.source_id}:uuid:{self.frame_uuid}:"
                f"{self.face_index}"
            )
        return (
            f"face:{self.source_id}:{self.person_track_id}:"
            f"{self.timestamp_ms}:{self.face_index}"
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "schema_version": FACE_ROI_SCHEMA_VERSION,
            "source_observation_id": self.source_observation_id,
            "source_id": self.source_id,
            "camera_id": self.camera_id,
            "person_track_id": self.person_track_id,
            "face_index": self.face_index,
            "timestamp_ms": self.timestamp_ms,
            "frame_num": self.frame_num,
            "frame_uuid": self.frame_uuid,
            "keyframe_uuid": self.keyframe_uuid,
            "previous_keyframe_uuid": self.previous_keyframe_uuid,
            "frame_pts": self.frame_pts,
            "frame_dts": self.frame_dts,
            "duration": self.duration,
            "time_base": self.time_base,
            "ntp_timestamp": self.ntp_timestamp,
            "runtime_epoch_id": self.runtime_epoch_id,
            "stream_session_id": self.stream_session_id,
            "face_bbox": self.face_bbox,
            "landmarks": self.landmarks,
            "face_confidence": self.face_confidence,
            "quality": self.quality,
            "association_score": self.association_score,
            "association_method": self.association_method,
            "throttle_key": self.throttle_key,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "image_format": "jpeg",
            "image_width": 112,
            "image_height": 112,
            "alignment": "adaface_5point_v1",
        }

    def redis_fields(self, jpeg: bytes) -> dict[str, Any]:
        metadata = self.metadata()
        return {
            "type": "face_roi",
            "source_observation_id": self.source_observation_id,
            "source_id": self.source_id,
            "camera_id": self.camera_id,
            "person_track_id": str(self.person_track_id),
            "frame_uuid": self.frame_uuid or "",
            "timestamp_ms": str(self.timestamp_ms),
            "created_at_ms": str(self.created_at_ms),
            "expires_at_ms": str(self.expires_at_ms),
            "metadata": json.dumps(metadata, separators=(",", ":")),
            "image": jpeg,
        }


def epoch_ms() -> int:
    return int(time.time() * 1000)


def is_expired(metadata: dict[str, Any], *, now_ms: int | None = None) -> bool:
    try:
        expires_at_ms = int(metadata.get("expires_at_ms") or 0)
    except (TypeError, ValueError):
        return True
    return expires_at_ms <= int(epoch_ms() if now_ms is None else now_ms)
