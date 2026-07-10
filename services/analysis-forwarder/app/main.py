"""Replay-to-Savant analysis forwarder."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from savant_rs.py.utils.zeromq import ZeroMQSource
from savant_rs.zmq import BlockingWriter, WriterConfigBuilder

from .queueing import BoundedDropQueue, ForwarderMessage
from .sampler import AnalysisFrameSampler, MetadataObjectFilter


LOGGER = logging.getLogger("analysis_forwarder")
SUCCESS_RESULTS = {"WriterResultSuccess", "WriterResultAck"}


@dataclass(frozen=True)
class ForwarderConfig:
    in_endpoint: str
    out_endpoint: str
    raw_out_endpoint: str
    analysis_fps: str
    min_fps: str
    sampler_enabled: bool
    queue_max_size: int
    receive_timeout_ms: int
    receive_hwm: int
    send_timeout_ms: int
    send_retries: int
    send_hwm: int
    metrics_port: int
    require_object_namespace: str = ""
    require_object_label: str = ""
    require_attribute_namespace: str = ""
    require_attribute_name: str = ""
    min_object_confidence: float = 0.0
    min_object_width: float = 0.0
    min_object_height: float = 0.0

    @classmethod
    def from_env(cls) -> "ForwarderConfig":
        return cls(
            in_endpoint=os.getenv("FORWARDER_IN_ENDPOINT", "router+bind:tcp://0.0.0.0:5557"),
            out_endpoint=os.getenv("FORWARDER_OUT_ENDPOINT", "dealer+connect:tcp://savant-security:5557"),
            raw_out_endpoint=os.getenv("FORWARDER_RAW_OUT_ENDPOINT", ""),
            analysis_fps=os.getenv("ANALYSIS_FPS", os.getenv("MAX_FPS", "8/1")),
            min_fps=os.getenv("ANALYSIS_MIN_FPS", os.getenv("MIN_FPS", "2/1")),
            sampler_enabled=_bool_env("FORWARDER_SAMPLER_ENABLED", True),
            queue_max_size=_int_env("FORWARDER_QUEUE_MAX_SIZE", 2048),
            receive_timeout_ms=_int_env("FORWARDER_RECEIVE_TIMEOUT_MS", 1000),
            receive_hwm=_int_env("FORWARDER_RECEIVE_HWM", 1000),
            send_timeout_ms=_int_env("FORWARDER_SEND_TIMEOUT_MS", 2000),
            send_retries=_int_env("FORWARDER_SEND_RETRIES", 3),
            send_hwm=_int_env("FORWARDER_SEND_HWM", 1000),
            metrics_port=_int_env("FORWARDER_METRICS_PORT", 8081),
            require_object_namespace=os.getenv(
                "FORWARDER_REQUIRE_OBJECT_NAMESPACE", ""
            ),
            require_object_label=os.getenv("FORWARDER_REQUIRE_OBJECT_LABEL", ""),
            require_attribute_namespace=os.getenv(
                "FORWARDER_REQUIRE_ATTRIBUTE_NAMESPACE", ""
            ),
            require_attribute_name=os.getenv(
                "FORWARDER_REQUIRE_ATTRIBUTE_NAME", ""
            ),
            min_object_confidence=_float_env(
                "FORWARDER_MIN_OBJECT_CONFIDENCE", 0.0
            ),
            min_object_width=_float_env(
                "FORWARDER_MIN_OBJECT_WIDTH", 0.0
            ),
            min_object_height=_float_env(
                "FORWARDER_MIN_OBJECT_HEIGHT", 0.0
            ),
        )


class ForwarderMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_source: dict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.queue_depth = 0
        self.running = 1
        self.null_sink_enabled = 0

    def inc(self, source_id: str, name: str, amount: int = 1) -> None:
        with self._lock:
            self._by_source[source_id or "_unknown_source"][name] += amount

    def set_queue_depth(self, value: int) -> None:
        with self._lock:
            self.queue_depth = max(int(value), 0)

    def render_prometheus(self) -> str:
        with self._lock:
            lines = [
                "# HELP va_forwarder_queue_depth Current analysis-forwarder queue depth.",
                "# TYPE va_forwarder_queue_depth gauge",
                f"va_forwarder_queue_depth {self.queue_depth}",
                "# HELP va_forwarder_running Whether the analysis-forwarder main process is running.",
                "# TYPE va_forwarder_running gauge",
                f"va_forwarder_running {self.running}",
                "# HELP va_forwarder_null_sink_enabled Whether forwarded frames are counted without sending to Savant.",
                "# TYPE va_forwarder_null_sink_enabled gauge",
                f"va_forwarder_null_sink_enabled {self.null_sink_enabled}",
            ]
            metric_names = {
                "seen": "va_forwarder_frames_seen_total",
                "forwarded": "va_forwarder_frames_forwarded_total",
                "dropped": "va_forwarder_frames_dropped_total",
                "send_failures": "va_forwarder_savant_send_failures_total",
                "raw_forwarded": "va_forwarder_raw_frames_forwarded_total",
                "raw_send_failures": "va_forwarder_raw_send_failures_total",
                "metadata_filtered": "va_forwarder_metadata_filtered_total",
            }
            for key, prom_name in metric_names.items():
                lines.append(f"# TYPE {prom_name} counter")
                for source_id, counters in sorted(self._by_source.items()):
                    value = counters.get(key, 0)
                    lines.append(f'{prom_name}{{source_id="{_escape_label(source_id)}"}} {value}')
            return "\n".join(lines) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    metrics: ForwarderMetrics | None = None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._write(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if self.path == "/metrics" and self.metrics is not None:
            self._write(
                200,
                self.metrics.render_prometheus().encode("utf-8"),
                "text/plain; version=0.0.4; charset=utf-8",
            )
            return
        self._write(404, b"not found\n", "text/plain; charset=utf-8")

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.debug("metrics_http " + fmt, *args)

    def _write(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AnalysisForwarder:
    def __init__(self, config: ForwarderConfig) -> None:
        self.config = config
        self.metrics = ForwarderMetrics()
        self.queue = BoundedDropQueue(config.queue_max_size)
        self.sampler = AnalysisFrameSampler(
            enabled=config.sampler_enabled,
            max_fps=config.analysis_fps,
            min_fps=config.min_fps,
        )
        self.metadata_filter = MetadataObjectFilter(
            object_namespace=config.require_object_namespace,
            object_label=config.require_object_label,
            attribute_namespace=config.require_attribute_namespace,
            attribute_name=config.require_attribute_name,
            min_confidence=config.min_object_confidence,
            min_width=config.min_object_width,
            min_height=config.min_object_height,
        )
        self.stop_event = threading.Event()
        self.reader = ZeroMQSource(
            config.in_endpoint,
            receive_timeout=config.receive_timeout_ms,
            receive_hwm=config.receive_hwm,
        )
        self.raw_writer = self._build_writer(config.raw_out_endpoint)
        self.raw_sink_enabled = not isinstance(self.raw_writer, NullWriter)
        self.writer = self._build_writer(config.out_endpoint)
        if isinstance(self.writer, NullWriter):
            self.metrics.null_sink_enabled = 1

    def _build_writer(self, endpoint: str) -> BlockingWriter | "NullWriter":
        if _is_null_endpoint(endpoint):
            return NullWriter()
        writer_config = WriterConfigBuilder(endpoint)
        writer_config.with_send_timeout(self.config.send_timeout_ms)
        writer_config.with_send_retries(self.config.send_retries)
        writer_config.with_send_hwm(self.config.send_hwm)
        return BlockingWriter(writer_config.build())

    def run(self) -> None:
        sink_mode = "null" if self.metrics.null_sink_enabled else "savant"
        LOGGER.info(
            "starting forwarder in=%s out=%s raw_out=%s sink_mode=%s raw_sink=%s",
            self.config.in_endpoint,
            self.config.out_endpoint,
            self.config.raw_out_endpoint or "<disabled>",
            sink_mode,
            "enabled" if self.raw_sink_enabled else "disabled",
        )
        self.reader.start()
        self.raw_writer.start()
        self.writer.start()
        writer_thread = threading.Thread(target=self._write_loop, name="forwarder-writer", daemon=True)
        writer_thread.start()
        self._read_loop()
        writer_thread.join(timeout=5)
        self._shutdown()

    def _read_loop(self) -> None:
        while not self.stop_event.is_set():
            zmq_message = self.reader.next_message()
            if zmq_message is None:
                continue
            item = self._build_queue_item(zmq_message)
            if item is None:
                continue
            result = self.queue.push(item)
            if result.dropped is not None:
                self.metrics.inc(result.dropped.source_id, "dropped")
                LOGGER.debug("dropped source_id=%s reason=%s", result.dropped.source_id, result.reason)
            if not result.accepted:
                self.metrics.set_queue_depth(len(self.queue))
                continue
            self.metrics.set_queue_depth(len(self.queue))

    def _build_queue_item(self, zmq_message: Any) -> ForwarderMessage | None:
        message = zmq_message.message
        if message.is_video_frame():
            video_frame = message.as_video_frame()
            source_id = str(video_frame.source_id or "")
            self.metrics.inc(source_id, "seen")
            self._fanout_raw(source_id, message, zmq_message.content or b"")
            if not self.metadata_filter.admit(video_frame):
                self.metrics.inc(source_id, "metadata_filtered")
                self.metrics.inc(source_id, "dropped")
                return None
            if not self.sampler.admit(video_frame):
                self.metrics.inc(source_id, "dropped")
                return None
            return ForwarderMessage(
                topic=source_id,
                message=message,
                content=zmq_message.content or b"",
                source_id=source_id,
                keyframe=bool(video_frame.keyframe),
                video_frame=True,
            )
        if message.is_end_of_stream():
            eos = message.as_end_of_stream()
            source_id = str(eos.source_id or "")
            self._fanout_raw(source_id, message, zmq_message.content or b"")
            return ForwarderMessage(
                topic=source_id,
                message=message,
                content=zmq_message.content or b"",
                source_id=source_id,
                keyframe=True,
                video_frame=False,
            )
        if message.is_shutdown():
            return ForwarderMessage(
                topic="shutdown",
                message=message,
                content=zmq_message.content or b"",
                source_id="_control",
                keyframe=True,
                video_frame=False,
            )
        LOGGER.warning("dropping unsupported message type: %r", message)
        self.metrics.inc("_unsupported", "dropped")
        return None

    def _fanout_raw(self, source_id: str, message: Any, content: bytes) -> None:
        if not self.raw_sink_enabled:
            return
        try:
            result = self.raw_writer.send_message(source_id, message, content)
        except Exception as exc:
            LOGGER.warning("failed to send raw branch source_id=%s: %s", source_id, exc)
            self.metrics.inc(source_id or "_unknown_source", "raw_send_failures")
            return
        if type(result).__name__ not in SUCCESS_RESULTS:
            LOGGER.warning(
                "raw branch send was not successful source_id=%s result=%r",
                source_id,
                result,
            )
            self.metrics.inc(source_id or "_unknown_source", "raw_send_failures")
            return
        self.metrics.inc(source_id or "_unknown_source", "raw_forwarded")

    def _write_loop(self) -> None:
        while not self.stop_event.is_set():
            item = self.queue.pop(timeout_s=0.2)
            if item is None:
                self.metrics.set_queue_depth(len(self.queue))
                continue
            self.metrics.set_queue_depth(len(self.queue))
            try:
                result = self.writer.send_message(item.topic, item.message, item.content)
            except Exception as exc:
                LOGGER.warning("failed to send to Savant source_id=%s: %s", item.source_id, exc)
                self.metrics.inc(item.source_id, "send_failures")
                self.metrics.inc(item.source_id, "dropped")
                continue
            if type(result).__name__ not in SUCCESS_RESULTS:
                LOGGER.warning("Savant send was not successful source_id=%s result=%r", item.source_id, result)
                self.metrics.inc(item.source_id, "send_failures")
                self.metrics.inc(item.source_id, "dropped")
                continue
            if item.video_frame:
                self.metrics.inc(item.source_id, "forwarded")

    def _shutdown(self) -> None:
        self.metrics.running = 0
        for endpoint in (self.writer, self.raw_writer, self.reader):
            try:
                endpoint.shutdown()
            except AttributeError:
                endpoint.terminate()
            except Exception:
                LOGGER.exception("failed to shut down endpoint")


def run_metrics_server(metrics: ForwarderMetrics, port: int) -> ThreadingHTTPServer:
    MetricsHandler.metrics = metrics
    server = ThreadingHTTPServer(("0.0.0.0", int(port)), MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, name="metrics-http", daemon=True)
    thread.start()
    return server


class WriterResultSuccess:
    pass


class NullWriter:
    """Fast diagnostic sink that exercises forwarder read/sample/queue without Savant."""

    def start(self) -> None:
        return None

    def send_message(self, _topic: str, _message: Any, _content: bytes) -> WriterResultSuccess:
        return WriterResultSuccess()

    def shutdown(self) -> None:
        return None


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOGLEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = ForwarderConfig.from_env()
    forwarder = AnalysisForwarder(config)
    server = run_metrics_server(forwarder.metrics, config.metrics_port)

    def stop(_signum: int, _frame: Any) -> None:
        forwarder.stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        forwarder.run()
    finally:
        server.shutdown()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _is_null_endpoint(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"null", "none", "null://"} or normalized.startswith("null://")


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


if __name__ == "__main__":
    main()
