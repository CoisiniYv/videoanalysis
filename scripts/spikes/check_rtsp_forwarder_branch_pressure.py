#!/usr/bin/env python3
"""Real-RTSP one-branch pressure probe for Replay -> analysis-forwarder.

This starts an isolated temporary Docker topology:

    N * RTSP source-adapter -> replay -> analysis-forwarder -> synthetic sink

It does not start Savant inference, Redis, PostgreSQL, or workers. The purpose is
to test real RTSP pull and the Replay/forwarder hop without disturbing the
current midterm deployment.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import textwrap
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_RTSP_URI = "rtsp://10.37.57.157:8554/camera"
DEFAULT_SOURCE_IMAGE = "ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0"
DEFAULT_REPLAY_IMAGE = "ghcr.io/insight-platform/savant-replay-x86:v0.6.0"
DEFAULT_FORWARDER_IMAGE = "video-analytics-midterm-analysis-forwarder:latest"
DEFAULT_SAVANT_IMAGE = "ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an isolated real-RTSP Replay -> analysis-forwarder branch pressure test."
    )
    parser.add_argument("--rtsp-uri", default=DEFAULT_RTSP_URI)
    parser.add_argument("--streams", type=int, default=30)
    parser.add_argument("--warmup-s", type=float, default=30.0)
    parser.add_argument("--duration-s", type=float, default=120.0)
    parser.add_argument("--drain-s", type=float, default=10.0)
    parser.add_argument("--analysis-fps", default="8/1")
    parser.add_argument("--analysis-min-fps", default="2/1")
    parser.add_argument("--queue-max-size", type=int, default=256)
    parser.add_argument("--receive-hwm", type=int, default=1000)
    parser.add_argument("--send-hwm", type=int, default=50)
    parser.add_argument("--send-timeout-ms", type=int, default=100)
    parser.add_argument("--send-retries", type=int, default=0)
    parser.add_argument("--source-image", default=DEFAULT_SOURCE_IMAGE)
    parser.add_argument("--replay-image", default=DEFAULT_REPLAY_IMAGE)
    parser.add_argument("--forwarder-image", default=DEFAULT_FORWARDER_IMAGE)
    parser.add_argument("--savant-image", default=DEFAULT_SAVANT_IMAGE)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("/data/video-analytics/artifacts/rtsp_forwarder_pressure"),
    )
    parser.add_argument("--keep-containers", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.streams <= 0:
        raise SystemExit("--streams must be positive")
    if args.duration_s <= 0:
        raise SystemExit("--duration-s must be positive")

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    prefix = f"va-rtsp-pressure-{stamp}"
    network = prefix
    artifact_dir = (args.artifact_root / stamp).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    containers: list[str] = []
    report: dict[str, Any] = {
        "probe": "rtsp_forwarder_branch_pressure",
        "created_at": stamp,
        "artifact_dir": str(artifact_dir),
        "config": {
            "rtsp_uri": args.rtsp_uri,
            "streams": args.streams,
            "warmup_s": args.warmup_s,
            "duration_s": args.duration_s,
            "drain_s": args.drain_s,
            "analysis_fps": args.analysis_fps,
            "analysis_min_fps": args.analysis_min_fps,
            "queue_max_size": args.queue_max_size,
            "receive_hwm": args.receive_hwm,
            "send_hwm": args.send_hwm,
            "send_timeout_ms": args.send_timeout_ms,
            "send_retries": args.send_retries,
            "source_image": args.source_image,
            "replay_image": args.replay_image,
            "forwarder_image": args.forwarder_image,
            "savant_image": args.savant_image,
        },
    }

    try:
        write_replay_config(artifact_dir / "replay-config.json", args.streams)
        write_sink_script(artifact_dir / "sink.py")
        docker(["network", "create", network])

        sink_run_s = args.warmup_s + args.duration_s + args.drain_s + max(30.0, args.streams * 0.5)
        containers.append(
            docker(
                [
                    "run",
                    "-d",
                    "--name",
                    f"{prefix}-sink",
                    "--network",
                    network,
                    "--network-alias",
                    "sink",
                    "-v",
                    f"{artifact_dir}:/work:rw",
                    "--entrypoint",
                    "python",
                    args.savant_image,
                    "/work/sink.py",
                    "--run-s",
                    str(sink_run_s),
                    "--out",
                    "/work/sink-report.json",
                ],
                capture=True,
            ).strip()
        )
        containers.append(
            docker(
                [
                    "run",
                    "-d",
                    "--name",
                    f"{prefix}-replay",
                    "--network",
                    network,
                    "--network-alias",
                    "replay",
                    "-v",
                    f"{artifact_dir / 'replay-config.json'}:/opt/etc/config.json:ro",
                    "-v",
                    f"{artifact_dir / 'rocksdb'}:/opt/rocksdb:rw",
                    args.replay_image,
                ],
                capture=True,
            ).strip()
        )
        containers.append(
            docker(
                [
                    "run",
                    "-d",
                    "--name",
                    f"{prefix}-forwarder",
                    "--network",
                    network,
                    "-e",
                    "LOGLEVEL=INFO",
                    "-e",
                    "FORWARDER_IN_ENDPOINT=router+bind:tcp://0.0.0.0:5557",
                    "-e",
                    "FORWARDER_OUT_ENDPOINT=dealer+connect:tcp://sink:5558",
                    "-e",
                    f"ANALYSIS_FPS={args.analysis_fps}",
                    "-e",
                    f"ANALYSIS_MIN_FPS={args.analysis_min_fps}",
                    "-e",
                    "FORWARDER_SAMPLER_ENABLED=true",
                    "-e",
                    f"FORWARDER_QUEUE_MAX_SIZE={args.queue_max_size}",
                    "-e",
                    "FORWARDER_RECEIVE_TIMEOUT_MS=100",
                    "-e",
                    f"FORWARDER_RECEIVE_HWM={args.receive_hwm}",
                    "-e",
                    f"FORWARDER_SEND_TIMEOUT_MS={args.send_timeout_ms}",
                    "-e",
                    f"FORWARDER_SEND_RETRIES={args.send_retries}",
                    "-e",
                    f"FORWARDER_SEND_HWM={args.send_hwm}",
                    "-e",
                    "FORWARDER_METRICS_PORT=8081",
                    "--network-alias",
                    "forwarder",
                    args.forwarder_image,
                ],
                capture=True,
            ).strip()
        )

        time.sleep(3.0)
        for index in range(args.streams):
            source_id = f"rtsp_pressure_{index:02d}"
            name = f"{prefix}-src-{index:02d}"
            containers.append(
                docker(
                    [
                        "run",
                        "-d",
                        "--name",
                        name,
                        "--network",
                        network,
                        "-e",
                        f"SOURCE_ID={source_id}",
                        "-e",
                        f"LOCATION={args.rtsp_uri}",
                        "-e",
                        f"RTSP_URI={args.rtsp_uri}",
                        "-e",
                        "RTSP_TRANSPORT=tcp",
                        "-e",
                        "ZMQ_ENDPOINT=dealer+connect:tcp://replay:5555",
                        "-e",
                        "SYNC_OUTPUT=false",
                        "-e",
                        "BUFFER_LEN=2000",
                        "-e",
                        "EOS_ON_START=false",
                        "-e",
                        "FFMPEG_TIMEOUT_MS=20000",
                        "--network-alias",
                        source_id,
                        "--entrypoint",
                        "/opt/savant/adapters/gst/sources/rtsp.sh",
                        args.source_image,
                    ],
                    capture=True,
                ).strip()
            )

        report["source_start_snapshot"] = inspect_sources(prefix, args.streams)
        print(
            f"started {args.streams} RTSP adapters; warmup={args.warmup_s}s "
            f"duration={args.duration_s}s",
            flush=True,
        )
        time.sleep(args.warmup_s)
        start_metrics = fetch_forwarder_metrics(f"{prefix}-forwarder")
        start_states = inspect_sources(prefix, args.streams)
        start_stats = docker_stats([f"{prefix}-forwarder", f"{prefix}-replay"])

        time.sleep(args.duration_s)

        end_metrics = fetch_forwarder_metrics(f"{prefix}-forwarder")
        end_states = inspect_sources(prefix, args.streams)
        end_stats = docker_stats([f"{prefix}-forwarder", f"{prefix}-replay"])
        time.sleep(args.drain_s)

        report.update(
            {
                "start_metrics": start_metrics,
                "end_metrics": end_metrics,
                "delta_metrics": delta_metrics(start_metrics, end_metrics),
                "start_source_states": start_states,
                "end_source_states": end_states,
                "start_stats": start_stats,
                "end_stats": end_stats,
                "forwarder_state": inspect_container(f"{prefix}-forwarder"),
                "replay_state": inspect_container(f"{prefix}-replay"),
                "sink_state": inspect_container(f"{prefix}-sink"),
            }
        )
        report["summary"] = summarize(report, args.streams, args.duration_s)
        report["passed"] = evaluate(report["summary"])
        report["pass_token"] = (
            "PASS_RTSP_FORWARDER_BRANCH_30_STREAM_PRESSURE"
            if report["passed"]
            else None
        )
        return_code = 0 if report["passed"] else 1
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["passed"] = False
        report["pass_token"] = None
        return_code = 1
    finally:
        collect_logs(prefix, args.streams, artifact_dir)
        sink_report = artifact_dir / "sink-report.json"
        if sink_report.exists():
            try:
                report["sink_report"] = json.loads(sink_report.read_text(encoding="utf-8"))
            except Exception as exc:
                report["sink_report_error"] = f"{type(exc).__name__}: {exc}"
        report_path = artifact_dir / "report.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(console_summary(report, report_path), indent=2, sort_keys=True), flush=True)
        if not args.keep_containers:
            cleanup(prefix, network)

    return return_code


def write_replay_config(path: Path, streams: int) -> None:
    path.write_text(
        json.dumps(
            {
                "common": {
                    "pass_metadata_only": False,
                    "management_port": 8080,
                    "stats_period": {"secs": 10, "nanos": 0},
                    "job_writer_cache_max_capacity": max(1000, streams * 50),
                    "job_writer_cache_ttl": {"secs": 60, "nanos": 0},
                    "job_eviction_ttl": {"secs": 60, "nanos": 0},
                    "default_job_sink_options": {
                        "send_timeout": {"secs": 5, "nanos": 0},
                        "send_retries": 5,
                        "receive_timeout": {"secs": 5, "nanos": 0},
                        "receive_retries": 5,
                        "send_hwm": 10000,
                        "receive_hwm": 10000,
                        "inflight_ops": 100,
                    },
                },
                "in_stream": {
                    "url": "router+bind:tcp://0.0.0.0:5555",
                    "options": {
                        "receive_timeout": {"secs": 1, "nanos": 0},
                        "receive_hwm": 1000,
                        "topic_prefix_spec": {"none": None},
                        "source_cache_size": max(1000, streams * 10),
                        "inflight_ops": 100,
                    },
                },
                "out_stream": {
                    "url": "dealer+connect:tcp://forwarder:5557",
                    "options": {
                        "send_timeout": {"secs": 1, "nanos": 0},
                        "send_retries": 2,
                        "receive_timeout": {"secs": 5, "nanos": 0},
                        "receive_retries": 10,
                        "send_hwm": 10000,
                        "receive_hwm": 10000,
                        "inflight_ops": 1000,
                    },
                },
                "storage": {
                    "rocksdb": {
                        "path": "/opt/rocksdb",
                        "data_expiration_ttl": {"secs": 300, "nanos": 0},
                        "compaction_period": {"secs": 120, "nanos": 0},
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def write_sink_script(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            r'''
            import argparse
            import json
            import time
            from collections import defaultdict
            from pathlib import Path

            from savant_rs.py.utils.zeromq import ZeroMQSource

            parser = argparse.ArgumentParser()
            parser.add_argument("--run-s", type=float, required=True)
            parser.add_argument("--out", required=True)
            args = parser.parse_args()

            counts = defaultdict(int)
            bytes_by_source = defaultdict(int)
            first_ts = {}
            last_ts = {}
            source = ZeroMQSource(
                "router+bind:tcp://0.0.0.0:5558",
                receive_timeout=100,
                receive_hwm=10000,
            )
            source.start()
            start = time.time()
            deadline = start + args.run_s
            try:
                while time.time() < deadline:
                    msg = source.next_message()
                    if msg is None or not msg.message.is_video_frame():
                        continue
                    frame = msg.message.as_video_frame()
                    source_id = str(frame.source_id or "")
                    now = time.time()
                    counts[source_id] += 1
                    bytes_by_source[source_id] += len(msg.content or b"")
                    first_ts.setdefault(source_id, now)
                    last_ts[source_id] = now
            finally:
                try:
                    source.terminate()
                except Exception:
                    pass
            report = {
                "started_at_epoch_s": start,
                "finished_at_epoch_s": time.time(),
                "total_frames": sum(counts.values()),
                "source_count": len(counts),
                "counts": dict(counts),
                "bytes_by_source": dict(bytes_by_source),
                "first_ts": first_ts,
                "last_ts": last_ts,
            }
            Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
            '''
        ).lstrip(),
        encoding="utf-8",
    )


def docker(args: list[str], *, capture: bool = False, check: bool = True) -> str:
    cmd = ["docker", *args]
    result = subprocess.run(
        cmd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and result.returncode != 0:
        detail = result.stderr or result.stdout or ""
        raise RuntimeError(f"{' '.join(cmd)} failed rc={result.returncode}: {detail.strip()}")
    return result.stdout or ""


def fetch_forwarder_metrics(container: str) -> dict[str, Any]:
    text = docker(
        [
            "exec",
            container,
            "python",
            "-c",
            (
                "import urllib.request; "
                "print(urllib.request.urlopen('http://127.0.0.1:8081/metrics', "
                "timeout=3).read().decode())"
            ),
        ],
        capture=True,
    )
    return parse_metrics(text)


def parse_metrics(text: str) -> dict[str, Any]:
    sources: dict[str, dict[str, int]] = defaultdict(dict)
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
        metric, source_id, value = match.groups()
        key = {
            "va_forwarder_frames_seen_total": "seen",
            "va_forwarder_frames_forwarded_total": "forwarded",
            "va_forwarder_frames_dropped_total": "dropped",
            "va_forwarder_savant_send_failures_total": "send_failures",
        }[metric]
        sources[source_id][key] = int(value)
    return {
        "queue_depth": queue_depth,
        "running": running,
        "sources": dict(sources),
    }


def delta_metrics(start: dict[str, Any], end: dict[str, Any]) -> dict[str, Any]:
    source_ids = sorted(set(start.get("sources", {})) | set(end.get("sources", {})))
    deltas: dict[str, dict[str, int]] = {}
    for source_id in source_ids:
        deltas[source_id] = {}
        for key in ("seen", "forwarded", "dropped", "send_failures"):
            deltas[source_id][key] = int(end["sources"].get(source_id, {}).get(key, 0)) - int(
                start["sources"].get(source_id, {}).get(key, 0)
            )
    return {
        "queue_depth_start": start.get("queue_depth"),
        "queue_depth_end": end.get("queue_depth"),
        "sources": deltas,
    }


def inspect_container(name: str) -> dict[str, Any]:
    text = docker(["inspect", name, "--format", "{{json .State}}"], capture=True, check=False)
    if not text.strip():
        return {"exists": False}
    try:
        state = json.loads(text)
    except json.JSONDecodeError:
        return {"exists": True, "raw": text.strip()}
    state["exists"] = True
    return state


def inspect_sources(prefix: str, streams: int) -> dict[str, Any]:
    states = {}
    for index in range(streams):
        name = f"{prefix}-src-{index:02d}"
        states[name] = inspect_container(name)
    running = sum(1 for state in states.values() if state.get("Running") is True)
    exited = sum(1 for state in states.values() if state.get("Status") == "exited")
    return {"running": running, "exited": exited, "states": states}


def docker_stats(names: list[str]) -> list[dict[str, Any]]:
    output = docker(["stats", "--no-stream", "--format", "{{json .}}", *names], capture=True, check=False)
    rows = []
    for line in output.splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"raw": line})
    return rows


def collect_logs(prefix: str, streams: int, artifact_dir: Path) -> None:
    names = [f"{prefix}-sink", f"{prefix}-replay", f"{prefix}-forwarder"]
    names.extend(f"{prefix}-src-{index:02d}" for index in range(streams))
    log_dir = artifact_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        result = subprocess.run(
            ["docker", "logs", "--tail", "300", name],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output = result.stdout or ""
        if output:
            (log_dir / f"{name}.log").write_text(output, encoding="utf-8", errors="replace")


def summarize(report: dict[str, Any], streams: int, duration_s: float) -> dict[str, Any]:
    delta = report.get("delta_metrics", {}).get("sources", {})
    seen_values = [int(values.get("seen", 0)) for values in delta.values()]
    forwarded_values = [int(values.get("forwarded", 0)) for values in delta.values()]
    dropped_values = [int(values.get("dropped", 0)) for values in delta.values()]
    send_failure_values = [int(values.get("send_failures", 0)) for values in delta.values()]
    seen_total = sum(seen_values)
    forwarded_total = sum(forwarded_values)
    dropped_total = sum(dropped_values)
    send_failures_total = sum(send_failure_values)
    active_sources = sum(1 for value in seen_values if value > 0)
    end_states = report.get("end_source_states", {})
    running_sources = int(end_states.get("running", 0))
    return {
        "expected_sources": streams,
        "active_sources_with_forwarder_seen": active_sources,
        "running_sources_at_end": running_sources,
        "exited_sources_at_end": int(end_states.get("exited", 0)),
        "seen_total": seen_total,
        "forwarded_total": forwarded_total,
        "dropped_total": dropped_total,
        "send_failures_total": send_failures_total,
        "seen_fps_total": round(seen_total / duration_s, 3),
        "forwarded_fps_total": round(forwarded_total / duration_s, 3),
        "seen_fps_per_active_source_min": round(min(seen_values) / duration_s, 3)
        if seen_values
        else 0.0,
        "seen_fps_per_active_source_max": round(max(seen_values) / duration_s, 3)
        if seen_values
        else 0.0,
        "forwarded_fps_per_active_source_min": round(min(forwarded_values) / duration_s, 3)
        if forwarded_values
        else 0.0,
        "forwarded_fps_per_active_source_max": round(max(forwarded_values) / duration_s, 3)
        if forwarded_values
        else 0.0,
        "queue_depth_end": report.get("delta_metrics", {}).get("queue_depth_end"),
    }


def evaluate(summary: dict[str, Any]) -> bool:
    return (
        summary.get("running_sources_at_end") == summary.get("expected_sources")
        and summary.get("active_sources_with_forwarder_seen") == summary.get("expected_sources")
        and summary.get("send_failures_total") == 0
        and float(summary.get("forwarded_fps_per_active_source_min") or 0.0) >= 6.0
        and int(summary.get("queue_depth_end") or 0) <= 64
    )


def console_summary(report: dict[str, Any], report_path: Path) -> dict[str, Any]:
    return {
        "passed": report.get("passed"),
        "pass_token": report.get("pass_token"),
        "artifact_dir": report.get("artifact_dir"),
        "report_path": str(report_path),
        "summary": report.get("summary"),
        "error": report.get("error"),
    }


def cleanup(prefix: str, network: str) -> None:
    ids = docker(["ps", "-aq", "--filter", f"name={prefix}"], capture=True, check=False).splitlines()
    if ids:
        docker(["rm", "-f", *ids], check=False)
    docker(["network", "rm", network], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
