"""Media worker — monitor sink output, update events table with clip paths."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import psycopg

from app.config import Config, load_config

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


def connect_postgres(cfg: Config) -> psycopg.Connection:
    conn = psycopg.connect(cfg.database_url, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    logger.info("connected to postgres")
    return conn


def run_worker(cfg: Config, pg_conn: psycopg.Connection) -> None:
    logger.info(
        "media-worker started sink_dir=%s poll_interval=%ds",
        cfg.sink_output_dir,
        cfg.poll_interval_s,
    )

    processed_dirs: set[str] = set()

    while not shutdown_requested:
        try:
            updates = _process_sink_output(
                pg_conn, cfg.sink_output_dir, processed_dirs
            )
            if updates:
                logger.info("media_worker: updated %d events", updates)
        except Exception:
            logger.exception("media worker loop error")

        time.sleep(cfg.poll_interval_s)

    logger.info("media-worker stopped (processed %d dirs)", len(processed_dirs))
