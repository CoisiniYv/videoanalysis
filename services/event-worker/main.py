import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

import psycopg
from redis import Redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://video:video@postgres:5432/video_analytics",
)
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "10"))

shutdown_requested = False


def request_shutdown(signum: int, _frame: object) -> None:
    global shutdown_requested
    shutdown_requested = True
    logging.info("shutdown requested by signal=%s", signum)


def connect_redis() -> Redis:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    client.ping()
    return client


def connect_postgres() -> psycopg.Connection:
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    return conn


def main() -> None:
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    logging.info("event-worker starting")
    redis_client = connect_redis()
    postgres_conn = connect_postgres()
    logging.info("event-worker connected to redis and postgres")

    while not shutdown_requested:
        now = datetime.now(timezone.utc).isoformat()
        redis_ok = False
        pg_ok = False
        try:
            redis_client.ping()
            redis_ok = True
        except Exception:
            pass
        try:
            with postgres_conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            pg_ok = True
        except Exception:
            pass
        logging.info(
            "heartbeat ts=%s redis=%s postgres=%s", now, redis_ok, pg_ok
        )
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)

    postgres_conn.close()
    redis_client.close()
    logging.info("event-worker stopped")


if __name__ == "__main__":
    main()
