"""Database index writer for materialized evidence bundles."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


PRODUCTION_ANNOTATIONS_FILE = "annotations.frame_cache.identity.jsonl"
SIDECAR_SUMMARY_FILE = "summary.frame_cache.identity.json"
BUNDLE_SUMMARY_FILE = "summary.json"
SINK_METADATA_FILE = "sink_metadata.json"
RAW_CLIP_FILE = "raw_clip.mov"
FFMPEG_LOG_FILE = "video_crop_ffmpeg.log"


def upsert_evidence_bundle_index(
    conn: psycopg.Connection,
    *,
    event_id: str,
    bundle_dir: str | Path,
    compute_sha256: bool = False,
    include_timeline: bool = False,
    include_overlays: bool = False,
) -> dict[str, int | str | bool]:
    """Upsert searchable DB rows for one evidence bundle.

    Video bytes stay in ``raw_clip.mov`` on the filesystem. PostgreSQL receives
    only URI/path strings and structured small metadata.
    """

    bundle = Path(bundle_dir)
    metadata = _load_json(bundle / "metadata.json")
    summary = _load_json(bundle / BUNDLE_SUMMARY_FILE)
    sidecar_summary = _load_json(bundle / SIDECAR_SUMMARY_FILE)
    raw_clip = _first_existing(bundle, (RAW_CLIP_FILE, "raw_clip.mp4", "raw_clip.mkv", "raw_clip.webm"))
    annotations_path = bundle / PRODUCTION_ANNOTATIONS_FILE
    sink_metadata_path = bundle / SINK_METADATA_FILE

    annotation_count = _annotation_count(sidecar_summary, annotations_path)
    object_counts = _dict(sidecar_summary.get("object_counts") or summary.get("object_counts"))
    media_status = _media_status(raw_clip=raw_clip, summary=summary, sidecar_summary=sidecar_summary)
    raw_clip_size = _file_size(raw_clip)
    raw_clip_sha = _sha256(raw_clip) if compute_sha256 and raw_clip else None
    raw_clip_duration = _float_or_none(summary.get("raw_clip_duration") or summary.get("clip_duration_seconds"))
    merged_summary = _merged_summary(
        bundle=bundle,
        metadata=metadata,
        summary=summary,
        sidecar_summary=sidecar_summary,
        annotations_path=annotations_path,
        sink_metadata_path=sink_metadata_path,
    )
    materialization = _materialization(summary, sidecar_summary)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO evidence_bundles (
                event_id, source_event_id, camera_id, source_id, camera_name,
                event_type, event_created_at, alarm_machine_time, media_status,
                evidence_state, evidence_reason, raw_clip_uri,
                raw_clip_size_bytes, raw_clip_duration_seconds, raw_clip_sha256,
                raw_clip_content_type, annotation_status, annotation_count,
                matched_objects, unknown_objects, visual_evidence_status,
                frontend_overlay_required, summary, materialization, updated_at
            )
            SELECT
                e.id, e.source_event_id, e.camera_id, e.source_id,
                COALESCE(c.name, e.payload->>'camera_name', e.payload->'camera'->>'name'),
                e.event_type, e.created_at, e.created_at, %(media_status)s,
                %(evidence_state)s, %(evidence_reason)s, %(raw_clip_uri)s,
                %(raw_clip_size_bytes)s, %(raw_clip_duration_seconds)s,
                %(raw_clip_sha256)s, %(raw_clip_content_type)s,
                %(annotation_status)s, %(annotation_count)s,
                %(matched_objects)s, %(unknown_objects)s,
                %(visual_evidence_status)s, %(frontend_overlay_required)s,
                %(summary)s, %(materialization)s, now()
            FROM events e
            LEFT JOIN cameras c
              ON c.id::text = e.camera_id
              OR c.source_id = e.source_id
            WHERE e.id = %(event_id)s::uuid
            ON CONFLICT (event_id) DO UPDATE SET
                source_event_id = EXCLUDED.source_event_id,
                camera_id = EXCLUDED.camera_id,
                source_id = EXCLUDED.source_id,
                camera_name = EXCLUDED.camera_name,
                event_type = EXCLUDED.event_type,
                event_created_at = EXCLUDED.event_created_at,
                alarm_machine_time = EXCLUDED.alarm_machine_time,
                media_status = EXCLUDED.media_status,
                evidence_state = EXCLUDED.evidence_state,
                evidence_reason = EXCLUDED.evidence_reason,
                raw_clip_uri = EXCLUDED.raw_clip_uri,
                raw_clip_size_bytes = EXCLUDED.raw_clip_size_bytes,
                raw_clip_duration_seconds = EXCLUDED.raw_clip_duration_seconds,
                raw_clip_sha256 = COALESCE(EXCLUDED.raw_clip_sha256, evidence_bundles.raw_clip_sha256),
                raw_clip_content_type = EXCLUDED.raw_clip_content_type,
                annotation_status = EXCLUDED.annotation_status,
                annotation_count = EXCLUDED.annotation_count,
                matched_objects = EXCLUDED.matched_objects,
                unknown_objects = EXCLUDED.unknown_objects,
                visual_evidence_status = EXCLUDED.visual_evidence_status,
                frontend_overlay_required = EXCLUDED.frontend_overlay_required,
                summary = EXCLUDED.summary,
                materialization = EXCLUDED.materialization,
                updated_at = now()
            """,
            {
                "event_id": event_id,
                "media_status": media_status,
                "evidence_state": media_status,
                "evidence_reason": "" if media_status == "materialized" else media_status,
                "raw_clip_uri": str(raw_clip) if raw_clip else None,
                "raw_clip_size_bytes": raw_clip_size,
                "raw_clip_duration_seconds": raw_clip_duration,
                "raw_clip_sha256": raw_clip_sha,
                "raw_clip_content_type": _content_type(raw_clip),
                "annotation_status": str(sidecar_summary.get("annotation_status") or summary.get("annotation_status") or ""),
                "annotation_count": annotation_count,
                "matched_objects": _matched_objects(object_counts, summary, sidecar_summary),
                "unknown_objects": _int_or_none(summary.get("unknown_objects") or sidecar_summary.get("unknown_objects")),
                "visual_evidence_status": str(
                    sidecar_summary.get("visual_evidence_status")
                    or summary.get("visual_evidence_status")
                    or ""
                ),
                "frontend_overlay_required": bool(
                    sidecar_summary.get("frontend_overlay_required")
                    if "frontend_overlay_required" in sidecar_summary
                    else True
                ),
                "summary": Jsonb(merged_summary),
                "materialization": Jsonb(materialization),
            },
        )

    artifacts = [
        ("raw_clip", raw_clip, _content_type(raw_clip), None, raw_clip_size, raw_clip_sha, {"filename": raw_clip.name if raw_clip else None}),
        ("overlay_annotations", annotations_path if annotations_path.is_file() else None, "application/x-ndjson", None, _file_size(annotations_path), None, {"records": annotation_count}),
        ("sink_timeline", sink_metadata_path if sink_metadata_path.is_file() else None, "application/json", None, _file_size(sink_metadata_path), None, {}),
        ("bundle_summary", bundle / BUNDLE_SUMMARY_FILE if (bundle / BUNDLE_SUMMARY_FILE).is_file() else None, "application/json", None, _file_size(bundle / BUNDLE_SUMMARY_FILE), None, {}),
    ]
    ffmpeg_log = bundle / FFMPEG_LOG_FILE
    if media_status != "materialized" and ffmpeg_log.is_file():
        artifacts.append(
            (
                "ffmpeg_log",
                ffmpeg_log,
                "text/plain",
                None,
                _file_size(ffmpeg_log),
                None,
                {"diagnostic": True},
            )
        )
    artifact_count = 0
    for artifact_type, path, content_type, compression, size, sha, artifact_meta in artifacts:
        if path is None:
            continue
        _upsert_artifact(
            conn,
            event_id=event_id,
            artifact_type=artifact_type,
            path=path,
            content_type=content_type,
            compression=compression,
            size_bytes=size,
            sha256=sha,
            metadata=artifact_meta,
        )
        artifact_count += 1

    timeline_count = 0
    overlay_count = 0
    if include_timeline and sink_metadata_path.is_file():
        timeline_count = _upsert_timeline(conn, event_id=event_id, path=sink_metadata_path)
    if include_overlays and annotations_path.is_file():
        overlay_count = _upsert_overlays(conn, event_id=event_id, path=annotations_path)

    return {
        "event_id": event_id,
        "bundle_indexed": True,
        "artifacts": artifact_count,
        "timeline_rows": timeline_count,
        "overlay_rows": overlay_count,
    }


def _upsert_artifact(
    conn: psycopg.Connection,
    *,
    event_id: str,
    artifact_type: str,
    path: Path,
    content_type: str | None,
    compression: str | None,
    size_bytes: int | None,
    sha256: str | None,
    metadata: dict[str, Any],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO evidence_artifacts (
                event_id, artifact_type, uri, storage_backend, content_type,
                compression, size_bytes, sha256, status, metadata, updated_at
            )
            VALUES (
                %(event_id)s::uuid, %(artifact_type)s, %(uri)s, 'filesystem',
                %(content_type)s, %(compression)s, %(size_bytes)s, %(sha256)s,
                'ready', %(metadata)s, now()
            )
            ON CONFLICT (event_id, artifact_type) DO UPDATE SET
                uri = EXCLUDED.uri,
                storage_backend = EXCLUDED.storage_backend,
                content_type = EXCLUDED.content_type,
                compression = EXCLUDED.compression,
                size_bytes = EXCLUDED.size_bytes,
                sha256 = COALESCE(EXCLUDED.sha256, evidence_artifacts.sha256),
                status = EXCLUDED.status,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            """,
            {
                "event_id": event_id,
                "artifact_type": artifact_type,
                "uri": str(path),
                "content_type": content_type,
                "compression": compression,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "metadata": Jsonb(metadata),
            },
        )


def _upsert_timeline(conn: psycopg.Connection, *, event_id: str, path: Path) -> int:
    rows = _load_records(path)
    count = 0
    with conn.cursor() as cur:
        for index, record in enumerate(rows):
            frame_index = _int_or_none(record.get("clip_frame_index"))
            if frame_index is None:
                frame_index = index
            cur.execute(
                """
                INSERT INTO evidence_frame_timeline (
                    event_id, clip_frame_index, frame_uuid, frame_pts, frame_dts,
                    duration_ns, timestamp_ms, width, height, source_id, camera_id,
                    stream_session_id, keyframe_uuid, metadata
                )
                VALUES (
                    %(event_id)s::uuid, %(clip_frame_index)s, %(frame_uuid)s,
                    %(frame_pts)s, %(frame_dts)s, %(duration_ns)s,
                    %(timestamp_ms)s, %(width)s, %(height)s, %(source_id)s,
                    %(camera_id)s, %(stream_session_id)s, %(keyframe_uuid)s,
                    %(metadata)s
                )
                ON CONFLICT (event_id, clip_frame_index) DO UPDATE SET
                    frame_uuid = EXCLUDED.frame_uuid,
                    frame_pts = EXCLUDED.frame_pts,
                    frame_dts = EXCLUDED.frame_dts,
                    duration_ns = EXCLUDED.duration_ns,
                    timestamp_ms = EXCLUDED.timestamp_ms,
                    width = EXCLUDED.width,
                    height = EXCLUDED.height,
                    source_id = EXCLUDED.source_id,
                    camera_id = EXCLUDED.camera_id,
                    stream_session_id = EXCLUDED.stream_session_id,
                    keyframe_uuid = EXCLUDED.keyframe_uuid,
                    metadata = EXCLUDED.metadata
                """,
                {
                    "event_id": event_id,
                    "clip_frame_index": frame_index,
                    "frame_uuid": _text(record.get("frame_uuid") or record.get("uuid")),
                    "frame_pts": _int_or_none(record.get("frame_pts") or record.get("pts")),
                    "frame_dts": _int_or_none(record.get("frame_dts") or record.get("dts")),
                    "duration_ns": _int_or_none(record.get("duration")),
                    "timestamp_ms": _int_or_none(record.get("timestamp_ms")),
                    "width": _int_or_none(record.get("width")),
                    "height": _int_or_none(record.get("height")),
                    "source_id": _text(record.get("source_id")),
                    "camera_id": _text(record.get("camera_id")),
                    "stream_session_id": _text(record.get("stream_session_id")),
                    "keyframe_uuid": _text(record.get("keyframe_uuid")),
                    "metadata": Jsonb(record),
                },
            )
            count += 1
    return count


def _upsert_overlays(conn: psycopg.Connection, *, event_id: str, path: Path) -> int:
    rows = _load_records(path)
    count = 0
    with conn.cursor() as cur:
        for index, record in enumerate(rows):
            frame_index = _int_or_none(record.get("clip_frame_index"))
            if frame_index is None:
                frame_index = index
            objects = record.get("objects")
            if not isinstance(objects, list):
                objects = []
            cur.execute(
                """
                INSERT INTO evidence_overlay_segments (
                    event_id, clip_frame_index, frame_uuid, frame_pts, t_ms,
                    object_count, objects, record
                )
                VALUES (
                    %(event_id)s::uuid, %(clip_frame_index)s, %(frame_uuid)s,
                    %(frame_pts)s, %(t_ms)s, %(object_count)s, %(objects)s,
                    %(record)s
                )
                ON CONFLICT (event_id, clip_frame_index) DO UPDATE SET
                    frame_uuid = EXCLUDED.frame_uuid,
                    frame_pts = EXCLUDED.frame_pts,
                    t_ms = EXCLUDED.t_ms,
                    object_count = EXCLUDED.object_count,
                    objects = EXCLUDED.objects,
                    record = EXCLUDED.record
                """,
                {
                    "event_id": event_id,
                    "clip_frame_index": frame_index,
                    "frame_uuid": _text(record.get("frame_uuid") or record.get("uuid")),
                    "frame_pts": _int_or_none(record.get("frame_pts") or record.get("pts")),
                    "t_ms": _int_or_none(record.get("t_ms")),
                    "object_count": len(objects),
                    "objects": Jsonb(objects),
                    "record": Jsonb(record),
                },
            )
            count += 1
    return count


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if not stripped:
        return []
    if stripped[0] == "[":
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return []
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _merged_summary(
    *,
    bundle: Path,
    metadata: dict[str, Any],
    summary: dict[str, Any],
    sidecar_summary: dict[str, Any],
    annotations_path: Path,
    sink_metadata_path: Path,
) -> dict[str, Any]:
    media = _dict(metadata.get("media"))
    event = _dict(metadata.get("event"))
    merged = {
        **summary,
        "sidecar_summary": sidecar_summary,
        "event_id": event.get("event_id") or bundle.name,
        "source_event_id": event.get("source_event_id"),
        "evidence_dir": str(bundle),
        "raw_clip_path": str(bundle / RAW_CLIP_FILE),
        "sink_metadata_path": str(sink_metadata_path) if sink_metadata_path.is_file() else media.get("sink_metadata_path"),
        "annotations_jsonl_path": str(annotations_path) if annotations_path.is_file() else media.get("annotations_jsonl_path"),
        "summary_json_path": str(bundle / BUNDLE_SUMMARY_FILE),
        "clip_status": summary.get("clip_status") or media.get("clip_status") or "ready",
    }
    return {key: value for key, value in merged.items() if value is not None}


def _materialization(summary: dict[str, Any], sidecar_summary: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "duration_guard_status",
        "duration_guard_failed",
        "duration_guard_reason",
        "sink_window_guard_status",
        "sink_window_guard_failed",
        "sink_window_guard_reason",
        "runtime_epoch_id",
        "epoch_guard_status",
        "epoch_guard_failed",
        "epoch_guard_reason",
    )
    return {key: summary.get(key) for key in keys if key in summary} | {
        "production_ready": bool(sidecar_summary.get("production_ready")),
        "timeline_domain": sidecar_summary.get("timeline_domain"),
    }


def _media_status(*, raw_clip: Path | None, summary: dict[str, Any], sidecar_summary: dict[str, Any]) -> str:
    if raw_clip is None:
        return str(summary.get("clip_status") or "not_implemented")
    if sidecar_summary.get("production_ready") is False:
        return "generated_unverified"
    return "materialized"


def _annotation_count(summary: dict[str, Any], annotations_path: Path) -> int:
    for key in ("annotation_lines", "displayable_record_count", "frame_count", "sidecar_frame_count"):
        value = _int_or_none(summary.get(key))
        if value is not None:
            return value
    if not annotations_path.is_file():
        return 0
    try:
        return sum(1 for line in annotations_path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def _matched_objects(object_counts: dict[str, Any], summary: dict[str, Any], sidecar_summary: dict[str, Any]) -> int | None:
    for value in (
        summary.get("matched_objects"),
        sidecar_summary.get("matched_objects"),
        object_counts.get("known_face"),
        object_counts.get("person"),
    ):
        parsed = _int_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _first_existing(root: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = root / name
        if path.is_file():
            return path
    return None


def _file_size(path: Path | None) -> int | None:
    if path is None or not path.is_file():
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


def _sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_type(path: Path | None) -> str | None:
    if path is None:
        return None
    guessed, _encoding = mimetypes.guess_type(str(path))
    return guessed or ("video/quicktime" if path.suffix == ".mov" else None)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
