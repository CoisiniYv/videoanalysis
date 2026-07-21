"""Dedicated high-rate person trajectory stream consumer.

Event handling includes alert/evidence policy work and can legitimately take
longer than one trajectory batch.  Keeping both streams in one loop allowed a
busy event stream to starve person observations until Redis MAXLEN trimming
discarded unread trajectory rows.  This worker reuses the same transactional
batch repository, but owns only the person observation consumer group.
"""

from __future__ import annotations

import logging
import time

import psycopg
from redis import Redis

from app import worker as worker_state
from app.config import Config
from app.redis_consumer import RedisStreamConsumer
from app.repository import EventRepository
from app.worker import _process_person_observation_batch


logger = logging.getLogger(__name__)


def run_person_observation_worker(
    cfg: Config,
    redis_client: Redis,
    pg_conn: psycopg.Connection,
) -> None:
    """Continuously persist trajectory batches without event-policy stalls."""

    if not cfg.person_observation_enabled:
        raise RuntimeError("person observation consumer is disabled")

    consumer = RedisStreamConsumer(
        redis_client,
        cfg.person_observation_stream,
        cfg.person_observation_consumer_group,
        cfg.person_observation_consumer_name,
        start_id=cfg.person_observation_consumer_start_id,
    )
    consumer.ensure_group()
    repo = EventRepository(pg_conn)
    batch_size = max(int(cfg.person_observation_batch_size), 1)
    totals = {
        "inserted": 0,
        "duplicates": 0,
        "skipped": 0,
        "failed": 0,
        "batches": 0,
    }
    last_report = time.monotonic()

    logger.info(
        "person observation worker started stream=%s group=%s consumer=%s "
        "start_id=%s batch_size=%d scheduling=dedicated",
        cfg.person_observation_stream,
        cfg.person_observation_consumer_group,
        cfg.person_observation_consumer_name,
        cfg.person_observation_consumer_start_id,
        batch_size,
    )

    def process(messages: list[tuple[str, dict[bytes, bytes]]], kind: str) -> None:
        if not messages:
            return
        inserted, duplicates, skipped, failed = _process_person_observation_batch(
            messages,
            repo,
            consumer,
        )
        totals["inserted"] += inserted
        totals["duplicates"] += duplicates
        totals["skipped"] += skipped
        totals["failed"] += failed
        totals["batches"] += 1
        if skipped or failed:
            logger.warning(
                "person observation batch completed kind=%s submitted=%d "
                "inserted=%d duplicates=%d skipped=%d failed=%d",
                kind,
                len(messages),
                inserted,
                duplicates,
                skipped,
                failed,
            )

    while not worker_state.shutdown_requested:
        try:
            pending = consumer.read_pending(count=batch_size)
            process(pending, "pending")

            # When recovering pending work, avoid an idle block before looking
            # for new rows. Otherwise block normally so an idle worker does not
            # poll Redis continuously.
            messages = consumer.read_new(
                count=batch_size,
                block_ms=1 if pending else cfg.poll_timeout_ms,
            )
            process(messages, "new")

            now = time.monotonic()
            if now - last_report >= 60:
                logger.info(
                    "person observation worker summary batches=%d inserted=%d "
                    "duplicates=%d skipped=%d failed=%d",
                    totals["batches"],
                    totals["inserted"],
                    totals["duplicates"],
                    totals["skipped"],
                    totals["failed"],
                )
                last_report = now
        except Exception:
            logger.exception("person observation worker loop error, sleeping 1s")
            time.sleep(1)

    logger.info(
        "person observation worker stopped batches=%d inserted=%d duplicates=%d "
        "skipped=%d failed=%d",
        totals["batches"],
        totals["inserted"],
        totals["duplicates"],
        totals["skipped"],
        totals["failed"],
    )
