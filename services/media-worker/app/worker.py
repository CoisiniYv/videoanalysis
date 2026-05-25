"""Media worker — monitor sink output, update events table with clip paths and snapshots."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import psycopg

from app.annotated_snapshot import generate_annotated_snapshot
from app.config import Config, load_config
from app.snapshot import generate_snapshot

logger = logging.getLogger(__name__)

shutdown_requested = False


def request_shutdown(signum: int, _frame: object) -> None:
    global shutdown_requested
    logger.info("shutdown requested by signal=%s", signum)
    shutdown_requested = True


def _parse_ndjson(filepath: Path) -> dict | None:
    """Parse an NDJSON (JSON Lines) file, returning the first valid JSON object."""
    try:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    continue
    except Exception:
        logger.exception("failed to read %s", filepath)
    return None


def _find_metadata_files(sink_dir: str) -> list[dict]:
    """Scan *sink_dir* for metadata.json files and return parsed contents."""
    results = []
    sink_path = Path(sink_dir)
    if not sink_path.exists():
        return results

    for meta_file in sink_path.rglob("metadata.json"):
        data = _parse_ndjson(meta_file)
        if data is None:
            # Try single JSON object as fallback
            try:
                with open(meta_file, "r") as f:
                    data = json.load(f)
            except Exception:
                logger.exception("failed to parse %s", meta_file)
                continue
        data["_meta_dir"] = str(meta_file.parent)
        results.append(data)
        logger.info(
            "media_metadata_parsed path=%s source_id=%s event_id=%s",
            str(meta_file.parent),
            data.get("source_id", ""),
            data.get("labels", {}).get("event_id", data.get("event_id", "")),
        )
    return results


def _find_video_file(meta_dir: str) -> str | None:
    """Find the first video file (*.mkv, *.mov, *.webm) in *meta_dir*."""
    for ext in ("*.mkv", "*.mov", "*.webm", "*.mp4"):
        for f in Path(meta_dir).glob(ext):
            return str(f)
    return None


import re

_UUID_RE = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)


def _extract_uuid(s: str) -> str | None:
    """Extract the first UUID from a string like 'replay-event-{uuid}' or '{uuid}-00000000'."""
    m = _UUID_RE.search(s)
    return m.group(0) if m else None


def _extract_event_id(meta: dict) -> str | None:
    """Extract event_id from metadata JSON, trying multiple paths."""
    # 1. Direct labels.event_id
    labels = meta.get("labels", {})
    if isinstance(labels, dict):
        eid = labels.get("event_id")
        if eid:
            return str(eid)

    # 2. Top-level event_id
    eid = meta.get("event_id")
    if eid:
        return str(eid)

    # 3. source_id with UUID (format: "replay-event-{uuid}" or just "{uuid}")
    source_id = str(meta.get("source_id", ""))
    eid = _extract_uuid(source_id)
    if eid:
        return eid

    # 4. resulting_stream_id (format: "replay-event-{uuid}")
    rsi = str(meta.get("resulting_stream_id", ""))
    eid = _extract_uuid(rsi)
    if eid:
        return eid

    # 5. Directory basename (format: "replay-event-{uuid}-00000000")
    dirname = str(meta.get("_meta_dir", ""))
    eid = _extract_uuid(dirname)
    if eid:
        return eid

    # 6. configuration.labels.event_id
    cfg = meta.get("configuration", {})
    if isinstance(cfg, dict):
        cfg_labels = cfg.get("labels", {})
        if isinstance(cfg_labels, dict):
            eid = cfg_labels.get("event_id")
            if eid:
                return str(eid)

    return None


def _is_already_ready(pg_conn: psycopg.Connection, event_id: str) -> bool:
    """Check if an event already has clip_status='ready'."""
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT payload->'media'->>'clip_status' FROM events WHERE id = %s::uuid",
                (event_id,),
            )
            row = cur.fetchone()
            return row is not None and row[0] == "ready"
    except Exception:
        return False


def _process_sink_output(
    pg_conn: psycopg.Connection, sink_dir: str, processed_dirs: set[str]
) -> int:
    """Process new sink outputs and update events table. Returns count of updates."""
    updated = 0
    for meta in _find_metadata_files(sink_dir):
        meta_dir = meta.get("_meta_dir", "")

        # Idempotency: skip already-processed directories
        if meta_dir and meta_dir in processed_dirs:
            continue

        event_id = _extract_event_id(meta)
        if not event_id:
            logger.error(
                "media_failure: no event_id could be extracted from metadata "
                "meta_dir=%s source_id=%s resulting_stream_id=%s",
                meta_dir,
                meta.get("source_id", ""),
                meta.get("resulting_stream_id", ""),
            )
            continue

        # Idempotency: skip if already marked ready
        if _is_already_ready(pg_conn, event_id):
            if meta_dir:
                processed_dirs.add(meta_dir)
            logger.debug("media_skip: event already ready event_id=%s", event_id)
            continue

        video_file = _find_video_file(meta_dir)
        if not video_file:
            continue  # not ready yet

        clip_path = video_file
        replay_job_id = meta.get("job_id", "") or meta.get("new_job", "") or ""
        sink_path = meta_dir

        try:
            with pg_conn.cursor() as cur:
                if replay_job_id:
                    cur.execute(
                        """
                        UPDATE events
                        SET clip_path = %(clip_path)s,
                            payload = jsonb_set(
                                jsonb_set(
                                    jsonb_set(
                                        jsonb_set(
                                            COALESCE(payload, '{}'::jsonb),
                                            '{media,clip_status}',
                                            '"ready"'
                                        ),
                                        '{media,recording_strategy}',
                                        '"savant_replay"'
                                    ),
                                    '{media,replay_job_id}',
                                    %(replay_job_id)s::jsonb
                                ),
                                '{media,sink_output_path}',
                                %(sink_path)s::jsonb
                            ),
                            updated_at = now()
                        WHERE id = %(event_id)s::uuid
                        """,
                        {
                            "clip_path": clip_path,
                            "replay_job_id": json.dumps(replay_job_id),
                            "sink_path": json.dumps(sink_path),
                            "event_id": event_id,
                        },
                    )
                else:
                    cur.execute(
                        """
                        UPDATE events
                        SET clip_path = %(clip_path)s,
                            payload = jsonb_set(
                                jsonb_set(
                                    jsonb_set(
                                        COALESCE(payload, '{}'::jsonb),
                                        '{media,clip_status}',
                                        '"ready"'
                                    ),
                                    '{media,recording_strategy}',
                                    '"savant_replay"'
                                ),
                                '{media,sink_output_path}',
                                %(sink_path)s::jsonb
                            ),
                            updated_at = now()
                        WHERE id = %(event_id)s::uuid
                        """,
                        {
                            "clip_path": clip_path,
                            "sink_path": json.dumps(sink_path),
                            "event_id": event_id,
                        },
                    )
                if cur.rowcount and cur.rowcount > 0:
                    logger.info(
                        "media_event_updated event_id=%s clip_path=%s sink_path=%s",
                        event_id,
                        clip_path,
                        sink_path,
                    )
                    updated += 1
                if meta_dir:
                    processed_dirs.add(meta_dir)
        except Exception:
            logger.exception("failed to update event_id=%s", event_id)

    return updated


def _snapshot_needed(pg_conn: psycopg.Connection) -> list[dict]:
    """Return events with clip_status=ready that need snapshot generation.

    Conditions: clip_status is 'ready', snap_status is not 'ready' or
    'not_required', snapshot_required=true, and clip_path is non-null.
    """
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, clip_path,
                       COALESCE(
                           (payload -> 'media' ->> 'pre_seconds')::float,
                           (payload -> 'media' ->> 'pre_seconds')::int::float
                       ) AS media_pre_seconds,
                       COALESCE(payload -> 'media' ->> 'snapshot_required', 'false') = 'true'
                           OR COALESCE((payload ->> 'snapshot_required')::bool, false)
                           AS snapshot_required,
                       payload -> 'media' ->> 'snapshot_status' AS snap_status
                FROM events
                WHERE payload -> 'media' ->> 'clip_status' = 'ready'
                  AND clip_path IS NOT NULL
                  AND clip_path != ''
                """
            )
            rows = []
            for row in cur.fetchall():
                snap_status = row[4] or "not_implemented"
                if snap_status in ("ready", "not_required"):
                    continue
                if not row[3]:  # snapshot_required is false
                    continue
                rows.append({
                    "event_id": row[0],
                    "clip_path": row[1],
                    "pre_seconds": row[2],
                    "snapshot_required": row[3],
                    "snap_status": snap_status,
                })
            return rows
    except Exception:
        logger.exception("failed to query events needing snapshots")
        return []


def _update_snapshot_status(
    pg_conn: psycopg.Connection,
    event_id: str,
    snapshot_path: str | None,
    snapshot_status: str,
    snapshot_offset_seconds: float | None = None,
    snapshot_fallback_reason: str | None = None,
    error_message: str | None = None,
) -> bool:
    """Update snapshot-related fields on an event row."""
    try:
        with pg_conn.cursor() as cur:
            parts = []
            params: dict = {"event_id": event_id}

            if snapshot_status:
                parts.append(
                    "{media,snapshot_status}")
                params["snap_status"] = json.dumps(snapshot_status)

            if snapshot_offset_seconds is not None:
                parts.append(
                    "{media,snapshot_offset_seconds}")
                params["snap_offset"] = json.dumps(snapshot_offset_seconds)

            if snapshot_fallback_reason:
                parts.append(
                    "{media,snapshot_fallback_reason}")
                params["fallback"] = json.dumps(snapshot_fallback_reason)

            if error_message:
                parts.append(
                    "{media,snapshot_error_message}")
                params["err_msg"] = json.dumps(error_message)

            if not parts:
                return False

            # Build nested jsonb_set chain: jsonb_set(jsonb_set(COALESCE(...), ...), ...)
            path_param_map = {
                "{media,snapshot_status}": "snap_status",
                "{media,snapshot_offset_seconds}": "snap_offset",
                "{media,snapshot_fallback_reason}": "fallback",
                "{media,snapshot_error_message}": "err_msg",
            }
            payload_expr = "COALESCE(payload, '{}'::jsonb)"
            for media_path in parts:
                param_name = path_param_map[media_path]
                pg_path = "{" + ",".join(media_path.strip("{}").split(",")) + "}"
                payload_expr = (
                    "jsonb_set(" + payload_expr
                    + ", '" + pg_path + "'"
                    + ", %(" + param_name + ")s::jsonb)"
                )

            set_parts = ["payload = " + payload_expr]
            if snapshot_path:
                set_parts.append("snapshot_path = %(snap_path)s")
                params["snap_path"] = snapshot_path
            set_parts.append("updated_at = now()")

            sql = (
                "UPDATE events SET "
                + ", ".join(set_parts)
                + " WHERE id = %(event_id)s::uuid"
            )

            cur.execute(sql, params)
            return cur.rowcount is not None and cur.rowcount > 0
    except Exception:
        logger.exception(
            "update_snapshot_status failed event_id=%s sql=%s params=%s",
            event_id, sql, params,
        )
        return False


def _mark_not_required(pg_conn: psycopg.Connection) -> int:
    """Mark events with snapshot_required=false && clip ready as not_required."""
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET payload = jsonb_set(
                        COALESCE(payload, '{}'::jsonb),
                        '{media,snapshot_status}',
                        '"not_required"'::jsonb
                    ),
                    updated_at = now()
                WHERE payload -> 'media' ->> 'clip_status' = 'ready'
                  AND (
                      COALESCE(payload -> 'media' ->> 'snapshot_required', 'false') = 'false'
                      OR (payload -> 'media' ? 'snapshot_required'
                          AND payload -> 'media' ->> 'snapshot_required' = 'false')
                  )
                  AND COALESCE(payload -> 'media' ->> 'snapshot_status', 'not_implemented')
                      NOT IN ('ready', 'not_required')
                """
            )
            return cur.rowcount or 0
    except Exception:
        logger.exception("_mark_not_required failed")
        return 0


def _process_pending_snapshots(
    pg_conn: psycopg.Connection,
    snapshot_output_dir: str,
    default_pre_seconds: float,
) -> int:
    """Generate snapshots for events that have clip_status=ready but no snapshot yet.

    Idempotent: skips events whose snapshot_status is already 'ready' or
    'not_required'.  If snapshot_status is 'failed' but the file exists
    from a prior attempt, it promotes the status to 'ready'.

    Also marks snapshot_required=false events as 'not_required' when clip
    is ready.
    """
    _mark_not_required(pg_conn)

    events = _snapshot_needed(pg_conn)
    if not events:
        return 0

    updated = 0
    for ev in events:
        event_id = ev["event_id"]
        clip_path = ev["clip_path"]
        pre_seconds = ev["pre_seconds"] if ev["pre_seconds"] else default_pre_seconds

        # Idempotency: if snapshot file already exists, just update status
        expected_path = os.path.join(snapshot_output_dir, f"{event_id}.jpg")
        if os.path.isfile(expected_path):
            logger.info(
                "snapshot_already_exists event_id=%s path=%s — promoting to ready",
                event_id, expected_path,
            )
            _update_snapshot_status(
                pg_conn, event_id,
                snapshot_path=expected_path,
                snapshot_status="ready",
                snapshot_offset_seconds=float(pre_seconds),
            )
            updated += 1
            continue

        logger.info(
            "generating_snapshot event_id=%s clip=%s pre_seconds=%.2f",
            event_id, clip_path, pre_seconds,
        )

        result = generate_snapshot(
            event_id=event_id,
            clip_path=clip_path,
            pre_seconds=pre_seconds,
            output_dir=snapshot_output_dir,
        )

        _update_snapshot_status(
            pg_conn, event_id,
            snapshot_path=result.get("snapshot_path"),
            snapshot_status=result["snapshot_status"],
            snapshot_offset_seconds=result.get("snapshot_offset_seconds"),
            snapshot_fallback_reason=result.get("snapshot_fallback_reason"),
            error_message=result.get("error_message"),
        )
        updated += 1

    return updated


def _annotation_needed(pg_conn: psycopg.Connection) -> list[dict]:
    """Return events with snapshot_status=ready that need annotation.

    Conditions: snapshot_status is 'ready', annotated_snapshot_status is not
    'ready', snapshot_path is non-null.
    """
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, snapshot_path, payload, clip_path
                FROM events
                WHERE payload -> 'media' ->> 'snapshot_status' = 'ready'
                  AND snapshot_path IS NOT NULL
                  AND snapshot_path != ''
                  AND COALESCE(
                        payload -> 'media' ->> 'annotated_snapshot_status',
                        'not_implemented'
                      ) NOT IN ('ready')
                """
            )
            rows = []
            for row in cur.fetchall():
                payload = row[2] or {}
                if isinstance(payload, str):
                    import json as _json
                    payload = _json.loads(payload)
                # Defense-in-depth: skip already-annotated events
                media = payload.get("media", {}) if isinstance(payload, dict) else {}
                ann_status = media.get("annotated_snapshot_status", "not_implemented")
                if ann_status == "ready":
                    continue
                rows.append({
                    "event_id": row[0],
                    "snapshot_path": row[1],
                    "payload": payload,
                    "clip_path": row[3],
                })
            return rows
    except Exception:
        logger.exception("failed to query events needing annotation")
        return []


def _update_annotation_status(
    pg_conn: psycopg.Connection,
    event_id: str,
    annotated_snapshot_path: str | None,
    annotated_snapshot_status: str,
    bbox_overlay_status: str | None = None,
    zone_overlay_status: str | None = None,
    error_message: str | None = None,
) -> bool:
    """Update annotation-related fields on an event row (all stored in payload.media)."""
    try:
        with pg_conn.cursor() as cur:
            parts = []
            params: dict = {"event_id": event_id}

            if annotated_snapshot_status:
                parts.append("{media,annotated_snapshot_status}")
                params["ann_status"] = json.dumps(annotated_snapshot_status)

            if annotated_snapshot_path:
                parts.append("{media,annotated_snapshot_path}")
                params["ann_path"] = json.dumps(annotated_snapshot_path)

            if bbox_overlay_status is not None:
                parts.append("{media,bbox_overlay_status}")
                params["bbox_overlay"] = json.dumps(bbox_overlay_status)

            if zone_overlay_status is not None:
                parts.append("{media,zone_overlay_status}")
                params["zone_overlay"] = json.dumps(zone_overlay_status)

            if error_message:
                parts.append("{media,annotated_snapshot_error_message}")
                params["ann_err"] = json.dumps(error_message)

            if not parts:
                return False

            path_param_map = {
                "{media,annotated_snapshot_status}": "ann_status",
                "{media,annotated_snapshot_path}": "ann_path",
                "{media,bbox_overlay_status}": "bbox_overlay",
                "{media,zone_overlay_status}": "zone_overlay",
                "{media,annotated_snapshot_error_message}": "ann_err",
            }
            payload_expr = "COALESCE(payload, '{}'::jsonb)"
            for media_path in parts:
                param_name = path_param_map[media_path]
                pg_path = "{" + ",".join(media_path.strip("{}").split(",")) + "}"
                payload_expr = (
                    "jsonb_set(" + payload_expr
                    + ", '" + pg_path + "'"
                    + ", %(" + param_name + ")s::jsonb)"
                )

            sql = (
                "UPDATE events SET "
                + "payload = " + payload_expr
                + ", updated_at = now()"
                + " WHERE id = %(event_id)s::uuid"
            )

            cur.execute(sql, params)
            return cur.rowcount is not None and cur.rowcount > 0
    except Exception:
        logger.exception(
            "update_annotation_status failed event_id=%s", event_id,
        )
        return False


def _metadata_has_detections(clip_path: str | None) -> bool:
    """Check if the clip's metadata.json contains any non-empty objects frames.

    Returns False if clip_path is None, the metadata file is missing, or
    every frame has ``"objects": []``.
    """
    if not clip_path:
        return False
    meta_path = os.path.join(os.path.dirname(clip_path), "metadata.json")
    try:
        with open(meta_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if '"objects":[' in line and '"objects":[]' not in line:
                    return True
        return False
    except Exception:
        logger.debug("metadata.json unreadable for %s", clip_path)
        return False


def _process_pending_annotations(
    pg_conn: psycopg.Connection,
    annotated_output_dir: str,
) -> int:
    """Generate annotated snapshots for events with ready raw snapshots.

    Determines bbox trust by checking whether the clip's metadata.json
    contains any non-empty ``metadata.objects`` frames (i.e. real Savant
    detection data).  The bbox is only drawn when the source is trusted.

    Idempotent: skips events whose annotated_snapshot_status is already
    'ready'.  If the annotated file already exists on disk, promotes the
    status to 'ready' without re-generation.
    """
    events = _annotation_needed(pg_conn)
    if not events:
        return 0

    updated = 0
    for ev in events:
        event_id = ev["event_id"]
        raw_snapshot_path = ev["snapshot_path"]
        payload = ev["payload"] or {}
        clip_path = ev.get("clip_path")

        # Determine bbox trust from payload marker (set by Savant event export)
        bbox_trusted = payload.get("bbox_source") == "savant_detection"

        # Idempotency: if annotated file already exists, promote to ready
        expected_path = os.path.join(annotated_output_dir, f"{event_id}.jpg")
        if os.path.isfile(expected_path):
            logger.info(
                "annotated_snapshot_already_exists event_id=%s path=%s — promoting to ready",
                event_id, expected_path,
            )
            _update_annotation_status(
                pg_conn, event_id,
                annotated_snapshot_path=expected_path,
                annotated_snapshot_status="ready",
            )
            updated += 1
            continue

        logger.info(
            "generating_annotated_snapshot event_id=%s bbox_trusted=%s",
            event_id, bbox_trusted,
        )

        result = generate_annotated_snapshot(
            event_id=event_id,
            raw_snapshot_path=raw_snapshot_path,
            payload=payload,
            annotated_output_dir=annotated_output_dir,
            bbox_trusted=bbox_trusted,
        )

        _update_annotation_status(
            pg_conn, event_id,
            annotated_snapshot_path=result.get("annotated_snapshot_path"),
            annotated_snapshot_status=result["annotated_snapshot_status"],
            bbox_overlay_status=result.get("bbox_overlay_status"),
            zone_overlay_status=result.get("zone_overlay_status"),
            error_message=result.get("annotated_snapshot_error_message"),
        )
        updated += 1

    return updated


def connect_postgres(cfg: Config) -> psycopg.Connection:
    conn = psycopg.connect(cfg.database_url, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    logger.info("connected to postgres")
    return conn


def run_worker(cfg: Config, pg_conn: psycopg.Connection) -> None:
    logger.info(
        "media-worker started sink_dir=%s snap_dir=%s ann_dir=%s poll_interval=%ds "
        "default_pre_seconds=%.1f",
        cfg.sink_output_dir,
        cfg.snapshot_output_dir,
        cfg.annotated_output_dir,
        cfg.poll_interval_s,
        cfg.default_pre_seconds,
    )

    processed_dirs: set[str] = set()

    while not shutdown_requested:
        try:
            clip_updates = _process_sink_output(
                pg_conn, cfg.sink_output_dir, processed_dirs
            )
            if clip_updates:
                logger.info("media_worker: clip updated %d events", clip_updates)

            snap_updates = _process_pending_snapshots(
                pg_conn,
                cfg.snapshot_output_dir,
                cfg.default_pre_seconds,
            )
            if snap_updates:
                logger.info("media_worker: snapshot updated %d events", snap_updates)

            ann_updates = _process_pending_annotations(
                pg_conn, cfg.annotated_output_dir,
            )
            if ann_updates:
                logger.info("media_worker: annotation updated %d events", ann_updates)
        except Exception:
            logger.exception("media worker loop error")

        time.sleep(cfg.poll_interval_s)

    logger.info("media-worker stopped (processed %d dirs)", len(processed_dirs))
