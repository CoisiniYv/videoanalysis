"""Face match event producer for midterm watchlist_hit events.

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
    "clip_required": False,
    "pre_seconds": 5,
    "post_seconds": 5,
    "evidence_mode": "image_only",
    "playback_kind": "image",
}
MIDTERM_FACE_MATCH_NOT_IMPLEMENTED_REASON = (
    "Midterm face match evidence created the evidence task, but production "
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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


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
                   embedding_model, embedding_dim, embedding_norm, payload
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


def _observation_media(observation: dict[str, Any]) -> dict[str, Any]:
    payload = observation.get("payload")
    if not isinstance(payload, dict):
        return {}
    media = payload.get("media")
    return media if isinstance(media, dict) else {}


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_ts_ms_from_observation(
    observation: dict[str, Any],
    media: dict[str, Any],
) -> int:
    """Return frame-domain event time for frame-origin face matches."""
    frame_pts_ns = _int_or_none(media.get("frame_pts"))
    if frame_pts_ns is not None and frame_pts_ns > 0:
        return frame_pts_ns // 1_000_000
    return int(observation.get("timestamp_ms") or 0)


def build_watchlist_hit_event(
    *,
    observation: dict[str, Any],
    gallery_match: dict[str, Any],
    threshold: float,
    severity: str = "high",
    rule_id: str = "watchlist_hit_mvp",
    rule_name: str = "watchlist_hit_mvp",
    match_source: str = "env_fallback",
    target_person_ids: list[int] | tuple[int, ...] | None = None,
    target_external_person_ids: list[str] | tuple[str, ...] | None = None,
    target_names: list[str] | tuple[str, ...] | None = None,
    evidence_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a unified SecurityEvent dict for one watchlist hit."""
    timestamp_ms = int(observation.get("timestamp_ms") or 0)
    source_media = _observation_media(observation)
    event_ts_ms = _event_ts_ms_from_observation(observation, source_media)
    person_id = int(gallery_match["person_id"])
    similarity = float(gallery_match["similarity"])
    source_observation_id = str(observation["source_observation_id"])
    source_event_id = build_source_event_id(source_observation_id, person_id)
    face_bbox = _jsonable(observation.get("face_bbox"))
    landmarks = _jsonable(observation.get("landmarks"))
    person_name = gallery_match.get("person_name") or ""
    label = f"{person_name} {similarity:.2f}".strip()
    frame_uuid = source_media.get("frame_uuid")
    keyframe_uuid = source_media.get("keyframe_uuid")
    previous_keyframe_uuid = source_media.get("previous_keyframe_uuid")
    effective_policy = {
        **DEFAULT_EVIDENCE_POLICY,
        **(evidence_policy or {}),
    }
    clip_required = _truthy(effective_policy.get("clip_required", False))
    evidence_mode = str(
        effective_policy.get("evidence_mode")
        or ("video_clip" if clip_required else "image_only")
    )
    playback_kind = str(
        effective_policy.get("playback_kind")
        or ("video" if clip_required else "image")
    )
    target_person_id_list = [int(value) for value in (target_person_ids or [])]
    target_external_id_list = [str(value) for value in (target_external_person_ids or [])]
    target_name_list = [str(value) for value in (target_names or [])]

    payload = {
        "watchlist": {
            "rule_id": rule_id,
            "rule_name": rule_name,
            "match_source": match_source,
            "threshold": float(threshold),
            "target_person_ids": target_person_id_list,
            "target_external_person_ids": target_external_id_list,
            "target_names": target_name_list,
        },
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
        "primary_identity_join_key": "source_observation_id",
        "track_id_join_warning": True,
        "observation": {
            "camera_id": observation.get("camera_id") or "",
            "source_id": observation.get("source_id") or "",
            "track_id": str(observation.get("track_id") or ""),
            "person_track_id": str(
                observation.get("person_track_id") or observation.get("track_id") or ""
            ),
            "face_track_id": observation.get("face_track_id"),
            "track_id_semantics": observation.get(
                "track_id_semantics",
                "person_track_id",
            ),
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
            "clip_required": clip_required,
            "evidence_mode": evidence_mode,
            "playback_kind": playback_kind,
            "frame_identity_anchor": "frame_uuid",
            "time_domain": "savant_frame",
            "media_status": "image_pending" if not clip_required else "not_implemented",
            "snapshot_status": "image_pending" if not clip_required else "not_implemented",
            "clip_status": "not_required" if not clip_required else "not_implemented",
            "metadata_status": "image_pending" if not clip_required else "not_implemented",
            "snapshot_path": observation.get("snapshot_path"),
            "crop_path": observation.get("crop_path"),
            "raw_clip_path": None,
            "annotated_clip_path": None,
            "metadata_path": None,
            "recording_strategy": "image_only" if not clip_required else "reserved",
            "pre_seconds": effective_policy["pre_seconds"],
            "post_seconds": effective_policy["post_seconds"],
            "frame_uuid": frame_uuid,
            "keyframe_uuid": keyframe_uuid,
            "previous_keyframe_uuid": previous_keyframe_uuid,
            "frame_pts": source_media.get("frame_pts"),
            "frame_dts": source_media.get("frame_dts"),
            "duration": source_media.get("duration"),
            "frame_num": source_media.get("frame_num"),
            "ntp_timestamp": source_media.get("ntp_timestamp"),
            "time_base": source_media.get("time_base"),
            "metadata_source": source_media.get("metadata_source"),
            "stream_session_id": source_media.get("stream_session_id"),
            "error_message": None if not clip_required else MIDTERM_FACE_MATCH_NOT_IMPLEMENTED_REASON,
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
        "algorithm_version": "midterm",
        "start_ts_ms": event_ts_ms,
        "end_ts_ms": event_ts_ms,
        "event_ts_ms": event_ts_ms,
        "frame_id": 0,
        "frame_uuid": frame_uuid,
        "keyframe_uuid": keyframe_uuid,
        "confidence": similarity,
        "severity": severity,
        "zone": "",
        "rule_id": rule_id,
        "rule_name": rule_name,
        "description": f"Watchlist hit for {person_name}".strip(),
        "snapshot_required": True,
        "clip_required": clip_required,
        "evidence_policy": dict(effective_policy),
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
