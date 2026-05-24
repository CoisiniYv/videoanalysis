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


def _find_metadata_files(sink_dir: str) -> list[dict]:
    """Scan *sink_dir* for metadata.json files and return parsed contents."""
    results = []
    sink_path = Path(sink_dir)
    if not sink_path.exists():
        return results

    for meta_file in sink_path.rglob("metadata.json"):
        try:
            with open(meta_file, "r") as f:
                data = json.load(f)
            data["_meta_dir"] = str(meta_file.parent)
            results.append(data)
        except Exception:
            logger.exception("failed to parse %s", meta_file)
    return results


def _find_video_file(meta_dir: str) -> str | None:
    """Find the first video file (*.mkv, *.mov, *.webm) in *meta_dir*."""
    for ext in ("*.mkv", "*.mov", "*.webm", "*.mp4"):
        for f in Path(meta_dir).glob(ext):
            return str(f)
    return None


def _process_sink_output(pg_conn: psycopg.Connection, sink_dir: str) -> int:
    """Process new sink outputs and update events table. Returns count of updates."""
    updated = 0
    for meta in _find_metadata_files(sink_dir):
        event_id = (
            meta.get("labels", {}).get("event_id")
            or meta.get("event_id")
        )
        if not event_id:
            continue

        video_file = _find_video_file(meta["_meta_dir"])
        if not video_file:
            continue  # not ready yet

        clip_path = video_file
        replay_job_id = meta.get("job_id", "")
        sink_path = meta["_meta_dir"]

        try:
            with pg_conn.cursor() as cur:
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
                            '{media,replay_job_id}',
                            %(replay_job_id)s::jsonb
                        ),
                        updated_at = now()
                    WHERE id = %(event_id)s::uuid
                    """,
                    {
                        "clip_path": clip_path,
                        "replay_job_id": json.dumps(replay_job_id),
                        "event_id": event_id,
                    },
                )
                if cur.rowcount and cur.rowcount > 0:
                    logger.info(
                        "media_updated event_id=%s clip_path=%s",
                        event_id,
                        clip_path,
                    )
                    updated += 1
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
            updates = _process_sink_output(pg_conn, cfg.sink_output_dir)
            if updates:
                logger.info("media_worker: updated %d events", updates)
        except Exception:
            logger.exception("media worker loop error")

        time.sleep(cfg.poll_interval_s)

    logger.info("media-worker stopped")
