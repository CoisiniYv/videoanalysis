"""R3.2A metadata + snapshot evidence processor."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.evidence_metadata_writer import build_metadata, write_metadata_file
from app.evidence_snapshot_writer import build_overlay, write_snapshot_jpg
from app.repository import update_evidence_media_result


DEFAULT_MEDIA_ROOT = "/data/video-analytics/media"


@dataclass(frozen=True)
class EvidenceMediaResult:
    event_id: str
    task_id: str | None
    media_status: str
    snapshot_status: str
    metadata_status: str
    clip_status: str
    output_root: str
    snapshot_path: str | None
    metadata_path: str
    error_message: str | None


def _event_output_root(media_root: str, event: dict[str, Any]) -> str:
    event_id = str(event["id"])
    created = event.get("created_at")
    yyyy = mm = dd = "unknown"
    if created is not None and hasattr(created, "strftime"):
        yyyy = created.strftime("%Y")
        mm = created.strftime("%m")
        dd = created.strftime("%d")
    return str(Path(media_root) / "events" / yyyy / mm / dd / event_id)


def _storage_root(media_root: str) -> tuple[str, bool, str | None]:
    root = Path(media_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return str(root), False, None
    except Exception as exc:
        fallback = Path.cwd() / "tmp" / "r3_2a_media"
        fallback.mkdir(parents=True, exist_ok=True)
        return str(fallback), True, f"media_root_not_writable: {exc}"


def load_event_and_task(
    conn: psycopg.Connection,
    event_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Load event row and latest evidence task by UUID or source_event_id."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT *
            FROM events
            WHERE id::text = %(event_id)s OR source_event_id = %(event_id)s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {"event_id": event_id},
        )
        event = cur.fetchone()
        if not event:
            return None, None
        cur.execute(
            """
            SELECT *
            FROM evidence_tasks
            WHERE event_id = %(event_uuid)s OR source_event_id = %(source_event_id)s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {
                "event_uuid": event["id"],
                "source_event_id": event["source_event_id"],
            },
        )
        return dict(event), cur.fetchone()


def process_event_evidence(
    conn: psycopg.Connection,
    *,
    event_id: str,
    rtsp_url: str | None = None,
    media_root: str = DEFAULT_MEDIA_ROOT,
    capture_backend: str = "opencv",
) -> EvidenceMediaResult:
    """Generate metadata.json and best-effort snapshot.jpg for one event."""
    event, task = load_event_and_task(conn, event_id)
    if event is None:
        raise ValueError(f"event not found: {event_id}")

    storage_root, fallback_used, fallback_reason = _storage_root(media_root)
    output_root = _event_output_root(storage_root, event)
    Path(output_root).mkdir(parents=True, exist_ok=True)

    metadata_path = str(Path(output_root) / "metadata.json")
    snapshot_path = str(Path(output_root) / "snapshot.jpg")
    overlay = build_overlay(event)

    snapshot = write_snapshot_jpg(
        rtsp_url=rtsp_url,
        output_path=snapshot_path,
        event=event,
        overlay=overlay,
        capture_backend=capture_backend,
    )

    snapshot_status = snapshot["snapshot_status"]
    metadata_status = "ready"
    media_status = "ready" if snapshot_status == "ready" else "partial"
    error_message = snapshot.get("error_message")
    if fallback_used:
        snapshot["capture"]["storage_fallback_used"] = True
        snapshot["capture"]["storage_fallback_reason"] = fallback_reason

    metadata = build_metadata(
        event=event,
        media_status=media_status,
        snapshot_status=snapshot_status,
        metadata_status=metadata_status,
        snapshot_path=snapshot.get("snapshot_path"),
        metadata_path=metadata_path,
        capture=snapshot["capture"],
        overlay=overlay,
    )
    write_metadata_file(metadata_path, metadata)

    task_id = str(task["task_id"]) if task else None
    update_evidence_media_result(
        conn,
        event_id=str(event["id"]),
        task_id=task_id,
        media_status=media_status,
        snapshot_status=snapshot_status,
        metadata_status=metadata_status,
        clip_status="not_implemented",
        snapshot_path=snapshot.get("snapshot_path"),
        metadata_path=metadata_path,
        output_root=output_root,
        storage_fallback_used=fallback_used,
        storage_fallback_reason=fallback_reason,
        error_message=error_message,
    )

    return EvidenceMediaResult(
        event_id=str(event["id"]),
        task_id=task_id,
        media_status=media_status,
        snapshot_status=snapshot_status,
        metadata_status=metadata_status,
        clip_status="not_implemented",
        output_root=output_root,
        snapshot_path=snapshot.get("snapshot_path"),
        metadata_path=metadata_path,
        error_message=error_message,
    )
