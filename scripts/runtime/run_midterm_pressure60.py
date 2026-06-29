#!/usr/bin/env python3
"""Run a midterm 60-source RTSP pressure test and retain evidence samples.

The script is intentionally operational rather than a unit-test harness.  It
uses PostgreSQL as the camera source of truth, applies the 8090 runtime control
APIs, samples runtime/GPU/DB state, then cleans temporary pressure data while
keeping a bounded set of playable evidence bundles for operator review.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import random
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psycopg
import yaml
from psycopg.rows import dict_row
from redis import Redis


DEFAULT_DB_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6396/0"
DEFAULT_API_BASE = "http://127.0.0.1:8090/api/v1"
DEFAULT_COMPOSE_FILE = "infra/docker-compose.midterm.yml"
DEFAULT_ENV_FILE = "infra/env/midterm.env"
DEFAULT_RTSP_URI = "rtsp://192.168.1.105:8554/live/1080movie"
DEFAULT_ARTIFACT_ROOT = Path("/data/video-analytics/artifacts")
DEFAULT_EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")
DEFAULT_REPLAY_EPOCH_ROOT = Path("/data/video-analytics/media/replay-sink-output/midterm/epochs")
DEFAULT_MODULE_CONFIG_PATH = Path("modules/savant_security/config/cameras.midterm.yml")
SOURCE_CONTROLLER = Path("scripts/runtime/camera_source_controller.py")
DUAL_SHARD_PROFILE = "dual-4090-two-source"
DUAL_SHARD_SERVICES = [
    "savant-a",
    "savant-b",
    "analysis-forwarder-a",
    "analysis-forwarder-b",
    "replay-a",
    "replay-b",
]
DUAL_SHARD_SINGLE_SERVICES = ["source-adapter", "analysis-forwarder", "savant-security"]
DUAL_SHARD_FORWARDER_METRICS = {
    "replay-a": "http://127.0.0.1:18182/metrics",
    "replay-b": "http://127.0.0.1:18183/metrics",
}
DUAL_SHARD_FORWARDER_HEALTH = {
    "replay-a": "http://127.0.0.1:18182/healthz",
    "replay-b": "http://127.0.0.1:18183/healthz",
}
DUAL_SHARD_SAVANT_METRICS = {
    "replay-a": "http://127.0.0.1:18180/metrics",
    "replay-b": "http://127.0.0.1:18181/metrics",
}
PROM_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)$"
)
PROM_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')
SAVANT_COUNTER_METRICS = {
    "va_savant_frames_seen_total",
    "va_savant_frame_annotations_exported_total",
    "va_savant_pose_stage_frames_total",
    "va_savant_pose_frames_with_person_total",
    "va_savant_pose_objects_total",
    "va_savant_face_stage_frames_total",
    "va_savant_face_frames_with_face_total",
    "va_savant_face_objects_total",
    "va_savant_adaface_embeddings_total",
    "va_savant_person_observations_exported_total",
    "va_savant_face_observations_exported_total",
}
SAVANT_GAUGE_METRICS = {
    "va_savant_effective_fps",
    "va_savant_last_frame_age_seconds",
}
ACTIVE_MATERIALIZATION_STATES = {
    "manifest_ready",
    "materialization_pending",
    "materialization_deferred",
    "materializing",
}
ACTIVE_TASK_STATES = {
    "pending",
    "waiting_proof",
    "queued",
    "replay_job_created",
    "replaying",
    "materializing",
    "finalizing",
}
TERMINAL_EVIDENCE_STATES = {
    "materialization_expired",
    "materialization_failed",
    "materialization_skipped",
}
SECURITY_STREAMS = [
    "security.events",
    "security.face_observations",
    "security.person_observations",
    "security.frame_annotations",
    "security.record_requests",
    "security.alerts",
]
DOWNSTREAM_OBSERVABILITY_SCHEMA_VERSION = 1
DOWNSTREAM_OBSERVABILITY_REQUIRED_SECTIONS = (
    "redis",
    "postgresql",
    "event_worker",
    "face_worker",
    "media_worker",
    "evidence_8090",
)
WORKER_CONTAINER_NAMES = {
    "event_worker": "video-analytics-midterm-event-worker",
    "face_worker": "video-analytics-midterm-face-worker",
    "media_worker": "video-analytics-midterm-media-worker",
    "clip_worker": "video-analytics-midterm-clip-worker",
}


@dataclass(frozen=True)
class PressureConfig:
    run_id: str
    stream_count: int
    rtsp_uri: str
    fps: str
    min_fps: str
    batch_size: int
    pose_batch_size: int
    face_detector_batch_size: int
    face_embedding_batch_size: int
    max_parallel_streams: int
    batched_push_timeout: int
    duration_s: int
    sample_interval_s: int
    drain_s: int
    guard_wait_s: int
    keep_evidence: int
    evidence_group_size: int
    evidence_policy_groups: tuple[tuple[int, int], ...]
    artifact_dir: Path
    db_url: str
    redis_url: str
    api_base: str
    compose_file: str
    env_file: str
    evidence_root: Path
    replay_epoch_root: Path
    force_runtime_restart: bool
    no_quiesce_before_guard: bool
    rtsp_republish_output_base: str
    rtsp_republish_input_uri: str
    rtsp_republish_mode: str
    rtsp_republish_warmup_s: int
    max_send_failures: int
    max_exited_sources: int
    max_validate_seq_iq: int
    forwarder_null_sink: bool
    dual_shard_same_gpu: bool
    dual_shard_api: bool
    dual_shard_gpu: str
    cleanup: bool


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a full midterm 60-source pressure test through 8090 runtime control."
    )
    parser.add_argument("--fps", default="2/1", help="Target analysis/Savant FPS, e.g. 2/1.")
    parser.add_argument("--min-fps", default="1/1")
    parser.add_argument("--streams", type=int, default=60)
    parser.add_argument("--duration-s", type=int, default=300)
    parser.add_argument("--sample-interval-s", type=int, default=30)
    parser.add_argument("--drain-s", type=int, default=300)
    parser.add_argument(
        "--guard-wait-s",
        type=int,
        default=1200,
        help="Seconds to wait for existing evidence tasks after quiescing current sources.",
    )
    parser.add_argument("--keep-evidence", type=int, default=50)
    parser.add_argument(
        "--evidence-group-size",
        type=int,
        default=10,
        help="Number of pressure cameras per evidence length group.",
    )
    parser.add_argument(
        "--evidence-policy-groups",
        default="2:2,3:3,5:5,8:8,10:10,15:15",
        help=(
            "Comma-separated pre:post seconds for pressure evidence groups. "
            "With --streams=60 and --evidence-group-size=10 this creates 6 groups."
        ),
    )
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument(
        "--pose-batch-size",
        type=int,
        default=None,
        help="YOLO26-pose nvinfer batch size. Defaults to --batch-size.",
    )
    parser.add_argument(
        "--face-detector-batch-size",
        type=int,
        default=None,
        help="YOLOv8-face detector batch size. Defaults to --batch-size.",
    )
    parser.add_argument(
        "--face-embedding-batch-size",
        type=int,
        default=16,
        help="AdaFace embedding batch size.",
    )
    parser.add_argument("--max-parallel-streams", type=int, default=64)
    parser.add_argument("--batched-push-timeout", type=int, default=40000)
    parser.add_argument("--rtsp-uri", default=DEFAULT_RTSP_URI)
    parser.add_argument("--run-id")
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL", DEFAULT_DB_URL))
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--compose-file", default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--replay-epoch-root", type=Path, default=DEFAULT_REPLAY_EPOCH_ROOT)
    parser.add_argument("--force-runtime-restart", action="store_true")
    parser.add_argument(
        "--no-quiesce-before-guard",
        action="store_true",
        help="Do not disable existing cameras before waiting for evidence guard clearance.",
    )
    parser.add_argument(
        "--rtsp-republish-output-base",
        default="",
        help=(
            "Optional RTSP output base for host ffmpeg republishers. "
            "Supports {run_id}, {index}, and {source_id}; when no placeholder is present, "
            "/{source_id} is appended. Example: rtsp://127.0.0.1:8554/pressure/{run_id}"
        ),
    )
    parser.add_argument(
        "--rtsp-republish-input-uri",
        default="",
        help="Input URI for RTSP republishers. Defaults to --rtsp-uri.",
    )
    parser.add_argument(
        "--rtsp-republish-mode",
        choices=("copy", "transcode"),
        default="copy",
        help="Use copy first to avoid extra CPU; transcode only if the RTSP server/source needs it.",
    )
    parser.add_argument(
        "--rtsp-republish-warmup-s",
        type=int,
        default=10,
        help="Seconds to let ffmpeg republishers warm up before 8090 runtime restart.",
    )
    parser.add_argument(
        "--max-send-failures",
        type=int,
        default=0,
        help="Maximum allowed analysis-forwarder Savant send failures.",
    )
    parser.add_argument(
        "--max-exited-sources",
        type=int,
        default=0,
        help="Maximum allowed pressure source containers not running during diagnostics.",
    )
    parser.add_argument(
        "--max-validate-seq-iq",
        type=int,
        default=0,
        help="Maximum allowed validate_seq_iq log lines in Savant logs.",
    )
    parser.add_argument(
        "--forwarder-null-sink",
        action="store_true",
        help=(
            "Run the pressure sources through Replay and analysis-forwarder, "
            "but count forwarded frames in a null sink instead of sending to Savant."
        ),
    )
    parser.add_argument(
        "--dual-shard-same-gpu",
        action="store_true",
        help=(
            "Run pressure sources through replay-a/b, analysis-forwarder-a/b, "
            "and savant-a/b, with both Savant branches pinned to one GPU."
        ),
    )
    parser.add_argument(
        "--dual-shard-api",
        action="store_true",
        help="Apply the dual-shard topology through the 8090 runtime topology API.",
    )
    parser.add_argument(
        "--dual-shard-gpu",
        default="0",
        help="Physical GPU id used by both Savant branches in --dual-shard-same-gpu mode.",
    )
    parser.add_argument("--no-cleanup", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.dual_shard_same_gpu and args.forwarder_null_sink:
        raise SystemExit("--dual-shard-same-gpu cannot be combined with --forwarder-null-sink")
    if args.dual_shard_api and not args.dual_shard_same_gpu:
        raise SystemExit("--dual-shard-api requires --dual-shard-same-gpu")
    if args.dual_shard_same_gpu and args.keep_evidence > 0 and not args.dual_shard_api:
        raise SystemExit(
            "--dual-shard-same-gpu with evidence retention requires "
            "--dual-shard-api so clip-worker can be wired to the topology "
            "replay shard plan."
        )
    run_id = args.run_id or _default_run_id(
        args.fps,
        forwarder_null_sink=args.forwarder_null_sink,
        dual_shard_same_gpu=args.dual_shard_same_gpu,
        dual_shard_api=args.dual_shard_api,
    )
    artifact_dir = (args.artifact_root / run_id).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    cfg = PressureConfig(
        run_id=run_id,
        stream_count=args.streams,
        rtsp_uri=args.rtsp_uri,
        fps=args.fps,
        min_fps=args.min_fps,
        batch_size=args.batch_size,
        pose_batch_size=args.pose_batch_size or args.batch_size,
        face_detector_batch_size=args.face_detector_batch_size or args.batch_size,
        face_embedding_batch_size=args.face_embedding_batch_size,
        max_parallel_streams=args.max_parallel_streams,
        batched_push_timeout=args.batched_push_timeout,
        duration_s=args.duration_s,
        sample_interval_s=args.sample_interval_s,
        drain_s=args.drain_s,
        guard_wait_s=args.guard_wait_s,
        keep_evidence=args.keep_evidence,
        evidence_group_size=args.evidence_group_size,
        evidence_policy_groups=parse_evidence_policy_groups(
            args.evidence_policy_groups
        ),
        artifact_dir=artifact_dir,
        db_url=args.db_url,
        redis_url=args.redis_url,
        api_base=args.api_base.rstrip("/"),
        compose_file=args.compose_file,
        env_file=args.env_file,
        evidence_root=args.evidence_root,
        replay_epoch_root=args.replay_epoch_root,
        force_runtime_restart=args.force_runtime_restart,
        no_quiesce_before_guard=args.no_quiesce_before_guard,
        rtsp_republish_output_base=args.rtsp_republish_output_base,
        rtsp_republish_input_uri=args.rtsp_republish_input_uri or args.rtsp_uri,
        rtsp_republish_mode=args.rtsp_republish_mode,
        rtsp_republish_warmup_s=args.rtsp_republish_warmup_s,
        max_send_failures=args.max_send_failures,
        max_exited_sources=args.max_exited_sources,
        max_validate_seq_iq=args.max_validate_seq_iq,
        forwarder_null_sink=args.forwarder_null_sink,
        dual_shard_same_gpu=args.dual_shard_same_gpu,
        dual_shard_api=args.dual_shard_api,
        dual_shard_gpu=str(args.dual_shard_gpu),
        cleanup=not args.no_cleanup,
    )
    report: dict[str, Any] = {
        "run_id": cfg.run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": _jsonable_config(cfg),
        "artifact_dir": str(cfg.artifact_dir),
        "steps": [],
    }
    write_json(cfg.artifact_dir / "run_config.json", report["config"])
    conn = psycopg.connect(cfg.db_url, row_factory=dict_row, autocommit=True)
    redis_client = Redis.from_url(cfg.redis_url, decode_responses=False)
    original_perf: dict[str, Any] | None = None
    original_topology: dict[str, Any] | None = None
    original_clip_worker_replay_shards: dict[str, str] | None = None
    original_cameras: list[dict[str, Any]] = []
    runtime_epoch_root: str | None = None
    rtsp_republishers: list[subprocess.Popen] = []
    started_at = datetime.now(timezone.utc)
    try:
        original_perf = api_json(cfg.api_base, "GET", "/runtime/performance-config")["data"][
            "saved_config"
        ]
        original_topology = api_json(cfg.api_base, "GET", "/runtime/topology-config")["data"][
            "saved_config"
        ]
        original_clip_worker_replay_shards = clip_worker_replay_shard_env_snapshot()
        original_cameras = fetch_cameras(conn)
        write_json(cfg.artifact_dir / "cameras_before.json", original_cameras)
        write_json(cfg.artifact_dir / "performance_before.json", original_perf)
        write_json(cfg.artifact_dir / "topology_before.json", original_topology)

        active = prepare_evidence_guard(conn, cfg, report)
        if active["blocking_count"] and not cfg.force_runtime_restart:
            report["status"] = "blocked_active_evidence"
            report["active_evidence"] = active
            write_json(cfg.artifact_dir / "report.json", report)
            restore_cameras(conn, original_cameras)
            apply_sources_only(cfg, "runtime_sources_apply_restore_after_guard_block.json")
            print(
                f"BLOCKED_ACTIVE_EVIDENCE blocking_count={active['blocking_count']} "
                f"details={cfg.artifact_dir / 'active_evidence_guard_snapshots.json'}"
            )
            return 2

        if cfg.dual_shard_same_gpu:
            if not cfg.dual_shard_api:
                stop_single_inference_runtime_for_dual_pressure(cfg)
        else:
            set_compose_operating_point(cfg)
        if cfg.dual_shard_same_gpu:
            write_json(
                cfg.artifact_dir / "performance_apply_pressure.json",
                {
                    "skipped": True,
                    "reason": (
                        "dual_shard_uses_8090_topology_api"
                        if cfg.dual_shard_api
                        else "dual_shard_same_gpu_uses_compose_env"
                    ),
                    "analysis_fps": cfg.fps,
                    "analysis_min_fps": cfg.min_fps,
                    "batch_size": cfg.batch_size,
                    "pose_batch_size": cfg.pose_batch_size,
                    "face_detector_batch_size": cfg.face_detector_batch_size,
                    "face_embedding_batch_size": cfg.face_embedding_batch_size,
                    "max_parallel_streams": cfg.max_parallel_streams,
                    "batched_push_timeout": cfg.batched_push_timeout,
                    "gpu": cfg.dual_shard_gpu,
                },
            )
        elif cfg.forwarder_null_sink:
            write_json(
                cfg.artifact_dir / "performance_apply_pressure.json",
                {
                    "skipped": True,
                    "reason": "forwarder_null_sink_only",
                    "analysis_fps": cfg.fps,
                    "analysis_min_fps": cfg.min_fps,
                    "forwarder_out_endpoint": "null://pressure",
                },
            )
        else:
            save_performance_config(cfg, original_perf)
        rtsp_republishers = start_rtsp_republishers(cfg)
        insert_pressure_cameras(conn, cfg)
        if cfg.dual_shard_same_gpu:
            if cfg.dual_shard_api:
                topology = save_and_apply_topology_config(cfg)
                clip_worker_shards = configure_clip_worker_for_topology_shards(
                    cfg,
                    topology,
                )
                write_json(cfg.artifact_dir / "runtime_restart_pressure.json", topology)
                report["steps"].append(
                    {
                        "name": "dual_shard_8090_topology_started",
                        "mode": topology.get("mode"),
                        "runtime_epoch_id": topology.get("runtime_epoch_id"),
                        "sources_config_path": topology.get("sources_config_path"),
                        "replay_shards_path": topology.get("replay_shards_path"),
                        "clip_worker_replay_shards": clip_worker_shards,
                    }
                )
            else:
                module_config = sync_module_config_snapshot(cfg)
                shard_plan = write_dual_shard_pressure_sources(conn, cfg)
                start_dual_shard_runtime(cfg)
                start_pressure_sources_from_manifest(
                    cfg,
                    sources_path=Path(str(shard_plan["sources_path"])),
                )
                report["steps"].append(
                    {
                        "name": "dual_shard_same_gpu_started",
                        "module_config_path": module_config["module_config_path"],
                        "sources_path": shard_plan["sources_path"],
                        "gpu": cfg.dual_shard_gpu,
                        "shards": shard_plan["shards"],
                    }
                )
        elif cfg.forwarder_null_sink:
            restart = apply_sources_only(cfg, "runtime_sources_apply_pressure.json")
            write_json(cfg.artifact_dir / "runtime_restart_pressure.json", restart)
            report["steps"].append({"name": "forwarder_null_sources_started"})
        else:
            restart = restart_camera_runtime(cfg)
            runtime_epoch_root = restart.get("runtime_epoch_root")
            write_json(cfg.artifact_dir / "runtime_restart_pressure.json", restart)
            report["steps"].append({"name": "runtime_started", "runtime_epoch_root": runtime_epoch_root})

        sample_runtime(cfg, started_at)
        diagnostics = collect_pressure_diagnostics(cfg)
        report["diagnostics"] = diagnostics
        stop_pressure_sources(conn, cfg)
        stop_rtsp_republishers(rtsp_republishers, cfg)
        rtsp_republishers = []
        kept: list[dict[str, Any]] = []
        if cfg.forwarder_null_sink:
            report["kept_evidence_count"] = 0
            report["kept_evidence"] = []
            report["steps"].append({"name": "evidence_drain_skipped", "reason": "forwarder_null_sink"})
        else:
            wait_for_drain(cfg)
            kept = select_kept_evidence(conn, cfg)
            write_kept_csv(cfg, kept)
            report["kept_evidence_count"] = len(kept)
            report["kept_evidence"] = kept[: cfg.keep_evidence]
        capture_runtime_logs_since_start(cfg, started_at)
        diagnostics["log_summary"] = summarize_logs(cfg)
        write_json(cfg.artifact_dir / "pressure_diagnostics.json", diagnostics)
        report["db_summary_before_cleanup"] = db_summary(conn, cfg.run_id)
        write_json(cfg.artifact_dir / "db_summary_before_cleanup.json", report["db_summary_before_cleanup"])
        report["downstream_observability"] = collect_downstream_observability(
            cfg,
            conn,
            redis_client,
            kept=kept,
            diagnostics=diagnostics,
        )
        report["failure_reasons"] = pressure_failure_reasons(cfg, kept, diagnostics)
        report["warnings"] = pressure_warnings(
            cfg,
            diagnostics,
            report["failure_reasons"],
        )

        keep_event_ids = {str(row["event_id"]) for row in kept}
        if cfg.cleanup:
            cleanup = cleanup_pressure_data(
                conn,
                redis_client,
                cfg,
                keep_event_ids=keep_event_ids,
                runtime_epoch_root=runtime_epoch_root,
            )
            report["cleanup"] = cleanup
        restore_cameras(conn, original_cameras)
        restore_runtime(cfg, original_perf)
        if original_clip_worker_replay_shards is not None:
            report["clip_worker_replay_shards_restore"] = configure_clip_worker_replay_shards(
                cfg,
                replay_shards_json=original_clip_worker_replay_shards.get(
                    "REPLAY_SHARDS_JSON",
                    "",
                ),
                replay_shards_config_path=original_clip_worker_replay_shards.get(
                    "REPLAY_SHARDS_CONFIG_PATH",
                    "",
                ),
                artifact_name="compose_restore_clip_worker_replay_shards.log",
            )
        if original_topology is not None:
            api_json(cfg.api_base, "PUT", "/runtime/topology-config", original_topology)
        if cfg.cleanup:
            report["cleanup_after_restore"] = cleanup_pressure_data(
                conn,
                redis_client,
                cfg,
                keep_event_ids=keep_event_ids,
                runtime_epoch_root=runtime_epoch_root,
            )
        report["db_summary_after_cleanup"] = db_summary(conn, cfg.run_id)
        report["runtime_after_restore"] = api_json(cfg.api_base, "GET", "/runtime/overview")["data"]
        report["status"] = "passed" if not report["failure_reasons"] else "failed_pressure_gates"
        write_json(cfg.artifact_dir / "report.json", report)
        print(f"PRESSURE_RUN_STATUS={report['status']}")
        print(f"RUN_ID={cfg.run_id}")
        print(f"ARTIFACT_DIR={cfg.artifact_dir}")
        print(f"KEPT_EVIDENCE={len(kept)}")
        if report["failure_reasons"]:
            print("FAILURE_REASONS=" + ",".join(report["failure_reasons"]))
        if report["warnings"]:
            print("WARNINGS=" + ",".join(report["warnings"]))
        return 0 if report["status"] == "passed" else 1
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        write_json(cfg.artifact_dir / "report.json", report)
        print(f"PRESSURE_RUN_INTERRUPTED artifact_dir={cfg.artifact_dir}", file=sys.stderr)
        try:
            stop_rtsp_republishers(rtsp_republishers, cfg)
            cleanup_after_aborted_run(
                conn,
                redis_client,
                cfg,
                report,
                runtime_epoch_root=runtime_epoch_root,
            )
            if original_cameras:
                restore_cameras(conn, original_cameras)
            if original_perf:
                restore_runtime(cfg, original_perf)
            if original_clip_worker_replay_shards is not None:
                configure_clip_worker_replay_shards(
                    cfg,
                    replay_shards_json=original_clip_worker_replay_shards.get(
                        "REPLAY_SHARDS_JSON",
                        "",
                    ),
                    replay_shards_config_path=original_clip_worker_replay_shards.get(
                        "REPLAY_SHARDS_CONFIG_PATH",
                        "",
                    ),
                    artifact_name="compose_restore_clip_worker_replay_shards_after_interrupt.log",
                )
            if original_topology is not None:
                api_json(cfg.api_base, "PUT", "/runtime/topology-config", original_topology)
        finally:
            return 130
    except Exception as exc:
        report["status"] = "failed_exception"
        report["error"] = repr(exc)
        write_json(cfg.artifact_dir / "report.json", report)
        print(f"PRESSURE_RUN_FAILED error={exc!r} artifact_dir={cfg.artifact_dir}", file=sys.stderr)
        try:
            stop_rtsp_republishers(rtsp_republishers, cfg)
            cleanup_after_aborted_run(
                conn,
                redis_client,
                cfg,
                report,
                runtime_epoch_root=runtime_epoch_root,
            )
            if original_cameras:
                restore_cameras(conn, original_cameras)
            if original_perf:
                restore_runtime(cfg, original_perf)
            if original_clip_worker_replay_shards is not None:
                configure_clip_worker_replay_shards(
                    cfg,
                    replay_shards_json=original_clip_worker_replay_shards.get(
                        "REPLAY_SHARDS_JSON",
                        "",
                    ),
                    replay_shards_config_path=original_clip_worker_replay_shards.get(
                        "REPLAY_SHARDS_CONFIG_PATH",
                        "",
                    ),
                    artifact_name="compose_restore_clip_worker_replay_shards_after_failure.log",
                )
            if original_topology is not None:
                api_json(cfg.api_base, "PUT", "/runtime/topology-config", original_topology)
        finally:
            return 1
    finally:
        conn.close()


def _default_run_id(
    fps: str,
    *,
    forwarder_null_sink: bool = False,
    dual_shard_same_gpu: bool = False,
    dual_shard_api: bool = False,
) -> str:
    fps_label = fps.replace("/", "p").replace(".", "_")
    if dual_shard_same_gpu and dual_shard_api:
        prefix = "pressure60_8090topology_dual1gpu"
    elif dual_shard_same_gpu:
        prefix = "pressure60_dual1gpu"
    elif forwarder_null_sink:
        prefix = "forwarder60_null"
    else:
        prefix = "pressure60"
    return f"{prefix}_{fps_label}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def _jsonable_config(cfg: PressureConfig) -> dict[str, Any]:
    data = cfg.__dict__.copy()
    for key, value in list(data.items()):
        if isinstance(value, Path):
            data[key] = str(value)
    return data


def parse_evidence_policy_groups(value: str) -> tuple[tuple[int, int], ...]:
    groups: list[tuple[int, int]] = []
    for raw_item in str(value or "").split(","):
        item = raw_item.strip()
        if not item:
            continue
        if ":" in item:
            pre_raw, post_raw = item.split(":", 1)
        elif "/" in item:
            pre_raw, post_raw = item.split("/", 1)
        else:
            pre_raw = post_raw = item
        try:
            pre_seconds = int(pre_raw)
            post_seconds = int(post_raw)
        except ValueError as exc:
            raise ValueError(f"invalid evidence policy group: {item!r}") from exc
        if pre_seconds < 0 or post_seconds < 0:
            raise ValueError(f"evidence policy group must be non-negative: {item!r}")
        groups.append((pre_seconds, post_seconds))
    if not groups:
        raise ValueError("at least one evidence policy group is required")
    return tuple(groups)


def evidence_policy_for_index(cfg: PressureConfig, index: int) -> dict[str, Any]:
    group_size = max(1, int(cfg.evidence_group_size))
    group_index = min(int(index) // group_size, len(cfg.evidence_policy_groups) - 1)
    pre_seconds, post_seconds = cfg.evidence_policy_groups[group_index]
    return {
        "pre_seconds": pre_seconds,
        "post_seconds": post_seconds,
        "clip_required": True,
        "snapshot_required": False,
        "pressure_group_index": group_index,
        "pressure_group_size": group_size,
        "pressure_total_seconds": pre_seconds + post_seconds,
    }


def active_evidence_tasks(conn) -> dict[str, Any]:
    blocking_states = sorted(ACTIVE_TASK_STATES)
    terminal_states = sorted(TERMINAL_EVIDENCE_STATES)
    rows = conn.execute(
        """
        WITH base AS (
          SELECT et.task_id,
                 et.event_id,
                 et.source_id,
                 et.event_type,
                 et.status,
                 et.materialization_status,
                 e.payload->'media'->>'evidence_state' AS event_evidence_state,
                 e.media_status,
                 e.payload->'media'->>'clip_status' AS event_clip_status,
                 (
                   et.status = ANY(%s)
                   OR et.materialization_status = ANY(%s)
                   OR e.payload->'media'->>'evidence_state' = ANY(%s)
                   OR e.media_status = ANY(%s)
                 ) AS terminal_evidence_state,
                 et.created_at,
                 et.updated_at,
                 (
                   SELECT max(deadline_at)
                   FROM (
                     VALUES
                       (et.materialization_deadline_at),
                       (et.replay_deadline_at),
                       (et.annotation_deadline_at)
                   ) AS deadlines(deadline_at)
                 ) AS latest_deadline_at
          FROM evidence_tasks et
          LEFT JOIN events e ON e.id = et.event_id
        )
        SELECT *,
               CASE
                 WHEN latest_deadline_at IS NOT NULL
                  AND latest_deadline_at <= now()
                  AND updated_at <= now() - interval '15 minutes'
                 THEN true
                 ELSE false
               END AS stale_nonblocking
        FROM base
        WHERE NOT terminal_evidence_state
          AND (
            status = ANY(%s)
            OR materialization_status = ANY(%s)
            OR event_evidence_state = ANY(%s)
            OR media_status = ANY(%s)
            OR event_clip_status = ANY(%s)
          )
        ORDER BY created_at
        """,
        (
            terminal_states,
            terminal_states,
            terminal_states,
            terminal_states,
            blocking_states,
            blocking_states,
            blocking_states,
            blocking_states,
            blocking_states,
        ),
    ).fetchall()
    blocking = [row for row in rows if not row.get("stale_nonblocking")]
    return {
        "active_count": len(rows),
        "blocking_count": len(blocking),
        "tasks": [_row_json(row) for row in rows],
    }


def prepare_evidence_guard(conn, cfg: PressureConfig, report: dict[str, Any]) -> dict[str, Any]:
    before = active_evidence_tasks(conn)
    write_json(cfg.artifact_dir / "active_evidence_before.json", before)
    report["active_evidence_before"] = before
    if cfg.force_runtime_restart:
        return before
    if not cfg.no_quiesce_before_guard:
        quiesce_existing_sources(conn, cfg)
        report["steps"].append({"name": "existing_sources_quiesced"})
    return wait_for_evidence_guard_clear(conn, cfg)


def quiesce_existing_sources(conn, cfg: PressureConfig) -> None:
    with conn.transaction():
        conn.execute(
            """
            UPDATE cameras
            SET enabled=false, updated_at=now()
            WHERE source_id NOT LIKE %s
            """,
            (f"{cfg.run_id}_%",),
        )
    apply_sources_only(cfg, "runtime_sources_apply_quiesce_before_guard.json")


def wait_for_evidence_guard_clear(conn, cfg: PressureConfig) -> dict[str, Any]:
    deadline = time.time() + max(0, cfg.guard_wait_s)
    snapshots: list[dict[str, Any]] = []
    latest = active_evidence_tasks(conn)
    while True:
        latest = active_evidence_tasks(conn)
        snapshots.append(
            {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "active_count": latest["active_count"],
                "blocking_count": latest["blocking_count"],
                "tasks": latest["tasks"],
            }
        )
        if not latest["blocking_count"]:
            break
        if time.time() >= deadline:
            break
        time.sleep(min(10, max(1, deadline - time.time())))
    write_json(cfg.artifact_dir / "active_evidence_guard_snapshots.json", snapshots)
    return latest


def fetch_cameras(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, source_id, enabled, rtsp_url FROM cameras ORDER BY name"
    ).fetchall()
    return [_row_json(row) for row in rows]


def set_compose_operating_point(cfg: PressureConfig) -> None:
    env = os.environ.copy()
    updates = {
        "BATCH_SIZE": str(cfg.batch_size),
        "POSE_BATCH_SIZE": str(cfg.pose_batch_size),
        "FACE_DETECTOR_BATCH_SIZE": str(cfg.face_detector_batch_size),
        "FACE_EMBEDDING_BATCH_SIZE": str(cfg.face_embedding_batch_size),
        "MAX_PARALLEL_STREAMS": str(cfg.max_parallel_streams),
        "BATCHED_PUSH_TIMEOUT": str(cfg.batched_push_timeout),
        "ANALYSIS_FPS": cfg.fps,
        "ANALYSIS_MIN_FPS": cfg.min_fps,
        "MAX_FPS": cfg.fps,
        "MIN_FPS": cfg.min_fps,
    }
    services = ["analysis-forwarder", "savant-security"]
    if cfg.forwarder_null_sink:
        updates["FORWARDER_OUT_ENDPOINT"] = "null://pressure"
        services = ["analysis-forwarder"]
    env.update(updates)
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            *services,
        ],
        cfg.artifact_dir / "compose_recreate_pressure.log",
        env=env,
    )


def stop_single_inference_runtime_for_dual_pressure(cfg: PressureConfig) -> None:
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            "stop",
            *DUAL_SHARD_SINGLE_SERVICES,
        ],
        cfg.artifact_dir / "compose_stop_single_for_dual_pressure.log",
        check=False,
    )


def start_dual_shard_runtime(cfg: PressureConfig) -> None:
    override_path = write_dual_shard_same_gpu_compose_override(cfg)
    env = os.environ.copy()
    env.update(
        {
            "BATCH_SIZE": str(cfg.batch_size),
            "POSE_BATCH_SIZE": str(cfg.pose_batch_size),
            "FACE_DETECTOR_BATCH_SIZE": str(cfg.face_detector_batch_size),
            "FACE_EMBEDDING_BATCH_SIZE": str(cfg.face_embedding_batch_size),
            "MAX_PARALLEL_STREAMS": str(cfg.max_parallel_streams),
            "BATCHED_PUSH_TIMEOUT": str(cfg.batched_push_timeout),
            "ANALYSIS_FPS": cfg.fps,
            "ANALYSIS_MIN_FPS": cfg.min_fps,
            "MAX_FPS": cfg.fps,
            "MIN_FPS": cfg.min_fps,
            "SAVANT_A_NVIDIA_VISIBLE_DEVICES": cfg.dual_shard_gpu,
            "SAVANT_B_NVIDIA_VISIBLE_DEVICES": cfg.dual_shard_gpu,
            "SAVANT_B_CUDA_VISIBLE_DEVICES": "0",
            "SAVANT_B_MODEL_ROOT": "/data/video-analytics/models",
        }
    )
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            "-f",
            str(override_path),
            "--profile",
            DUAL_SHARD_PROFILE,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            *DUAL_SHARD_SERVICES,
        ],
        cfg.artifact_dir / "compose_recreate_dual_shard_same_gpu.log",
        env=env,
    )
    wait_for_dual_shard_metrics(cfg)


def stop_dual_shard_runtime(cfg: PressureConfig) -> None:
    override_path = cfg.artifact_dir / "compose.dual-shard-same-gpu.override.yml"
    compose = [
        "docker",
        "compose",
        "--env-file",
        cfg.env_file,
        "-f",
        cfg.compose_file,
    ]
    if override_path.exists():
        compose.extend(["-f", str(override_path)])
    compose.extend(["--profile", DUAL_SHARD_PROFILE, "stop", *DUAL_SHARD_SERVICES])
    run(
        compose,
        cfg.artifact_dir / "compose_stop_dual_shard_same_gpu.log",
        check=False,
    )


def write_dual_shard_same_gpu_compose_override(cfg: PressureConfig) -> Path:
    path = cfg.artifact_dir / "compose.dual-shard-same-gpu.override.yml"
    device = str(cfg.dual_shard_gpu)
    doc = {
        "services": {
            "savant-a": {
                "environment": {
                    "NVIDIA_VISIBLE_DEVICES": device,
                    "BATCH_SIZE": str(cfg.batch_size),
                    "POSE_BATCH_SIZE": str(cfg.pose_batch_size),
                    "FACE_DETECTOR_BATCH_SIZE": str(cfg.face_detector_batch_size),
                    "FACE_EMBEDDING_BATCH_SIZE": str(cfg.face_embedding_batch_size),
                    "MAX_PARALLEL_STREAMS": str(cfg.max_parallel_streams),
                    "BATCHED_PUSH_TIMEOUT": str(cfg.batched_push_timeout),
                },
                "deploy": {
                    "resources": {
                        "reservations": {
                            "devices": [
                                {
                                    "driver": "nvidia",
                                    "device_ids": [device],
                                    "capabilities": ["gpu"],
                                }
                            ]
                        }
                    }
                },
            },
            "savant-b": {
                "environment": {
                    "NVIDIA_VISIBLE_DEVICES": device,
                    "CUDA_VISIBLE_DEVICES": "0",
                    "BATCH_SIZE": str(cfg.batch_size),
                    "POSE_BATCH_SIZE": str(cfg.pose_batch_size),
                    "FACE_DETECTOR_BATCH_SIZE": str(cfg.face_detector_batch_size),
                    "FACE_EMBEDDING_BATCH_SIZE": str(cfg.face_embedding_batch_size),
                    "MAX_PARALLEL_STREAMS": str(cfg.max_parallel_streams),
                    "BATCHED_PUSH_TIMEOUT": str(cfg.batched_push_timeout),
                },
                "deploy": {
                    "resources": {
                        "reservations": {
                            "devices": [
                                {
                                    "driver": "nvidia",
                                    "device_ids": [device],
                                    "capabilities": ["gpu"],
                                }
                            ]
                        }
                    }
                },
            },
        }
    }
    write_text(path, yaml.safe_dump(doc, sort_keys=False))
    return path


def wait_for_dual_shard_metrics(cfg: PressureConfig) -> None:
    urls = {
        **{
            f"{shard_id}-forwarder-health": url
            for shard_id, url in DUAL_SHARD_FORWARDER_HEALTH.items()
        },
        **{
            f"{shard_id}-savant-metrics": url
            for shard_id, url in DUAL_SHARD_SAVANT_METRICS.items()
        },
    }
    deadline = time.time() + 300
    pending = dict(urls)
    observations: list[dict[str, Any]] = []
    while pending and time.time() < deadline:
        for name, url in list(pending.items()):
            try:
                body = fetch_text_url(url, timeout_s=3)
            except Exception as exc:
                observations.append(
                    {
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "name": name,
                        "url": url,
                        "ready": False,
                        "error": repr(exc),
                    }
                )
                continue
            if body:
                observations.append(
                    {
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "name": name,
                        "url": url,
                        "ready": True,
                    }
                )
                pending.pop(name, None)
        if pending:
            time.sleep(2)
    write_json(
        cfg.artifact_dir / "dual_shard_metrics_ready.json",
        {"pending": pending, "observations": observations[-100:]},
    )
    if pending:
        raise RuntimeError(f"dual shard metrics endpoints not ready: {pending}")


def save_performance_config(cfg: PressureConfig, original: dict[str, Any]) -> None:
    payload = dict(original)
    payload.update(
        {
            "forwarder_sampler_enabled": True,
            "analysis_fps": cfg.fps,
            "analysis_min_fps": cfg.min_fps,
            "ingress_fps_gate_enabled": True,
            "savant_max_fps": cfg.fps,
            "savant_min_fps": cfg.min_fps,
            "savant_batch_size": cfg.batch_size,
            "pose_batch_size": cfg.pose_batch_size,
            "face_detector_batch_size": cfg.face_detector_batch_size,
            "face_embedding_batch_size": cfg.face_embedding_batch_size,
            "max_parallel_streams": cfg.max_parallel_streams,
            "batched_push_timeout": cfg.batched_push_timeout,
        }
    )
    write_json(cfg.artifact_dir / "performance_pressure_payload.json", payload)
    write_json(
        cfg.artifact_dir / "performance_save_pressure.json",
        api_json(cfg.api_base, "PUT", "/runtime/performance-config", payload),
    )
    write_json(
        cfg.artifact_dir / "performance_apply_pressure.json",
        api_json(
            cfg.api_base,
            "POST",
            f"/runtime/performance-config/apply?force={str(cfg.force_runtime_restart).lower()}",
        ),
    )


def topology_pressure_payload(cfg: PressureConfig) -> dict[str, Any]:
    branch = {
        "gpu_id": int(cfg.dual_shard_gpu),
        "savant_batch_size": cfg.batch_size,
        "pose_batch_size": cfg.pose_batch_size,
        "face_detector_batch_size": cfg.face_detector_batch_size,
        "face_embedding_batch_size": cfg.face_embedding_batch_size,
        "max_parallel_streams": cfg.max_parallel_streams,
        "analysis_fps": cfg.fps,
        "analysis_min_fps": cfg.min_fps,
        "savant_max_fps": cfg.fps,
        "savant_min_fps": cfg.min_fps,
        "batched_push_timeout": cfg.batched_push_timeout,
    }
    return {
        "topology_mode": "dual_same_gpu",
        "shard_strategy": "balanced",
        "streams_per_branch": max(1, cfg.stream_count // 2),
        "branches": {
            "a": dict(branch),
            "b": dict(branch),
        },
    }


def save_and_apply_topology_config(cfg: PressureConfig) -> dict[str, Any]:
    payload = topology_pressure_payload(cfg)
    write_json(cfg.artifact_dir / "topology_pressure_payload.json", payload)
    write_json(
        cfg.artifact_dir / "topology_save_pressure.json",
        api_json(cfg.api_base, "PUT", "/runtime/topology-config", payload, timeout_s=60),
    )
    response = api_json(
        cfg.api_base,
        "POST",
        f"/runtime/topology-config/apply?force={str(cfg.force_runtime_restart).lower()}",
        timeout_s=900,
    )
    write_json(cfg.artifact_dir / "topology_apply_pressure.json", response)
    if response.get("error"):
        raise RuntimeError(f"runtime topology apply failed: {response['error']}")
    return response["data"]


def docker_container_env(name: str) -> dict[str, str]:
    completed = subprocess.run(
        ["docker", "inspect", name, "--format", "{{json .Config.Env}}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        return {}
    try:
        values = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    result: dict[str, str] = {}
    for item in values or []:
        key, sep, value = str(item).partition("=")
        if sep:
            result[key] = value
    return result


def clip_worker_replay_shard_env_snapshot() -> dict[str, str]:
    env = docker_container_env("video-analytics-midterm-clip-worker")
    return {
        "REPLAY_SHARDS_JSON": env.get("REPLAY_SHARDS_JSON", ""),
        "REPLAY_SHARDS_CONFIG_PATH": env.get("REPLAY_SHARDS_CONFIG_PATH", ""),
    }


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def configure_clip_worker_replay_shards(
    cfg: PressureConfig,
    *,
    replay_shards_json: str,
    replay_shards_config_path: str,
    artifact_name: str,
) -> dict[str, Any]:
    env = os.environ.copy()
    env.update(
        {
            "REPLAY_SHARDS_JSON": replay_shards_json,
            "REPLAY_SHARDS_CONFIG_PATH": replay_shards_config_path,
        }
    )
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "clip-worker",
        ],
        cfg.artifact_dir / artifact_name,
        env=env,
    )
    digest = hashlib.sha256(replay_shards_json.encode("utf-8")).hexdigest() if replay_shards_json else ""
    observed = clip_worker_replay_shard_env_snapshot()
    observed_json = observed.get("REPLAY_SHARDS_JSON", "")
    observed_config_path = observed.get("REPLAY_SHARDS_CONFIG_PATH", "")
    summary = {
        "container": "video-analytics-midterm-clip-worker",
        "replay_shards_json_sha256": digest,
        "replay_shards_config_path": replay_shards_config_path,
        "replay_shards_json_bytes": len(replay_shards_json.encode("utf-8")),
        "observed_replay_shards_json_sha256": (
            hashlib.sha256(observed_json.encode("utf-8")).hexdigest()
            if observed_json
            else ""
        ),
        "observed_replay_shards_config_path": observed_config_path,
        "observed_replay_shards_json_bytes": len(observed_json.encode("utf-8")),
    }
    write_json(cfg.artifact_dir / artifact_name.replace(".log", ".json"), summary)
    if observed_json != replay_shards_json:
        raise RuntimeError(
            "clip-worker REPLAY_SHARDS_JSON was not applied after compose recreate"
        )
    if observed_config_path != replay_shards_config_path:
        raise RuntimeError(
            "clip-worker REPLAY_SHARDS_CONFIG_PATH was not applied after compose recreate"
        )
    return summary


def configure_clip_worker_for_topology_shards(
    cfg: PressureConfig,
    topology: dict[str, Any],
) -> dict[str, Any]:
    replay_shards_path = Path(str(topology.get("replay_shards_path") or ""))
    if not replay_shards_path.exists():
        raise RuntimeError(f"topology replay shard file not found: {replay_shards_path}")
    replay_shards_doc = json.loads(replay_shards_path.read_text(encoding="utf-8"))
    replay_shards_json = _compact_json(replay_shards_doc)
    summary = configure_clip_worker_replay_shards(
        cfg,
        replay_shards_json=replay_shards_json,
        replay_shards_config_path="",
        artifact_name="compose_recreate_clip_worker_replay_shards.log",
    )
    summary.update(
        {
            "topology_replay_shards_path": str(replay_shards_path),
            "topology_replay_shards_sha256": hashlib.sha256(
                replay_shards_json.encode("utf-8")
            ).hexdigest(),
            "shard_count": len(replay_shards_doc.get("shards") or []),
            "source_count": sum(
                len(shard.get("source_ids") or [])
                for shard in replay_shards_doc.get("shards") or []
                if isinstance(shard, dict)
            ),
        }
    )
    write_json(cfg.artifact_dir / "clip_worker_replay_shards_pressure.json", summary)
    return summary


def insert_pressure_cameras(conn, cfg: PressureConfig) -> None:
    with conn.transaction():
        conn.execute("UPDATE cameras SET enabled=false, updated_at=now()")
        for index in range(cfg.stream_count):
            camera_id = uuid.uuid4()
            source_id = f"{cfg.run_id}_{index:02d}"
            zone_id = f"{source_id}_full_frame"
            camera_name = f"pressure {index:02d}"
            rtsp_uri = pressure_rtsp_uri(cfg, index=index, source_id=source_id)
            evidence_policy = evidence_policy_for_index(cfg, index)
            metadata = {
                "pressure_run_id": cfg.run_id,
                "pressure_index": index,
                "pressure_group_index": evidence_policy["pressure_group_index"],
                "pressure_group_size": evidence_policy["pressure_group_size"],
                "pressure_evidence_pre_seconds": evidence_policy["pre_seconds"],
                "pressure_evidence_post_seconds": evidence_policy["post_seconds"],
                "pressure_evidence_total_seconds": evidence_policy[
                    "pressure_total_seconds"
                ],
                "created_by": "run_midterm_pressure60.py",
                "rtsp_republished": bool(cfg.rtsp_republish_output_base),
            }
            points = [[0.0, 0.0], [1920.0, 0.0], [1920.0, 1080.0], [0.0, 1080.0]]
            conn.execute(
                """
                INSERT INTO cameras (id, name, stream_url, enabled, metadata, source_id, rtsp_url, site_id, location, gpu_id, input_type, rtsp_transport, fps_policy, alert_policy)
                VALUES (%s, %s, %s, true, %s::jsonb, %s, %s, 'pressure', %s, 0, 'rtsp', 'tcp', '{}'::jsonb, '{}'::jsonb)
                """,
                (
                    camera_id,
                    camera_name,
                    rtsp_uri,
                    json.dumps(metadata),
                    source_id,
                    rtsp_uri,
                    f"pressure-{index:02d}",
                ),
            )
            conn.execute(
                """
                INSERT INTO camera_zones (camera_id, name, polygon, enabled, zone_id, zone_name, zone_type, coordinate_space, points, payload)
                VALUES (%s, %s, %s::jsonb, true, %s, %s, 'polygon', 'pixel', %s::jsonb, '{}'::jsonb)
                """,
                (camera_id, "full frame", json.dumps(points), zone_id, "full frame", json.dumps(points)),
            )
            intrusion_config = {
                "zone": zone_id,
                "zone_id": zone_id,
                "severity": "medium",
                "cooldown_s": 30,
                "min_inside_ms": 1000,
                "min_person_width": 20,
                "min_person_height": 40,
                "snapshot_required": False,
                "clip_required": True,
                "max_bbox_area_ratio": 0.9,
                "min_person_confidence": 0.25,
                "min_visible_keypoints": 0,
            }
            watchlist_config = {
                "severity": "medium",
                "threshold": 0.60,
                "min_similarity": 0.60,
                "cooldown_s": 30,
                "watchlist_enabled": True,
                "live_search_enabled": True,
                "target_names": ["Reese", "Finch"],
                "target_external_person_ids": ["demo:midterm:reese", "demo:midterm:finch"],
            }
            conn.execute(
                """
                INSERT INTO camera_rules (camera_id, zone_id, rule_type, config, enabled, rule_id, algorithm_id, evidence_policy)
                VALUES (%s, %s, 'intrusion', %s::jsonb, true, %s, 'behavior.intrusion', %s::jsonb)
                """,
                (
                    camera_id,
                    zone_id,
                    json.dumps(intrusion_config),
                    f"{source_id}_intrusion",
                    json.dumps(evidence_policy),
                ),
            )
            conn.execute(
                """
                INSERT INTO camera_rules (camera_id, zone_id, rule_type, config, enabled, rule_id, algorithm_id, evidence_policy)
                VALUES (%s, NULL, 'face.watchlist', %s::jsonb, true, %s, 'face.watchlist', %s::jsonb)
                """,
                (
                    camera_id,
                    json.dumps(watchlist_config),
                    f"{source_id}_watchlist",
                    json.dumps(evidence_policy),
                ),
            )


def sync_module_config_snapshot(cfg: PressureConfig) -> dict[str, Any]:
    body = api_text(cfg.api_base, "/cameras/config/export?include_disabled=true", timeout_s=60)
    artifact_path = cfg.artifact_dir / "cameras.midterm.pressure.yml"
    write_text(artifact_path, body)
    module_path = DEFAULT_MODULE_CONFIG_PATH
    _atomic_write_text(module_path, body)
    return {
        "module_config_path": str(module_path),
        "artifact_config_path": str(artifact_path),
    }


def write_dual_shard_pressure_sources(conn, cfg: PressureConfig) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, name, source_id, rtsp_url, enabled
        FROM cameras
        WHERE source_id LIKE %s
        ORDER BY source_id
        """,
        (f"{cfg.run_id}_%",),
    ).fetchall()
    if len(rows) != cfg.stream_count:
        raise RuntimeError(
            f"expected {cfg.stream_count} pressure cameras, found {len(rows)}"
        )
    split_at = max(1, cfg.stream_count // 2)
    source_ids_by_shard = {"replay-a": [], "replay-b": []}
    sources: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        camera = _row_json(row)
        source_id = str(camera["source_id"])
        shard_id = "replay-a" if index < split_at else "replay-b"
        source_ids_by_shard[shard_id].append(source_id)
        sources[str(camera["id"])] = {
            "camera_id": str(camera["id"]),
            "source_id": source_id,
            "uri": str(camera["rtsp_url"]),
            "enabled": bool(camera.get("enabled", True)),
            "adapter_type": "gstreamer",
            "zmq_endpoint": f"dealer+connect:tcp://{shard_id}:5555",
            "replay_shard_id": shard_id,
            "camera_name": str(camera.get("name") or ""),
        }
    sources_path = cfg.artifact_dir / "sources.dual-shard.generated.yml"
    write_text(
        sources_path,
        yaml.safe_dump({"sources": sources}, sort_keys=False, allow_unicode=True),
    )
    shard_plan = {
        "default_shard_id": "replay-a",
        "shards": [
            {
                "shard_id": "replay-a",
                "replay_api_url": "http://replay-a:8080",
                "in_stream_endpoint": "dealer+connect:tcp://replay-a:5555",
                "replay_job_sink_url": "dealer+connect:tcp://video-file-sink-a:6666",
                "source_ids": source_ids_by_shard["replay-a"],
            },
            {
                "shard_id": "replay-b",
                "replay_api_url": "http://replay-b:8080",
                "in_stream_endpoint": "dealer+connect:tcp://replay-b:5555",
                "replay_job_sink_url": "dealer+connect:tcp://video-file-sink-b:6666",
                "source_ids": source_ids_by_shard["replay-b"],
            },
        ],
    }
    shard_plan_path = cfg.artifact_dir / "replay_shards.dual-shard.pressure.json"
    write_json(shard_plan_path, shard_plan)
    result = {
        "sources_path": str(sources_path),
        "shard_plan_path": str(shard_plan_path),
        "shards": {
            "replay-a": len(source_ids_by_shard["replay-a"]),
            "replay-b": len(source_ids_by_shard["replay-b"]),
        },
    }
    write_json(cfg.artifact_dir / "dual_shard_source_plan.json", result)
    return result


def pressure_rtsp_uri(cfg: PressureConfig, *, index: int, source_id: str) -> str:
    if not cfg.rtsp_republish_output_base:
        return cfg.rtsp_uri
    base = cfg.rtsp_republish_output_base.rstrip("/")
    mapping = {
        "run_id": cfg.run_id,
        "index": f"{index:02d}",
        "source_id": source_id,
    }
    if re.search(r"\{(run_id|index|source_id)\}", base):
        return base.format(**mapping)
    return f"{base}/{source_id}"


def start_rtsp_republishers(cfg: PressureConfig) -> list[subprocess.Popen]:
    if not cfg.rtsp_republish_output_base:
        return []
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("rtsp republish requested but host ffmpeg is not available")
    log_dir = cfg.artifact_dir / "rtsp_republish"
    log_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    processes: list[subprocess.Popen] = []
    for index in range(cfg.stream_count):
        source_id = f"{cfg.run_id}_{index:02d}"
        output_uri = pressure_rtsp_uri(cfg, index=index, source_id=source_id)
        command = rtsp_republish_command(
            ffmpeg=ffmpeg,
            input_uri=cfg.rtsp_republish_input_uri,
            output_uri=output_uri,
            mode=cfg.rtsp_republish_mode,
        )
        log_path = log_dir / f"{source_id}.log"
        log_fh = log_path.open("wb")
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_fh.close()
        processes.append(proc)
        manifest.append(
            {
                "source_id": source_id,
                "input_uri": cfg.rtsp_republish_input_uri,
                "output_uri": output_uri,
                "mode": cfg.rtsp_republish_mode,
                "pid": proc.pid,
                "log_path": str(log_path),
                "command": command,
            }
        )
    write_json(cfg.artifact_dir / "rtsp_republish_manifest.json", manifest)
    if cfg.rtsp_republish_warmup_s > 0:
        time.sleep(cfg.rtsp_republish_warmup_s)
    failed = [
        {"pid": proc.pid, "returncode": proc.poll()}
        for proc in processes
        if proc.poll() is not None
    ]
    if failed:
        write_json(cfg.artifact_dir / "rtsp_republish_start_failures.json", failed)
        stop_rtsp_republishers(processes, cfg)
        raise RuntimeError(f"rtsp republishers exited during warmup: {failed[:5]}")
    return processes


def rtsp_republish_command(
    *,
    ffmpeg: str,
    input_uri: str,
    output_uri: str,
    mode: str,
) -> list[str]:
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-fflags",
        "+genpts",
        "-use_wallclock_as_timestamps",
        "1",
    ]
    if input_uri.startswith(("rtsp://", "rtsps://")):
        command.extend(["-rtsp_transport", "tcp"])
    command.extend(["-re", "-i", input_uri, "-map", "0:v:0", "-an"])
    if mode == "transcode":
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-pix_fmt",
                "yuv420p",
            ]
        )
    else:
        command.extend(["-c:v", "copy"])
    command.extend(
        [
            "-avoid_negative_ts",
            "make_zero",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            output_uri,
        ]
    )
    return command


def stop_rtsp_republishers(processes: list[subprocess.Popen], cfg: PressureConfig) -> None:
    if not processes:
        return
    stopped: list[dict[str, Any]] = []
    for proc in processes:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.time() + 10
    for proc in processes:
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stopped.append({"pid": proc.pid, "returncode": proc.poll()})
    write_json(cfg.artifact_dir / "rtsp_republish_stop.json", stopped)


def restart_camera_runtime(cfg: PressureConfig) -> dict[str, Any]:
    response = api_json(
        cfg.api_base,
        "POST",
        f"/cameras/runtime/restart?include_disabled=true&force={str(cfg.force_runtime_restart).lower()}",
        timeout_s=360,
    )
    if response.get("error"):
        raise RuntimeError(f"runtime restart failed: {response['error']}")
    return response["data"]


def sample_runtime(cfg: PressureConfig, started_at: datetime) -> None:
    samples_dir = cfg.artifact_dir / "samples"
    samples_dir.mkdir(exist_ok=True)
    end_at = time.time() + cfg.duration_s
    index = 0
    while True:
        now = time.time()
        if cfg.dual_shard_same_gpu:
            overview = dual_shard_runtime_overview(cfg)
        else:
            overview = api_json(cfg.api_base, "GET", "/runtime/overview", timeout_s=10)["data"]
        if isinstance(overview, dict):
            overview["_pressure_sample_observed_at"] = datetime.now(timezone.utc).isoformat()
        write_json(samples_dir / f"runtime_{index:03d}.json", overview)
        write_text(samples_dir / f"gpu_{index:03d}.csv", nvidia_smi_csv())
        write_json(samples_dir / f"docker_stats_{index:03d}.json", docker_stats_json(cfg))
        write_json(samples_dir / f"db_{index:03d}.json", db_summary_connect(cfg))
        if now >= end_at:
            break
        index += 1
        time.sleep(max(1, min(cfg.sample_interval_s, end_at - now)))
    capture_runtime_logs_since_start(cfg, started_at)


def capture_runtime_logs_since_start(cfg: PressureConfig, started_at: datetime) -> None:
    since = started_at.isoformat().replace("+00:00", "Z")
    if cfg.dual_shard_same_gpu:
        write_combined_docker_logs(
            ["video-analytics-midterm-savant-a", "video-analytics-midterm-savant-b"],
            cfg.artifact_dir / "savant_logs_since_start.txt",
            since=since,
        )
        write_combined_docker_logs(
            [
                "video-analytics-midterm-analysis-forwarder-a",
                "video-analytics-midterm-analysis-forwarder-b",
            ],
            cfg.artifact_dir / "analysis_forwarder_logs_since_start.txt",
            since=since,
        )
    else:
        run(["docker", "logs", "--since", since, "video-analytics-midterm-savant"], cfg.artifact_dir / "savant_logs_since_start.txt", check=False)
        run(["docker", "logs", "--since", since, "video-analytics-midterm-analysis-forwarder"], cfg.artifact_dir / "analysis_forwarder_logs_since_start.txt", check=False)
    run(["docker", "logs", "--since", since, "video-analytics-midterm-media-worker"], cfg.artifact_dir / "media_worker_logs_since_start.txt", check=False)
    run(["docker", "logs", "--since", since, "video-analytics-midterm-clip-worker"], cfg.artifact_dir / "clip_worker_logs_since_start.txt", check=False)
    run(["docker", "logs", "--since", since, "video-analytics-midterm-event-worker"], cfg.artifact_dir / "event_worker_logs_since_start.txt", check=False)
    run(["docker", "logs", "--since", since, "video-analytics-midterm-face-worker"], cfg.artifact_dir / "face_worker_logs_since_start.txt", check=False)


def start_pressure_sources_from_manifest(cfg: PressureConfig, *, sources_path: Path) -> None:
    remove_pressure_source_containers(cfg.run_id)
    log_path = cfg.artifact_dir / "source_controller_start_dual_shard.log"
    entries: list[dict[str, Any]] = []
    with log_path.open("w", encoding="utf-8") as log_fh:
        for index in range(cfg.stream_count):
            source_id = f"{cfg.run_id}_{index:02d}"
            cmd = [
                sys.executable,
                str(SOURCE_CONTROLLER),
                "start",
                "--sources",
                str(sources_path),
                "--source-id",
                source_id,
            ]
            completed = subprocess.run(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            log_fh.write(f"$ {' '.join(cmd)}\n")
            log_fh.write(completed.stdout)
            if completed.stdout and not completed.stdout.endswith("\n"):
                log_fh.write("\n")
            entries.append(
                {
                    "source_id": source_id,
                    "returncode": completed.returncode,
                    "output_tail": completed.stdout[-1000:],
                }
            )
            if completed.returncode != 0:
                write_json(cfg.artifact_dir / "source_controller_start_dual_shard.json", entries)
                raise RuntimeError(f"failed to start pressure source {source_id}")
    write_json(cfg.artifact_dir / "source_controller_start_dual_shard.json", entries)


def stop_pressure_sources(conn, cfg: PressureConfig) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE cameras SET enabled=false, updated_at=now() WHERE source_id LIKE %s",
            (f"{cfg.run_id}_%",),
        )
    if cfg.dual_shard_same_gpu:
        removed = remove_pressure_source_containers(cfg.run_id)
        write_json(
            cfg.artifact_dir / "runtime_sources_apply_stop_pressure.json",
            {"dual_shard_same_gpu": True, "removed_source_containers": removed},
        )
        return
    apply_sources_only(cfg, "runtime_sources_apply_stop_pressure.json")


def apply_sources_only(cfg: PressureConfig, artifact_name: str) -> dict[str, Any]:
    response = api_json(
        cfg.api_base,
        "POST",
        "/cameras/runtime/sources/apply?include_disabled=true",
        timeout_s=180,
    )
    write_json(cfg.artifact_dir / artifact_name, response)
    if response.get("error"):
        raise RuntimeError(f"runtime source apply failed: {response['error']}")
    return response


def wait_for_drain(cfg: PressureConfig) -> None:
    deadline = time.time() + cfg.drain_s
    snapshots: list[dict[str, Any]] = []
    while time.time() < deadline:
        summary = db_summary_connect(cfg)
        snapshots.append({"observed_at": datetime.now(timezone.utc).isoformat(), "summary": summary})
        if summary["playable_bundles"] >= cfg.keep_evidence:
            break
        time.sleep(10)
    write_json(cfg.artifact_dir / "drain_snapshots.json", snapshots)


def collect_pressure_diagnostics(cfg: PressureConfig) -> dict[str, Any]:
    source_containers = inspect_pressure_source_containers(cfg.run_id)
    save_pressure_source_logs(cfg, source_containers)
    diagnostics: dict[str, Any] = {
        "sample_summary": summarize_runtime_samples(cfg),
        "source_containers": source_containers,
        "rtsp_republishers": inspect_rtsp_republishers(cfg),
        "log_summary": summarize_logs(cfg),
    }
    write_json(cfg.artifact_dir / "pressure_diagnostics.json", diagnostics)
    return diagnostics


def summarize_runtime_samples(cfg: PressureConfig) -> dict[str, Any]:
    samples_dir = cfg.artifact_dir / "samples"
    rows: list[dict[str, Any]] = []
    max_queue_depth = 0.0
    max_send_failures = 0.0
    max_forwarder_sources = 0
    max_savant_sources = 0
    max_forwarder_cpu_percent = 0.0
    max_savant_cpu_percent = 0.0
    max_source_adapter_cpu_percent = 0.0
    max_worker_cpu_percent: dict[str, float] = {
        key: 0.0 for key in WORKER_CONTAINER_NAMES
    }
    queue_full_samples = 0
    final_forwarder_seen = 0.0
    final_forwarder_forwarded = 0.0
    final_forwarder_dropped = 0.0
    final_savant_pose_objects = 0.0
    final_savant_face_objects = 0.0
    final_savant_adaface_embeddings = 0.0
    final_savant_person_observations = 0.0
    final_savant_face_observations = 0.0
    stable_samples = 0
    for path in sorted(samples_dir.glob("runtime_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            rows.append({"sample": path.name, "error": repr(exc)})
            continue
        metrics = payload.get("metrics") or {}
        forwarder = payload.get("forwarder") or {}
        savant_sources = metrics.get("sources") or []
        forwarder_sources = forwarder.get("sources") or []
        sample_index = path.stem.rsplit("_", 1)[-1]
        stats = _read_docker_stats_sample(samples_dir / f"docker_stats_{sample_index}.json")
        effective_fps = []
        for source in savant_sources:
            window = ((source.get("windows") or {}).get("10s") or {})
            value = window.get("va_savant_effective_fps")
            if value is not None:
                effective_fps.append(float(value))
        queue_depth = float((forwarder.get("global") or {}).get("queue_depth") or 0.0)
        send_failures = sum(
            float(source.get("savant_send_failures_total") or 0.0)
            for source in forwarder_sources
        )
        max_queue_depth = max(max_queue_depth, queue_depth)
        if queue_depth >= 2048:
            queue_full_samples += 1
        max_send_failures = max(max_send_failures, send_failures)
        max_forwarder_sources = max(max_forwarder_sources, len(forwarder_sources))
        max_savant_sources = max(max_savant_sources, len(savant_sources))
        forwarder_seen = sum(float(source.get("frames_seen_total") or 0.0) for source in forwarder_sources)
        forwarder_forwarded = sum(
            float(source.get("frames_forwarded_total") or 0.0) for source in forwarder_sources
        )
        forwarder_dropped = sum(float(source.get("frames_dropped_total") or 0.0) for source in forwarder_sources)
        final_forwarder_seen = forwarder_seen
        final_forwarder_forwarded = forwarder_forwarded
        final_forwarder_dropped = forwarder_dropped
        savant_pose_objects = sum(float(source.get("pose_objects_total") or 0.0) for source in savant_sources)
        savant_face_objects = sum(float(source.get("face_objects_total") or 0.0) for source in savant_sources)
        savant_adaface_embeddings = sum(
            float(source.get("adaface_embeddings_total") or 0.0) for source in savant_sources
        )
        savant_person_observations = sum(
            float(source.get("person_observations_exported_total") or 0.0)
            for source in savant_sources
        )
        savant_face_observations = sum(
            float(source.get("face_observations_exported_total") or 0.0)
            for source in savant_sources
        )
        final_savant_pose_objects = savant_pose_objects
        final_savant_face_objects = savant_face_objects
        final_savant_adaface_embeddings = savant_adaface_embeddings
        final_savant_person_observations = savant_person_observations
        final_savant_face_observations = savant_face_observations
        if cfg.dual_shard_same_gpu:
            forwarder_cpu = _stats_cpu_percent_sum(
                stats,
                [
                    "video-analytics-midterm-analysis-forwarder-a",
                    "video-analytics-midterm-analysis-forwarder-b",
                ],
            )
            savant_cpu = _stats_cpu_percent_sum(
                stats,
                [
                    "video-analytics-midterm-savant-a",
                    "video-analytics-midterm-savant-b",
                ],
            )
        else:
            forwarder_cpu = _stats_cpu_percent(stats, "video-analytics-midterm-analysis-forwarder")
            savant_cpu = _stats_cpu_percent(stats, "video-analytics-midterm-savant")
        source_cpu = max(
            (
                _parse_percent(str(item.get("CPUPerc") or "0"))
                for name, item in stats.items()
                if name.startswith(f"video-analytics-source-{cfg.run_id}_")
            ),
            default=0.0,
        )
        max_forwarder_cpu_percent = max(max_forwarder_cpu_percent, forwarder_cpu)
        max_savant_cpu_percent = max(max_savant_cpu_percent, savant_cpu)
        max_source_adapter_cpu_percent = max(max_source_adapter_cpu_percent, source_cpu)
        worker_cpu = {
            key: _stats_cpu_percent(stats, container)
            for key, container in WORKER_CONTAINER_NAMES.items()
        }
        for key, value in worker_cpu.items():
            max_worker_cpu_percent[key] = max(max_worker_cpu_percent[key], value)
        if len(forwarder_sources) >= cfg.stream_count and queue_depth <= 0 and send_failures <= cfg.max_send_failures:
            stable_samples += 1
        rows.append(
            {
                "sample": path.name,
                "observed_at": payload.get("_pressure_sample_observed_at"),
                "savant_sources": len(savant_sources),
                "forwarder_sources": len(forwarder_sources),
                "queue_depth": queue_depth,
                "savant_send_failures_total": send_failures,
                "forwarder_frames_seen_total": int(forwarder_seen),
                "forwarder_frames_forwarded_total": int(forwarder_forwarded),
                "forwarder_frames_dropped_total": int(forwarder_dropped),
                "savant_pose_objects_total": int(savant_pose_objects),
                "savant_face_objects_total": int(savant_face_objects),
                "savant_adaface_embeddings_total": int(savant_adaface_embeddings),
                "savant_person_observations_exported_total": int(savant_person_observations),
                "savant_face_observations_exported_total": int(savant_face_observations),
                "forwarder_forwarded_seen_ratio": (
                    round(forwarder_forwarded / forwarder_seen, 4)
                    if forwarder_seen > 0
                    else None
                ),
                "forwarder_cpu_percent": forwarder_cpu,
                "savant_cpu_percent": savant_cpu,
                "worker_cpu_percent": worker_cpu,
                "max_source_adapter_cpu_percent": source_cpu,
                "avg_effective_fps_10s": (
                    round(sum(effective_fps) / len(effective_fps), 3)
                    if effective_fps
                    else None
                ),
                "min_effective_fps_10s": min(effective_fps) if effective_fps else None,
                "max_effective_fps_10s": max(effective_fps) if effective_fps else None,
            }
        )
    summary = {
        "sample_count": len(rows),
        "max_queue_depth": max_queue_depth,
        "max_forwarder_queue_depth": max_queue_depth,
        "queue_full_samples": queue_full_samples,
        "max_savant_send_failures_total": int(max_send_failures),
        "max_forwarder_sources": max_forwarder_sources,
        "max_savant_sources": max_savant_sources,
        "final_forwarder_frames_seen_total": int(final_forwarder_seen),
        "final_forwarder_frames_forwarded_total": int(final_forwarder_forwarded),
        "final_forwarder_frames_dropped_total": int(final_forwarder_dropped),
        "final_savant_pose_objects_total": int(final_savant_pose_objects),
        "final_savant_face_objects_total": int(final_savant_face_objects),
        "final_savant_adaface_embeddings_total": int(final_savant_adaface_embeddings),
        "final_savant_person_observations_exported_total": int(final_savant_person_observations),
        "final_savant_face_observations_exported_total": int(final_savant_face_observations),
        "final_forwarder_forwarded_seen_ratio": (
            round(final_forwarder_forwarded / final_forwarder_seen, 4)
            if final_forwarder_seen > 0
            else None
        ),
        "target_forwarded_frames": int(
            round(cfg.stream_count * _fps_to_float(cfg.fps) * max(cfg.duration_s, 0))
        ),
        "final_forwarded_target_ratio": (
            round(
                final_forwarder_forwarded
                / max(cfg.stream_count * _fps_to_float(cfg.fps) * max(cfg.duration_s, 1), 1),
                4,
            )
            if final_forwarder_forwarded
            else 0.0
        ),
        "max_forwarder_cpu_percent": round(max_forwarder_cpu_percent, 3),
        "max_savant_cpu_percent": round(max_savant_cpu_percent, 3),
        "max_source_adapter_cpu_percent": round(max_source_adapter_cpu_percent, 3),
        "max_worker_cpu_percent": {
            key: round(value, 3) for key, value in max_worker_cpu_percent.items()
        },
        "stable_samples": stable_samples,
        "samples": rows,
    }
    write_json(cfg.artifact_dir / "sample_summary.json", summary)
    return summary


def save_pressure_source_logs(cfg: PressureConfig, source_summary: dict[str, Any]) -> None:
    log_dir = cfg.artifact_dir / "source_adapter_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    for item in source_summary.get("items") or []:
        name = str(item.get("name") or "")
        if not name:
            continue
        log_path = log_dir / f"{name}.log"
        run(
            ["docker", "logs", "--tail", "10000", name],
            log_path,
            check=False,
        )
        item["log_path"] = str(log_path)


def inspect_rtsp_republishers(cfg: PressureConfig) -> dict[str, Any]:
    manifest_path = cfg.artifact_dir / "rtsp_republish_manifest.json"
    if not manifest_path.exists():
        return {"enabled": False}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"enabled": True, "error": repr(exc)}
    items: list[dict[str, Any]] = []
    for item in manifest:
        log_path = Path(str(item.get("log_path") or ""))
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        pid = int(item.get("pid") or 0)
        returncode = None
        if pid > 0:
            completed = subprocess.run(
                ["ps", "-p", str(pid), "-o", "pid="],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if not completed.stdout.strip():
                returncode = "exited_or_missing"
        items.append(
            {
                "source_id": item.get("source_id"),
                "pid": pid,
                "returncode": returncode,
                "log_path": str(log_path),
                "non_monotonic_dts": text.lower().count("non-monoton"),
                "negative_timestamp": text.lower().count("negative"),
                "connection_errors": text.lower().count("connection refused")
                + text.lower().count("connection timed out"),
            }
        )
    exited = [item for item in items if item.get("returncode") is not None]
    summary = {
        "enabled": True,
        "total": len(items),
        "exited": len(exited),
        "non_monotonic_dts_total": sum(int(item.get("non_monotonic_dts") or 0) for item in items),
        "negative_timestamp_total": sum(int(item.get("negative_timestamp") or 0) for item in items),
        "connection_error_total": sum(int(item.get("connection_errors") or 0) for item in items),
        "exited_sources": exited[:20],
        "items": items,
    }
    return summary


def inspect_pressure_source_containers(run_id: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{json .}}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    prefix = f"video-analytics-source-{run_id}_"
    items: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(row.get("Names") or "")
        if not name.startswith(prefix):
            continue
        inspect = inspect_container(name)
        status = str(row.get("Status") or "")
        state = inspect.get("State") or {}
        restart_count = int(inspect.get("RestartCount") or 0)
        running = bool(state.get("Running"))
        items.append(
            {
                "name": name,
                "status": status,
                "running": running,
                "exit_code": state.get("ExitCode"),
                "restart_count": restart_count,
                "started_at": state.get("StartedAt"),
                "finished_at": state.get("FinishedAt"),
                "negative_pts_errors": count_docker_log_pattern(name, "OverflowError: -"),
                "processed_zero_frames": count_docker_log_pattern(name, "Processed 0 frames"),
            }
        )
    exited = [item for item in items if not item["running"]]
    restarted = [item for item in items if int(item.get("restart_count") or 0) > 0]
    summary = {
        "total": len(items),
        "running": len(items) - len(exited),
        "exited": len(exited),
        "restart_count_total": sum(int(item.get("restart_count") or 0) for item in items),
        "negative_pts_error_total": sum(int(item.get("negative_pts_errors") or 0) for item in items),
        "processed_zero_frames_total": sum(int(item.get("processed_zero_frames") or 0) for item in items),
        "exited_sources": exited[:20],
        "restarted_sources": restarted[:20],
        "items": items,
    }
    return summary


def inspect_container(name: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "inspect", name],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        return {}
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    return data[0] if data else {}


def count_docker_log_pattern(name: str, pattern: str) -> int:
    completed = subprocess.run(
        ["docker", "logs", "--tail", "5000", name],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return completed.stdout.count(pattern)


def _extract_metric_ints(text: str, name: str) -> list[int]:
    pattern = re.compile(rf"\b{re.escape(name)}=(\d+)")
    return [int(match.group(1)) for match in pattern.finditer(text or "")]


def _extract_metric_numbers(text: str, name: str) -> list[float]:
    pattern = re.compile(rf"\b{re.escape(name)}=(-?\d+(?:\.\d+)?)")
    return [float(match.group(1)) for match in pattern.finditer(text or "")]


def _metric_sum(values: list[int]) -> int:
    return int(sum(values))


def _numeric_distribution(values: list[float] | list[int]) -> dict[str, Any]:
    if not values:
        return {"status": "not_enough_data", "count": 0}
    sorted_values = sorted(float(value) for value in values)
    return {
        "status": "measured",
        "count": len(sorted_values),
        "min": round(sorted_values[0], 3),
        "p50": round(_percentile(sorted_values, 0.50), 3),
        "p95": round(_percentile(sorted_values, 0.95), 3),
        "p99": round(_percentile(sorted_values, 0.99), 3),
        "max": round(sorted_values[-1], 3),
    }


def _percentile(sorted_values: list[float], quantile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def summarize_logs(cfg: PressureConfig) -> dict[str, Any]:
    paths = {
        "savant": cfg.artifact_dir / "savant_logs_since_start.txt",
        "analysis_forwarder": cfg.artifact_dir / "analysis_forwarder_logs_since_start.txt",
        "media_worker": cfg.artifact_dir / "media_worker_logs_since_start.txt",
        "clip_worker": cfg.artifact_dir / "clip_worker_logs_since_start.txt",
        "event_worker": cfg.artifact_dir / "event_worker_logs_since_start.txt",
        "face_worker": cfg.artifact_dir / "face_worker_logs_since_start.txt",
    }
    summary: dict[str, Any] = {}
    for key, path in paths.items():
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        media_finalization_ms = _extract_metric_ints(text, "finalization_duration_ms")
        media_queue_wait_ms = _extract_metric_ints(text, "queue_wait_ms")
        media_lifecycle_ms = _extract_metric_ints(text, "lifecycle_elapsed_ms")
        media_post_savant_finalization_ms = _extract_metric_ints(
            text,
            "post_savant_finalization_elapsed_ms",
        )
        media_ffprobe_ms = _extract_metric_ints(text, "ffprobe_duration_ms")
        media_ffmpeg_ms = _extract_metric_ints(text, "ffmpeg_duration_ms")
        media_imageio_ffmpeg_fallback_counts = _extract_metric_ints(
            text,
            "imageio_ffmpeg_fallback_count",
        )
        media_throttle_sleep_s = _extract_metric_numbers(text, "throttle_sleep_s")
        media_deadline_slack_s = _extract_metric_numbers(text, "deadline_slack_s")
        face_gallery_query_ms = _extract_metric_ints(text, "gallery_query_duration_ms")
        summary[key] = {
            "line_count": len(text.splitlines()),
            "validate_seq_iq": text.count("validate_seq_iq"),
            "writer_send_timeout": text.count("WriterResultSendTimeout"),
            "frame_annotation_redis_write_error": text.count(
                "frame_annotation_redis_writer action=write_error"
            ),
            "frame_annotation_redis_timeout": text.count("TimeoutError:timed out"),
            "negative_pts_overflow": len(re.findall(r"OverflowError: -\\d+", text)),
            "ffprobe_missing": text.count("ffprobe not found"),
            "imageio_ffmpeg_fallback": _metric_sum(
                media_imageio_ffmpeg_fallback_counts
            ),
            "imageio_ffmpeg_fallback_count_distribution": _numeric_distribution(
                media_imageio_ffmpeg_fallback_counts
            ),
            "record_request_dedupe_reserved": text.count(
                "record_request_dedupe_reserved"
            ),
            "record_request_dedupe_duplicate": text.count(
                "record_request_dedupe_duplicate"
            ),
            "record_request_dedupe_reserve_failed": text.count(
                "record_request_dedupe_reserve_failed"
            ),
            "watchlist_hit_emitted": text.count("watchlist_hit_emitted"),
            "watchlist_emit_failed": text.count("watchlist emit failed"),
            "watchlist_gallery_query_completed": text.count(
                "watchlist_gallery_query_completed"
            ),
            "watchlist_gallery_query_failed": text.count(
                "watchlist_gallery_query_failed"
            ),
            "face_gallery_query_latency_ms": _numeric_distribution(
                face_gallery_query_ms
            ),
            "media_event_finalized": text.count("media_event_finalized"),
            "media_materialization_deferred": text.count(
                "media_materialization_deferred"
            ),
            "media_materialization_paced": text.count("media_materialization_paced"),
            "media_materialization_throttle_paced": text.count(
                "throttle_reason=paced"
            ),
            "media_materialization_throttle_deadline_guard": text.count(
                "throttle_reason=deadline_guard"
            ),
            "media_materialization_throttle_disabled": text.count(
                "throttle_reason=disabled"
            ),
            "post_savant_finalizer_failed": text.count(
                "post_savant_finalizer_failed"
            ),
            "media_finalization_duration_ms": _numeric_distribution(
                media_finalization_ms
            ),
            "media_queue_wait_ms": _numeric_distribution(media_queue_wait_ms),
            "media_lifecycle_elapsed_ms": _numeric_distribution(media_lifecycle_ms),
            "media_post_savant_finalization_elapsed_ms": _numeric_distribution(
                media_post_savant_finalization_ms
            ),
            "media_ffprobe_duration_ms": _numeric_distribution(media_ffprobe_ms),
            "media_ffmpeg_duration_ms": _numeric_distribution(media_ffmpeg_ms),
            "media_throttle_sleep_s": _numeric_distribution(media_throttle_sleep_s),
            "media_deadline_slack_s": _numeric_distribution(media_deadline_slack_s),
        }
    return summary


def pressure_failure_reasons(
    cfg: PressureConfig,
    kept: list[dict[str, Any]],
    diagnostics: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    sample_summary = diagnostics.get("sample_summary") or {}
    source_summary = diagnostics.get("source_containers") or {}
    republish_summary = diagnostics.get("rtsp_republishers") or {}
    log_summary = diagnostics.get("log_summary") or {}
    savant_logs = log_summary.get("savant") or {}
    if not cfg.forwarder_null_sink and len(kept) < cfg.keep_evidence:
        reasons.append("insufficient_playable_evidence")
    if int(source_summary.get("exited") or 0) > cfg.max_exited_sources:
        reasons.append("source_containers_exited")
    if int(source_summary.get("restart_count_total") or 0) > 0:
        reasons.append("source_containers_restarted")
    if int(source_summary.get("negative_pts_error_total") or 0) > 0:
        reasons.append("source_adapter_negative_pts")
    if bool(republish_summary.get("enabled")):
        if int(republish_summary.get("exited") or 0) > 0:
            reasons.append("rtsp_republishers_exited")
        if int(republish_summary.get("connection_error_total") or 0) > 0:
            reasons.append("rtsp_republishers_connection_errors")
    if int(sample_summary.get("max_savant_send_failures_total") or 0) > cfg.max_send_failures:
        reasons.append("savant_send_failures")
    if (
        cfg.keep_evidence > 0
        and not cfg.forwarder_null_sink
        and int(sample_summary.get("max_savant_sources") or 0) >= cfg.stream_count
    ):
        if int(sample_summary.get("final_savant_pose_objects_total") or 0) <= 0:
            reasons.append("savant_pose_objects_zero")
        if int(sample_summary.get("final_savant_person_observations_exported_total") or 0) <= 0:
            reasons.append("savant_person_observations_zero")
        if int(sample_summary.get("final_savant_face_observations_exported_total") or 0) <= 0:
            reasons.append("savant_face_observations_zero")
    if int(savant_logs.get("frame_annotation_redis_write_error") or 0) > 0:
        reasons.append("frame_annotation_redis_write_errors")
    if int(sample_summary.get("max_forwarder_sources") or 0) < cfg.stream_count:
        reasons.append("forwarder_did_not_see_all_sources")
    if int(sample_summary.get("queue_full_samples") or 0) > 0:
        reasons.append("forwarder_queue_full")
    if not cfg.forwarder_null_sink and int(sample_summary.get("max_savant_sources") or 0) < cfg.stream_count:
        reasons.append("savant_did_not_see_all_sources")
    if not cfg.forwarder_null_sink and _validate_seq_iq_is_failure(cfg, diagnostics, reasons):
        reasons.append("validate_seq_iq_exceeded")
    return reasons


def pressure_warnings(
    cfg: PressureConfig,
    diagnostics: dict[str, Any],
    failure_reasons: list[str],
) -> list[str]:
    warnings: list[str] = []
    log_summary = diagnostics.get("log_summary") or {}
    savant_logs = log_summary.get("savant") or {}
    validate_count = int(savant_logs.get("validate_seq_iq") or 0)
    if (
        not cfg.forwarder_null_sink
        and
        validate_count > cfg.max_validate_seq_iq
        and "validate_seq_iq_exceeded" not in failure_reasons
    ):
        warnings.append("validate_seq_iq_expected_sampling_gap")
    return warnings


def _validate_seq_iq_is_failure(
    cfg: PressureConfig,
    diagnostics: dict[str, Any],
    current_reasons: list[str],
) -> bool:
    log_summary = diagnostics.get("log_summary") or {}
    savant_logs = log_summary.get("savant") or {}
    validate_count = int(savant_logs.get("validate_seq_iq") or 0)
    if validate_count <= cfg.max_validate_seq_iq:
        return False

    sample_summary = diagnostics.get("sample_summary") or {}
    source_summary = diagnostics.get("source_containers") or {}
    ingress_unhealthy = (
        int(sample_summary.get("max_savant_send_failures_total") or 0)
        > cfg.max_send_failures
        or int(
            sample_summary.get(
                "max_forwarder_queue_depth",
                sample_summary.get("max_queue_depth", 0),
            )
            or 0
        )
        > 0
        or int(source_summary.get("exited") or 0) > cfg.max_exited_sources
        or int(source_summary.get("restart_count_total") or 0) > 0
        or int(source_summary.get("negative_pts_error_total") or 0) > 0
    )
    if ingress_unhealthy:
        return True
    source_visibility_failed = {
        "forwarder_did_not_see_all_sources",
        "savant_did_not_see_all_sources",
    }
    return bool(source_visibility_failed.intersection(current_reasons))


def select_kept_evidence(conn, cfg: PressureConfig) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT eb.event_id, eb.source_id, eb.camera_id, eb.camera_name, eb.event_type,
               eb.evidence_state, eb.media_status, eb.raw_clip_uri, eb.raw_clip_size_bytes,
               eb.raw_clip_duration_seconds, eb.created_at, e.created_at AS event_created_at,
               e.payload, c.metadata AS camera_metadata,
               rule_policy.evidence_policy AS rule_evidence_policy
        FROM evidence_bundles eb
        JOIN events e ON e.id = eb.event_id
        LEFT JOIN cameras c ON c.id::text = eb.camera_id
        LEFT JOIN LATERAL (
            SELECT cr.evidence_policy
            FROM camera_rules cr
            WHERE cr.camera_id::text = eb.camera_id
              AND (
                  cr.rule_type = eb.event_type
                  OR cr.algorithm_id = eb.event_type
                  OR (
                      eb.event_type = 'watchlist_hit'
                      AND cr.algorithm_id = 'face.watchlist'
                  )
                  OR (
                      eb.event_type = 'intrusion'
                      AND cr.algorithm_id = 'behavior.intrusion'
                  )
              )
            ORDER BY cr.updated_at DESC NULLS LAST, cr.created_at DESC NULLS LAST
            LIMIT 1
        ) rule_policy ON true
        WHERE eb.source_id LIKE %s
          AND eb.raw_clip_uri IS NOT NULL
          AND COALESCE(eb.raw_clip_size_bytes, 0) > 0
        ORDER BY random()
        LIMIT %s
        """,
        (f"{cfg.run_id}_%", cfg.keep_evidence),
    ).fetchall()
    kept = []
    for row in rows:
        item = _row_json(row)
        raw_clip_path = raw_clip_uri_to_path(cfg.evidence_root, str(item.get("raw_clip_uri") or ""))
        item["raw_clip_path"] = str(raw_clip_path) if raw_clip_path else ""
        item["raw_clip_exists"] = bool(raw_clip_path and raw_clip_path.is_file())
        camera_metadata = item.get("camera_metadata")
        if isinstance(camera_metadata, dict):
            item["pressure_group_index"] = camera_metadata.get("pressure_group_index")
            item["expected_pre_seconds"] = camera_metadata.get(
                "pressure_evidence_pre_seconds"
            )
            item["expected_post_seconds"] = camera_metadata.get(
                "pressure_evidence_post_seconds"
            )
            item["expected_total_seconds"] = camera_metadata.get(
                "pressure_evidence_total_seconds"
            )
        rule_policy = item.get("rule_evidence_policy")
        if isinstance(rule_policy, dict):
            item["rule_pre_seconds"] = rule_policy.get("pre_seconds")
            item["rule_post_seconds"] = rule_policy.get("post_seconds")
        kept.append(item)
    kept = [row for row in kept if row["raw_clip_exists"]]
    return kept[: cfg.keep_evidence]


def write_kept_csv(cfg: PressureConfig, kept: list[dict[str, Any]]) -> None:
    path = cfg.artifact_dir / "kept_50_evidence.csv"
    fields = [
        "event_id",
        "source_id",
        "camera_id",
        "camera_name",
        "event_type",
        "evidence_state",
        "media_status",
        "pressure_group_index",
        "expected_pre_seconds",
        "expected_post_seconds",
        "expected_total_seconds",
        "rule_pre_seconds",
        "rule_post_seconds",
        "raw_clip_uri",
        "raw_clip_path",
        "raw_clip_size_bytes",
        "raw_clip_duration_seconds",
        "created_at",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in kept:
            writer.writerow({field: row.get(field, "") for field in fields})


def cleanup_pressure_data(
    conn,
    redis_client: Redis,
    cfg: PressureConfig,
    *,
    keep_event_ids: set[str],
    runtime_epoch_root: str | None,
) -> dict[str, Any]:
    prefix = f"{cfg.run_id}_%"
    deleted_event_ids = [
        str(row["id"])
        for row in conn.execute(
            "SELECT id FROM events WHERE source_id LIKE %s AND NOT (id = ANY(%s::uuid[]))",
            (prefix, list(keep_event_ids)),
        ).fetchall()
    ]
    cleanup: dict[str, Any] = {"deleted_event_count": len(deleted_event_ids)}
    for event_id in deleted_event_ids:
        shutil.rmtree(cfg.evidence_root / event_id, ignore_errors=True)
    with conn.transaction():
        cleanup["deleted_face_observations"] = conn.execute(
            "DELETE FROM face_observations WHERE source_id LIKE %s", (prefix,)
        ).rowcount
        cleanup["deleted_person_bbox_observations"] = conn.execute(
            "DELETE FROM person_bbox_observations WHERE source_id LIKE %s", (prefix,)
        ).rowcount
        cleanup["deleted_events"] = conn.execute(
            "DELETE FROM events WHERE source_id LIKE %s AND NOT (id = ANY(%s::uuid[]))",
            (prefix, list(keep_event_ids)),
        ).rowcount
        cleanup["deleted_rules"] = conn.execute(
            "DELETE FROM camera_rules WHERE camera_id IN (SELECT id FROM cameras WHERE source_id LIKE %s)",
            (prefix,),
        ).rowcount
        cleanup["deleted_zones"] = conn.execute(
            "DELETE FROM camera_zones WHERE camera_id IN (SELECT id FROM cameras WHERE source_id LIKE %s)",
            (prefix,),
        ).rowcount
        cleanup["deleted_cameras"] = conn.execute(
            "DELETE FROM cameras WHERE source_id LIKE %s", (prefix,)
        ).rowcount
    cleanup["redis_deleted"] = cleanup_redis_streams(redis_client, cfg.run_id)
    cleanup["removed_source_containers"] = remove_pressure_source_containers(cfg.run_id)
    if runtime_epoch_root:
        epoch_path = Path(runtime_epoch_root)
        if epoch_path.is_dir() and epoch_path.is_relative_to(cfg.replay_epoch_root):
            shutil.rmtree(epoch_path, ignore_errors=True)
            cleanup["removed_runtime_epoch_root"] = str(epoch_path)
    return cleanup


def cleanup_after_aborted_run(
    conn,
    redis_client: Redis,
    cfg: PressureConfig,
    report: dict[str, Any],
    *,
    runtime_epoch_root: str | None,
) -> None:
    if not cfg.cleanup:
        return
    try:
        stop_pressure_sources(conn, cfg)
    except Exception as exc:
        report["stop_pressure_sources_after_abort_error"] = repr(exc)
    try:
        report["cleanup_after_abort"] = cleanup_pressure_data(
            conn,
            redis_client,
            cfg,
            keep_event_ids=set(),
            runtime_epoch_root=runtime_epoch_root,
        )
    except Exception as exc:
        report["cleanup_after_abort_error"] = repr(exc)
    write_json(cfg.artifact_dir / "report.json", report)


def restore_cameras(conn, cameras: list[dict[str, Any]]) -> None:
    with conn.transaction():
        for camera in cameras:
            conn.execute(
                "UPDATE cameras SET enabled=%s, updated_at=now() WHERE id=%s",
                (bool(camera["enabled"]), camera["id"]),
            )


def restore_runtime(cfg: PressureConfig, original_perf: dict[str, Any]) -> None:
    if cfg.dual_shard_same_gpu:
        if cfg.dual_shard_api:
            api_json(cfg.api_base, "POST", "/runtime/control/dual/stop", timeout_s=360)
        else:
            stop_dual_shard_runtime(cfg)
    env = os.environ.copy()
    env.update(
        {
            "BATCH_SIZE": str(original_perf.get("savant_batch_size", 1)),
            "POSE_BATCH_SIZE": str(original_perf.get("pose_batch_size", 1)),
            "FACE_DETECTOR_BATCH_SIZE": str(original_perf.get("face_detector_batch_size", 1)),
            "FACE_EMBEDDING_BATCH_SIZE": str(original_perf.get("face_embedding_batch_size", 16)),
            "MAX_PARALLEL_STREAMS": str(original_perf.get("max_parallel_streams", 4)),
            "BATCHED_PUSH_TIMEOUT": str(original_perf.get("batched_push_timeout", 40000)),
            "ANALYSIS_FPS": str(original_perf.get("analysis_fps", "8/1")),
            "ANALYSIS_MIN_FPS": str(original_perf.get("analysis_min_fps", "2/1")),
            "FORWARDER_OUT_ENDPOINT": "dealer+connect:tcp://savant-security:5557",
            "MAX_FPS": str(original_perf.get("savant_max_fps", "8/1")),
            "MIN_FPS": str(original_perf.get("savant_min_fps", "2/1")),
        }
    )
    run(
        [
            "docker",
            "compose",
            "--env-file",
            cfg.env_file,
            "-f",
            cfg.compose_file,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "analysis-forwarder",
            "savant-security",
        ],
        cfg.artifact_dir / "compose_recreate_restore.log",
        env=env,
        check=False,
    )
    api_json(cfg.api_base, "PUT", "/runtime/performance-config", original_perf)
    api_json(cfg.api_base, "POST", "/runtime/performance-config/apply?force=true", timeout_s=360)
    api_json(cfg.api_base, "POST", "/cameras/runtime/restart?include_disabled=true&force=true", timeout_s=360)


def db_summary_connect(cfg: PressureConfig) -> dict[str, Any]:
    with psycopg.connect(cfg.db_url, row_factory=dict_row) as conn:
        return db_summary(conn, cfg.run_id)


def db_summary(conn, run_id: str) -> dict[str, Any]:
    prefix = f"{run_id}_%"
    row = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM cameras WHERE source_id LIKE %(prefix)s) AS cameras,
          (SELECT count(*) FROM events WHERE source_id LIKE %(prefix)s) AS events,
          (SELECT count(*) FROM evidence_tasks WHERE source_id LIKE %(prefix)s) AS tasks,
          (SELECT count(*) FROM evidence_bundles WHERE source_id LIKE %(prefix)s) AS bundles,
          (SELECT count(*) FROM evidence_bundles WHERE source_id LIKE %(prefix)s AND raw_clip_uri IS NOT NULL AND COALESCE(raw_clip_size_bytes, 0) > 0) AS playable_bundles
        """,
        {"prefix": prefix},
    ).fetchone()
    statuses = conn.execute(
        """
        SELECT status, materialization_status, count(*) AS count
        FROM evidence_tasks
        WHERE source_id LIKE %s
        GROUP BY status, materialization_status
        ORDER BY count DESC
        """,
        (prefix,),
    ).fetchall()
    event_types = conn.execute(
        "SELECT event_type, count(*) AS count FROM events WHERE source_id LIKE %s GROUP BY event_type ORDER BY count DESC",
        (prefix,),
    ).fetchall()
    data = _row_json(row)
    data["task_statuses"] = [_row_json(item) for item in statuses]
    data["event_types"] = [_row_json(item) for item in event_types]
    return data


def collect_downstream_observability(
    cfg: PressureConfig,
    conn,
    redis_client: Redis,
    *,
    kept: list[dict[str, Any]],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    summary = {
        "schema_version": DOWNSTREAM_OBSERVABILITY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "redis": redis_observability_summary(redis_client),
        "postgresql": postgres_observability_summary(conn, cfg.run_id),
        "event_worker": event_worker_observability_summary(diagnostics),
        "face_worker": face_worker_observability_summary(diagnostics),
        "media_worker": media_worker_observability_summary(diagnostics),
        "evidence_8090": evidence_8090_observability_summary(cfg, kept),
    }
    validate_downstream_observability_schema(summary)
    write_json(cfg.artifact_dir / "downstream_observability_summary.json", summary)
    return summary


def redis_observability_summary(redis_client: Redis) -> dict[str, Any]:
    streams: dict[str, Any] = {}
    for stream in SECURITY_STREAMS:
        item: dict[str, Any] = {}
        try:
            item["length"] = int(redis_client.xlen(stream))
        except Exception as exc:
            item["length"] = _not_enough_data(f"xlen_failed:{type(exc).__name__}")
        try:
            groups = redis_client.xinfo_groups(stream)
            item["consumer_groups"] = [
                _decode_redis_mapping(group) for group in groups
            ]
        except Exception as exc:
            item["consumer_groups"] = []
            item["consumer_groups_status"] = _not_enough_data(
                f"xinfo_groups_failed:{type(exc).__name__}"
            )
        streams[stream] = item
    return {"streams": streams}


def postgres_observability_summary(conn, run_id: str) -> dict[str, Any]:
    summary: dict[str, Any] = {"run_summary": db_summary(conn, run_id)}
    tables = [
        "events",
        "evidence_tasks",
        "evidence_bundles",
        "face_observations",
        "person_bbox_observations",
    ]
    try:
        rows = conn.execute(
            """
            SELECT relname, n_live_tup, n_dead_tup, seq_scan, seq_tup_read,
                   idx_scan, idx_tup_fetch
            FROM pg_stat_user_tables
            WHERE relname = ANY(%s)
            ORDER BY relname
            """,
            (tables,),
        ).fetchall()
        summary["table_stats"] = [_row_json(row) for row in rows]
    except Exception as exc:
        summary["table_stats"] = _not_enough_data(
            f"pg_stat_user_tables_failed:{type(exc).__name__}"
        )
    try:
        row = conn.execute(
            """
            SELECT count(*) AS count,
                   percentile_cont(0.50) WITHIN GROUP (
                     ORDER BY EXTRACT(EPOCH FROM (COALESCE(last_materialization_at, updated_at) - created_at))
                   ) AS p50_seconds,
                   percentile_cont(0.95) WITHIN GROUP (
                     ORDER BY EXTRACT(EPOCH FROM (COALESCE(last_materialization_at, updated_at) - created_at))
                   ) AS p95_seconds,
                   percentile_cont(0.99) WITHIN GROUP (
                     ORDER BY EXTRACT(EPOCH FROM (COALESCE(last_materialization_at, updated_at) - created_at))
                   ) AS p99_seconds
            FROM evidence_tasks
            WHERE source_id LIKE %s
              AND created_at IS NOT NULL
              AND COALESCE(last_materialization_at, updated_at) IS NOT NULL
            """,
            (f"{run_id}_%",),
        ).fetchone()
        lifecycle = _row_json(row)
        lifecycle["status"] = (
            "measured" if int(lifecycle.get("count") or 0) > 0 else "not_enough_data"
        )
        summary["evidence_task_lifecycle_seconds"] = lifecycle
    except Exception as exc:
        summary["evidence_task_lifecycle_seconds"] = _not_enough_data(
            f"evidence_lifecycle_query_failed:{type(exc).__name__}"
        )
    return summary


def event_worker_observability_summary(diagnostics: dict[str, Any]) -> dict[str, Any]:
    logs = ((diagnostics.get("log_summary") or {}).get("event_worker") or {})
    return {
        "record_request_dedupe": {
            "reserved_count": int(logs.get("record_request_dedupe_reserved") or 0),
            "duplicate_count": int(logs.get("record_request_dedupe_duplicate") or 0),
            "reserve_failed_count": int(
                logs.get("record_request_dedupe_reserve_failed") or 0
            ),
            "latency_ms": _not_enough_data("dedupe path is O(1); latency timer not instrumented yet"),
        },
        "cpu_percent": (
            (diagnostics.get("sample_summary") or {})
            .get("max_worker_cpu_percent", {})
            .get("event_worker")
        ),
    }


def face_worker_observability_summary(diagnostics: dict[str, Any]) -> dict[str, Any]:
    logs = ((diagnostics.get("log_summary") or {}).get("face_worker") or {})
    return {
        "watchlist_hit_emitted_count": int(logs.get("watchlist_hit_emitted") or 0),
        "watchlist_emit_failed_count": int(logs.get("watchlist_emit_failed") or 0),
        "gallery_query_completed_count": int(
            logs.get("watchlist_gallery_query_completed") or 0
        ),
        "gallery_query_failed_count": int(
            logs.get("watchlist_gallery_query_failed") or 0
        ),
        "gallery_query_latency_ms": logs.get("face_gallery_query_latency_ms")
        or _not_enough_data("face-worker gallery query logs unavailable"),
        "cpu_percent": (
            (diagnostics.get("sample_summary") or {})
            .get("max_worker_cpu_percent", {})
            .get("face_worker")
        ),
    }


def media_worker_observability_summary(diagnostics: dict[str, Any]) -> dict[str, Any]:
    logs = ((diagnostics.get("log_summary") or {}).get("media_worker") or {})
    return {
        "finalized_count": int(logs.get("media_event_finalized") or 0),
        "deferred_count": int(logs.get("media_materialization_deferred") or 0),
        "paced_count": int(logs.get("media_materialization_paced") or 0),
        "throttle_paced_count": int(
            logs.get("media_materialization_throttle_paced") or 0
        ),
        "throttle_deadline_guard_count": int(
            logs.get("media_materialization_throttle_deadline_guard") or 0
        ),
        "throttle_disabled_count": int(
            logs.get("media_materialization_throttle_disabled") or 0
        ),
        "finalizer_failed_count": int(logs.get("post_savant_finalizer_failed") or 0),
        "imageio_ffmpeg_fallback_count": int(logs.get("imageio_ffmpeg_fallback") or 0),
        "throttle_sleep_s": logs.get("media_throttle_sleep_s")
        or _not_enough_data("media throttle logs unavailable"),
        "deadline_slack_s": logs.get("media_deadline_slack_s")
        or _not_enough_data("media deadline slack logs unavailable"),
        "finalization_duration_ms": logs.get("media_finalization_duration_ms")
        or _not_enough_data("media finalization logs unavailable"),
        "queue_wait_ms": logs.get("media_queue_wait_ms")
        or _not_enough_data("media queue wait logs unavailable"),
        "lifecycle_elapsed_ms": logs.get("media_lifecycle_elapsed_ms")
        or _not_enough_data("media lifecycle logs unavailable"),
        "post_savant_finalization_elapsed_ms": logs.get(
            "media_post_savant_finalization_elapsed_ms"
        )
        or _not_enough_data("post-Savant finalization logs unavailable"),
        "ffprobe_duration_ms": logs.get("media_ffprobe_duration_ms")
        or _not_enough_data("ffprobe logs unavailable"),
        "ffmpeg_duration_ms": logs.get("media_ffmpeg_duration_ms")
        or _not_enough_data("ffmpeg logs unavailable"),
        "cpu_percent": (
            (diagnostics.get("sample_summary") or {})
            .get("max_worker_cpu_percent", {})
            .get("media_worker")
        ),
    }


def evidence_8090_observability_summary(
    cfg: PressureConfig,
    kept: list[dict[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "retained_count": len(kept),
        "checked_count": 0,
        "ok_count": 0,
        "failed": [],
    }
    try:
        result["health"] = api_json(cfg.api_base, "GET", "/evidence/health", timeout_s=10)
    except Exception as exc:
        result["health"] = _not_enough_data(f"health_request_failed:{type(exc).__name__}")
    for row in kept[: max(0, cfg.keep_evidence)]:
        event_id = str(row.get("event_id") or "")
        if not event_id:
            continue
        result["checked_count"] += 1
        try:
            response = api_json(
                cfg.api_base,
                "GET",
                f"/evidence/bundles/{event_id}",
                timeout_s=10,
            )
        except Exception as exc:
            result["failed"].append(
                {"event_id": event_id, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        if response.get("error"):
            result["failed"].append({"event_id": event_id, "error": response["error"]})
            continue
        data = response.get("data") or {}
        if str(data.get("event_id") or "") == event_id:
            result["ok_count"] += 1
        else:
            result["failed"].append(
                {
                    "event_id": event_id,
                    "error": "event_id_mismatch",
                    "observed_event_id": data.get("event_id"),
                }
            )
    result["status"] = (
        "measured" if result["checked_count"] else "not_enough_data"
    )
    return result


def validate_downstream_observability_schema(summary: dict[str, Any]) -> bool:
    missing = [
        section
        for section in DOWNSTREAM_OBSERVABILITY_REQUIRED_SECTIONS
        if section not in summary
    ]
    if summary.get("schema_version") != DOWNSTREAM_OBSERVABILITY_SCHEMA_VERSION:
        missing.append("schema_version")
    nested_requirements = {
        "redis": ("streams",),
        "postgresql": ("run_summary", "table_stats", "evidence_task_lifecycle_seconds"),
        "event_worker": ("record_request_dedupe",),
        "face_worker": ("gallery_query_latency_ms",),
        "media_worker": (
            "finalization_duration_ms",
            "ffprobe_duration_ms",
            "throttle_sleep_s",
            "deadline_slack_s",
        ),
        "evidence_8090": ("retained_count", "checked_count", "ok_count"),
    }
    for section, keys in nested_requirements.items():
        value = summary.get(section)
        if not isinstance(value, dict):
            continue
        for key in keys:
            if key not in value:
                missing.append(f"{section}.{key}")
    if missing:
        raise ValueError(
            "downstream observability summary missing required fields: "
            + ", ".join(sorted(missing))
        )
    return True


def _not_enough_data(reason: str) -> dict[str, Any]:
    return {"status": "not_enough_data", "reason": reason}


def _decode_redis_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"value": _decode_redis_value(value)}
    return {
        str(_decode_redis_value(key)): _decode_redis_value(item)
        for key, item in value.items()
    }


def _decode_redis_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list):
        return [_decode_redis_value(item) for item in value]
    if isinstance(value, tuple):
        return [_decode_redis_value(item) for item in value]
    if isinstance(value, dict):
        return _decode_redis_mapping(value)
    return value


def cleanup_redis_streams(redis_client: Redis, run_id: str) -> dict[str, int]:
    deleted: dict[str, int] = {}
    token = run_id.encode("utf-8")
    for stream in SECURITY_STREAMS:
        ids: list[bytes] = []
        last_id = b"-"
        while True:
            rows = redis_client.xrange(stream, min=last_id, max=b"+", count=500)
            if not rows:
                break
            for entry_id, fields in rows:
                if entry_id == last_id:
                    continue
                haystack = b" ".join(
                    key + b"=" + value for key, value in fields.items()
                    if isinstance(key, bytes) and isinstance(value, bytes)
                )
                if token in haystack:
                    ids.append(entry_id)
                last_id = entry_id
            if len(rows) < 500:
                break
        deleted[stream] = int(redis_client.xdel(stream, *ids)) if ids else 0
    return deleted


def remove_pressure_source_containers(run_id: str) -> list[str]:
    completed = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    names = [
        name
        for name in completed.stdout.splitlines()
        if name.startswith(f"video-analytics-source-{run_id}_")
    ]
    if names:
        subprocess.run(["docker", "rm", "-f", *names], check=False)
    return names


def raw_clip_uri_to_path(evidence_root: Path, uri: str) -> Path | None:
    prefix = "/media/evidence/"
    if not uri.startswith(prefix):
        return None
    rel = uri[len(prefix):]
    if "/" not in rel:
        return None
    return evidence_root / rel


def api_json(
    api_base: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_s: float = 60,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(
        api_base + path,
        data=data,
        method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urlopen(req, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            body = {"error": {"message": str(exc), "code": exc.code}}
        return body
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"api request failed {method} {path}: {exc}") from exc


def api_text(api_base: str, path: str, *, timeout_s: float = 60) -> str:
    return fetch_text_url(api_base + path, timeout_s=timeout_s)


def fetch_text_url(url: str, *, timeout_s: float = 10) -> str:
    with urlopen(url, timeout=timeout_s) as response:
        return response.read().decode("utf-8", errors="replace")


def dual_shard_runtime_overview(cfg: PressureConfig) -> dict[str, Any]:
    forwarder_shards: list[dict[str, Any]] = []
    forwarder_sources: list[dict[str, Any]] = []
    forwarder_queue_depth = 0.0
    forwarder_running = 0.0
    for shard_id, url in DUAL_SHARD_FORWARDER_METRICS.items():
        try:
            parsed = parse_forwarder_metrics_text(fetch_text_url(url, timeout_s=5))
        except Exception as exc:
            parsed = {
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
                "global": {},
                "sources": [],
            }
        for source in parsed.get("sources") or []:
            source["replay_shard_id"] = shard_id
        forwarder_sources.extend(parsed.get("sources") or [])
        shard_global = parsed.get("global") or {}
        forwarder_queue_depth += float(shard_global.get("queue_depth") or 0.0)
        forwarder_running += float(shard_global.get("running") or 0.0)
        forwarder_shards.append({"shard_id": shard_id, "url": url, **parsed})

    savant_shards: list[dict[str, Any]] = []
    savant_sources: list[dict[str, Any]] = []
    savant_sources_active = 0.0
    for shard_id, url in DUAL_SHARD_SAVANT_METRICS.items():
        try:
            parsed = parse_savant_metrics_text(fetch_text_url(url, timeout_s=5))
        except Exception as exc:
            parsed = {
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
                "global": {},
                "sources": [],
            }
        for source in parsed.get("sources") or []:
            source["replay_shard_id"] = shard_id
        savant_sources.extend(parsed.get("sources") or [])
        savant_sources_active += float(parsed.get("sources_active") or 0.0)
        savant_shards.append({"shard_id": shard_id, "url": url, **parsed})

    return {
        "dual_shard_same_gpu": True,
        "dual_shard_gpu": cfg.dual_shard_gpu,
        "metrics_url": "dual-shard://savant-a,savant-b",
        "forwarder_metrics_url": "dual-shard://analysis-forwarder-a,analysis-forwarder-b",
        "metrics": {
            "available": any(bool(item.get("available")) for item in savant_shards),
            "sources_active": savant_sources_active,
            "global": {"va_savant_sources_active": savant_sources_active},
            "sources": savant_sources,
            "shards": savant_shards,
        },
        "forwarder": {
            "available": any(bool(item.get("available")) for item in forwarder_shards),
            "global": {
                "queue_depth": forwarder_queue_depth,
                "running": forwarder_running,
            },
            "sources": forwarder_sources,
            "shards": forwarder_shards,
        },
    }


def parse_forwarder_metrics_text(text: str) -> dict[str, Any]:
    samples = _parse_prometheus_samples(text, prefix="va_forwarder_")
    by_source: dict[str, dict[str, float]] = {}
    global_metrics: dict[str, float] = {}
    for sample in samples:
        name = str(sample["name"])
        value = float(sample["value"])
        source_id = str(sample["labels"].get("source_id") or "")
        if source_id:
            by_source.setdefault(source_id, {})[name] = value
        else:
            global_metrics[name] = value
    sources = [
        {
            "source_id": source_id,
            "frames_seen_total": values.get("va_forwarder_frames_seen_total"),
            "frames_forwarded_total": values.get("va_forwarder_frames_forwarded_total"),
            "frames_dropped_total": values.get("va_forwarder_frames_dropped_total"),
            "savant_send_failures_total": values.get(
                "va_forwarder_savant_send_failures_total"
            ),
        }
        for source_id, values in sorted(by_source.items())
    ]
    return {
        "available": bool(samples),
        "sample_count": len(samples),
        "global": {
            "queue_depth": global_metrics.get("va_forwarder_queue_depth"),
            "running": global_metrics.get("va_forwarder_running"),
        },
        "sources": sources,
    }


def parse_savant_metrics_text(text: str) -> dict[str, Any]:
    samples = _parse_prometheus_samples(text, prefix="va_savant_")
    by_source: dict[str, dict[str, Any]] = {}
    global_metrics: dict[str, float] = {}
    required_seen: set[str] = set()
    for sample in samples:
        name = str(sample["name"])
        value = float(sample["value"])
        labels = sample["labels"]
        source_id = str(labels.get("source_id") or "")
        if source_id:
            row = by_source.setdefault(
                source_id,
                {
                    "source_id": source_id,
                    "counters": {},
                    "gauges": {},
                    "windows": {},
                },
            )
            window = str(labels.get("window") or "")
            if window:
                row["windows"].setdefault(window, {})[name] = value
            elif name in SAVANT_GAUGE_METRICS:
                row["gauges"][name] = value
            else:
                row["counters"][name] = value
            if name in SAVANT_COUNTER_METRICS or name in SAVANT_GAUGE_METRICS:
                required_seen.add(name)
        else:
            global_metrics[name] = value
            if name == "va_savant_sources_active":
                required_seen.add(name)
    sources = []
    for source_id, row in sorted(by_source.items()):
        gauges = row["gauges"]
        counters = row["counters"]
        sources.append(
            {
                "source_id": source_id,
                "effective_fps": gauges.get("va_savant_effective_fps"),
                "last_frame_age_seconds": gauges.get("va_savant_last_frame_age_seconds"),
                "frames_seen_total": counters.get("va_savant_frames_seen_total"),
                "frame_annotations_exported_total": counters.get(
                    "va_savant_frame_annotations_exported_total"
                ),
                "pose_objects_total": counters.get("va_savant_pose_objects_total"),
                "face_objects_total": counters.get("va_savant_face_objects_total"),
                "adaface_embeddings_total": counters.get("va_savant_adaface_embeddings_total"),
                "person_observations_exported_total": counters.get(
                    "va_savant_person_observations_exported_total"
                ),
                "face_observations_exported_total": counters.get(
                    "va_savant_face_observations_exported_total"
                ),
                "counters": counters,
                "gauges": gauges,
                "windows": row["windows"],
            }
        )
    return {
        "available": bool(samples),
        "sample_count": len(samples),
        "required_metric_names_seen": sorted(required_seen),
        "sources_active": global_metrics.get("va_savant_sources_active"),
        "global": global_metrics,
        "sources": sources,
    }


def _parse_prometheus_samples(text: str, *, prefix: str) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = PROM_SAMPLE_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        if not name.startswith(prefix):
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        samples.append(
            {
                "name": name,
                "labels": _parse_prom_labels(match.group("labels") or ""),
                "value": value,
            }
        )
    return samples


def _parse_prom_labels(raw: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for key, value in PROM_LABEL_RE.findall(raw or ""):
        labels[key] = value.replace(r"\"", '"').replace(r"\\", "\\")
    return labels


def write_combined_docker_logs(containers: list[str], path: Path, *, since: str) -> None:
    chunks: list[str] = []
    for name in containers:
        completed = subprocess.run(
            ["docker", "logs", "--since", since, name],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        chunks.append(f"===== {name} rc={completed.returncode} =====\n{completed.stdout}\n")
    write_text(path, "".join(chunks))


def nvidia_smi_csv() -> str:
    query = "timestamp,index,name,utilization.gpu,utilization.decoder,memory.used,memory.total"
    try:
        return subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout
    except Exception as exc:
        return f"nvidia-smi failed: {exc}\n"


def docker_stats_json(cfg: PressureConfig) -> dict[str, Any]:
    if cfg.dual_shard_same_gpu:
        desired = [
            "video-analytics-midterm-analysis-forwarder-a",
            "video-analytics-midterm-analysis-forwarder-b",
            "video-analytics-midterm-savant-a",
            "video-analytics-midterm-savant-b",
            *WORKER_CONTAINER_NAMES.values(),
            *pressure_source_container_names(cfg.run_id),
        ]
    else:
        desired = [
            "video-analytics-midterm-analysis-forwarder",
            "video-analytics-midterm-savant",
            *WORKER_CONTAINER_NAMES.values(),
            *pressure_source_container_names(cfg.run_id),
        ]
    if not desired:
        return {"_meta": {"error": "no_containers"}}
    completed = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *desired],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    rows: dict[str, Any] = {}
    errors: list[str] = []
    for line in completed.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                errors.append(line.strip())
            continue
        name = str(item.get("Name") or item.get("Container") or item.get("ID") or "")
        if name:
            rows[name] = item
    rows["_meta"] = {"returncode": completed.returncode, "errors": errors[:20]}
    return rows


def pressure_source_container_names(run_id: str) -> list[str]:
    completed = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    prefix = f"video-analytics-source-{run_id}_"
    return sorted(
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip().startswith(prefix)
    )


def _read_docker_stats_sample(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): value
        for key, value in payload.items()
        if not str(key).startswith("_") and isinstance(value, dict)
    }


def _stats_cpu_percent(stats: dict[str, dict[str, Any]], name: str) -> float:
    return _parse_percent(str((stats.get(name) or {}).get("CPUPerc") or "0"))


def _stats_cpu_percent_sum(stats: dict[str, dict[str, Any]], names: list[str]) -> float:
    return sum(_stats_cpu_percent(stats, name) for name in names)


def _parse_percent(value: str) -> float:
    try:
        return float(str(value).strip().rstrip("%"))
    except Exception:
        return 0.0


def _fps_to_float(value: str) -> float:
    try:
        return float(Fraction(str(value).strip()))
    except Exception:
        try:
            return float(value)
        except Exception:
            return 0.0


def run(
    cmd: list[str],
    log_path: Path,
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    write_text(log_path, completed.stdout)
    if check and completed.returncode != 0:
        raise RuntimeError(f"command failed rc={completed.returncode}: {' '.join(cmd)}")
    return completed


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def write_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def _atomic_write_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def _row_json(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    out = {}
    for key, value in dict(row).items():
        if isinstance(value, (datetime, uuid.UUID)):
            out[key] = str(value)
        else:
            out[key] = value
    return out


if __name__ == "__main__":
    raise SystemExit(main())
