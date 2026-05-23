"""Event worker — consume SecurityEvent from Redis Stream, insert into PostgreSQL."""

from __future__ import annotations

import json
import logging
import signal
import sys
import time
from typing import Dict

import psycopg
from redis import Redis

from app.config import Config, load_config
from app.redis_consumer import RedisStreamConsumer
from app.repository import EventRepository

logger = logging.getLogger(__name__)

shutdown_requested = False


def request_shutdown(signum: int, _frame: object) -> None:
    global shutdown_requested
    logger.info("shutdown requested by signal=%s", signum)
    shutdown_requested = True


def _parse_event(fields: Dict[bytes, bytes]) -> dict | None:
    """Parse a SecurityEvent dict from Redis stream fields.

    The ``data`` field contains the full JSON.  Top-level stream fields
    (source_event_id, event_type, camera_id, track_id) are used as
    supplementary validation.
    """
    data_raw = fields.get(b"data")
    if not data_raw:
        logger.warning("stream entry missing data field, skipping")
        return None

    try:
        event = json.loads(data_raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("failed to parse event JSON: %s", exc)
        return None

    required = ["source_event_id", "event_type", "camera_id"]
    for field in required:
        if not event.get(field):
            logger.warning("event missing required field=%s, skipping", field)
            return None

    return event


def _handle_event(
    event: dict,
    msg_id: str,
    repo: EventRepository,
    consumer: RedisStreamConsumer,
) -> bool:
    """Process a single event: insert into DB, then ACK.

    Returns True if the event was newly inserted, False if duplicate.
    In both cases the message is ACKed (we have handled it).
    """
    try:
        inserted = repo.insert_event(event)
    except Exception:
        logger.exception(
            "db insert failed for source_event_id=%s msg_id=%s",
            event.get("source_event_id"),
            msg_id,
        )
        return False

    if not consumer.ack(msg_id):
        logger.error("ack failed for msg_id=%s", msg_id)
    else:
        logger.debug(
            "acked msg_id=%s source_event_id=%s inserted=%s",
            msg_id,
            event.get("source_event_id"),
            inserted,
        )

    return inserted


def _process_batch(
    messages: list[tuple[str, dict[bytes, bytes]]],
    repo: EventRepository,
    consumer: RedisStreamConsumer,
) -> tuple[int, int]:
    inserted = 0
    duplicates = 0
    for msg_id, fields in messages:
        event = _parse_event(fields)
        if event is None:
            consumer.ack(msg_id)
            continue

        if _handle_event(event, msg_id, repo, consumer):
            inserted += 1
        else:
            duplicates += 1
    return inserted, duplicates


def connect_redis(cfg: Config) -> Redis:
    client = Redis.from_url(cfg.redis_url, decode_responses=False)
    client.ping()
    logger.info("connected to redis url=%s", cfg.redis_url)
    return client


def connect_postgres(cfg: Config) -> psycopg.Connection:
    conn = psycopg.connect(cfg.database_url, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    logger.info("connected to postgres url=%s", cfg.database_url)
    return conn


def run_worker(
    cfg: Config,
    redis_client: Redis,
    pg_conn: psycopg.Connection,
) -> None:
    consumer = RedisStreamConsumer(
        redis_client, cfg.event_stream, cfg.consumer_group, cfg.consumer_name
    )
    consumer.ensure_group()
    repo = EventRepository(pg_conn)

    logger.info(
        "worker started stream=%s group=%s consumer=%s",
        cfg.event_stream,
        cfg.consumer_group,
        cfg.consumer_name,
    )

    total_inserted = 0
    total_duplicates = 0
    last_report = time.monotonic()

    while not shutdown_requested:
        try:
            # 1. Process pending messages (recovery)
            pending = consumer.read_pending(count=cfg.batch_size)
            if pending:
                ins, dup = _process_batch(pending, repo, consumer)
                total_inserted += ins
                total_duplicates += dup
                if ins or dup:
                    logger.info(
                        "pending batch: inserted=%d duplicates=%d", ins, dup
                    )

            # 2. Read new messages
            new_msgs = consumer.read_new(
                count=cfg.batch_size, block_ms=cfg.poll_timeout_ms
            )
            if new_msgs:
                ins, dup = _process_batch(new_msgs, repo, consumer)
                total_inserted += ins
                total_duplicates += dup

            # 3. Periodic summary
            now = time.monotonic()
            if now - last_report >= 60:
                logger.info(
                    "worker summary: total_inserted=%d total_duplicates=%d",
                    total_inserted,
                    total_duplicates,
                )
                last_report = now

        except Exception:
            logger.exception("worker loop error, sleeping 1s")
            time.sleep(1)

    logger.info(
        "worker stopped: total_inserted=%d total_duplicates=%d",
        total_inserted,
        total_duplicates,
    )
