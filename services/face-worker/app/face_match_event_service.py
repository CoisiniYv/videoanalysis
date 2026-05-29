"""Face match event producer for R3.1B watchlist_hit MVP.

This module runs outside the Savant pipeline. It reads persisted
``face_observations``, searches active ``person_gallery_embeddings`` via
pgvector, and emits unified ``SecurityEvent`` dictionaries to Redis
``security.events``. The event-worker remains the only writer for ``events`` and
``evidence_tasks``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from redis import Redis

from app.vector_store import FaceVectorStore

EVENT_STREAM_DEFAULT = "security.events"
FACE_INTELLIGENCE_ALGORITHM_TYPE = "face_intelligence"
WATCHLIST_HIT_EVENT_TYPE = "watchlist_hit"
LIVE_SEARCH_HIT_EVENT_TYPE = "live_search_hit"
DEFAULT_FACE_MATCH_THRESHOLD = 0.50
DEFAULT_EVIDENCE_POLICY = {
    "snapshot_required": True,
    "clip_required": True,
    "pre_seconds": 5,
    "post_seconds": 10,
}
R3_1B_NOT_IMPLEMENTED_REASON = (
    "R3.1B face match evidence MVP created the evidence task, but production "
    "snapshot/raw_clip/metadata generation is not implemented in this deployment."
)


@dataclass(frozen=True)
class FaceMatchCandidate:
    """Top candidate produced while evaluating one face observation."""

    source_observation_id: str
    person_id: int
    external_person_id: str | None
    person_name: str
    gallery_embedding_id: int
    similarity: float
    threshold: float
    event_created: bool
    source_event_id: str


def _jsonable(value: Any) -> Any:
    """Convert pgvector/Decimal/JSONB-ish values into JSON-safe objects."""
    if value is None:
        return None
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def build_source_event_id(source_observation_id: str, person_id: int) -> str:
    """Return an idempotent source_event_id for a face/person hit."""
    return f"watchlist_hit:{source_observation_id}:{person_id}"


def fetch_face_observations(
    conn: psycopg.Connection,
    *,
    source_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch recent face observations for a source, newest first."""
    register_vector(conn)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, source_observation_id, camera_id, source_id, track_id,
                   timestamp_ms, face_bbox, landmarks, face_confidence, quality,
                   person_bbox, snapshot_path, crop_path, embedding,
                   embedding_model, embedding_dim, embedding_norm
            FROM face_observations
            WHERE source_id = %(source_id)s
              AND embedding IS NOT NULL
            ORDER BY created_at DESC
            LIMIT %(limit)s
            """,
            {"source_id": source_id, "limit": limit},
        )
        return list(cur.fetchall())


def observation_to_embedding(observation: dict[str, Any]) -> list[float]:
    """Extract a 512-d embedding list from a face observation row."""
    embedding = observation.get("embedding")
    if embedding is None:
        raise ValueError("face observation has no embedding")
    if hasattr(embedding, "tolist"):
        return [float(x) for x in embedding.tolist()]
    return [float(x) for x in embedding]


def build_watchlist_hit_event(
    *,
    observation: dict[str, Any],
    gallery_match: dict[str, Any],
    threshold: float,
    severity: str = "high",
) -> dict[str, Any]:
    """Build a unified SecurityEvent dict for one watchlist hit."""
    timestamp_ms = int(observation.get("timestamp_ms") or 0)
    person_id = int(gallery_match["person_id"])
    similarity = float(gallery_match["similarity"])
    source_observation_id = str(observation["source_observation_id"])
    source_event_id = build_source_event_id(source_observation_id, person_id)
    face_bbox = _jsonable(observation.get("face_bbox"))
    landmarks = _jsonable(observation.get("landmarks"))
    person_name = gallery_match.get("person_name") or ""
    label = f"{person_name} {similarity:.2f}".strip()

    payload = {
        "matched_person": {
            "person_id": person_id,
            "external_person_id": gallery_match.get("external_person_id"),
            "name": person_name,
        },
        "match": {
            "similarity": similarity,
            "threshold": float(threshold),
            "gallery_embedding_id": int(gallery_match["id"]),
            "source_observation_id": source_observation_id,
        },
        "observation": {
            "camera_id": observation.get("camera_id") or "",
            "source_id": observation.get("source_id") or "",
            "track_id": str(observation.get("track_id") or ""),
            "timestamp_ms": timestamp_ms,
            "face_bbox": face_bbox,
            "landmarks": landmarks,
            "quality": float(observation.get("quality") or 0.0),
            "face_confidence": float(observation.get("face_confidence") or 0.0),
        },
        "overlay": {
            "type": "face_match",
            "label": label,
            "face_bbox": face_bbox,
            "person_bbox": _jsonable(observation.get("person_bbox")),
        },
        "media": {
            "snapshot_required": True,
            "clip_required": True,
            "media_status": "not_implemented",
            "snapshot_status": "not_implemented",
            "clip_status": "not_implemented",
            "metadata_status": "not_implemented",
            "raw_clip_path": None,
            "annotated_clip_path": None,
            "metadata_path": None,
            "recording_strategy": "reserved",
            "pre_seconds": DEFAULT_EVIDENCE_POLICY["pre_seconds"],
            "post_seconds": DEFAULT_EVIDENCE_POLICY["post_seconds"],
            "error_message": R3_1B_NOT_IMPLEMENTED_REASON,
        },
    }

    return {
        "schema_version": "1.0",
        "source_event_id": source_event_id,
        "producer": "face-worker",
        "gpu_id": 0,
        "event_type": WATCHLIST_HIT_EVENT_TYPE,
        "camera_id": observation.get("camera_id") or "",
        "source_id": observation.get("source_id") or "",
        "track_id": str(observation.get("track_id") or ""),
        "person_id": person_id,
        "algorithm_type": FACE_INTELLIGENCE_ALGORITHM_TYPE,
        "algorithm_version": "r3.1b-mvp",
        "start_ts_ms": timestamp_ms,
        "end_ts_ms": timestamp_ms,
        "event_ts_ms": timestamp_ms,
        "frame_id": 0,
        "frame_uuid": None,
        "keyframe_uuid": None,
        "confidence": similarity,
        "severity": severity,
        "zone": "",
        "rule_name": "watchlist_hit_mvp",
        "description": f"Watchlist hit for {person_name}".strip(),
        "snapshot_required": True,
        "clip_required": True,
        "evidence_policy": dict(DEFAULT_EVIDENCE_POLICY),
        "payload": payload,
    }


def publish_security_event(
    redis_client: Redis,
    event: dict[str, Any],
    *,
    stream: str = EVENT_STREAM_DEFAULT,
    maxlen: int = 10000,
) -> str:
    """Publish one SecurityEvent dict to Redis security.events."""
    event_json = json.dumps(event, ensure_ascii=False)
    fields = {
        "type": "security_event",
        "source_event_id": event["source_event_id"],
        "event_type": event["event_type"],
        "algorithm_type": event["algorithm_type"],
        "camera_id": event["camera_id"],
        "track_id": str(event.get("track_id", "")),
        "start_ts_ms": str(event.get("start_ts_ms", "")),
        "end_ts_ms": str(event.get("end_ts_ms", "")),
        "severity": event.get("severity", ""),
        "data": event_json,
    }
    msg_id = redis_client.xadd(stream, fields, maxlen=maxlen, approximate=True)
    if isinstance(msg_id, bytes):
        return msg_id.decode()
    return str(msg_id)


def emit_watchlist_hits_for_source(
    *,
    conn: psycopg.Connection,
    redis_client: Redis | None,
    source_id: str,
    threshold: float = DEFAULT_FACE_MATCH_THRESHOLD,
    top_k: int = 1,
    observation_limit: int = 100,
    external_person_ids: list[str] | None = None,
    dry_run: bool = False,
    event_stream: str = EVENT_STREAM_DEFAULT,
) -> dict[str, Any]:
    """Search gallery for a source_id and emit watchlist_hit events.

    Returns a JSON-serializable summary with top candidates. Dry-run performs
    all DB searches but does not write Redis events.
    """
    observations = fetch_face_observations(
        conn, source_id=source_id, limit=observation_limit
    )
    store = FaceVectorStore(conn)
    candidates: list[FaceMatchCandidate] = []
    emitted_events: list[dict[str, Any]] = []
    allowed_external_ids = set(external_person_ids or [])

    for observation in observations:
        embedding = observation_to_embedding(observation)
        gallery_results = store.search_gallery(
            embedding,
            top_k=top_k,
            min_similarity=None,
        )
        if allowed_external_ids:
            gallery_results = [
                row for row in gallery_results
                if row.get("external_person_id") in allowed_external_ids
            ]
        if not gallery_results:
            continue

        best = gallery_results[0]
        event = build_watchlist_hit_event(
            observation=observation,
            gallery_match=best,
            threshold=threshold,
        )
        similarity = float(best["similarity"])
        should_emit = similarity >= threshold
        if should_emit and not dry_run:
            if redis_client is None:
                raise ValueError("redis_client is required when dry_run=false")
            publish_security_event(redis_client, event, stream=event_stream)
            emitted_events.append(event)

        candidates.append(
            FaceMatchCandidate(
                source_observation_id=str(observation["source_observation_id"]),
                person_id=int(best["person_id"]),
                external_person_id=best.get("external_person_id"),
                person_name=best.get("person_name") or "",
                gallery_embedding_id=int(best["id"]),
                similarity=similarity,
                threshold=float(threshold),
                event_created=bool(should_emit and not dry_run),
                source_event_id=event["source_event_id"],
            )
        )

    return {
        "source_id": source_id,
        "threshold": float(threshold),
        "dry_run": dry_run,
        "observations_checked": len(observations),
        "events_emitted": len(emitted_events),
        "top_candidates": [asdict(c) for c in candidates[:20]],
        "emitted_events": emitted_events,
        "event_type": WATCHLIST_HIT_EVENT_TYPE,
        "algorithm_type": FACE_INTELLIGENCE_ALGORITHM_TYPE,
        "live_search_hit": "contract_only_deferred",
    }
