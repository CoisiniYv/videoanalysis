from __future__ import annotations

import logging

from prometheus_client import start_http_server

from app.config import load_config
from app.tensorrt_runner import TensorRTAdaFaceRunner
from app.worker import install_signal_handlers, run


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config()
    install_signal_handlers()
    start_http_server(cfg.metrics_port)
    runner = TensorRTAdaFaceRunner(cfg.engine_path, cfg.batch_size)
    run(cfg, runner)


if __name__ == "__main__":
    main()
