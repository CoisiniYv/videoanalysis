"""Face Worker — consume face observations from Redis Stream → PostgreSQL.

Usage::

    python -m services.face-worker.main
    # or from the face-worker directory:
    python main.py
"""

from __future__ import annotations

import logging
import signal
import sys

from app.config import load_config
from app.worker import (
    connect_postgres,
    connect_redis,
    request_shutdown,
    run_worker,
    shutdown_requested,
)

logger = logging.getLogger("face-worker")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    cfg = load_config()
    logger.info(
        "face-worker starting stream=%s group=%s consumer=%s",
        cfg.face_observation_stream,
        cfg.consumer_group,
        cfg.consumer_name,
    )

    redis_client = connect_redis(cfg)
    pg_conn = connect_postgres(cfg)

    try:
        run_worker(cfg, redis_client, pg_conn)
    finally:
        pg_conn.close()
        redis_client.close()
        logger.info("face-worker stopped")


if __name__ == "__main__":
    main()
