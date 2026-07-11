"""Convert ROI metadata and AdaFace embeddings to the existing observation wire format."""

from __future__ import annotations

import json
import math
from typing import Any


def build_face_observation(metadata: dict[str, Any], embedding: list[float]) -> dict[str, Any]:
    track_id = str(int(metadata["person_track_id"]))
    media_keys = (
        "frame_uuid",
        "keyframe_uuid",
        "previous_keyframe_uuid",
        "frame_pts",
        "frame_dts",
        "duration",
        "frame_num",
        "ntp_timestamp",
        "time_base",
        "source_id",
        "stream_session_id",
        "runtime_epoch_id",
    )
    media = {key: metadata.get(key) for key in media_keys}
    media["metadata_source"] = "roi_adaface_worker"
    norm = math.sqrt(sum(float(value) * float(value) for value in embedding))
    return {
        "schema_version": "1.0",
        "source_observation_id": metadata["source_observation_id"],
        "producer": "adaface-roi-worker",
        "message_type": "face_observation",
        "camera_id": metadata["camera_id"],
        "source_id": metadata["source_id"],
        "track_id": track_id,
        "person_track_id": track_id,
        "face_track_id": None,
        "track_id_semantics": "person_track_id",
        "timestamp_ms": int(metadata["timestamp_ms"]),
        "frame_num": metadata.get("frame_num"),
        "person_bbox": None,
        "face_bbox": metadata.get("face_bbox"),
        "landmarks": metadata.get("landmarks"),
        "face_confidence": float(metadata.get("face_confidence") or 0.0),
        "quality": float(metadata.get("quality") or 0.0),
        "detector_model": "yolov8_face",
        "model_name": "yolov8_face",
        "embedding_model": "adaface",
        "embedding_dim": len(embedding),
        "embedding": embedding,
        "embedding_norm": norm,
        "reid_allowed": True,
        "reid_throttle_key": metadata.get("throttle_key", ""),
        "association_score": float(metadata.get("association_score") or 0.0),
        "association_method": metadata.get("association_method", ""),
        "model_version": "adaface_ir50_webface4m",
        "snapshot_path": None,
        "crop_path": None,
        "payload": {
            "camera_config_resolved": metadata["camera_id"] != metadata["source_id"],
            "person_track_id": track_id,
            "face_track_id": None,
            "track_id_semantics": "person_track_id",
            "runtime_epoch_id": metadata.get("runtime_epoch_id"),
            "roi_transport": {
                "schema_version": metadata.get("schema_version"),
                "alignment": metadata.get("alignment"),
                "created_at_ms": metadata.get("created_at_ms"),
                "thumbnail_redis_key": metadata.get("thumbnail_redis_key"),
            },
            "media": media,
        },
    }


def observation_redis_fields(observation: dict[str, Any]) -> dict[str, str]:
    return {
        "type": "face_observation",
        "source_observation_id": observation["source_observation_id"],
        "camera_id": observation["camera_id"],
        "source_id": observation["source_id"],
        "track_id": str(observation["track_id"]),
        "timestamp_ms": str(observation["timestamp_ms"]),
        "face_confidence": str(observation["face_confidence"]),
        "quality": str(observation["quality"]),
        "embedding_model": observation["embedding_model"],
        "embedding_dim": str(observation["embedding_dim"]),
        "data": json.dumps(observation, separators=(",", ":")),
    }
