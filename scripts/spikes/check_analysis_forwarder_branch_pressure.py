#!/usr/bin/env python3
"""Offline pressure probe for one analysis-forwarder branch.

The default host mode re-execs this script inside a Docker image that carries
Savant's ``savant_rs`` package. The in-container mode runs a synthetic topology:

    synthetic Replay writer -> AnalysisForwarder -> synthetic Savant sink

No live RTSP, Replay, Savant, Redis, database, or GPU service is required.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import resource
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any


DEFAULT_IMAGE = os.getenv(
    "FORWARDER_PRESSURE_IMAGE",
    "ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1",
)
NANOS_PER_SECOND = 1_000_000_000
PASS_TOKEN = "PASS_ANALYSIS_FORWARDER_BRANCH_30_STREAM_PRESSURE"


@dataclass(frozen=True)
class Thresholds:
    max_send_failures: int
    max_queue_depth: int
    max_forward_skew_ratio: float
    min_forwarded_fps: float
    max_p95_latency_ms: float
    max_generator_overrun_ratio: float


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run an offline one-branch analysis-forwarder pressure probe with "
            "synthetic Savant VideoFrame messages."
        )
    )
    parser.add_argument("--streams", type=int, default=30)
    parser.add_argument("--duration-s", type=float, default=60.0)
    parser.add_argument("--input-fps", type=float, default=30.0)
    parser.add_argument("--analysis-fps", default="8/1")
    parser.add_argument("--analysis-min-fps", default="2/1")
    parser.add_argument(
        "--payload-bytes",
        type=int,
        default=25_000,
        help="Encoded payload bytes per frame. 25000 is about 6 Mbps at 30 FPS.",
    )
    parser.add_argument("--queue-max-size", type=int, default=256)
    parser.add_argument("--receive-hwm", type=int, default=1000)
    parser.add_argument("--send-hwm", type=int, default=50)
    parser.add_argument("--send-timeout-ms", type=int, default=100)
    parser.add_argument("--send-retries", type=int, default=0)
    parser.add_argument("--receive-timeout-ms", type=int, default=100)
    parser.add_argument("--keyframe-interval", type=int, default=30)
    parser.add_argument("--base-port", type=int, default=39111)
    parser.add_argument("--no-stagger-sources", action="store_true")
    parser.add_argument("--drain-timeout-s", type=float, default=10.0)
    parser.add_argument("--monitor-period-s", type=float, default=0.05)
    parser.add_argument("--max-send-failures", type=int, default=0)
    parser.add_argument("--max-queue-depth", type=int, default=64)
    parser.add_argument("--max-forward-skew-ratio", type=float, default=0.20)
    parser.add_argument(
        "--min-forwarded-fps",
        type=float,
        default=None,
        help=(
            "Minimum accepted FPS per source. Defaults to 90 percent of the "
            "discrete PTS-gate rate implied by --input-fps and --analysis-fps."
        ),
    )
    parser.add_argument("--max-p95-latency-ms", type=float, default=1000.0)
    parser.add_argument("--max-generator-overrun-ratio", type=float, default=0.20)
    parser.add_argument(
        "--report-path",
        type=Path,
        default=None,
        help="Path for the JSON report. Host mode maps the parent dir into Docker.",
    )
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument(
        "--in-container",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.in_container:
        return run_in_docker(args)
    return run_probe(args)


def run_in_docker(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[2]
    report_path = args.report_path
    if report_path is None:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        report_path = Path(
            f"/data/video-analytics/artifacts/analysis_forwarder_pressure/"
            f"analysis_forwarder_branch_pressure_{stamp}.json"
        )
    report_path = report_path.expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    inner_report_path = Path("/reports") / report_path.name

    inner_args = [
        "/workspace/scripts/spikes/check_analysis_forwarder_branch_pressure.py",
        "--in-container",
        "--streams",
        str(args.streams),
        "--duration-s",
        str(args.duration_s),
        "--input-fps",
        str(args.input_fps),
        "--analysis-fps",
        args.analysis_fps,
        "--analysis-min-fps",
        args.analysis_min_fps,
        "--payload-bytes",
        str(args.payload_bytes),
        "--queue-max-size",
        str(args.queue_max_size),
        "--receive-hwm",
        str(args.receive_hwm),
        "--send-hwm",
        str(args.send_hwm),
        "--send-timeout-ms",
        str(args.send_timeout_ms),
        "--send-retries",
        str(args.send_retries),
        "--receive-timeout-ms",
        str(args.receive_timeout_ms),
        "--keyframe-interval",
        str(args.keyframe_interval),
        "--base-port",
        str(args.base_port),
        "--drain-timeout-s",
        str(args.drain_timeout_s),
        "--monitor-period-s",
        str(args.monitor_period_s),
        "--max-send-failures",
        str(args.max_send_failures),
        "--max-queue-depth",
        str(args.max_queue_depth),
        "--max-forward-skew-ratio",
        str(args.max_forward_skew_ratio),
        "--max-p95-latency-ms",
        str(args.max_p95_latency_ms),
        "--max-generator-overrun-ratio",
        str(args.max_generator_overrun_ratio),
        "--report-path",
        str(inner_report_path),
    ]
    if args.no_stagger_sources:
        inner_args.append("--no-stagger-sources")
    if args.min_forwarded_fps is not None:
        inner_args.extend(["--min-forwarded-fps", str(args.min_forwarded_fps)])

    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "python",
        "-v",
        f"{root}:/workspace:ro",
        "-v",
        f"{report_path.parent}:/reports:rw",
        "-w",
        "/workspace",
        args.image,
        *inner_args,
    ]
    print("running:", " ".join(cmd), flush=True)
    return subprocess.call(cmd)


def run_probe(args: argparse.Namespace) -> int:
    _validate_args(args)
    repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root / "services" / "analysis-forwarder"))

    from app.main import AnalysisForwarder, ForwarderConfig  # noqa: PLC0415
    from savant_rs.primitives import VideoFrame, VideoFrameContent  # noqa: PLC0415
    from savant_rs.py.utils.zeromq import ZeroMQSource  # noqa: PLC0415
    from savant_rs.zmq import BlockingWriter, WriterConfigBuilder  # noqa: PLC0415

    in_endpoint = f"router+bind:tcp://127.0.0.1:{args.base_port}"
    source_endpoint = f"dealer+connect:tcp://127.0.0.1:{args.base_port}"
    out_bind_endpoint = f"router+bind:tcp://127.0.0.1:{args.base_port + 1}"
    out_connect_endpoint = f"dealer+connect:tcp://127.0.0.1:{args.base_port + 1}"

    config = ForwarderConfig(
        in_endpoint=in_endpoint,
        out_endpoint=out_connect_endpoint,
        analysis_fps=args.analysis_fps,
        min_fps=args.analysis_min_fps,
        sampler_enabled=True,
        queue_max_size=args.queue_max_size,
        receive_timeout_ms=args.receive_timeout_ms,
        receive_hwm=args.receive_hwm,
        send_timeout_ms=args.send_timeout_ms,
        send_retries=args.send_retries,
        send_hwm=args.send_hwm,
        metrics_port=0,
    )

    sink = ZeroMQSource(
        out_bind_endpoint,
        receive_timeout=args.receive_timeout_ms,
        receive_hwm=max(args.receive_hwm, args.streams * 4),
    )
    source_writer_config = WriterConfigBuilder(source_endpoint)
    source_writer_config.with_send_timeout(args.send_timeout_ms)
    source_writer_config.with_send_retries(args.send_retries)
    source_writer_config.with_send_hwm(max(args.receive_hwm, args.streams * 4))
    source_writer = BlockingWriter(source_writer_config.build())
    forwarder = AnalysisForwarder(config)

    stop_sink = threading.Event()
    stop_monitor = threading.Event()
    send_times_ns: dict[str, int] = {}
    send_times_lock = threading.Lock()
    sink_counts: dict[str, int] = defaultdict(int)
    sink_bytes: dict[str, int] = defaultdict(int)
    latency_ms: list[float] = []
    sink_lock = threading.Lock()
    monitor = {"max_queue_depth": 0, "samples": 0}

    def sink_loop() -> None:
        while not stop_sink.is_set():
            item = sink.next_message()
            if item is None:
                continue
            message = item.message
            if not message.is_video_frame():
                continue
            frame = message.as_video_frame()
            source_id = str(frame.source_id or "")
            with sink_lock:
                sink_counts[source_id] += 1
                sink_bytes[source_id] += len(item.content or b"")
            frame_uuid = str(frame.uuid)
            with send_times_lock:
                sent_ns = send_times_ns.pop(frame_uuid, None)
            if sent_ns is not None:
                latency_ms.append((time.perf_counter_ns() - sent_ns) / 1_000_000)

    def monitor_loop() -> None:
        while not stop_monitor.is_set():
            depth = len(forwarder.queue)
            monitor["samples"] += 1
            if depth > monitor["max_queue_depth"]:
                monitor["max_queue_depth"] = depth
            time.sleep(args.monitor_period_s)

    sink.start()
    forwarder_thread = threading.Thread(target=forwarder.run, name="pressure-forwarder", daemon=True)
    sink_thread = threading.Thread(target=sink_loop, name="pressure-sink", daemon=True)
    monitor_thread = threading.Thread(target=monitor_loop, name="pressure-monitor", daemon=True)

    tracemalloc.start()
    process_cpu_start = time.process_time()
    wall_start = time.perf_counter()
    forwarder_thread.start()
    sink_thread.start()
    monitor_thread.start()
    source_writer.start()
    time.sleep(0.3)

    source_ids = [f"pressure_source_{idx:02d}" for idx in range(args.streams)]
    frames_per_source = int(math.floor(args.duration_s * args.input_fps))
    expected_sent = frames_per_source * args.streams
    pts_step_ns = int(round(NANOS_PER_SECOND / args.input_fps))
    payload = _payload(args.payload_bytes)
    send_success = 0
    source_send_failures: dict[str, int] = defaultdict(int)
    max_schedule_lag_ms = 0.0
    send_start = time.perf_counter()

    try:
        for frame_no in range(frames_per_source):
            frame_target = send_start + (frame_no / args.input_fps)
            for source_index, source_id in enumerate(source_ids):
                if args.no_stagger_sources:
                    target = frame_target
                else:
                    target = frame_target + (source_index / (args.input_fps * args.streams))
                now = time.perf_counter()
                if now < target:
                    time.sleep(target - now)
                    now = time.perf_counter()
                lag_ms = max(0.0, (now - target) * 1000.0)
                if lag_ms > max_schedule_lag_ms:
                    max_schedule_lag_ms = lag_ms

                keyframe = frame_no == 0 or frame_no % args.keyframe_interval == 0
                frame = VideoFrame(
                    source_id=source_id,
                    framerate=f"{int(args.input_fps)}/1",
                    width=1920,
                    height=1080,
                    codec="h264",
                    content=VideoFrameContent.external("zeromq", None),
                    keyframe=keyframe,
                    time_base=(1, NANOS_PER_SECOND),
                    pts=frame_no * pts_step_ns,
                    dts=frame_no * pts_step_ns,
                    duration=pts_step_ns,
                )
                frame_uuid = str(frame.uuid)
                with send_times_lock:
                    send_times_ns[frame_uuid] = time.perf_counter_ns()
                result = source_writer.send_message(source_id, frame.to_message(), payload)
                if _writer_success(result):
                    send_success += 1
                else:
                    source_send_failures[source_id] += 1
                    with send_times_lock:
                        send_times_ns.pop(frame_uuid, None)
    finally:
        send_end = time.perf_counter()

    deadline = time.perf_counter() + args.drain_timeout_s
    while time.perf_counter() < deadline:
        metrics = _parse_forwarder_metrics(forwarder.metrics.render_prometheus())
        forwarded_total = sum(v.get("forwarded", 0) for v in metrics["sources"].values())
        with sink_lock:
            sink_total = sum(sink_counts.values())
        if sink_total >= forwarded_total and len(forwarder.queue) == 0:
            break
        time.sleep(0.1)

    forwarder.stop_event.set()
    stop_sink.set()
    stop_monitor.set()
    forwarder_thread.join(timeout=5)
    sink_thread.join(timeout=2)
    monitor_thread.join(timeout=2)
    _shutdown(source_writer)
    _shutdown(sink)
    current_alloc, peak_alloc = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    wall_end = time.perf_counter()
    process_cpu_end = time.process_time()

    final_metrics = _parse_forwarder_metrics(forwarder.metrics.render_prometheus())
    report = _build_report(
        args=args,
        source_ids=source_ids,
        expected_sent=expected_sent,
        send_success=send_success,
        source_send_failures=source_send_failures,
        send_elapsed_s=send_end - send_start,
        wall_elapsed_s=wall_end - wall_start,
        process_cpu_s=process_cpu_end - process_cpu_start,
        max_schedule_lag_ms=max_schedule_lag_ms,
        metrics=final_metrics,
        sink_counts=dict(sink_counts),
        sink_bytes=dict(sink_bytes),
        latency_ms=latency_ms,
        max_queue_depth=int(monitor["max_queue_depth"]),
        monitor_samples=int(monitor["samples"]),
        current_alloc=current_alloc,
        peak_alloc=peak_alloc,
    )
    report["checks"] = _evaluate_report(report, _thresholds(args))
    report["passed"] = all(check["ok"] for check in report["checks"])
    report["pass_token"] = PASS_TOKEN if report["passed"] else None
    report["report_path"] = str(args.report_path) if args.report_path is not None else None

    if args.report_path is not None:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(_console_summary(report), indent=2, sort_keys=True), flush=True)
    if report["passed"]:
        print(PASS_TOKEN, flush=True)
        return 0
    print("FAIL_ANALYSIS_FORWARDER_BRANCH_PRESSURE", flush=True)
    return 1


def _validate_args(args: argparse.Namespace) -> None:
    if args.streams <= 0:
        raise SystemExit("--streams must be positive")
    if args.duration_s <= 0:
        raise SystemExit("--duration-s must be positive")
    if args.input_fps <= 0:
        raise SystemExit("--input-fps must be positive")
    if args.payload_bytes <= 0:
        raise SystemExit("--payload-bytes must be positive")
    if args.keyframe_interval <= 0:
        raise SystemExit("--keyframe-interval must be positive")


def _payload(size: int) -> bytes:
    prefix = b"\x00\x00\x00\x01"
    if size <= len(prefix):
        return prefix[:size]
    return prefix + (b"P" * (size - len(prefix)))


def _writer_success(result: Any) -> bool:
    return type(result).__name__ in {"WriterResultSuccess", "WriterResultAck"}


def _shutdown(endpoint: Any) -> None:
    try:
        endpoint.shutdown()
    except AttributeError:
        endpoint.terminate()
    except Exception:
        pass


def _parse_forwarder_metrics(text: str) -> dict[str, Any]:
    source_metrics: dict[str, dict[str, int]] = defaultdict(dict)
    queue_depth = 0
    running = 0
    for line in text.splitlines():
        if line.startswith("va_forwarder_queue_depth "):
            queue_depth = int(float(line.rsplit(" ", 1)[1]))
            continue
        if line.startswith("va_forwarder_running "):
            running = int(float(line.rsplit(" ", 1)[1]))
            continue
        match = re.match(
            r'^(va_forwarder_frames_seen_total|va_forwarder_frames_forwarded_total|'
            r'va_forwarder_frames_dropped_total|'
            r'va_forwarder_savant_send_failures_total)\{source_id="([^"]+)"\} (\d+)',
            line,
        )
        if not match:
            continue
        metric, source_id, raw_value = match.groups()
        name = {
            "va_forwarder_frames_seen_total": "seen",
            "va_forwarder_frames_forwarded_total": "forwarded",
            "va_forwarder_frames_dropped_total": "dropped",
            "va_forwarder_savant_send_failures_total": "send_failures",
        }[metric]
        source_metrics[source_id][name] = int(raw_value)
    return {
        "queue_depth": queue_depth,
        "running": running,
        "sources": {source: dict(values) for source, values in source_metrics.items()},
    }


def _build_report(
    *,
    args: argparse.Namespace,
    source_ids: list[str],
    expected_sent: int,
    send_success: int,
    source_send_failures: dict[str, int],
    send_elapsed_s: float,
    wall_elapsed_s: float,
    process_cpu_s: float,
    max_schedule_lag_ms: float,
    metrics: dict[str, Any],
    sink_counts: dict[str, int],
    sink_bytes: dict[str, int],
    latency_ms: list[float],
    max_queue_depth: int,
    monitor_samples: int,
    current_alloc: int,
    peak_alloc: int,
) -> dict[str, Any]:
    forwarded_by_source = {
        source_id: int(metrics["sources"].get(source_id, {}).get("forwarded", 0))
        for source_id in source_ids
    }
    seen_by_source = {
        source_id: int(metrics["sources"].get(source_id, {}).get("seen", 0))
        for source_id in source_ids
    }
    dropped_by_source = {
        source_id: int(metrics["sources"].get(source_id, {}).get("dropped", 0))
        for source_id in source_ids
    }
    send_failures_by_source = {
        source_id: int(metrics["sources"].get(source_id, {}).get("send_failures", 0))
        for source_id in source_ids
    }
    forwarded_values = list(forwarded_by_source.values())
    sink_values = [int(sink_counts.get(source_id, 0)) for source_id in source_ids]
    latency_summary = _latency_summary(latency_ms)
    input_fps_actual = send_success / send_elapsed_s if send_elapsed_s > 0 else 0.0
    forwarded_total = sum(forwarded_values)
    dropped_total = sum(dropped_by_source.values())
    seen_total = sum(seen_by_source.values())
    sink_total = sum(sink_values)
    ru_maxrss_kb = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    return {
        "probe": "analysis_forwarder_branch_pressure",
        "created_at_epoch_s": time.time(),
        "config": {
            "streams": args.streams,
            "duration_s": args.duration_s,
            "input_fps": args.input_fps,
            "analysis_fps": args.analysis_fps,
            "analysis_min_fps": args.analysis_min_fps,
            "payload_bytes": args.payload_bytes,
            "queue_max_size": args.queue_max_size,
            "receive_hwm": args.receive_hwm,
            "send_hwm": args.send_hwm,
            "send_timeout_ms": args.send_timeout_ms,
            "send_retries": args.send_retries,
            "keyframe_interval": args.keyframe_interval,
            "stagger_sources": not args.no_stagger_sources,
        },
        "source_count": len(source_ids),
        "expected_sent": expected_sent,
        "send_success": send_success,
        "source_send_failures_total": sum(source_send_failures.values()),
        "source_send_failures_by_source": dict(source_send_failures),
        "forwarder_seen_total": seen_total,
        "forwarder_forwarded_total": forwarded_total,
        "forwarder_dropped_total": dropped_total,
        "forwarder_send_failures_total": sum(send_failures_by_source.values()),
        "sink_received_total": sink_total,
        "sink_payload_bytes_total": sum(int(v) for v in sink_bytes.values()),
        "input_fps_actual": round(input_fps_actual, 3),
        "input_fps_target_total": round(args.streams * args.input_fps, 3),
        "forwarded_fps_total": round(forwarded_total / send_elapsed_s, 3) if send_elapsed_s > 0 else 0.0,
        "forwarded_fps_per_source_min": round(min(forwarded_values) / send_elapsed_s, 3)
        if forwarded_values and send_elapsed_s > 0
        else 0.0,
        "forwarded_fps_per_source_max": round(max(forwarded_values) / send_elapsed_s, 3)
        if forwarded_values and send_elapsed_s > 0
        else 0.0,
        "forwarded_per_source_min": min(forwarded_values) if forwarded_values else 0,
        "forwarded_per_source_max": max(forwarded_values) if forwarded_values else 0,
        "sink_per_source_min": min(sink_values) if sink_values else 0,
        "sink_per_source_max": max(sink_values) if sink_values else 0,
        "drop_ratio": round(dropped_total / seen_total, 6) if seen_total else 0.0,
        "max_queue_depth": max_queue_depth,
        "monitor_samples": monitor_samples,
        "latency_ms": latency_summary,
        "max_schedule_lag_ms": round(max_schedule_lag_ms, 3),
        "send_elapsed_s": round(send_elapsed_s, 3),
        "wall_elapsed_s": round(wall_elapsed_s, 3),
        "process_cpu_s": round(process_cpu_s, 3),
        "process_cpu_ratio": round(process_cpu_s / wall_elapsed_s, 3) if wall_elapsed_s > 0 else 0.0,
        "ru_maxrss_kb": ru_maxrss_kb,
        "tracemalloc_current_bytes": current_alloc,
        "tracemalloc_peak_bytes": peak_alloc,
        "per_source": {
            source_id: {
                "seen": seen_by_source[source_id],
                "forwarded": forwarded_by_source[source_id],
                "dropped": dropped_by_source[source_id],
                "send_failures": send_failures_by_source[source_id],
                "sink_received": int(sink_counts.get(source_id, 0)),
                "sink_payload_bytes": int(sink_bytes.get(source_id, 0)),
            }
            for source_id in source_ids
        },
    }


def _latency_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "p50": round(_percentile(ordered, 50), 3),
        "p95": round(_percentile(ordered, 95), 3),
        "p99": round(_percentile(ordered, 99), 3),
        "max": round(ordered[-1], 3),
        "mean": round(statistics.fmean(ordered), 3),
    }


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = (len(sorted_values) - 1) * (percentile / 100.0)
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return sorted_values[int(index)]
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (index - low)


def _thresholds(args: argparse.Namespace) -> Thresholds:
    return Thresholds(
        max_send_failures=args.max_send_failures,
        max_queue_depth=args.max_queue_depth,
        max_forward_skew_ratio=args.max_forward_skew_ratio,
        min_forwarded_fps=(
            float(args.min_forwarded_fps)
            if args.min_forwarded_fps is not None
            else _default_min_forwarded_fps(args.input_fps, args.analysis_fps)
        ),
        max_p95_latency_ms=args.max_p95_latency_ms,
        max_generator_overrun_ratio=args.max_generator_overrun_ratio,
    )


def _default_min_forwarded_fps(input_fps: float, analysis_fps: str) -> float:
    max_fps = _parse_fps(analysis_fps, default=8.0)
    if input_fps <= 0 or max_fps <= 0:
        return 0.0
    frame_stride = max(1, math.ceil(input_fps / max_fps))
    return round((input_fps / frame_stride) * 0.90, 3)


def _parse_fps(value: str | float | int | None, *, default: float) -> float:
    if value in (None, ""):
        return float(default)
    try:
        if isinstance(value, str) and "/" in value:
            return float(Fraction(value.strip()))
        return float(value)  # type: ignore[arg-type]
    except Exception:
        return float(default)


def _evaluate_report(report: dict[str, Any], thresholds: Thresholds) -> list[dict[str, Any]]:
    checks = []
    send_elapsed_s = float(report["send_elapsed_s"])
    expected_send_elapsed = float(report["config"]["duration_s"])
    forwarded_min = int(report["forwarded_per_source_min"])
    forwarded_max = int(report["forwarded_per_source_max"])
    skew_ratio = (
        (forwarded_max - forwarded_min) / max(float(forwarded_max), 1.0)
        if forwarded_max
        else 0.0
    )
    p95_latency = report["latency_ms"].get("p95")
    p95_latency = float(p95_latency) if p95_latency is not None else 0.0

    checks.append(
        _check(
            "source_generator_sent_all_frames",
            report["send_success"] == report["expected_sent"],
            f"send_success={report['send_success']} expected={report['expected_sent']}",
        )
    )
    checks.append(
        _check(
            "forwarder_saw_all_successful_frames",
            report["forwarder_seen_total"] == report["send_success"],
            f"seen={report['forwarder_seen_total']} send_success={report['send_success']}",
        )
    )
    checks.append(
        _check(
            "forwarder_send_failures_within_limit",
            int(report["forwarder_send_failures_total"]) <= thresholds.max_send_failures,
            f"forwarder_send_failures={report['forwarder_send_failures_total']} "
            f"limit={thresholds.max_send_failures}",
        )
    )
    checks.append(
        _check(
            "sink_received_every_forwarded_frame",
            report["sink_received_total"] == report["forwarder_forwarded_total"],
            f"sink_received={report['sink_received_total']} "
            f"forwarded={report['forwarder_forwarded_total']}",
        )
    )
    checks.append(
        _check(
            "queue_depth_within_limit",
            int(report["max_queue_depth"]) <= thresholds.max_queue_depth,
            f"max_queue_depth={report['max_queue_depth']} limit={thresholds.max_queue_depth}",
        )
    )
    checks.append(
        _check(
            "per_source_forwarding_fair",
            skew_ratio <= thresholds.max_forward_skew_ratio,
            f"skew_ratio={skew_ratio:.4f} limit={thresholds.max_forward_skew_ratio:.4f}",
        )
    )
    checks.append(
        _check(
            "per_source_forwarded_fps_above_min",
            float(report["forwarded_fps_per_source_min"]) >= thresholds.min_forwarded_fps,
            f"min_forwarded_fps={report['forwarded_fps_per_source_min']} "
            f"limit={thresholds.min_forwarded_fps}",
        )
    )
    checks.append(
        _check(
            "p95_forward_latency_within_limit",
            p95_latency <= thresholds.max_p95_latency_ms,
            f"p95_latency_ms={p95_latency:.3f} limit={thresholds.max_p95_latency_ms}",
        )
    )
    checks.append(
        _check(
            "source_generator_kept_up",
            send_elapsed_s <= expected_send_elapsed * (1.0 + thresholds.max_generator_overrun_ratio),
            f"send_elapsed_s={send_elapsed_s:.3f} expected={expected_send_elapsed:.3f} "
            f"overrun_limit={thresholds.max_generator_overrun_ratio:.3f}",
        )
    )
    return checks


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _console_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "passed": report["passed"],
        "pass_token": report["pass_token"],
        "config": report["config"],
        "send_success": report["send_success"],
        "forwarder_seen_total": report["forwarder_seen_total"],
        "forwarder_forwarded_total": report["forwarder_forwarded_total"],
        "forwarder_dropped_total": report["forwarder_dropped_total"],
        "forwarder_send_failures_total": report["forwarder_send_failures_total"],
        "sink_received_total": report["sink_received_total"],
        "input_fps_actual": report["input_fps_actual"],
        "forwarded_fps_total": report["forwarded_fps_total"],
        "forwarded_fps_per_source_min": report["forwarded_fps_per_source_min"],
        "forwarded_fps_per_source_max": report["forwarded_fps_per_source_max"],
        "max_queue_depth": report["max_queue_depth"],
        "latency_ms": report["latency_ms"],
        "max_schedule_lag_ms": report["max_schedule_lag_ms"],
        "process_cpu_ratio": report["process_cpu_ratio"],
        "ru_maxrss_kb": report["ru_maxrss_kb"],
        "tracemalloc_peak_bytes": report["tracemalloc_peak_bytes"],
        "failed_checks": [check for check in report["checks"] if not check["ok"]],
        "report_path": str(report.get("report_path", "")),
    }


if __name__ == "__main__":
    raise SystemExit(main())
