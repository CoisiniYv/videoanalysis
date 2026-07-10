"""Clip Worker — consume record_requests, call Replay API."""

from __future__ import annotations

import logging
import multiprocessing as mp
import signal
import sys
from dataclasses import replace

import psycopg

from app.config import Config, load_config
from app import worker as worker_module
from app.worker import connect_redis, run_worker


logger = logging.getLogger("clip-worker")


def _consumer_name(base_name: str, index: int, count: int) -> str:
    if count <= 1:
        return base_name
    return f"{base_name}-{index + 1:02d}"


def _run_consumer(worker_cfg: Config) -> None:
    redis_client = None
    pg_conn = None
    try:
        redis_client = connect_redis(worker_cfg)
        pg_conn = psycopg.connect(worker_cfg.database_url, autocommit=True)
        run_worker(worker_cfg, redis_client, pg_conn)
    except Exception:
        logger.exception(
            "clip-worker consumer crashed consumer_name=%s",
            worker_cfg.consumer_name,
        )
        worker_module.request_shutdown(signal.SIGTERM, None)
        raise
    finally:
        if pg_conn is not None:
            pg_conn.close()
        if redis_client is not None:
            redis_client.close()
        logger.info(
            "clip-worker consumer stopped consumer_name=%s",
            worker_cfg.consumer_name,
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    signal.signal(signal.SIGTERM, worker_module.request_shutdown)
    signal.signal(signal.SIGINT, worker_module.request_shutdown)

    cfg = load_config()
    consumer_count = max(1, cfg.consumer_count)
    logger.info(
        "clip-worker launching consumers count=%s base_consumer_name=%s",
        consumer_count,
        cfg.consumer_name,
    )
    if consumer_count == 1:
        _run_consumer(cfg)
        logger.info("clip-worker stopped")
        return

    processes: list[mp.Process] = []
    for index in range(consumer_count):
        worker_cfg = replace(
            cfg,
            consumer_name=_consumer_name(cfg.consumer_name, index, consumer_count),
        )
        process = mp.Process(
            target=_run_consumer,
            args=(worker_cfg,),
            name=f"clip-worker-{index + 1:02d}",
            daemon=False,
        )
        process.start()
        processes.append(process)

    try:
        while any(process.is_alive() for process in processes):
            if worker_module.shutdown_requested:
                break
            for process in processes:
                process.join(timeout=0.5)
                if process.exitcode not in (None, 0):
                    logger.error(
                        "clip-worker consumer exited abnormally name=%s exitcode=%s",
                        process.name,
                        process.exitcode,
                    )
                    worker_module.request_shutdown(signal.SIGTERM, None)
                    break
    finally:
        worker_module.request_shutdown(signal.SIGTERM, None)
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=10.0)
            if process.is_alive():
                logger.warning(
                    "clip-worker consumer did not stop after terminate; killing name=%s",
                    process.name,
                )
                process.kill()
                process.join(timeout=5.0)
        logger.info("clip-worker stopped")


if __name__ == "__main__":
    main()
