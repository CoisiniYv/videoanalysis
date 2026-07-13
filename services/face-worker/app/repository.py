"""FaceObservationRepository — idempotent PostgreSQL face observation insertion.

Payload JSONB excludes the 512-d embedding vector (already stored in the
``embedding`` column) to avoid ~8KB of redundant float storage per row.
Embedding is required — callers must validate before calling insert.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row


_INSERT_SQL = """
INSERT INTO face_observations (
    source_observation_id,
    camera_id,
    source_id,
    track_id,
    timestamp_ms,
    captured_at,
    frame_num,
    person_bbox,
    face_bbox,
    landmarks,
    face_confidence,
    quality,
    detector_model,
    embedding_model,
    model_version,
    embedding_dim,
    embedding,
    embedding_norm,
    reid_throttle_key,
    association_score,
    association_method,
    camera_config_resolved,
    snapshot_path,
    crop_path,
    payload
) VALUES (
    %(source_observation_id)s,
    %(camera_id)s,
    %(source_id)s,
    %(track_id)s,
    %(timestamp_ms)s,
    %(captured_at)s,
    %(frame_num)s,
    %(person_bbox)s::jsonb,
    %(face_bbox)s::jsonb,
    %(landmarks)s::jsonb,
    %(face_confidence)s,
    %(quality)s,
    %(detector_model)s,
    %(embedding_model)s,
    %(model_version)s,
    %(embedding_dim)s,
    %(embedding)s,
    %(embedding_norm)s,
    %(reid_throttle_key)s,
    %(association_score)s,
    %(association_method)s,
    %(camera_config_resolved)s,
    %(snapshot_path)s,
    %(crop_path)s,
    %(payload)s::jsonb
)
ON CONFLICT (source_observation_id) DO NOTHING
RETURNING id
"""


def _to_jsonb(val):
    """Serialize a value to JSON string for JSONB cast, or None."""
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return json.dumps(val, ensure_ascii=False)


def _observation_params(data: Dict[str, Any]) -> dict[str, Any]:
    embedding_vector = [float(x) for x in data["embedding"]]
    payload = data.get("payload", {})
    payload_copy = dict(payload) if isinstance(payload, dict) else {}
    return {
        "source_observation_id": data["source_observation_id"],
        "camera_id": data.get("camera_id", ""),
        "source_id": data.get("source_id", ""),
        "track_id": str(data.get("track_id", "")),
        "timestamp_ms": int(data.get("timestamp_ms", 0)),
        "captured_at": data.get("captured_at"),
        "frame_num": data.get("frame_num"),
        "person_bbox": _to_jsonb(data.get("person_bbox")),
        "face_bbox": _to_jsonb(data.get("face_bbox")),
        "landmarks": _to_jsonb(data.get("landmarks")),
        "face_confidence": float(data.get("face_confidence", 0.0)),
        "quality": float(data.get("quality", 0.0)),
        "detector_model": data.get("detector_model", "yolov8_face"),
        "embedding_model": data.get("embedding_model", "adaface"),
        "model_version": data.get("model_version"),
        "embedding_dim": int(data.get("embedding_dim", 512)),
        "embedding": embedding_vector,
        "embedding_norm": float(data.get("embedding_norm", 0.0)),
        "reid_throttle_key": data.get("reid_throttle_key", ""),
        "association_score": float(data.get("association_score", 0.0))
        if data.get("association_score") is not None
        else None,
        "association_method": data.get("association_method"),
        "camera_config_resolved": payload_copy.pop("camera_config_resolved", False),
        "snapshot_path": data.get("snapshot_path"),
        "crop_path": data.get("crop_path"),
        "payload": json.dumps(payload_copy, ensure_ascii=False),
    }


class FaceObservationRepository:
    """Idempotent face observation store backed by PostgreSQL + pgvector."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        register_vector(conn)

    def insert_observation(self, data: Dict[str, Any]) -> str | None:
        """Insert a face observation dict into the face_observations table.

        *data* is the parsed ``data`` JSON field from a Redis stream entry.
        Caller must ensure ``embedding`` is a valid list of 512 floats.

        Returns the UUID of the new row if inserted, or None if a duplicate
        ``source_observation_id`` was skipped.
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_INSERT_SQL, _observation_params(data))
            row = cur.fetchone()
            return str(row["id"]) if row else None

    def insert_observations(self, rows: list[Dict[str, Any]]) -> list[str | None]:
        """Persist one Redis batch in one transaction and one pipeline flush."""

        if not rows:
            return []
        cursors = []
        try:
            with self._conn.transaction():
                with self._conn.pipeline():
                    for data in rows:
                        cur = self._conn.cursor(row_factory=dict_row)
                        cur.execute(_INSERT_SQL, _observation_params(data))
                        cursors.append(cur)
                results: list[str | None] = []
                for cur in cursors:
                    row = cur.fetchone()
                    results.append(str(row["id"]) if row else None)
                return results
        finally:
            for cur in cursors:
                cur.close()
