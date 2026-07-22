"""Small dependency-free Prometheus and health HTTP surface."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class SinkMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, float] = {
            "pipelines": 0,
            "pipelines_created_total": 0,
            "pipelines_closed_total": 0,
            "pipeline_errors_total": 0,
            "pipeline_errors_active": 0,
            "frames_received_total": 0,
            "frames_written_total": 0,
            "input_pts_synthesized_total": 0,
            "input_pts_regressions_clamped_total": 0,
            "input_pts_clamp_ns_total": 0,
            "mux_cadence_frames_total": 0,
            "mux_pts_synthesized_total": 0,
            "mux_pts_regressions_corrected_total": 0,
            "mux_pts_correction_ns_total": 0,
            "frames_dropped_before_keyframe_total": 0,
            "frames_dropped_no_content_total": 0,
            "unsupported_codec_frames_total": 0,
            "segments_published_total": 0,
            "segment_publish_errors_total": 0,
            "staging_fragments_abandoned_total": 0,
            "staging_bytes_abandoned_total": 0,
            "segment_bytes_total": 0,
            "pending_fragments": 0,
            "publication_accepting": 0,
            "publication_capacity": 0,
            "publication_worker_count": 0,
            "publication_queue_depth": 0,
            "publication_queue_depth_peak": 0,
            "publication_outstanding": 0,
            "publication_outstanding_peak": 0,
            "publication_active": 0,
            "publication_submitted_total": 0,
            "publication_completed_total": 0,
            "publication_failed_total": 0,
            "publication_queue_wait_ms_total": 0,
            "publication_queue_wait_ms_max": 0,
            "publication_queue_wait_events_total": 0,
            "publication_shutdown_timeout_total": 0,
            "source_sessions_total": 0,
            "source_eos_total": 0,
            "epoch_rotations_total": 0,
            "caps_rotations_total": 0,
            "pts_rotations_total": 0,
        }

    def inc(self, name: str, amount: float = 1.0) -> None:
        with self._lock:
            self._values[name] = self._values.get(name, 0.0) + amount

    def set(self, name: str, value: float) -> None:
        with self._lock:
            self._values[name] = float(value)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            values = dict(self._values)
        values["python_threads"] = float(threading.active_count())
        values["process_threads"] = float(_process_thread_count())
        return values

    def prometheus_text(self) -> str:
        lines = []
        for name, value in sorted(self.snapshot().items()):
            metric = f"rolling_cache_sink_{name}"
            kind = "counter" if name.endswith("_total") else "gauge"
            lines.append(f"# TYPE {metric} {kind}")
            lines.append(f"{metric} {value:g}")
        return "\n".join(lines) + "\n"


class HealthState:
    def __init__(self, metrics: SinkMetrics) -> None:
        self._metrics = metrics
        self._lock = threading.Lock()
        self._started = False
        self._stopping = False
        self._fatal_error = ""
        self._started_at = time.monotonic()

    def mark_started(self) -> None:
        with self._lock:
            self._started = True

    def mark_stopping(self) -> None:
        with self._lock:
            self._stopping = True

    def mark_fatal(self, error: BaseException | str) -> None:
        with self._lock:
            self._fatal_error = str(error)

    def document(self) -> dict[str, object]:
        with self._lock:
            started = self._started
            stopping = self._stopping
            fatal_error = self._fatal_error
        metrics = self._metrics.snapshot()
        ready = (
            started
            and not stopping
            and not fatal_error
            and metrics["pipeline_errors_active"] <= 0
        )
        return {
            "status": "ok" if ready else "not_ready",
            "ready": ready,
            "started": started,
            "stopping": stopping,
            "fatal_error": fatal_error or None,
            "uptime_seconds": round(time.monotonic() - self._started_at, 3),
            "pipelines": int(metrics["pipelines"]),
            "pending_fragments": int(metrics["pending_fragments"]),
            "python_threads": int(metrics["python_threads"]),
            "process_threads": int(metrics["process_threads"]),
        }


def start_http_server(
    host: str,
    port: int,
    *,
    metrics: SinkMetrics,
    health: HealthState,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path == "/metrics":
                payload = metrics.prometheus_text().encode("utf-8")
                self._send(200, "text/plain; version=0.0.4", payload)
                return
            if self.path in {"/healthz", "/readyz"}:
                document = health.document()
                status = 200 if document["ready"] else 503
                payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
                self._send(status, "application/json", payload)
                return
            self._send(404, "text/plain", b"not found\n")

        def _send(self, status: int, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="rolling-cache-health",
        daemon=True,
    )
    thread.start()
    return server


def _process_thread_count() -> int:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("Threads:"):
                    return int(line.split(":", 1)[1].strip())
    except (OSError, ValueError):
        return -1
    return -1
