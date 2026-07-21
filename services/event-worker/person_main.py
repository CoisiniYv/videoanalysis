"""Entrypoint for the dedicated person trajectory persistence worker."""

from __future__ import annotations

import logging
import signal
import sys

from app.config import load_config
from app.person_worker import run_person_observation_worker
from app.worker import connect_postgres, connect_redis, request_shutdown


logger = logging.getLogger("person-observation-worker")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    cfg = load_config()
    redis_client = connect_redis(cfg)
    pg_conn = connect_postgres(cfg)
    try:
        run_person_observation_worker(cfg, redis_client, pg_conn)
    finally:
        pg_conn.close()
        redis_client.close()
        logger.info("person-observation-worker stopped")


if __name__ == "__main__":
    main()
