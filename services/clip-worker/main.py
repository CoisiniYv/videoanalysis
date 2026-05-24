"""Clip Worker — consume record_requests, call Replay API."""

from __future__ import annotations

import logging
import signal
import sys

from app.config import load_config
from app.worker import connect_redis, request_shutdown, run_worker


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

    try:
        run_worker(cfg, redis_client)
    finally:
        redis_client.close()
        logging.getLogger("clip-worker").info("clip-worker stopped")


if __name__ == "__main__":
    main()
