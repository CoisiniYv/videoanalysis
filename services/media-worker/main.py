"""Media Worker — monitor sink output, update events table."""

from __future__ import annotations

import logging
import signal
import sys

from app.config import load_config
from app.worker import connect_postgres, request_shutdown, run_worker


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    cfg = load_config()
    pg_conn = connect_postgres(cfg)

    try:
        run_worker(cfg, pg_conn)
    finally:
        pg_conn.close()
        logging.getLogger("media-worker").info("media-worker stopped")


if __name__ == "__main__":
    main()
