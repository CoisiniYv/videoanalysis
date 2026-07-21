#!/usr/bin/env python3
"""Entrypoint for the long-lived rolling-cache sink."""

from __future__ import annotations

import logging
import os
import signal

from config import EpochResolver, SinkConfig
from gst_sink import RollingCacheSink
from observability import HealthState, SinkMetrics, start_http_server


LOGGER = logging.getLogger("rolling_cache_sink")


def main() -> None:
    from savant.utils.log import init_logging
    from savant_rs.py.utils.zeromq import ZeroMQSource

    init_logging()
    log_level = getattr(
        logging, os.getenv("LOGLEVEL", "INFO").strip().upper(), logging.INFO
    )
    sink_loggers = (
        LOGGER,
        logging.getLogger("rolling_cache_sink.gst"),
        logging.getLogger("rolling_cache_sink.publisher"),
    )
    for sink_logger in sink_loggers:
        sink_logger.disabled = False
        sink_logger.setLevel(log_level)
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            )
        )
        LOGGER.addHandler(handler)
        LOGGER.propagate = False
    signal.signal(signal.SIGTERM, signal.getsignal(signal.SIGINT))
    config = SinkConfig.from_env()
    metrics = SinkMetrics()
    health = HealthState(metrics)
    http_server = start_http_server(
        config.http_host,
        config.http_port,
        metrics=metrics,
        health=health,
    )
    source = None
    sink = None

    try:
        epoch_resolver = EpochResolver(
            explicit_epoch_id=config.explicit_epoch_id,
            state_path=config.epoch_state_path,
        )
        source = ZeroMQSource(
            config.zmq_endpoint,
            source_id=config.source_id,
            source_id_prefix=config.source_id_prefix,
        )
        sink = RollingCacheSink(config, epoch_resolver, metrics)
        LOGGER.info(
            "rolling-cache sink starting endpoint=%s root=%s namespace=%s epoch=%s "
            "segment_s=%.3f http=%s:%d",
            config.zmq_endpoint,
            config.cache_root,
            config.namespace,
            epoch_resolver.current(),
            config.segment_seconds,
            config.http_host,
            config.http_port,
        )
        source.start()
        health.mark_started()
        for zmq_message in source:
            sink.write(zmq_message)
    except KeyboardInterrupt:
        LOGGER.info("rolling-cache sink interrupted")
    except Exception as exc:
        health.mark_fatal(exc)
        LOGGER.exception("rolling-cache sink failed")
        raise
    finally:
        health.mark_stopping()
        if source is not None:
            source.terminate()
        if sink is not None:
            sink.terminate()
        http_server.shutdown()
        http_server.server_close()


if __name__ == "__main__":
    main()
