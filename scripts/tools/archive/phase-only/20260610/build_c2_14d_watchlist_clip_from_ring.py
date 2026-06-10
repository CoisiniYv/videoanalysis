#!/usr/bin/env python3
"""Build C2.14D RTSP watchlist evidence from real Redis/DB hits and ring media."""

from __future__ import annotations

import argparse
import html
import json
import math
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"
for path in (ROOT, MEDIA_WORKER_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import psycopg  # noqa: E402
import redis  # noqa: E402
from pgvector.psycopg import register_vector  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app.post_savant_metadata_annotation_builder import (  # type: ignore  # noqa: E402
    SIDECAR_ANNOTATIONS_FILE,
    SIDECAR_SUMMARY_FILE,
    build_post_savant_annotation_sidecar,
)
from app.post_savant_video_integrity import inspect_video_integrity  # type: ignore  # noqa: E402
from scripts.tools import manage_c2_14_rtsp_segment_ring as ring  # noqa: E402


SCHEMA_VERSION = "1.0-c2.14d-watchlist-known-face-evidence"
CANONICAL_ROOT = Path("/home/user/video-analytics")
DEFAULT_SOURCE_ID = "c2_post_savant_fps_probe"
DEFAULT_RING_ROOT = Path("/data/video-analytics/media/rtsp-ring")
DEFAULT_EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")
DEFAULT_DATABASE_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6395/0"
DEFAULT_THRESHOLD = 0.65
DEFAULT_PRE_SECONDS = 5.0
DEFAULT_POST_SECONDS = 5.0
DEFAULT_MAX_REDIS_MESSAGES = 10000
TARGET_EXTERNAL_IDS = {"demo:f4_3:reese", "demo:f4_3:finch"}
TARGET_PERSON_IDS = {5, 6}
FACE_STREAM = "security.face_observations"
EVENT_STREAM = "security.events"

RESULT_PASS = "PASS_C2_14D_RTSP_WATCHLIST_KNOWN_FACE_EVIDENCE_READY"
RESULT_NO_HIT = "PARTIAL_C2_14D_NO_WATCHLIST_HIT"
RESULT_HIT_OUTSIDE_RING = "PARTIAL_C2_14D_WATCHLIST_HIT_OUTSIDE_RING"
RESULT_IDENTITY_GAP = "PARTIAL_C2_14D_IDENTITY_BINDING_GAP"
RESULT_VIDEO_GAP = "PARTIAL_C2_14D_VIDEO_INTEGRITY_GAP"
RESULT_FAKE_HIT = "FAIL_C2_14D_FAKE_WATCHLIST_HIT"
RESULT_UNSAFE_PAYLOAD = "FAIL_C2_14D_UNSAFE_PAYLOAD"
RESULT_DB_BROAD_FALLBACK = "FAIL_C2_14D_DB_BROAD_WINDOW_FALLBACK_USED"
RESULT_WRONG_WORKTREE = "FAIL_C2_14D_WRONG_WORKTREE"

FORBIDDEN_PAYLOAD_KEYS = {
    "embedding",
    "embeddings",
    "embedding_vector",
    "embedding_values",
    "feature",
    "features",
    "image",
    "image_bytes",
    "image_base64",
    "base64_image",
    "base64",
    "crop",
    "crop_bytes",
    "face_crop_bytes",
    "crop_base64",
    "raw_frame",
    "frame_bytes",
}


@dataclass(frozen=True)
class RingFrameHit:
    segment: dict[str, Any]
    frame_index: int
    frame: dict[str, Any]
    binding_basis: str


@dataclass(frozen=True)
class WatchlistCandidate:
    event: dict[str, Any]
    observation: dict[str, Any]
    redis_id: str | None
    source: str
    similarity: float
    threshold: float
    person_id: int
    external_person_id: str
    person_name: str
    gallery_embedding_id: int
    source_observation_id: str
    frame_pts: int
    frame_uuid: str | None
    ring_window: dict[str, Any]
    frame_hit: RingFrameHit | None
    identity_binding_method: str | None
    identity_binding_detail: str | None


def run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"expected object at {path}:{line_number}")
        rows.append(payload)
    return rows


def first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def int_or_none(value: Any) -> int | None:
    return ring.int_or_none(value)


def float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def media_payload(value: dict[str, Any]) -> dict[str, Any]:
    payload = value.get("payload")
    if not isinstance(payload, dict):
        return {}
    media = payload.get("media")
    return media if isinstance(media, dict) else {}


def event_frame_pts(event: dict[str, Any]) -> int | None:
    return int_or_none(first_present(event.get("frame_pts"), media_payload(event).get("frame_pts")))


def event_source_observation_id(event: dict[str, Any]) -> str | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    match = payload.get("match") if isinstance(payload.get("match"), dict) else {}
    return text_or_none(first_present(event.get("source_observation_id"), match.get("source_observation_id")))


def event_external_person_id(event: dict[str, Any]) -> str | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    matched = payload.get("matched_person") if isinstance(payload.get("matched_person"), dict) else {}
    return text_or_none(first_present(event.get("external_person_id"), matched.get("external_person_id")))


def event_gallery_embedding_id(event: dict[str, Any]) -> int | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    match = payload.get("match") if isinstance(payload.get("match"), dict) else {}
    return int_or_none(first_present(event.get("gallery_embedding_id"), match.get("gallery_embedding_id")))


def event_similarity(event: dict[str, Any]) -> float | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    match = payload.get("match") if isinstance(payload.get("match"), dict) else {}
    return float_or_none(first_present(event.get("similarity"), event.get("match_score"), event.get("confidence"), match.get("similarity")))


def event_threshold(event: dict[str, Any]) -> float | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    match = payload.get("match") if isinstance(payload.get("match"), dict) else {}
    return float_or_none(first_present(event.get("threshold"), match.get("threshold")))


def text_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def vector_from_db(value: Any) -> list[float]:
    if hasattr(value, "tolist"):
        return [float(item) for item in value.tolist()]
    if isinstance(value, (list, tuple)):
        return [float(item) for item in value]
    raise ValueError("gallery embedding is not a vector")


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimensions differ")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_PAYLOAD_KEYS:
                continue
            sanitized[str(key)] = sanitize_payload(child)
        return sanitized
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    return value


def scan_for_unsafe_payload(payload: Any) -> dict[str, Any]:
    return ring.scan_for_unsafe_payload(payload)


def load_on_disk_index_rows(ring_root: Path, source_id: str) -> list[dict[str, Any]]:
    return ring.read_jsonl(ring.index_path(ring_root, source_id))


def load_current_ring_inventory(ring_root: Path, source_id: str) -> list[dict[str, Any]]:
    source_segments = ring.segments_root(ring_root, source_id)
    if not source_segments.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for metadata_path in sorted(source_segments.glob("*/metadata.json")):
        video_path = metadata_path.with_name("video.mov")
        if not video_path.is_file():
            video_path = metadata_path.with_name("video.mp4")
        if not video_path.is_file():
            continue
        try:
            frames = ring.load_native_metadata(metadata_path)
        except Exception:
            continue
        source_frames = [frame for frame in frames if frame.get("source_id") == source_id]
        pts_values = [value for value in (ring.frame_pts(frame) for frame in source_frames) if value is not None]
        ts_values = [value for value in (ring.frame_timestamp_ms(frame) for frame in source_frames) if value is not None]
        if not pts_values and not ts_values:
            continue
        rows.append(
            {
                "schema_version": ring.SCHEMA_VERSION,
                "source_id": source_id,
                "segment_id": metadata_path.parent.name,
                "segment_dir": str(metadata_path.parent),
                "video_path": str(video_path),
                "metadata_path": str(metadata_path),
                "first_frame_pts": min(pts_values) if pts_values else None,
                "last_frame_pts": max(pts_values) if pts_values else None,
                "first_timestamp_ms": min(ts_values) if ts_values else None,
                "last_timestamp_ms": max(ts_values) if ts_values else None,
                "frame_count": len(source_frames),
                "keyframe_count": sum(1 for frame in source_frames if ring.frame_keyframe(frame)),
                "time_basis": "pts" if pts_values else "timestamp_ms",
                "completed": True,
                "cleanup_eligible": True,
            }
        )
    rows.sort(key=lambda row: (int_or_none(row.get("first_frame_pts")) or int_or_none(row.get("first_timestamp_ms")) or 0, row.get("segment_id") or ""))
    return rows


def find_window_from_rows(
    rows: list[dict[str, Any]],
    *,
    source_id: str,
    center_pts: int | None,
    center_timestamp_ms: int | None,
    pre_seconds: float,
    post_seconds: float,
) -> dict[str, Any]:
    if center_pts is None and center_timestamp_ms is None:
        raise ValueError("center_pts or center_timestamp_ms is required")
    if center_pts is not None:
        basis = "pts"
        first_key = "first_frame_pts"
        last_key = "last_frame_pts"
        center = center_pts
        requested_start = int(center_pts - pre_seconds * 1_000_000_000)
        requested_end = int(center_pts + post_seconds * 1_000_000_000)
    else:
        basis = "timestamp_ms"
        first_key = "first_timestamp_ms"
        last_key = "last_timestamp_ms"
        center = int(center_timestamp_ms or 0)
        requested_start = int(center - pre_seconds * 1000)
        requested_end = int(center + post_seconds * 1000)
    selected = []
    for row in rows:
        if row.get("source_id") != source_id:
            continue
        first = int_or_none(row.get(first_key))
        last = int_or_none(row.get(last_key))
        if first is None or last is None:
            continue
        if last >= requested_start and first <= requested_end:
            selected.append(row)
    selected.sort(key=lambda row: int_or_none(row.get(first_key)) or 0)
    covered = bool(selected) and min(int(row[first_key]) for row in selected if row.get(first_key) is not None) <= requested_start and max(int(row[last_key]) for row in selected if row.get(last_key) is not None) >= requested_end
    event_located = bool(selected) and any(
        (int_or_none(row.get(first_key)) or 0) <= center <= (int_or_none(row.get(last_key)) or -1)
        for row in selected
    )
    reason = None
    if not selected:
        reason = "no_segment_overlap"
    elif not covered:
        reason = "segment_window_gap"
    elif not event_located:
        reason = "event_frame_not_located"
    return {
        "schema_version": ring.SCHEMA_VERSION,
        "mode": "find-window-transient-current-ring-inventory",
        "source_id": source_id,
        "basis": basis,
        "center_pts": center_pts,
        "center_timestamp_ms": center_timestamp_ms,
        "pre_seconds": pre_seconds,
        "post_seconds": post_seconds,
        "requested_start": requested_start,
        "requested_end": requested_end,
        "selected_segment_count": len(selected),
        "selected_segments": selected,
        "window_covered": covered,
        "event_frame_located": event_located,
        "status": "pass" if covered and event_located else "partial",
        "reason": reason,
    }


def find_frame_hit(
    *,
    rows: list[dict[str, Any]],
    source_id: str,
    frame_pts: int | None,
    frame_uuid: str | None,
) -> RingFrameHit | None:
    for segment in rows:
        if segment.get("source_id") != source_id:
            continue
        first = int_or_none(segment.get("first_frame_pts"))
        last = int_or_none(segment.get("last_frame_pts"))
        if frame_pts is not None and first is not None and last is not None and not (first <= frame_pts <= last):
            continue
        frames = ring.load_native_metadata(Path(str(segment["metadata_path"])))
        for frame_index, frame in enumerate(frames):
            if frame.get("source_id") != source_id:
                continue
            if frame_uuid and ring.frame_uuid(frame) == frame_uuid:
                return RingFrameHit(segment=segment, frame_index=frame_index, frame=frame, binding_basis="direct_frame_uuid")
            if frame_pts is not None and ring.frame_pts(frame) == frame_pts:
                return RingFrameHit(segment=segment, frame_index=frame_index, frame=frame, binding_basis="direct_frame_pts")
    return None


def face_objects(frame: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        obj for obj in (frame.get("objects") or [])
        if isinstance(obj, dict) and (obj.get("namespace") == "yolov8_face" or obj.get("label") == "face")
    ]


def fetch_gallery_targets(database_url: str) -> list[dict[str, Any]]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pge.id, pge.person_id, p.external_person_id, p.name AS person_name, pge.embedding
                FROM person_gallery_embeddings pge
                JOIN persons p ON p.id = pge.person_id
                WHERE p.external_person_id = ANY(%s) AND pge.is_active = true
                ORDER BY pge.id
                """,
                (sorted(TARGET_EXTERNAL_IDS),),
            )
            rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        row["embedding_vector"] = vector_from_db(row.pop("embedding"))
    return rows


def fetch_db_watchlist_events(database_url: str, *, source_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source_event_id, event_type, source_id, camera_id, track_id, person_id,
                       event_ts_ms, frame_uuid, payload, created_at
                FROM events
                WHERE source_id = %s AND event_type = 'watchlist_hit'
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (source_id, limit),
            )
            return [dict(row) for row in cur.fetchall()]


def event_from_db_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    match = payload.get("match") if isinstance(payload.get("match"), dict) else {}
    matched = payload.get("matched_person") if isinstance(payload.get("matched_person"), dict) else {}
    event = {
        "schema_version": "1.0",
        "source_event_id": row.get("source_event_id"),
        "event_type": row.get("event_type"),
        "camera_id": row.get("camera_id"),
        "source_id": row.get("source_id"),
        "track_id": row.get("track_id"),
        "person_id": row.get("person_id"),
        "external_person_id": matched.get("external_person_id"),
        "gallery_embedding_id": match.get("gallery_embedding_id"),
        "similarity": match.get("similarity"),
        "threshold": match.get("threshold"),
        "source_observation_id": match.get("source_observation_id"),
        "event_ts_ms": row.get("event_ts_ms"),
        "timestamp_ms": row.get("event_ts_ms"),
        "frame_pts": media.get("frame_pts"),
        "frame_uuid": row.get("frame_uuid") or media.get("frame_uuid"),
        "payload": sanitize_payload(payload),
        "origin": "postgres_events",
    }
    return event


def validate_watchlist_event_contract(event: dict[str, Any], *, threshold: float = DEFAULT_THRESHOLD) -> list[str]:
    failures: list[str] = []
    if event.get("event_type") != "watchlist_hit":
        failures.append("event_type_not_watchlist_hit")
    person_id = int_or_none(event.get("person_id"))
    if person_id not in TARGET_PERSON_IDS:
        failures.append("person_id_not_reese_or_finch")
    external = event_external_person_id(event)
    if external not in TARGET_EXTERNAL_IDS:
        failures.append("external_person_id_not_reese_or_finch")
    similarity = event_similarity(event)
    event_threshold_value = event_threshold(event)
    effective_threshold = event_threshold_value if event_threshold_value is not None else threshold
    if similarity is None:
        failures.append("similarity_missing")
    elif similarity < effective_threshold:
        failures.append("similarity_below_threshold")
    if not event_source_observation_id(event):
        failures.append("source_observation_id_missing")
    if event_frame_pts(event) is None:
        failures.append("frame_pts_missing")
    if not event.get("source_id"):
        failures.append("source_id_missing")
    return failures


def select_existing_db_candidate(
    *,
    events: list[dict[str, Any]],
    ring_rows: list[dict[str, Any]],
    source_id: str,
    threshold: float,
    pre_seconds: float,
    post_seconds: float,
) -> tuple[WatchlistCandidate | None, dict[str, Any]]:
    considered: list[dict[str, Any]] = []
    for row in events:
        event = event_from_db_row(row)
        failures = validate_watchlist_event_contract(event, threshold=threshold)
        frame_pts = event_frame_pts(event)
        window = find_window_from_rows(
            ring_rows,
            source_id=source_id,
            center_pts=frame_pts,
            center_timestamp_ms=None if frame_pts is not None else int_or_none(event.get("event_ts_ms")),
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
        )
        considered.append(
            {
                "source_event_id": event.get("source_event_id"),
                "person_id": event.get("person_id"),
                "external_person_id": event_external_person_id(event),
                "similarity": event_similarity(event),
                "threshold": event_threshold(event),
                "frame_pts": frame_pts,
                "window_status": window.get("status"),
                "contract_failures": failures,
            }
        )
        if failures or window.get("status") != "pass":
            continue
        hit = find_frame_hit(rows=ring_rows, source_id=source_id, frame_pts=frame_pts, frame_uuid=text_or_none(event.get("frame_uuid")))
        candidate = WatchlistCandidate(
            event=event,
            observation={},
            redis_id=None,
            source="postgres_events",
            similarity=float(event_similarity(event) or 0.0),
            threshold=float(event_threshold(event) or threshold),
            person_id=int(event.get("person_id")),
            external_person_id=str(event_external_person_id(event)),
            person_name=str((_dict(event.get("payload")).get("matched_person") or {}).get("name") or ""),
            gallery_embedding_id=int(event_gallery_embedding_id(event) or 0),
            source_observation_id=str(event_source_observation_id(event)),
            frame_pts=int(frame_pts),
            frame_uuid=text_or_none(event.get("frame_uuid")),
            ring_window=window,
            frame_hit=hit,
            identity_binding_method="direct_source_observation_id" if hit and face_objects(hit.frame) else None,
            identity_binding_detail="postgres_watchlist_hit",
        )
        return candidate, {"mode": "postgres_events", "considered": considered}
    return None, {"mode": "postgres_events", "considered": considered}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_redis_observation(redis_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    data = fields.get("data")
    if not data:
        return None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    payload["_redis_id"] = redis_id
    return payload


def observation_embedding(observation: dict[str, Any]) -> list[float] | None:
    embedding = observation.get("embedding")
    if not isinstance(embedding, list):
        return None
    if len(embedding) != 512:
        return None
    try:
        return [float(item) for item in embedding]
    except (TypeError, ValueError):
        return None


def observation_frame_pts(observation: dict[str, Any]) -> int | None:
    media = media_payload(observation)
    return int_or_none(first_present(media.get("frame_pts"), observation.get("frame_pts")))


def observation_frame_uuid(observation: dict[str, Any]) -> str | None:
    return text_or_none(first_present(media_payload(observation).get("frame_uuid"), observation.get("frame_uuid")))


def sanitize_observation_for_event(observation: dict[str, Any]) -> dict[str, Any]:
    sanitized = sanitize_payload(observation)
    if isinstance(sanitized, dict):
        sanitized.pop("_redis_id", None)
    return sanitized


def build_event_from_observation_match(
    *,
    observation: dict[str, Any],
    gallery: dict[str, Any],
    similarity: float,
    threshold: float,
) -> dict[str, Any]:
    gallery_match = {
        "id": int(gallery["id"]),
        "person_id": int(gallery["person_id"]),
        "external_person_id": gallery.get("external_person_id"),
        "person_name": gallery.get("person_name") or gallery.get("name") or "",
        "similarity": float(similarity),
    }
    event = build_watchlist_event_from_observation(
        observation=sanitize_observation_for_event(observation),
        gallery_match=gallery_match,
        threshold=threshold,
    )
    media = media_payload(observation)
    event.update(
        {
            "origin": "redis_face_observation_gallery_match",
            "source_observation_id": observation.get("source_observation_id"),
            "external_person_id": gallery_match["external_person_id"],
            "gallery_embedding_id": gallery_match["id"],
            "similarity": float(similarity),
            "threshold": float(threshold),
            "frame_pts": media.get("frame_pts"),
            "frame_uuid": media.get("frame_uuid"),
            "redis_id": observation.get("_redis_id"),
        }
    )
    return sanitize_payload(event)


def build_watchlist_event_from_observation(
    *,
    observation: dict[str, Any],
    gallery_match: dict[str, Any],
    threshold: float,
) -> dict[str, Any]:
    media = media_payload(observation)
    timestamp_ms = int(first_present(observation.get("timestamp_ms"), media.get("timestamp_ms"), 0))
    person_id = int(gallery_match["person_id"])
    similarity = float(gallery_match["similarity"])
    source_observation_id = str(observation["source_observation_id"])
    source_event_id = f"watchlist_hit:{source_observation_id}:{person_id}"
    person_name = str(gallery_match.get("person_name") or "")
    external_person_id = gallery_match.get("external_person_id")
    payload = {
        "matched_person": {
            "person_id": person_id,
            "external_person_id": external_person_id,
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
            "face_bbox": observation.get("face_bbox"),
            "landmarks": observation.get("landmarks"),
            "quality": float(observation.get("quality") or 0.0),
            "face_confidence": float(observation.get("face_confidence") or 0.0),
        },
        "overlay": {
            "type": "face_match",
            "label": f"{person_name} {similarity:.2f}".strip(),
            "face_bbox": observation.get("face_bbox"),
            "person_bbox": observation.get("person_bbox"),
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
            "pre_seconds": DEFAULT_PRE_SECONDS,
            "post_seconds": DEFAULT_POST_SECONDS,
            "frame_uuid": media.get("frame_uuid"),
            "keyframe_uuid": media.get("keyframe_uuid"),
            "previous_keyframe_uuid": media.get("previous_keyframe_uuid"),
            "frame_pts": media.get("frame_pts"),
            "frame_dts": media.get("frame_dts"),
            "duration": media.get("duration"),
            "frame_num": media.get("frame_num") or observation.get("frame_num"),
            "ntp_timestamp": media.get("ntp_timestamp"),
            "time_base": media.get("time_base"),
            "metadata_source": media.get("metadata_source"),
        },
    }
    return {
        "schema_version": "1.0",
        "source_event_id": source_event_id,
        "producer": "c2_14d_builder",
        "event_type": "watchlist_hit",
        "camera_id": observation.get("camera_id") or "",
        "source_id": observation.get("source_id") or "",
        "track_id": str(observation.get("track_id") or ""),
        "person_id": person_id,
        "algorithm_type": "face_intelligence",
        "algorithm_version": "c2.14d",
        "start_ts_ms": timestamp_ms,
        "end_ts_ms": timestamp_ms,
        "event_ts_ms": timestamp_ms,
        "frame_uuid": media.get("frame_uuid"),
        "keyframe_uuid": media.get("keyframe_uuid"),
        "confidence": similarity,
        "severity": "high",
        "rule_name": "c2_14d_rtsp_reese_finch_watchlist_rule",
        "description": f"Watchlist hit for {person_name}".strip(),
        "snapshot_required": True,
        "clip_required": True,
        "evidence_policy": {"snapshot_required": True, "clip_required": True, "pre_seconds": 5, "post_seconds": 5},
        "payload": payload,
    }


def select_redis_watchlist_candidate(
    *,
    redis_url: str,
    gallery_targets: list[dict[str, Any]],
    ring_rows: list[dict[str, Any]],
    source_id: str,
    threshold: float,
    pre_seconds: float,
    post_seconds: float,
    max_messages: int,
) -> tuple[WatchlistCandidate | None, dict[str, Any]]:
    client = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    entries = client.xrevrange(FACE_STREAM, count=max_messages)
    considered: list[dict[str, Any]] = []
    outside_ring_hits: list[dict[str, Any]] = []
    best: WatchlistCandidate | None = None
    for redis_id, fields in entries:
        observation = parse_redis_observation(redis_id, fields)
        if not observation or observation.get("source_id") != source_id:
            continue
        embedding = observation_embedding(observation)
        frame_pts = observation_frame_pts(observation)
        if embedding is None or frame_pts is None:
            continue
        frame_uuid = observation_frame_uuid(observation)
        window = find_window_from_rows(
            ring_rows,
            source_id=source_id,
            center_pts=frame_pts,
            center_timestamp_ms=None,
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
        )
        frame_hit = find_frame_hit(rows=ring_rows, source_id=source_id, frame_pts=frame_pts, frame_uuid=frame_uuid)
        for gallery in gallery_targets:
            similarity = cosine_similarity(embedding, gallery["embedding_vector"])
            external = str(gallery.get("external_person_id") or "")
            item = {
                "redis_id": redis_id,
                "source_observation_id": observation.get("source_observation_id"),
                "frame_pts": frame_pts,
                "frame_uuid": frame_uuid,
                "track_id": observation.get("track_id"),
                "person_id": gallery.get("person_id"),
                "external_person_id": external,
                "gallery_embedding_id": gallery.get("id"),
                "similarity": similarity,
                "threshold": threshold,
                "matched": similarity >= threshold,
                "window_status": window.get("status"),
                "frame_hit": frame_hit is not None,
                "face_objects_at_frame": len(face_objects(frame_hit.frame)) if frame_hit else 0,
            }
            if len(considered) < 50:
                considered.append(item)
            if similarity < threshold or external not in TARGET_EXTERNAL_IDS:
                continue
            if window.get("status") != "pass":
                outside_ring_hits.append(item)
                continue
            event = build_event_from_observation_match(
                observation=observation,
                gallery=gallery,
                similarity=similarity,
                threshold=threshold,
            )
            faces = face_objects(frame_hit.frame) if frame_hit else []
            identity_method = None
            detail = None
            if frame_hit and faces:
                identity_method = "direct_frame_pts"
                detail = f"{frame_hit.binding_basis}_face_object"
            candidate = WatchlistCandidate(
                event=event,
                observation=observation,
                redis_id=redis_id,
                source="redis_face_observation_gallery_match",
                similarity=similarity,
                threshold=threshold,
                person_id=int(gallery["person_id"]),
                external_person_id=external,
                person_name=str(gallery.get("person_name") or ""),
                gallery_embedding_id=int(gallery["id"]),
                source_observation_id=str(observation["source_observation_id"]),
                frame_pts=frame_pts,
                frame_uuid=frame_uuid,
                ring_window=window,
                frame_hit=frame_hit,
                identity_binding_method=identity_method,
                identity_binding_detail=detail,
            )
            if best is None or candidate_rank(candidate) > candidate_rank(best):
                best = candidate
    return best, {
        "mode": "redis_face_observations",
        "entries_scanned": len(entries),
        "considered_sample": considered,
        "outside_ring_hits": outside_ring_hits[:20],
        "outside_ring_hit_count": len(outside_ring_hits),
    }


def candidate_rank(candidate: WatchlistCandidate) -> tuple[int, int, float]:
    has_identity_binding = 1 if candidate.frame_hit and candidate.identity_binding_method else 0
    has_window = 1 if candidate.ring_window.get("status") == "pass" else 0
    return (has_identity_binding, has_window, candidate.similarity)


def filter_metadata_frames_with_indices(frames: list[dict[str, Any]], window: dict[str, Any]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    start = int(window["requested_start"])
    end = int(window["requested_end"])
    basis = window["basis"]
    for frame_index, frame in enumerate(frames):
        value = ring.frame_pts(frame) if basis == "pts" else ring.frame_timestamp_ms(frame)
        if value is None:
            continue
        if start <= value <= end:
            selected.append({"frame_index": frame_index, "frame": frame, "time_value": value})
    return selected


def build_window_raw_clip_from_segments(
    *,
    selected_segments: list[dict[str, Any]],
    selected_by_segment: list[dict[str, Any]],
    output_path: Path,
    requested_duration_s: float,
) -> dict[str, Any]:
    non_empty = [item for item in selected_by_segment if item["selected_indices"]]
    if not non_empty:
        return {"status": "failed", "reason": "no_frames_selected_for_window"}
    if shutil.which("ffmpeg") is None:
        return {"status": "failed", "reason": "ffmpeg_missing_for_window_crop"}
    selected_total = sum(len(item["selected_indices"]) for item in non_empty)
    if selected_total <= 0 or requested_duration_s <= 0:
        return {"status": "failed", "reason": "invalid_selected_frame_count_or_duration"}
    output_fps = selected_total / requested_duration_s
    command = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error"]
    filters: list[str] = []
    concat_inputs: list[str] = []
    input_index = 0
    segment_lookup = {str(segment.get("segment_id")): segment for segment in selected_segments}
    for item in non_empty:
        indices = item["selected_indices"]
        expected = max(indices) - min(indices) + 1
        if expected != len(indices):
            return {
                "status": "failed",
                "reason": "non_contiguous_frame_window",
                "segment_id": item.get("segment_id"),
                "selected_frame_count": len(indices),
            }
        segment = segment_lookup[str(item["segment_id"])]
        command.extend(["-i", str(segment["video_path"])])
        start = min(indices)
        end = max(indices)
        label = f"v{input_index}"
        filters.append(f"[{input_index}:v]select=between(n\\,{start}\\,{end}),setpts=N/({output_fps:.8f}*TB)[{label}]")
        concat_inputs.append(f"[{label}]")
        input_index += 1
    if len(non_empty) == 1:
        filter_complex = filters[0].replace("[v0]", "[out]")
    else:
        filter_complex = ";".join(filters + ["".join(concat_inputs) + f"concat=n={len(non_empty)}:v=1:a=0[out]"])
    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            f"{output_fps:.8f}",
            str(output_path),
        ]
    )
    proc = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0:
        return {"status": "failed", "reason": "ffmpeg_window_crop_failed", "stderr": proc.stderr[-1200:]}
    return {
        "status": "reencoded_ring_window",
        "raw_clip_path": str(output_path),
        "segment_video_paths": [str(segment_lookup[str(item["segment_id"])]["video_path"]) for item in non_empty],
        "time_domain_crop_applied": True,
        "segments_with_selected_frames": len(non_empty),
        "selected_frame_count": selected_total,
        "requested_duration_s": requested_duration_s,
        "output_fps": output_fps,
        "codec": "libx264",
    }


def bbox_xyxy_from_observation(value: Any) -> list[float] | None:
    if not isinstance(value, dict):
        return None
    values = value.get("values")
    fmt = value.get("format")
    if not isinstance(values, list) or len(values) != 4:
        return None
    numbers = [float(item) for item in values]
    if fmt == "xyxy":
        return numbers
    if fmt == "cxcywh":
        cx, cy, width, height = numbers
        return [cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2]
    return None


def bbox_iou(left: list[float] | None, right: list[float] | None) -> float | None:
    if left is None or right is None:
        return None
    lx1, ly1, lx2, ly2 = left
    rx1, ry1, rx2, ry2 = right
    ix1 = max(lx1, rx1)
    iy1 = max(ly1, ry1)
    ix2 = min(lx2, rx2)
    iy2 = min(ly2, ry2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    left_area = max(0.0, lx2 - lx1) * max(0.0, ly2 - ly1)
    right_area = max(0.0, rx2 - rx1) * max(0.0, ry2 - ry1)
    union = left_area + right_area - inter
    if union <= 0:
        return None
    return inter / union


def sidecar_bbox(obj: dict[str, Any]) -> list[float] | None:
    bbox = obj.get("bbox") if isinstance(obj.get("bbox"), dict) else {}
    xyxy = bbox.get("xyxy")
    if isinstance(xyxy, list) and len(xyxy) == 4:
        return [float(item) for item in xyxy]
    return None


def patch_sidecar_identity(
    *,
    rows: list[dict[str, Any]],
    candidate: WatchlistCandidate,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    patched = json.loads(json.dumps(rows))
    observation_bbox = bbox_xyxy_from_observation(candidate.observation.get("face_bbox"))
    target_row_index = None
    for index, row in enumerate(patched):
        if candidate.frame_uuid and row.get("frame_uuid") == candidate.frame_uuid:
            target_row_index = index
            break
        if int_or_none(row.get("frame_pts")) == candidate.frame_pts:
            target_row_index = index
            break
    if target_row_index is None:
        return patched, {"patched": False, "reason": "event_frame_not_in_sidecar"}
    row = patched[target_row_index]
    objects = row.get("objects") if isinstance(row.get("objects"), list) else []
    best_index = None
    best_iou = -1.0
    track_id = str(candidate.event.get("track_id") or candidate.observation.get("track_id") or "")
    for object_index, obj in enumerate(objects):
        if not isinstance(obj, dict) or obj.get("object_type") != "face":
            continue
        if track_id and str(obj.get("track_id") or "") == track_id:
            best_index = object_index
            best_iou = bbox_iou(observation_bbox, sidecar_bbox(obj)) or 0.0
            break
        current_iou = bbox_iou(observation_bbox, sidecar_bbox(obj))
        if current_iou is not None and current_iou > best_iou:
            best_iou = current_iou
            best_index = object_index
    if best_index is None and len([obj for obj in objects if isinstance(obj, dict) and obj.get("object_type") == "face"]) == 1:
        best_index = next(i for i, obj in enumerate(objects) if isinstance(obj, dict) and obj.get("object_type") == "face")
        best_iou = bbox_iou(observation_bbox, sidecar_bbox(objects[best_index])) or 0.0
    if best_index is None:
        return patched, {"patched": False, "reason": "matching_face_object_not_found", "row_index": target_row_index}
    obj = objects[best_index]
    identity_binding_method = candidate.identity_binding_method or "direct_frame_pts"
    identity = {
        "source_observation_id": candidate.source_observation_id,
        "event_type": "watchlist_hit",
        "person_id": candidate.person_id,
        "external_person_id": candidate.external_person_id,
        "person_name": candidate.person_name,
        "gallery_embedding_id": candidate.gallery_embedding_id,
        "similarity": candidate.similarity,
        "match_score": candidate.similarity,
        "threshold": candidate.threshold,
        "matched": True,
        "match_status": "matched",
        "identity_binding_method": identity_binding_method,
        "identity_binding_detail": candidate.identity_binding_detail,
        "source_event_id": candidate.event.get("source_event_id"),
    }
    obj["object_type"] = "known_face"
    obj["annotation_role"] = "watchlist_hit_identity"
    obj["label"] = {
        "kind": "known_face",
        "display_name": candidate.person_name or candidate.external_person_id,
        "external_person_id": candidate.external_person_id,
    }
    obj["identity"] = identity
    obj["watchlist_hit"] = {
        "event_type": "watchlist_hit",
        "source_observation_id": candidate.source_observation_id,
        "similarity": candidate.similarity,
        "threshold": candidate.threshold,
        "matched": True,
    }
    row["objects"] = objects
    row["displayable"] = True
    row["production_ready"] = True
    return patched, {
        "patched": True,
        "identity_binding_method": identity_binding_method,
        "identity_binding_detail": candidate.identity_binding_detail,
        "row_index": target_row_index,
        "object_index": best_index,
        "bbox_iou": best_iou if best_iou >= 0 else None,
        "track_id_matched": bool(track_id and str(obj.get("track_id") or "") == track_id),
        "frame_uuid_matched": bool(candidate.frame_uuid and row.get("frame_uuid") == candidate.frame_uuid),
        "frame_pts_matched": int_or_none(row.get("frame_pts")) == candidate.frame_pts,
    }


def update_sidecar_summary(summary: dict[str, Any], rows: list[dict[str, Any]], patch: dict[str, Any]) -> dict[str, Any]:
    known = 0
    objects = 0
    source_observation_ids = 0
    for row in rows:
        for obj in row.get("objects") or []:
            if not isinstance(obj, dict):
                continue
            objects += 1
            if obj.get("object_type") == "known_face":
                known += 1
            identity = obj.get("identity") if isinstance(obj.get("identity"), dict) else {}
            if identity.get("source_observation_id"):
                source_observation_ids += 1
    updated = dict(summary)
    updated.update(
        {
            "production_ready": known > 0,
            "canonical_clip": known > 0,
            "visual_binding_status": "verified" if known > 0 else "unverified",
            "visual_binding_reason": "watchlist_hit_identity_bound" if known > 0 else "identity_binding_missing",
            "known_face_objects_count": known,
            "object_count": objects,
            "source_observation_id_count": source_observation_ids,
            "watchlist_hit_count": known,
            "identity_binding_method": patch.get("identity_binding_method"),
            "identity_binding_detail": patch.get("identity_binding_detail"),
            "trigger_face_row_exists": True,
            "trigger_face_row_match_type": "watchlist_hit",
            "source_observation_id": patch.get("source_observation_id"),
            "legacy_used_for_visual_binding": False,
        }
    )
    return updated


def render_html(summary: dict[str, Any]) -> str:
    rows = [
        ("Event type", summary.get("event_type")),
        ("Source", summary.get("source_id")),
        ("Camera", summary.get("camera_id")),
        ("Person", f"{summary.get('person_id')} {summary.get('external_person_id') or ''}"),
        ("Similarity", f"{summary.get('similarity')} / {summary.get('threshold')}"),
        ("Source observation", summary.get("source_observation_id")),
        ("Frame", f"{summary.get('frame_pts')} ts={summary.get('event_ts_ms')}"),
        ("Raw clip", summary.get("raw_clip")),
        ("Sidecar", summary.get("sidecar")),
        ("Metadata", summary.get("sink_metadata")),
        ("Identity binding", summary.get("identity_binding_method")),
        ("Limitations", ", ".join(summary.get("limitations") or [])),
    ]
    body = "\n".join(f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>" for key, value in rows)
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>C2.14D RTSP Watchlist Evidence</title></head>
<body>
  <h1>C2.14D RTSP Watchlist Evidence</h1>
  <table>{body}</table>
</body>
</html>
"""


def render_markdown(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# C2.14D RTSP Watchlist Evidence",
            "",
            f"- event_type: {summary.get('event_type')}",
            f"- source_id: {summary.get('source_id')}",
            f"- camera_id: {summary.get('camera_id')}",
            f"- person_id: {summary.get('person_id')}",
            f"- external_person_id: {summary.get('external_person_id')}",
            f"- similarity: {summary.get('similarity')}",
            f"- threshold: {summary.get('threshold')}",
            f"- selected_event_time: {summary.get('event_ts_ms')}",
            f"- raw_clip: {summary.get('raw_clip')}",
            f"- sidecar: {summary.get('sidecar')}",
            f"- identity_binding_method: {summary.get('identity_binding_method')}",
            f"- limitations: {', '.join(summary.get('limitations') or [])}",
            "",
        ]
    )


def build_partial_output(output_dir: Path, *, marker: str, reason: str, diagnostics: dict[str, Any]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "partial" if marker.startswith("PARTIAL_") else "fail",
        "result_marker": marker,
        "reason": reason,
        "diagnostics": diagnostics,
        "event_style_replay_job_passed": False,
        "db_broad_window_fallback_used": False,
        "db_window_fallback_used": False,
        "legacy_used_for_visual_binding": False,
        "fake_watchlist_hit": False,
    }
    write_json(output_dir / "summary.json", summary)
    (output_dir / "report.md").write_text(render_markdown(summary), encoding="utf-8")
    return {"status": summary["status"], "result_marker": marker, "bundle_path": str(output_dir), "summary": summary}


def build_watchlist_clip_bundle(
    *,
    output_dir: Path,
    candidate: WatchlistCandidate,
    source_id: str,
    pre_seconds: float,
    post_seconds: float,
    ring_rows: list[dict[str, Any]],
    on_disk_index_count: int,
    publish_event: bool,
    redis_url: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    requested_duration_s = pre_seconds + post_seconds
    selected_segments = candidate.ring_window["selected_segments"]
    filtered_frames: list[dict[str, Any]] = []
    selected_by_segment: list[dict[str, Any]] = []
    for segment in selected_segments:
        frames = ring.load_native_metadata(Path(str(segment["metadata_path"])))
        indexed = filter_metadata_frames_with_indices(frames, candidate.ring_window)
        selected_by_segment.append(
            {
                "segment_id": segment.get("segment_id"),
                "selected_indices": [int(item["frame_index"]) for item in indexed],
                "selected_frame_count": len(indexed),
            }
        )
        filtered_frames.extend(item["frame"] for item in indexed)
    sink_metadata_path = output_dir / "sink_metadata.json"
    write_jsonl(sink_metadata_path, filtered_frames)
    raw_clip_path = output_dir / "raw_clip.mp4"
    clip_result = build_window_raw_clip_from_segments(
        selected_segments=selected_segments,
        selected_by_segment=selected_by_segment,
        output_path=raw_clip_path,
        requested_duration_s=requested_duration_s,
    )
    if clip_result.get("status") == "failed":
        return build_partial_output(
            output_dir,
            marker=RESULT_VIDEO_GAP,
            reason=str(clip_result.get("reason") or "clip_build_failed"),
            diagnostics={"clip_result": clip_result, "selected_event": candidate.event},
        )
    sidecar_path = output_dir / SIDECAR_ANNOTATIONS_FILE
    sidecar_summary_path = output_dir / SIDECAR_SUMMARY_FILE
    sidecar_result = build_post_savant_annotation_sidecar(
        metadata_path=sink_metadata_path,
        output_jsonl_path=sidecar_path,
        summary_path=sidecar_summary_path,
        extra_limitations=["rtsp_watchlist_known_face_c2_14d"],
    )
    patched_rows, identity_patch = patch_sidecar_identity(rows=sidecar_result.rows, candidate=candidate)
    identity_patch["source_observation_id"] = candidate.source_observation_id
    if not identity_patch.get("patched"):
        write_jsonl(sidecar_path, patched_rows)
        return build_partial_output(
            output_dir,
            marker=RESULT_IDENTITY_GAP,
            reason=str(identity_patch.get("reason") or "identity_binding_failed"),
            diagnostics={"identity_patch": identity_patch, "selected_event": candidate.event},
        )
    sidecar_summary = update_sidecar_summary(sidecar_result.summary, patched_rows, identity_patch)
    write_jsonl(sidecar_path, patched_rows)
    write_json(sidecar_summary_path, sidecar_summary)
    integrity = inspect_video_integrity(
        raw_clip_path,
        decode_log_path=output_dir / "video_integrity_decode_errors.log",
        requested_duration_s=requested_duration_s,
        sidecar_frame_count=len(patched_rows),
        trim_occurred=True,
        time_domain_crop_applied=True,
    )
    publication = {"requested": False, "status": "not_requested", "redis_id": None}
    if publish_event:
        client = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
        redis_event_id = client.xadd(
            EVENT_STREAM,
            {
                "type": "security_event",
                "source_event_id": str(candidate.event["source_event_id"]),
                "event_type": "watchlist_hit",
                "algorithm_type": str(candidate.event.get("algorithm_type") or "face_intelligence"),
                "camera_id": str(candidate.event.get("camera_id") or source_id),
                "track_id": str(candidate.event.get("track_id") or ""),
                "start_ts_ms": str(candidate.event.get("start_ts_ms") or ""),
                "end_ts_ms": str(candidate.event.get("end_ts_ms") or ""),
                "severity": str(candidate.event.get("severity") or "high"),
                "data": json.dumps(candidate.event, ensure_ascii=False),
            },
            maxlen=10000,
            approximate=True,
        )
        publication = {"requested": True, "status": "published", "redis_id": str(redis_event_id)}
    unsafe_scan = scan_for_unsafe_payload(
        {
            "watchlist_event": candidate.event,
            "sidecar_rows": patched_rows,
            "sidecar_summary": sidecar_summary,
            "identity_patch": identity_patch,
        }
    )
    if not unsafe_scan["passed"]:
        marker = RESULT_UNSAFE_PAYLOAD
        status = "fail"
    elif not bool(integrity.get("production_gate_passed")):
        marker = RESULT_VIDEO_GAP
        status = "partial"
    else:
        marker = RESULT_PASS
        status = "pass"
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "result_marker": marker,
        "event_type": "watchlist_hit",
        "event_identity": candidate.event.get("source_event_id"),
        "source_event_id": candidate.event.get("source_event_id"),
        "source_id": source_id,
        "camera_id": candidate.event.get("camera_id") or source_id,
        "person_id": candidate.person_id,
        "external_person_id": candidate.external_person_id,
        "person_name": candidate.person_name,
        "gallery_embedding_id": candidate.gallery_embedding_id,
        "similarity": candidate.similarity,
        "match_score": candidate.similarity,
        "threshold": candidate.threshold,
        "matched": True,
        "source_observation_id": candidate.source_observation_id,
        "track_id": candidate.event.get("track_id"),
        "frame_pts": candidate.frame_pts,
        "frame_uuid": candidate.frame_uuid,
        "event_ts_ms": candidate.event.get("event_ts_ms"),
        "raw_clip": str(raw_clip_path),
        "sink_metadata": str(sink_metadata_path),
        "sidecar": str(sidecar_path),
        "sidecar_summary": str(sidecar_summary_path),
        "selected_segment_ids": [segment.get("segment_id") for segment in selected_segments],
        "selected_frames_by_segment": selected_by_segment,
        "decoded_video_frame_count": integrity.get("decoded_frame_count"),
        "sidecar_frame_count": len(patched_rows),
        "duration_s": integrity.get("duration_s"),
        "video_integrity_status": integrity.get("integrity_status"),
        "video_integrity_pass": bool(integrity.get("production_gate_passed")),
        "identity_binding_method": identity_patch.get("identity_binding_method"),
        "identity_binding_detail": identity_patch.get("identity_binding_detail"),
        "identity_patch": identity_patch,
        "watchlist_event_source": candidate.source,
        "redis_face_observation_id": candidate.redis_id,
        "redis_event_publication": publication,
        "ring_index_mode": "transient_current_metadata_inventory",
        "on_disk_segment_index_row_count": on_disk_index_count,
        "transient_ring_inventory_row_count": len(ring_rows),
        "event_style_replay_job_passed": False,
        "db_broad_window_fallback_used": False,
        "db_window_fallback_used": False,
        "legacy_used_for_visual_binding": False,
        "fake_watchlist_hit": False,
        "unsafe_payload_scan": unsafe_scan,
        "limitations": [
            "runtime_module_provenance_not_normalized_yet",
            "native_sink_metadata_may_contain_savant_feature_vectors_not_copied_to_sidecar_or_report",
            "worker_api_product_chain_not_restored",
        ],
    }
    write_json(output_dir / "selected_watchlist_event.json", candidate.event)
    write_json(output_dir / "identity_patch.json", identity_patch)
    write_json(output_dir / "evidence_join_report.json", candidate.ring_window)
    write_json(output_dir / "video_integrity_report.json", integrity)
    write_json(output_dir / "unsafe_payload_scan.json", unsafe_scan)
    write_json(output_dir / "summary.json", summary)
    (output_dir / "index.html").write_text(render_html(summary), encoding="utf-8")
    (output_dir / "report.md").write_text(render_markdown(summary), encoding="utf-8")
    return {
        "status": status,
        "result_marker": marker,
        "bundle_path": str(output_dir),
        "raw_clip": str(raw_clip_path),
        "summary": summary,
    }


def build_c2_14d_watchlist_evidence(
    *,
    output_dir: Path,
    database_url: str,
    redis_url: str,
    ring_root: Path,
    source_id: str,
    threshold: float,
    pre_seconds: float,
    post_seconds: float,
    max_redis_messages: int,
    publish_event: bool,
    require_canonical_root: bool = True,
) -> dict[str, Any]:
    if require_canonical_root and ROOT.resolve(strict=False) != CANONICAL_ROOT.resolve(strict=False):
        return build_partial_output(
            output_dir,
            marker=RESULT_WRONG_WORKTREE,
            reason="wrong_worktree",
            diagnostics={"repo_root": str(ROOT), "expected_root": str(CANONICAL_ROOT)},
        )
    on_disk_index_rows = load_on_disk_index_rows(ring_root, source_id)
    ring_rows = load_current_ring_inventory(ring_root, source_id)
    if not ring_rows:
        return build_partial_output(
            output_dir,
            marker=RESULT_HIT_OUTSIDE_RING,
            reason="ring_inventory_empty",
            diagnostics={"ring_root": str(ring_root), "source_id": source_id},
        )
    db_candidate, db_diag = select_existing_db_candidate(
        events=fetch_db_watchlist_events(database_url, source_id=source_id),
        ring_rows=ring_rows,
        source_id=source_id,
        threshold=threshold,
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
    )
    if db_candidate and db_candidate.frame_hit and db_candidate.identity_binding_method:
        candidate = db_candidate
        redis_diag: dict[str, Any] = {"skipped": True, "reason": "postgres_candidate_selected"}
    else:
        gallery = fetch_gallery_targets(database_url)
        candidate, redis_diag = select_redis_watchlist_candidate(
            redis_url=redis_url,
            gallery_targets=gallery,
            ring_rows=ring_rows,
            source_id=source_id,
            threshold=threshold,
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
            max_messages=max_redis_messages,
        )
    if candidate is None:
        outside_count = int((redis_diag.get("outside_ring_hit_count") or 0) if isinstance(redis_diag, dict) else 0)
        marker = RESULT_HIT_OUTSIDE_RING if outside_count else RESULT_NO_HIT
        return build_partial_output(
            output_dir,
            marker=marker,
            reason="no_threshold_passing_watchlist_hit_with_ring_coverage" if not outside_count else "threshold_passing_hit_outside_ring",
            diagnostics={
                "postgres_events": db_diag,
                "redis_scan": redis_diag,
                "on_disk_segment_index_row_count": len(on_disk_index_rows),
                "transient_ring_inventory_row_count": len(ring_rows),
            },
        )
    contract_failures = validate_watchlist_event_contract(candidate.event, threshold=threshold)
    if contract_failures:
        return build_partial_output(
            output_dir,
            marker=RESULT_FAKE_HIT,
            reason="selected_event_contract_failed",
            diagnostics={"contract_failures": contract_failures, "selected_event": candidate.event},
        )
    if not candidate.frame_hit or not candidate.identity_binding_method:
        return build_partial_output(
            output_dir,
            marker=RESULT_IDENTITY_GAP,
            reason="selected_hit_lacks_direct_frame_identity_binding",
            diagnostics={"selected_event": candidate.event, "postgres_events": db_diag, "redis_scan": redis_diag},
        )
    return build_watchlist_clip_bundle(
        output_dir=output_dir,
        candidate=candidate,
        source_id=source_id,
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
        ring_rows=ring_rows,
        on_disk_index_count=len(on_disk_index_rows),
        publish_event=publish_event,
        redis_url=redis_url,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    parser.add_argument("--ring-root", type=Path, default=DEFAULT_RING_ROOT)
    parser.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--pre-seconds", type=float, default=DEFAULT_PRE_SECONDS)
    parser.add_argument("--post-seconds", type=float, default=DEFAULT_POST_SECONDS)
    parser.add_argument("--max-redis-messages", type=int, default=DEFAULT_MAX_REDIS_MESSAGES)
    parser.add_argument("--publish-event", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir or args.evidence_root / f"c2_14d_rtsp_watchlist_known_face_clip_{run_stamp()}"
    try:
        result = build_c2_14d_watchlist_evidence(
            output_dir=output_dir,
            database_url=args.database_url,
            redis_url=args.redis_url,
            ring_root=args.ring_root,
            source_id=args.source_id,
            threshold=args.threshold,
            pre_seconds=args.pre_seconds,
            post_seconds=args.post_seconds,
            max_redis_messages=args.max_redis_messages,
            publish_event=args.publish_event,
        )
    except Exception as exc:
        result = build_partial_output(
            output_dir,
            marker=RESULT_IDENTITY_GAP,
            reason=f"builder_exception:{exc}",
            diagnostics={"error": str(exc)},
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    marker = str(result.get("result_marker") or "")
    if marker == RESULT_PASS:
        return 0
    if marker.startswith("PARTIAL_"):
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
