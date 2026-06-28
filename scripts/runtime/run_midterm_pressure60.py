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


@dataclass(frozen=True)
class PressureConfig:
    run_id: str
    stream_count: int
    rtsp_uri: str
    fps: str
    min_fps: str
    batch_size: int
    max_parallel_streams: int
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
    parser.add_argument("--max-parallel-streams", type=int, default=64)
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
    parser.add_argument("--no-cleanup", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    run_id = args.run_id or _default_run_id(args.fps, forwarder_null_sink=args.forwarder_null_sink)
    artifact_dir = (args.artifact_root / run_id).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    cfg = PressureConfig(
        run_id=run_id,
        stream_count=args.streams,
        rtsp_uri=args.rtsp_uri,
        fps=args.fps,
        min_fps=args.min_fps,
        batch_size=args.batch_size,
        max_parallel_streams=args.max_parallel_streams,
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
    original_cameras: list[dict[str, Any]] = []
    runtime_epoch_root: str | None = None
    rtsp_republishers: list[subprocess.Popen] = []
    started_at = datetime.now(timezone.utc)
    try:
        original_perf = api_json(cfg.api_base, "GET", "/runtime/performance-config")["data"][
            "saved_config"
        ]
        original_cameras = fetch_cameras(conn)
        write_json(cfg.artifact_dir / "cameras_before.json", original_cameras)
        write_json(cfg.artifact_dir / "performance_before.json", original_perf)

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

        set_compose_operating_point(cfg)
        if cfg.forwarder_null_sink:
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
        if cfg.forwarder_null_sink:
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
        report["db_summary_before_cleanup"] = db_summary(conn, cfg.run_id)
        write_json(cfg.artifact_dir / "db_summary_before_cleanup.json", report["db_summary_before_cleanup"])
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
            if original_cameras:
                restore_cameras(conn, original_cameras)
            if original_perf:
                restore_runtime(cfg, original_perf)
        finally:
            return 130
    except Exception as exc:
        report["status"] = "failed_exception"
        report["error"] = repr(exc)
        write_json(cfg.artifact_dir / "report.json", report)
        print(f"PRESSURE_RUN_FAILED error={exc!r} artifact_dir={cfg.artifact_dir}", file=sys.stderr)
        try:
            stop_rtsp_republishers(rtsp_republishers, cfg)
            if original_cameras:
                restore_cameras(conn, original_cameras)
            if original_perf:
                restore_runtime(cfg, original_perf)
        finally:
            return 1
    finally:
        conn.close()


def _default_run_id(fps: str, *, forwarder_null_sink: bool = False) -> str:
    fps_label = fps.replace("/", "p").replace(".", "_")
    prefix = "forwarder60_null" if forwarder_null_sink else "pressure60"
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
        "MAX_PARALLEL_STREAMS": str(cfg.max_parallel_streams),
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
    since = started_at.isoformat().replace("+00:00", "Z")
    run(["docker", "logs", "--since", since, "video-analytics-midterm-savant"], cfg.artifact_dir / "savant_logs_since_start.txt", check=False)
    run(["docker", "logs", "--since", since, "video-analytics-midterm-analysis-forwarder"], cfg.artifact_dir / "analysis_forwarder_logs_since_start.txt", check=False)
    run(["docker", "logs", "--since", since, "video-analytics-midterm-media-worker"], cfg.artifact_dir / "media_worker_logs_since_start.txt", check=False)
    run(["docker", "logs", "--since", since, "video-analytics-midterm-clip-worker"], cfg.artifact_dir / "clip_worker_logs_since_start.txt", check=False)


def stop_pressure_sources(conn, cfg: PressureConfig) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE cameras SET enabled=false, updated_at=now() WHERE source_id LIKE %s",
            (f"{cfg.run_id}_%",),
        )
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
    queue_full_samples = 0
    final_forwarder_seen = 0.0
    final_forwarder_forwarded = 0.0
    final_forwarder_dropped = 0.0
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
                "forwarder_forwarded_seen_ratio": (
                    round(forwarder_forwarded / forwarder_seen, 4)
                    if forwarder_seen > 0
                    else None
                ),
                "forwarder_cpu_percent": forwarder_cpu,
                "savant_cpu_percent": savant_cpu,
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


def summarize_logs(cfg: PressureConfig) -> dict[str, Any]:
    paths = {
        "savant": cfg.artifact_dir / "savant_logs_since_start.txt",
        "analysis_forwarder": cfg.artifact_dir / "analysis_forwarder_logs_since_start.txt",
        "media_worker": cfg.artifact_dir / "media_worker_logs_since_start.txt",
        "clip_worker": cfg.artifact_dir / "clip_worker_logs_since_start.txt",
    }
    summary: dict[str, Any] = {}
    for key, path in paths.items():
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
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
            "imageio_ffmpeg_fallback": text.count("imageio_ffmpeg"),
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


def restore_cameras(conn, cameras: list[dict[str, Any]]) -> None:
    with conn.transaction():
        for camera in cameras:
            conn.execute(
                "UPDATE cameras SET enabled=%s, updated_at=now() WHERE id=%s",
                (bool(camera["enabled"]), camera["id"]),
            )


def restore_runtime(cfg: PressureConfig, original_perf: dict[str, Any]) -> None:
    env = os.environ.copy()
    env.update(
        {
            "BATCH_SIZE": "1",
            "MAX_PARALLEL_STREAMS": "4",
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
    desired = [
        "video-analytics-midterm-analysis-forwarder",
        "video-analytics-midterm-savant",
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
