"""Continuous evidence overlay JSONL generation.

The output is intentionally metadata-only: no embeddings and no image/crop
bytes. Frontend clients can combine ``raw_clip`` with this JSONL timeline to
render overlays on demand.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.annotation_style import build_style

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
DEFAULT_PRE_SECONDS = 5.0
DEFAULT_POST_SECONDS = 10.0


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _payload_media(payload: Any) -> dict[str, Any]:
    payload_dict = _as_dict(payload)
    media = payload_dict.get("media")
    return media if isinstance(media, dict) else {}


def _clip_window(event_context: dict[str, Any]) -> tuple[int, int, float, float]:
    payload = _as_dict(event_context.get("payload"))
    media = _payload_media(payload)
    policy = _as_dict(event_context.get("evidence_policy"))
    pre_seconds = _to_float(
        policy.get("pre_seconds", media.get("pre_seconds")),
        DEFAULT_PRE_SECONDS,
    )
    post_seconds = _to_float(
        policy.get("post_seconds", media.get("post_seconds")),
        DEFAULT_POST_SECONDS,
    )
    pre_seconds = pre_seconds if pre_seconds is not None else DEFAULT_PRE_SECONDS
    post_seconds = (
        post_seconds if post_seconds is not None else DEFAULT_POST_SECONDS
    )
    event_ts_ms = _to_int(event_context.get("event_ts_ms"))
    start_ts_ms = event_ts_ms - int(pre_seconds * 1000)
    end_ts_ms = event_ts_ms + int(post_seconds * 1000)
    return start_ts_ms, end_ts_ms, pre_seconds, post_seconds


def _normalise_bbox(face_bbox: Any, confidence: Any) -> dict[str, Any] | None:
    if isinstance(face_bbox, dict):
        values = face_bbox.get("values") or face_bbox.get("bbox")
        fmt = face_bbox.get("format") or face_bbox.get("bbox_format")
        conf = face_bbox.get("confidence", confidence)
    else:
        values = face_bbox
        fmt = "cxcywh"
        conf = confidence

    if not isinstance(values, list) or len(values) < 4:
        return None

    numbers = [_to_float(item) for item in values[:4]]
    if any(item is None for item in numbers):
        return None

    return {
        "format": fmt or "xyxy_or_cxcywh",
        "values": [float(item) for item in numbers if item is not None],
        "confidence": _to_float(conf, 0.0),
    }


def _normalise_landmarks(landmarks: Any) -> dict[str, Any]:
    points: list[Any] = []
    if isinstance(landmarks, list):
        if landmarks and all(isinstance(item, (int, float)) for item in landmarks):
            coords = [float(item) for item in landmarks]
            points = [
                [coords[i], coords[i + 1]]
                for i in range(0, min(len(coords) - 1, 10), 2)
            ]
        elif landmarks and all(isinstance(item, list) for item in landmarks):
            points = landmarks[:5]
    return {
        "format": "5_point",
        "points": _jsonable(points),
    }


def _load_observations(
    pg_conn: psycopg.Connection,
    *,
    source_id: str,
    start_ts_ms: int,
    end_ts_ms: int,
) -> list[dict[str, Any]]:
    try:
        with pg_conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, source_observation_id, camera_id, source_id, track_id,
                       timestamp_ms, frame_num, face_bbox, landmarks,
                       face_confidence, quality, detector_model, embedding_model,
                       embedding_dim, embedding_norm, payload
                FROM face_observations
                WHERE source_id = %(source_id)s
                  AND timestamp_ms BETWEEN %(start_ts_ms)s AND %(end_ts_ms)s
                ORDER BY timestamp_ms ASC, source_observation_id ASC
                """,
                {
                    "source_id": source_id,
                    "start_ts_ms": start_ts_ms,
                    "end_ts_ms": end_ts_ms,
                },
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        logger.exception(
            "continuous annotation observation lookup failed source_id=%s",
            source_id,
        )
        return []


def _load_gallery_candidates(
    pg_conn: psycopg.Connection,
    source_observation_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not source_observation_ids:
        return {}
    try:
        with pg_conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (mr.query_source_observation_id)
                       mr.query_source_observation_id,
                       mr.query_person_id AS person_id,
                       mr.query_gallery_embedding_id AS gallery_embedding_id,
                       mr.similarity,
                       mr.rank,
                       mr.similarity_threshold,
                       p.external_person_id,
                       p.name AS display_name
                FROM match_results mr
                LEFT JOIN persons p ON p.id = mr.query_person_id
                WHERE mr.search_mode = 'gallery_match'
                  AND mr.query_source_observation_id = ANY(
                      %(source_observation_ids)s::text[]
                  )
                ORDER BY mr.query_source_observation_id, mr.rank ASC,
                         mr.created_at DESC
                """,
                {"source_observation_ids": source_observation_ids},
            )
            return {
                str(row["query_source_observation_id"]): dict(row)
                for row in cur.fetchall()
                if row.get("query_source_observation_id")
            }
    except Exception:
        logger.exception("continuous annotation match_result lookup failed")
        return {}


def _event_identity(event_context: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(event_context.get("payload"))
    match = _as_dict(payload.get("match"))
    person = _as_dict(payload.get("matched_person"))
    source_observation_id = str(match.get("source_observation_id") or "")
    similarity = _to_float(match.get("similarity"))
    threshold = _to_float(match.get("threshold"), 0.5)
    return {
        "source_observation_id": source_observation_id,
        "person_id": person.get("person_id"),
        "external_person_id": person.get("external_person_id"),
        "display_name": person.get("name") or person.get("display_name") or "",
        "similarity": similarity,
        "rank": 1,
        "threshold": threshold,
        "gallery_embedding_id": match.get("gallery_embedding_id"),
    }


def _identity_for_observation(
    *,
    observation: dict[str, Any],
    event_identity: dict[str, Any],
    gallery_candidates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source_observation_id = str(observation.get("source_observation_id") or "")
    candidate = None
    if source_observation_id and source_observation_id == event_identity.get(
        "source_observation_id"
    ):
        candidate = event_identity
    elif source_observation_id:
        candidate = gallery_candidates.get(source_observation_id)

    if not candidate:
        return {
            "status": "unknown",
            "person_id": None,
            "external_person_id": None,
            "display_name": "",
            "similarity": None,
            "rank": None,
            "threshold": event_identity.get("threshold", 0.5),
            "match_status": "not_searched",
        }

    similarity = _to_float(candidate.get("similarity"))
    threshold = _to_float(candidate.get("threshold"), None)
    if threshold is None:
        threshold = _to_float(candidate.get("similarity_threshold"), 0.5)
    above = similarity is not None and threshold is not None and similarity >= threshold

    return {
        "status": "matched" if above else "low_similarity_candidate",
        "person_id": candidate.get("person_id"),
        "external_person_id": candidate.get("external_person_id"),
        "display_name": candidate.get("display_name") or "",
        "similarity": similarity,
        "rank": candidate.get("rank"),
        "threshold": threshold,
        "match_status": "above_threshold" if above else "below_threshold",
    }


def _fallback_event_observation(event_context: dict[str, Any]) -> dict[str, Any] | None:
    payload = _as_dict(event_context.get("payload"))
    observation = _as_dict(payload.get("observation"))
    media = _payload_media(payload)
    face_bbox = observation.get("face_bbox")
    if face_bbox is None:
        face_bbox = _as_dict(payload.get("overlay")).get("face_bbox")
    if face_bbox is None:
        return None

    identity = _event_identity(event_context)
    return {
        "id": None,
        "source_observation_id": identity.get("source_observation_id") or "",
        "camera_id": observation.get("camera_id") or event_context.get("camera_id", ""),
        "source_id": observation.get("source_id") or event_context.get("source_id", ""),
        "track_id": observation.get("track_id") or event_context.get("track_id", ""),
        "timestamp_ms": observation.get("timestamp_ms")
        or event_context.get("event_ts_ms", 0),
        "frame_num": media.get("frame_num"),
        "face_bbox": face_bbox,
        "landmarks": observation.get("landmarks"),
        "face_confidence": observation.get("face_confidence")
        or event_context.get("confidence", 0.0),
        "quality": observation.get("quality", 0.0),
        "detector_model": "yolov8_face",
        "embedding_model": "adaface",
        "embedding_dim": None,
        "embedding_norm": None,
        "payload": {"media": media},
    }


def _annotation_object(
    *,
    observation: dict[str, Any],
    identity: dict[str, Any],
    event_context: dict[str, Any],
) -> dict[str, Any] | None:
    bbox = _normalise_bbox(
        observation.get("face_bbox"),
        observation.get("face_confidence"),
    )
    if bbox is None:
        return None

    style = build_style(
        event_type=str(event_context.get("event_type", "")),
        severity=str(event_context.get("severity", "")),
        identity_status=str(identity.get("status") or "unknown"),
        display_name=str(identity.get("display_name") or ""),
        similarity=_to_float(identity.get("similarity")),
    )

    return {
        "object_type": "face",
        "object_id": f"face:{observation.get('source_observation_id', '')}",
        "track_id": str(observation.get("track_id") or ""),
        "bbox": bbox,
        "landmarks": _normalise_landmarks(observation.get("landmarks")),
        "identity": {
            "status": identity.get("status", "unknown"),
            "person_id": identity.get("person_id"),
            "external_person_id": identity.get("external_person_id"),
            "display_name": identity.get("display_name") or "",
            "similarity": identity.get("similarity"),
            "rank": identity.get("rank"),
            "threshold": identity.get("threshold"),
            "match_status": identity.get("match_status"),
        },
        "pose": {
            "status": "unavailable",
            "reason": "pose_observation_not_yet_persisted",
        },
        "action": {
            "status": "none",
            "event_type": None,
            "severity": None,
        },
        "style": style,
    }


def _line_key(observation: dict[str, Any]) -> tuple[int, str]:
    media = _payload_media(observation.get("payload"))
    return (
        _to_int(observation.get("timestamp_ms")),
        str(media.get("frame_uuid") or ""),
    )


def _line_base(
    *,
    observation: dict[str, Any],
    start_ts_ms: int,
) -> dict[str, Any]:
    payload = _as_dict(observation.get("payload"))
    media = _payload_media(payload)
    timestamp_ms = _to_int(observation.get("timestamp_ms"))
    return {
        "schema_version": SCHEMA_VERSION,
        "source_id": observation.get("source_id") or media.get("source_id") or "",
        "camera_id": observation.get("camera_id") or "",
        "timestamp_ms": timestamp_ms,
        "time_offset_ms": timestamp_ms - start_ts_ms,
        "frame_num": observation.get("frame_num") or media.get("frame_num"),
        "frame_uuid": media.get("frame_uuid"),
        "keyframe_uuid": media.get("keyframe_uuid"),
        "previous_keyframe_uuid": media.get("previous_keyframe_uuid"),
        "frame_pts": media.get("frame_pts"),
        "ntp_timestamp": media.get("ntp_timestamp"),
        "objects": [],
    }


def _contains_key_fragment(value: Any, fragments: tuple[str, ...]) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in fragments):
                return True
            if _contains_key_fragment(child, fragments):
                return True
    elif isinstance(value, list):
        return any(_contains_key_fragment(item, fragments) for item in value)
    elif isinstance(value, str):
        lowered = value.lower()
        if "data:image/" in lowered:
            return True
        if len(value) > 2048 and any(mark in lowered[:128] for mark in ("jpeg", "png")):
            return True
    return False


def _summary(lines: list[dict[str, Any]]) -> dict[str, Any]:
    face_objects = 0
    matched_objects = 0
    low_similarity_objects = 0
    unknown_objects = 0
    pose_unavailable_objects = 0
    action_none_objects = 0
    colors_used: set[str] = set()

    for line in lines:
        for obj in line.get("objects", []):
            if obj.get("object_type") != "face":
                continue
            face_objects += 1
            identity_status = _as_dict(obj.get("identity")).get("status")
            if identity_status == "matched":
                matched_objects += 1
            elif identity_status == "low_similarity_candidate":
                low_similarity_objects += 1
            else:
                unknown_objects += 1
            if _as_dict(obj.get("pose")).get("status") == "unavailable":
                pose_unavailable_objects += 1
            if _as_dict(obj.get("action")).get("status") == "none":
                action_none_objects += 1
            color = _as_dict(obj.get("style")).get("bbox_color")
            if color:
                colors_used.add(str(color))

    return {
        "schema_version": SCHEMA_VERSION,
        "annotation_lines": len(lines),
        "face_objects": face_objects,
        "matched_objects": matched_objects,
        "low_similarity_objects": low_similarity_objects,
        "unknown_objects": unknown_objects,
        "pose_unavailable_objects": pose_unavailable_objects,
        "action_none_objects": action_none_objects,
        "colors_used": sorted(colors_used),
        "embedding_leaked": _contains_key_fragment(lines, ("embedding",)),
        "image_bytes_leaked": _contains_key_fragment(
            lines,
            (
                "image",
                "crop",
                "crop_bytes",
                "base64",
                "jpeg",
                "png",
                "raw_bytes",
                "frame_bytes",
            ),
        ),
    }


def build_continuous_annotations(
    pg_conn: psycopg.Connection,
    event_context: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build ordered JSONL-ready annotation lines and summary metadata."""
    start_ts_ms, end_ts_ms, pre_seconds, post_seconds = _clip_window(event_context)
    source_id = str(event_context.get("source_id") or "")
    observations = _load_observations(
        pg_conn,
        source_id=source_id,
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
    )
    if not observations:
        fallback = _fallback_event_observation(event_context)
        if fallback is not None:
            observations = [fallback]

    source_observation_ids = [
        str(row.get("source_observation_id"))
        for row in observations
        if row.get("source_observation_id")
    ]
    gallery_candidates = _load_gallery_candidates(pg_conn, source_observation_ids)
    event_identity = _event_identity(event_context)

    grouped: "OrderedDict[tuple[int, str], dict[str, Any]]" = OrderedDict()
    for observation in sorted(observations, key=_line_key):
        identity = _identity_for_observation(
            observation=observation,
            event_identity=event_identity,
            gallery_candidates=gallery_candidates,
        )
        obj = _annotation_object(
            observation=observation,
            identity=identity,
            event_context=event_context,
        )
        if obj is None:
            continue
        key = _line_key(observation)
        if key not in grouped:
            grouped[key] = _line_base(
                observation=observation,
                start_ts_ms=start_ts_ms,
            )
        grouped[key]["objects"].append(obj)

    lines = list(grouped.values())
    summary = _summary(lines)
    summary.update(
        {
            "clip_start_ts_ms": start_ts_ms,
            "clip_end_ts_ms": end_ts_ms,
            "pre_seconds": pre_seconds,
            "post_seconds": post_seconds,
            "source_id": source_id,
            "event_id": event_context.get("event_id", ""),
            "event_type": event_context.get("event_type", ""),
            "frontend_overlay_required": True,
            "annotation_mode": "continuous_jsonl",
        }
    )
    return lines, summary


def write_continuous_annotation_bundle(
    pg_conn: psycopg.Connection,
    event_context: dict[str, Any],
    *,
    annotations_path: str,
    summary_path: str,
) -> dict[str, Any]:
    """Write ``annotations.jsonl`` and ``summary.json`` for one evidence bundle."""
    lines, summary = build_continuous_annotations(pg_conn, event_context)
    annotations_file = Path(annotations_path)
    summary_file = Path(summary_path)
    annotations_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.parent.mkdir(parents=True, exist_ok=True)

    with annotations_file.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(_jsonable(line), ensure_ascii=False))
            fh.write("\n")

    with summary_file.open("w", encoding="utf-8") as fh:
        json.dump(_jsonable(summary), fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    return summary
