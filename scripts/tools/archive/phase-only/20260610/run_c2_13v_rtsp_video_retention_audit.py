#!/usr/bin/env python3
"""C2.13V RTSP event video evidence and retention audit.

The audit is intentionally conservative:

* RTSP runtime events are real Redis/DB events only.
* Video evidence PASS requires source/time/frame join plus video integrity.
* DB window and legacy annotation fallback never satisfy visual evidence PASS.
* Replay, when enabled, uses a time-domain stop condition only.
* Retention is reported as unknown risk unless every growing store is bounded
  by config or runtime deletion/rotation is observed.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import redis
import yaml
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(MEDIA_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(MEDIA_WORKER_ROOT))

from scripts.tools import run_c2_13_rtsp_watchlist_intrusion_probe as c2_13  # noqa: E402
from app.post_savant_metadata_annotation_builder import (  # type: ignore  # noqa: E402
    SIDECAR_ANNOTATIONS_FILE,
    build_post_savant_annotation_sidecar,
    load_native_metadata,
)
from app.post_savant_video_integrity import inspect_video_integrity  # type: ignore  # noqa: E402


DEFAULT_DATABASE_URL = c2_13.DEFAULT_DATABASE_URL
DEFAULT_REDIS_URL = c2_13.DEFAULT_REDIS_URL
DEFAULT_EVIDENCE_ROOT = c2_13.DEFAULT_EVIDENCE_ROOT
DEFAULT_COMPOSE_FILE = c2_13.DEFAULT_COMPOSE_FILE
DEFAULT_ENV_FILE = c2_13.DEFAULT_ENV_FILE
DEFAULT_CAMERA_CONFIG = c2_13.DEFAULT_CAMERA_CONFIG
DEFAULT_REPLAY_CONFIG = ROOT / "modules" / "savant_replay" / "config.c2_post_savant_replay_poc.json"
DEFAULT_MEDIA_ROOT = Path("/data/video-analytics/media")
DEFAULT_EVIDENCE_AUDIT_ROOT = DEFAULT_MEDIA_ROOT / "evidence_audit"
DEFAULT_REPLAY_API_URL = "http://127.0.0.1:8098"
DEFAULT_REPLAY_SINK_URL = "dealer+connect:tcp://video-file-sink:6666"
DEFAULT_DISK_SAMPLE_SECONDS = 300.0
DEFAULT_DISK_SAMPLE_INTERVAL_SECONDS = 30.0
DEFAULT_EVENT_CAPTURE_SECONDS = 120.0
DEFAULT_MAX_MESSAGES = 500
DEFAULT_REPLAY_WAIT_SECONDS = 75.0

RESULT_PASS = "PASS_C2_13V_RTSP_VIDEO_EVIDENCE_RETENTION_READY"
RESULT_VIDEO_GAP = "PARTIAL_C2_13V_EVENTS_READY_VIDEO_EVIDENCE_GAP"
RESULT_RETENTION_UNKNOWN = "PARTIAL_C2_13V_RETENTION_POLICY_UNKNOWN"
RESULT_NO_EVENTS = "PARTIAL_C2_13V_NO_EVENTS_IN_WINDOW"
RESULT_FAIL = "FAIL_C2_13V_RTSP_VIDEO_RETENTION_BLOCKED"

RETENTION_UNKNOWN = "unknown_unbounded_risk"
RETENTION_BOUNDED_CONFIG = "bounded_config_found"
RETENTION_BOUNDED_RUNTIME = "bounded_runtime_verified"

REPLAY_NOT_ATTEMPTED = "not_attempted"
REPLAY_PASSED = "passed"
REPLAY_FAILED = "replay_event_video_failed"

VIDEO_CANDIDATES = ("video.mov", "video.mp4", "raw_clip.mov", "raw_clip.mp4")
METADATA_CANDIDATES = ("metadata.json", "sink_metadata.json")
EVENT_TYPES = {"watchlist_hit", "intrusion"}


def run_audit(
    *,
    output_dir: Path,
    database_url: str,
    redis_url: str,
    replay_api_url: str,
    replay_sink_url: str,
    disk_sample_seconds: float,
    disk_sample_interval_seconds: float,
    event_capture_seconds: float,
    max_messages: int,
    compose_file: Path,
    env_file: Path,
    camera_config_path: Path,
    replay_config_path: Path,
    media_root: Path,
    evidence_audit_root: Path,
    try_replay: bool,
    replay_wait_seconds: float,
    overwrite: bool = False,
) -> dict[str, Any]:
    validate_runtime_limits(
        disk_sample_seconds=disk_sample_seconds,
        disk_sample_interval_seconds=disk_sample_interval_seconds,
        event_capture_seconds=event_capture_seconds,
        max_messages=max_messages,
    )
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_audit_root.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc)
    containers_before = c2_13.inspect_runtime_containers()
    architecture = inspect_runtime_media_architecture(
        containers=containers_before,
        compose_file=compose_file,
        env_file=env_file,
        camera_config_path=camera_config_path,
        replay_config_path=replay_config_path,
        media_root=media_root,
        evidence_audit_root=evidence_audit_root,
    )
    retention_config = inspect_retention_config(architecture)
    source_status = architecture["rtsp_source_status"]
    source_id = str(source_status.get("source_id") or "")
    camera_id = str(source_status.get("camera_id") or source_id)
    watched_paths = watched_media_paths(architecture, media_root=media_root)

    redis_client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    redis_before = c2_13.inspect_redis(redis_client)
    db_before = inspect_event_counts(database_url, source_id)

    samples: list[dict[str, Any]] = []
    sample_index = 0
    samples.extend(sample_disk_usage(sample_index, watched_paths))
    event_start = datetime.now(timezone.utc)
    event_deadline = time.monotonic() + event_capture_seconds
    total_deadline = time.monotonic() + disk_sample_seconds
    next_sample = time.monotonic() + disk_sample_interval_seconds

    while time.monotonic() < event_deadline:
        if time.monotonic() >= next_sample:
            sample_index += 1
            samples.extend(sample_disk_usage(sample_index, watched_paths))
            next_sample += disk_sample_interval_seconds
        time.sleep(0.5)

    event_end = datetime.now(timezone.utc)
    captured = capture_rtsp_events(
        redis_client=redis_client,
        database_url=database_url,
        source_id=source_id,
        redis_before=redis_before,
        start_time=event_start,
        end_time=event_end,
        max_messages=max_messages,
    )
    association_attempts = associate_visual_evidence(
        events=captured["events"],
        architecture=architecture,
        output_dir=output_dir,
        replay_api_url=replay_api_url,
        replay_sink_url=replay_sink_url,
        try_replay=try_replay,
        replay_wait_seconds=replay_wait_seconds,
    )

    while time.monotonic() < total_deadline:
        if time.monotonic() >= next_sample:
            sample_index += 1
            samples.extend(sample_disk_usage(sample_index, watched_paths))
            next_sample += disk_sample_interval_seconds
        time.sleep(0.5)
    sample_index += 1
    samples.extend(sample_disk_usage(sample_index, watched_paths))

    disk_summary = summarize_disk_growth(samples)
    retention = decide_retention_policy_status(
        config_report=retention_config,
        disk_growth_summary=disk_summary,
    )
    evidence_results = build_video_evidence_results(association_attempts)
    redis_after = c2_13.inspect_redis(redis_client)
    db_after = inspect_event_counts(database_url, source_id)
    unsafe_scan = c2_13.scan_for_unsafe_payload(
        {
            "architecture": architecture,
            "retention": retention,
            "captured": captured,
            "association_attempts": association_attempts,
            "evidence_results": evidence_results,
        }
    )
    decision = decide_overall_marker(
        input_type=str(source_status.get("input_type") or ""),
        event_count=int(captured["summary"]["events_captured"]),
        evidence_generated_count=int(evidence_results["evidence_generated_count"]),
        retention_policy_status=str(retention["retention_policy_status"]),
        unsafe_payload_passed=bool(unsafe_scan["passed"]),
        runtime_errors=architecture.get("runtime_errors") or [],
    )
    ended_at = datetime.now(timezone.utc)
    summary = {
        "schema_version": "1.0",
        "phase": "C2.13V",
        "result_marker": decision["result_marker"],
        "decision_reason": decision["reason"],
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "input_type": source_status.get("input_type"),
        "source_id": source_id,
        "camera_id": camera_id,
        "rtsp_url_redacted": source_status.get("rtsp_url_redacted"),
        "retention_policy_status": retention["retention_policy_status"],
        "bounded_config_found": retention["bounded_config_found"],
        "bounded_runtime_verified": retention["bounded_runtime_verified"],
        "unknown_unbounded_risk": retention["unknown_unbounded_risk"],
        "disk_growth_delta": disk_summary["total_delta_bytes"],
        "top_growing_directories": disk_summary["largest_growing_directories"],
        "events_captured": captured["summary"]["events_captured"],
        "watchlist_count": captured["summary"]["watchlist_count"],
        "intrusion_count": captured["summary"]["intrusion_count"],
        "event_ids": captured["summary"]["event_ids"],
        "evidence_generated_count": evidence_results["evidence_generated_count"],
        "evidence_bundle_paths": evidence_results["evidence_bundle_paths"],
        "replay_video_status": evidence_results["replay_video_status"],
        "video_integrity_status": evidence_results["video_integrity_status"],
        "unsafe_payload_scan_passed": unsafe_scan["passed"],
        "payload_has_embedding": unsafe_scan["payload_has_embedding"],
        "payload_has_image_bytes": unsafe_scan["payload_has_image_bytes"],
        "output_dir": str(output_dir),
    }

    write_json(output_dir / "runtime_media_architecture.json", architecture)
    write_json(output_dir / "replay_retention_config_report.json", retention_config)
    write_disk_samples_csv(output_dir / "disk_growth_samples.csv", samples)
    write_json(output_dir / "disk_growth_summary.json", disk_summary)
    write_json(output_dir / "rtsp_events_captured.json", captured)
    write_json(output_dir / "evidence_association_attempts.json", association_attempts)
    write_json(output_dir / "video_evidence_results.json", evidence_results)
    write_json(output_dir / "unsafe_payload_scan.json", unsafe_scan)
    write_json(
        output_dir / "runtime_status.json",
        {
            "redis_before": redis_before,
            "redis_after": redis_after,
            "database_events_before": db_before,
            "database_events_after": db_after,
            "containers_before": containers_before,
            "containers_after": c2_13.inspect_runtime_containers(),
        },
    )
    write_json(output_dir / "decision_summary.json", {**decision, **summary})
    (output_dir / "operator_rtsp_video_retention_report.html").write_text(
        render_operator_report(
            summary=summary,
            architecture=architecture,
            retention=retention,
            disk_summary=disk_summary,
            captured=captured,
            evidence_results=evidence_results,
            attempts=association_attempts,
        ),
        encoding="utf-8",
    )
    return summary


def validate_runtime_limits(
    *,
    disk_sample_seconds: float,
    disk_sample_interval_seconds: float,
    event_capture_seconds: float,
    max_messages: int,
) -> None:
    if disk_sample_seconds < 0 or disk_sample_seconds > 600:
        raise ValueError("disk_sample_seconds must be between 0 and 600")
    if disk_sample_interval_seconds <= 0 or disk_sample_interval_seconds > 120:
        raise ValueError("disk_sample_interval_seconds must be in (0, 120]")
    if event_capture_seconds < 0 or event_capture_seconds > 300:
        raise ValueError("event_capture_seconds must be between 0 and 300")
    if max_messages < 1 or max_messages > 5000:
        raise ValueError("max_messages must be between 1 and 5000")


def inspect_runtime_media_architecture(
    *,
    containers: dict[str, Any],
    compose_file: Path,
    env_file: Path,
    camera_config_path: Path,
    replay_config_path: Path,
    media_root: Path,
    evidence_audit_root: Path,
) -> dict[str, Any]:
    source_status = c2_13.inspect_rtsp_source_config(
        containers=containers,
        compose_file=compose_file,
        env_file=env_file,
        camera_config_path=camera_config_path,
    )
    replay = inspect_container("c2-poc-replay-service")
    sink = inspect_container("c2-poc-video-file-sink")
    source = inspect_container("c2-poc-source-adapter")
    savant = inspect_container("c2-poc-savant")
    viewer = inspect_container("c2-poc-evidence-viewer")
    media_worker = inspect_container("c2-poc-media-worker")
    replay_config = read_json_file(replay_config_path)
    replay_dir = resolve_container_path_to_host(replay, "/opt/rocksdb")
    sink_pattern = str((sink.get("env") or {}).get("DIR_LOCATION") or "")
    sink_root = sink_root_from_pattern(sink_pattern, sink)
    evidence_root = resolve_container_path_to_host(viewer, "/evidence") or media_root / "evidence"
    services = {
        "source-adapter": source,
        "savant-security": savant,
        "replay-service": replay,
        "video-file-sink": sink,
        "media-worker": media_worker,
        "evidence-viewer": viewer,
    }
    return {
        "schema_version": "1.0",
        "rtsp_source_status": source_status,
        "services": services,
        "compose_file": str(compose_file),
        "env_file": str(env_file),
        "camera_config_path": str(camera_config_path),
        "replay_config_path": str(replay_config_path),
        "replay_config": redact_runtime_value(replay_config),
        "active_paths": {
            "media_root": str(media_root),
            "evidence_root": str(evidence_root),
            "evidence_audit_root": str(evidence_audit_root),
            "replay_storage_dir": str(replay_dir) if replay_dir else None,
            "video_file_sink_root": str(sink_root) if sink_root else None,
            "video_file_sink_container_pattern": sink_pattern,
        },
        "stream_ids": {
            "source_id": source_status.get("source_id"),
            "camera_id": source_status.get("camera_id"),
            "source_adapter_zmq_endpoint": (source.get("env") or {}).get("ZMQ_ENDPOINT"),
            "savant_source_endpoint": (savant.get("env") or {}).get("ZMQ_SRC_ENDPOINT"),
            "savant_sink_endpoint": (savant.get("env") or {}).get("ZMQ_SINK_ENDPOINT"),
            "replay_in_stream": nested(replay_config, "in_stream", "url"),
            "replay_out_stream": replay_config.get("out_stream") if isinstance(replay_config, dict) else None,
            "video_file_sink_endpoint": (sink.get("env") or {}).get("ZMQ_ENDPOINT"),
        },
        "architecture_answers": architecture_answers(
            replay_config=replay_config,
            sink_env=sink.get("env") or {},
            replay_dir=replay_dir,
            sink_root=sink_root,
            media_root=media_root,
            evidence_root=evidence_root,
            evidence_audit_root=evidence_audit_root,
        ),
        "runtime_errors": runtime_errors(source_status, services),
    }


def inspect_container(name: str) -> dict[str, Any]:
    info = c2_13.docker_inspect(name)
    if not info:
        return {"name": name, "exists": False, "running": False, "env": {}, "mounts": []}
    state = c2_13._dict(info.get("State"))
    env = c2_13.parse_env_list(c2_13._dict(info.get("Config")).get("Env") or [])
    return {
        "name": name,
        "exists": True,
        "running": bool(state.get("Running")),
        "status": state.get("Status"),
        "health": c2_13._dict(state.get("Health")).get("Status"),
        "image": c2_13._dict(info.get("Config")).get("Image"),
        "env": c2_13.redact_env(env),
        "mounts": [
            {
                "source": str(mount.get("Source") or ""),
                "destination": str(mount.get("Destination") or ""),
                "mode": str(mount.get("Mode") or ""),
                "rw": bool(mount.get("RW")),
            }
            for mount in info.get("Mounts") or []
        ],
    }


def resolve_container_path_to_host(container: dict[str, Any], container_path: str) -> Path | None:
    best: tuple[int, Path] | None = None
    for mount in container.get("mounts") or []:
        dest = str(mount.get("destination") or "")
        src = str(mount.get("source") or "")
        if not dest or not src:
            continue
        if container_path == dest or container_path.startswith(dest.rstrip("/") + "/"):
            suffix = Path(container_path).relative_to(Path(dest))
            candidate = Path(src) / suffix
            if best is None or len(dest) > best[0]:
                best = (len(dest), candidate)
    return best[1] if best else None


def sink_root_from_pattern(pattern: str, sink_container: dict[str, Any]) -> Path | None:
    if not pattern:
        return None
    root = pattern.split("%source_id%", 1)[0].rstrip("/")
    return resolve_container_path_to_host(sink_container, root) if root else None


def architecture_answers(
    *,
    replay_config: dict[str, Any],
    sink_env: dict[str, Any],
    replay_dir: Path | None,
    sink_root: Path | None,
    media_root: Path,
    evidence_root: Path,
    evidence_audit_root: Path,
) -> dict[str, Any]:
    replay_ttl = nested(replay_config, "storage", "rocksdb", "data_expiration_ttl")
    replay_compaction = nested(replay_config, "storage", "rocksdb", "compaction_period")
    chunk_size = str(sink_env.get("CHUNK_SIZE") or "")
    return {
        "which_service_stores_rtsp_video_packets_frames": "replay-service stores post-Savant RTSP frames and metadata in RocksDB",
        "which_service_writes_video_files": "video-file-sink writes video files when Replay jobs send streams to it",
        "directories_expected_to_grow_during_rtsp_runtime": [
            str(path)
            for path in (media_root, replay_dir, sink_root, evidence_root, evidence_audit_root)
            if path is not None
        ],
        "replay_storage_policy": {
            "classification": RETENTION_BOUNDED_CONFIG if replay_ttl else RETENTION_UNKNOWN,
            "kind": "ttl_store" if replay_ttl else "unknown",
            "data_expiration_ttl": replay_ttl,
            "compaction_period": replay_compaction,
            "max_disk_usage_configured": False,
            "storage_dir": str(replay_dir) if replay_dir else None,
        },
        "video_file_sink_policy": {
            "classification": RETENTION_UNKNOWN,
            "chunk_size": chunk_size,
            "rotation": "none_detected" if chunk_size in ("", "0") else "chunk_size_configured",
            "one_file_per_stream_or_job": chunk_size in ("", "0"),
            "cleanup_job_detected": False,
            "max_disk_usage_configured": False,
            "output_root": str(sink_root) if sink_root else None,
        },
        "cleanup_job_detected": False,
        "max_disk_usage_configuration_detected": False,
        "production_like_paths": [str(path) for path in (media_root, evidence_root, replay_dir) if path],
        "test_artifact_paths": [str(path) for path in (sink_root, evidence_audit_root, media_root / "debug", media_root / "_archive") if path],
        "event_style_replay_current_status": "checked_during_evidence_association",
        "stable_post_savant_sink_output_status": "checked_during_evidence_association",
    }


def runtime_errors(source_status: dict[str, Any], services: dict[str, Any]) -> list[str]:
    errors = []
    if source_status.get("input_type") != "rtsp":
        errors.append("active_input_not_rtsp")
    for name in ("source-adapter", "savant-security", "replay-service", "video-file-sink"):
        if not c2_13._dict(services.get(name)).get("running"):
            errors.append(f"{name}_not_running")
    return errors


def inspect_retention_config(architecture: dict[str, Any]) -> dict[str, Any]:
    answers = architecture.get("architecture_answers") or {}
    replay_policy = answers.get("replay_storage_policy") or {}
    sink_policy = answers.get("video_file_sink_policy") or {}
    evidence_policy = {
        "classification": RETENTION_UNKNOWN,
        "cleanup_job_detected": False,
        "max_disk_usage_configured": False,
        "ttl_configured": False,
        "path": architecture.get("active_paths", {}).get("evidence_root"),
    }
    return {
        "schema_version": "1.0",
        "replay_storage_policy": replay_policy,
        "video_file_sink_policy": sink_policy,
        "evidence_storage_policy": evidence_policy,
        "bounded_config_found": replay_policy.get("classification") == RETENTION_BOUNDED_CONFIG,
        "bounded_runtime_verified": False,
        "unknown_unbounded_risk": True,
        "retention_policy_status": RETENTION_UNKNOWN,
        "reason": "Replay TTL exists, but video-file-sink/evidence cleanup or max disk usage is not configured",
    }


def watched_media_paths(architecture: dict[str, Any], *, media_root: Path) -> list[Path]:
    paths = [
        media_root,
        media_root / "c2-post-savant-replay-fps-probe",
        media_root / "evidence",
        media_root / "evidence_audit",
    ]
    active = architecture.get("active_paths") or {}
    for key in ("replay_storage_dir", "video_file_sink_root", "evidence_root", "evidence_audit_root"):
        if active.get(key):
            paths.append(Path(str(active[key])))
    unique = []
    seen = set()
    for path in paths:
        if str(path) not in seen:
            unique.append(path)
            seen.add(str(path))
    return unique


def sample_disk_usage(sample_index: int, paths: list[Path]) -> list[dict[str, Any]]:
    sampled_at = datetime.now(timezone.utc).isoformat()
    return [
        {"sample_index": sample_index, "sampled_at": sampled_at, "path": str(path), **directory_snapshot(path)}
        for path in paths
    ]


def directory_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "bytes": 0, "file_count": 0, "newest_files": []}
    total = 0
    count = 0
    newest: list[tuple[float, int, str]] = []
    for root, _dirs, files in os.walk(path):
        for name in files:
            fpath = Path(root) / name
            try:
                stat = fpath.stat()
            except OSError:
                continue
            total += int(stat.st_size)
            count += 1
            newest.append((float(stat.st_mtime), int(stat.st_size), str(fpath)))
    newest.sort(reverse=True)
    return {
        "exists": True,
        "bytes": total,
        "file_count": count,
        "newest_files": [
            {
                "path": item[2],
                "size_bytes": item[1],
                "mtime": datetime.fromtimestamp(item[0], timezone.utc).isoformat(),
            }
            for item in newest[:10]
        ],
    }


def summarize_disk_growth(samples: list[dict[str, Any]]) -> dict[str, Any]:
    by_path: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        by_path.setdefault(str(sample["path"]), []).append(sample)
    paths = []
    initial_total = 0
    final_total = 0
    removal = False
    for path, rows in sorted(by_path.items()):
        rows.sort(key=lambda item: int(item["sample_index"]))
        first, last = rows[0], rows[-1]
        initial = int(first.get("bytes") or 0)
        final = int(last.get("bytes") or 0)
        first_count = int(first.get("file_count") or 0)
        last_count = int(last.get("file_count") or 0)
        delta = final - initial
        file_delta = last_count - first_count
        removal = removal or delta < 0 or file_delta < 0
        initial_total += initial
        final_total += final
        paths.append(
            {
                "path": path,
                "initial_size_bytes": initial,
                "final_size_bytes": final,
                "delta_bytes": delta,
                "initial_file_count": first_count,
                "final_file_count": last_count,
                "file_count_delta": file_delta,
                "newest_files": last.get("newest_files") or [],
            }
        )
    return {
        "schema_version": "1.0",
        "initial_size_bytes": initial_total,
        "final_size_bytes": final_total,
        "total_delta_bytes": final_total - initial_total,
        "paths": paths,
        "largest_growing_directories": sorted(paths, key=lambda item: int(item["delta_bytes"]), reverse=True)[:8],
        "newest_files": newest_files(paths),
        "automatic_removal_observed": removal,
        "growth_appears_bounded": removal,
        "unbounded_growth_risk_reported": True,
    }


def newest_files(paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    files = []
    for path in paths:
        files.extend(path.get("newest_files") or [])
    files.sort(key=lambda item: str(item.get("mtime") or ""), reverse=True)
    return files[:20]


def capture_rtsp_events(
    *,
    redis_client: redis.Redis,
    database_url: str,
    source_id: str,
    redis_before: dict[str, Any],
    start_time: datetime,
    end_time: datetime,
    max_messages: int,
) -> dict[str, Any]:
    event_start_id = str((redis_before.get("streams") or {}).get(c2_13.EVENT_STREAM, {}).get("last_id") or "0-0")
    redis_events = [
        normalize_event(event, origin="redis")
        for event in c2_13.parse_security_events(
            c2_13.read_stream_after(
                redis_client,
                c2_13.EVENT_STREAM,
                event_start_id,
                max_messages=max_messages,
            )
        )
        if event.get("source_id") == source_id and event.get("event_type") in EVENT_TYPES
    ]
    db_events = [normalize_event(event, origin="database") for event in fetch_recent_event_rows(database_url, source_id, start_time, end_time)]
    events = dedupe_events(redis_events + db_events)
    watchlist = [event for event in events if event.get("event_type") == "watchlist_hit"]
    intrusion = [event for event in events if event.get("event_type") == "intrusion"]
    return {
        "schema_version": "1.0",
        "capture_window": {
            "started_at": start_time.isoformat(),
            "ended_at": end_time.isoformat(),
            "duration_seconds": (end_time - start_time).total_seconds(),
        },
        "source_id": source_id,
        "events": events,
        "watchlist_events": watchlist,
        "intrusion_events": intrusion,
        "summary": {
            "events_captured": len(events),
            "watchlist_count": len(watchlist),
            "intrusion_count": len(intrusion),
            "event_ids": [event_identity(event) for event in events],
        },
    }


def fetch_recent_event_rows(database_url: str, source_id: str, start_time: datetime, end_time: datetime) -> list[dict[str, Any]]:
    try:
        with psycopg.connect(database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id::text AS id, source_event_id, event_type,
                           camera_id, source_id, track_id, person_id,
                           confidence, severity, rule_name, created_at, payload
                    FROM events
                    WHERE created_at >= %(start_time)s
                      AND created_at <= %(end_time)s
                      AND source_id = %(source_id)s
                      AND event_type = ANY(%(event_types)s)
                    ORDER BY created_at ASC
                    LIMIT 200
                    """,
                    {
                        "start_time": start_time,
                        "end_time": end_time,
                        "source_id": source_id,
                        "event_types": list(EVENT_TYPES),
                    },
                )
                return [c2_13.strip_unsafe_payload(c2_13.json_safe(dict(row))) for row in cur.fetchall()]
    except Exception:
        return []


def inspect_event_counts(database_url: str, source_id: str) -> dict[str, Any]:
    try:
        with psycopg.connect(database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT event_type, COUNT(*) AS row_count, MAX(created_at) AS max_created_at
                    FROM events
                    WHERE source_id = %(source_id)s
                    GROUP BY event_type
                    ORDER BY event_type
                    """,
                    {"source_id": source_id},
                )
                return {"status": "ok", "rows": [c2_13.json_safe(dict(row)) for row in cur.fetchall()]}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def normalize_event(event: dict[str, Any], *, origin: str) -> dict[str, Any]:
    safe = c2_13.strip_unsafe_payload(c2_13.json_safe(event))
    payload = safe.get("payload") if isinstance(safe.get("payload"), dict) else {}
    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    observation = payload.get("observation") if isinstance(payload.get("observation"), dict) else {}
    match = payload.get("match") if isinstance(payload.get("match"), dict) else {}
    return {
        "origin": origin,
        "event_id": safe.get("id"),
        "redis_id": safe.get("redis_id"),
        "source_event_id": safe.get("source_event_id"),
        "event_type": safe.get("event_type"),
        "source_id": safe.get("source_id"),
        "camera_id": safe.get("camera_id"),
        "track_id": safe.get("track_id"),
        "source_observation_id": first_non_empty(safe.get("source_observation_id"), match.get("source_observation_id"), observation.get("source_observation_id")),
        "frame_pts": to_int_or_none(first_non_empty(safe.get("frame_pts"), media.get("frame_pts"), safe.get("timestamp_ms"))),
        "timestamp_ms": to_int_or_none(first_non_empty(safe.get("timestamp_ms"), observation.get("timestamp_ms"))),
        "event_ts_ms": to_int_or_none(first_non_empty(safe.get("event_ts_ms"), media.get("event_ts_ms"), safe.get("end_ts_ms"))),
        "created_at": safe.get("created_at"),
        "person_id": safe.get("person_id"),
        "external_person_id": first_non_empty(safe.get("external_person_id"), observation.get("external_person_id")),
        "rule_id": first_non_empty(safe.get("rule_id"), safe.get("rule_name"), payload.get("rule"), payload.get("zone_id")),
        "roi_id": first_non_empty(payload.get("zone_id"), safe.get("zone")),
        "frame_uuid": first_non_empty(safe.get("frame_uuid"), media.get("frame_uuid")),
        "keyframe_uuid": first_non_empty(safe.get("keyframe_uuid"), media.get("keyframe_uuid")),
        "previous_keyframe_uuid": media.get("previous_keyframe_uuid"),
        "raw_event": safe,
    }


def dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for event in events:
        key = event.get("source_event_id") or event.get("event_id") or event.get("redis_id")
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    out.sort(key=lambda item: str(item.get("created_at") or item.get("redis_id") or ""))
    return out


def associate_visual_evidence(
    *,
    events: list[dict[str, Any]],
    architecture: dict[str, Any],
    output_dir: Path,
    replay_api_url: str,
    replay_sink_url: str,
    try_replay: bool,
    replay_wait_seconds: float,
) -> dict[str, Any]:
    attempts: dict[str, Any] = {
        "schema_version": "1.0",
        "priority_a_sink_attempts": [],
        "priority_b_replay_attempts": [],
        "priority_c_no_visual_evidence": [],
        "generated_bundles": [],
    }
    if not events:
        attempts["priority_c_no_visual_evidence"].append({"status": "partial", "reason": "no_events_in_window"})
        return attempts
    sink_root_text = (architecture.get("active_paths") or {}).get("video_file_sink_root")
    sink_root = Path(sink_root_text) if sink_root_text else None
    replay_attempted = False
    for event in events:
        sink_attempt = try_existing_sink_coverage(event=event, sink_root=sink_root, output_dir=output_dir)
        attempts["priority_a_sink_attempts"].append(sink_attempt)
        if sink_attempt.get("evidence_status") == "pass":
            attempts["generated_bundles"].append(sink_attempt["bundle_path"])
            continue
        replay_attempt = {"event_identity": event_identity(event), "priority": "B", "status": REPLAY_NOT_ATTEMPTED, "reason": "try_replay_disabled"}
        if try_replay and not replay_attempted and not attempts["generated_bundles"]:
            replay_attempted = True
            replay_attempt = try_replay_event_video(
                event=event,
                sink_root=sink_root,
                output_dir=output_dir,
                replay_api_url=replay_api_url,
                replay_sink_url=replay_sink_url,
                replay_wait_seconds=replay_wait_seconds,
            )
        elif try_replay and replay_attempted:
            replay_attempt = {
                "event_identity": event_identity(event),
                "priority": "B",
                "status": REPLAY_NOT_ATTEMPTED,
                "reason": "one_replay_event_already_attempted",
            }
        attempts["priority_b_replay_attempts"].append(replay_attempt)
    if not attempts["generated_bundles"]:
        reasons = sorted(
            set(
                str(item.get("reason") or item.get("join_failure_reason") or "no_visual_evidence")
                for item in attempts["priority_a_sink_attempts"] + attempts["priority_b_replay_attempts"]
            )
        )
        attempts["priority_c_no_visual_evidence"].append({"status": "partial", "reason": reasons[0] if reasons else "no_visual_evidence", "reasons": reasons})
    return attempts


def try_existing_sink_coverage(*, event: dict[str, Any], sink_root: Path | None, output_dir: Path) -> dict[str, Any]:
    if sink_root is None or not sink_root.exists():
        return {"event_identity": event_identity(event), "priority": "A", "status": "no_sink_coverage", "reason": "video_file_sink_not_recording", "evidence_status": "fail"}
    checked = []
    for candidate in find_sink_candidates(sink_root):
        join = inspect_sink_event_join(candidate, event, mode="direct_sink")
        checked.append(join)
        if join.get("event_frame_located"):
            return build_event_evidence_bundle(
                input_dir=candidate,
                event=event,
                output_dir=output_dir / f"c2_13v_rtsp_event_evidence_{run_stamp()}",
                join_report=join,
                capture_mode="stable_rtsp_post_savant_sink",
                replay_request=None,
            )
    return {
        "event_identity": event_identity(event),
        "priority": "A",
        "status": "no_sink_coverage",
        "reason": "no_sink_coverage",
        "checked_candidate_count": len(checked),
        "checked_candidates": checked[:20],
        "evidence_status": "fail",
    }


def try_replay_event_video(
    *,
    event: dict[str, Any],
    sink_root: Path | None,
    output_dir: Path,
    replay_api_url: str,
    replay_sink_url: str,
    replay_wait_seconds: float,
) -> dict[str, Any]:
    keyframe_uuid = event.get("previous_keyframe_uuid") or event.get("keyframe_uuid")
    if not keyframe_uuid:
        return {"event_identity": event_identity(event), "priority": "B", "status": REPLAY_FAILED, "reason": "timestamp_mapping_gap", "evidence_status": "fail"}
    if sink_root is None:
        return {"event_identity": event_identity(event), "priority": "B", "status": REPLAY_FAILED, "reason": "video_file_sink_not_recording", "evidence_status": "fail"}
    resulting_stream_id = f"replay-event-c2-13v-{sanitize_id(event_identity(event))[:42]}"
    payload = build_replay_job_payload(event=event, keyframe_uuid=str(keyframe_uuid), replay_sink_url=replay_sink_url, resulting_stream_id=resulting_stream_id)
    if fixed_frame_count_as_duration_proxy(payload, requested_duration_s=10.0):
        return {"event_identity": event_identity(event), "priority": "B", "status": REPLAY_FAILED, "reason": "fixed_frame_count_duration_proxy_rejected", "replay_payload": payload, "evidence_status": "fail"}
    response = submit_replay_job(replay_api_url, payload)
    if not response.get("accepted"):
        return {"event_identity": event_identity(event), "priority": "B", "status": REPLAY_FAILED, "reason": "replay_job_rejected", "replay_response": response, "replay_payload": payload, "evidence_status": "fail"}
    sink_output = wait_for_replay_sink_output(sink_root=sink_root, resulting_stream_id=resulting_stream_id, wait_seconds=replay_wait_seconds)
    if not sink_output.get("ready"):
        return {"event_identity": event_identity(event), "priority": "B", "status": REPLAY_FAILED, "reason": "replay_retention_gap", "replay_response": response, "replay_payload": payload, "sink_output": sink_output, "evidence_status": "fail"}
    join = inspect_sink_event_join(Path(str(sink_output["sink_dir"])), event, mode="replay_job", replay_payload=payload)
    bundle = build_event_evidence_bundle(
        input_dir=Path(str(sink_output["sink_dir"])),
        event=event,
        output_dir=output_dir / f"c2_13v_rtsp_event_evidence_{run_stamp()}",
        join_report=join,
        capture_mode="event_style_replay_time_domain",
        replay_request=payload,
    )
    bundle.update({"priority": "B", "status": REPLAY_PASSED if bundle.get("evidence_status") == "pass" else REPLAY_FAILED, "replay_payload": payload, "replay_response": response, "sink_output": sink_output})
    return bundle


def find_sink_candidates(sink_root: Path) -> list[Path]:
    candidates = []
    try:
        for metadata in sink_root.glob("*%/*%/metadata.json"):
            if any((metadata.parent / name).is_file() for name in VIDEO_CANDIDATES):
                candidates.append(metadata.parent)
    except OSError:
        return []
    candidates.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0.0, reverse=True)
    return candidates[:100]


def inspect_sink_event_join(
    sink_dir: Path,
    event: dict[str, Any],
    *,
    mode: str,
    replay_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata_path = first_existing(sink_dir, METADATA_CANDIDATES)
    video_path = first_existing(sink_dir, VIDEO_CANDIDATES)
    if metadata_path is None or video_path is None:
        return {"sink_dir": str(sink_dir), "metadata_exists": metadata_path is not None, "video_exists": video_path is not None, "event_frame_located": False, "join_failure_reason": "metadata_or_video_missing"}
    try:
        frames = load_native_metadata(metadata_path)
    except Exception as exc:
        return {"sink_dir": str(sink_dir), "metadata_path": str(metadata_path), "video_path": str(video_path), "event_frame_located": False, "join_failure_reason": "metadata_parse_failed", "error": str(exc)}
    event_source = str(event.get("source_id") or "")
    event_pts = to_int_or_none(event.get("frame_pts"))
    event_uuid = str(event.get("frame_uuid") or "")
    pts_values = [to_int_or_none(frame.get("pts") or frame.get("frame_pts")) for frame in frames]
    pts_values = [value for value in pts_values if value is not None]
    sources = sorted(set(str(frame.get("source_id") or "") for frame in frames if frame.get("source_id")))
    if mode == "replay_job":
        source_matches = nested(replay_payload or {}, "configuration", "stored_stream_id") == event_source
    else:
        source_matches = event_source in sources
    uuid_match = bool(event_uuid) and any(str(frame.get("uuid") or frame.get("frame_uuid") or "") == event_uuid for frame in frames)
    pts_match = event_pts is not None and any(abs(int(value) - event_pts) <= 1 for value in pts_values)
    pts_covered = event_pts is not None and bool(pts_values) and min(pts_values) <= event_pts <= max(pts_values)
    covered = bool(uuid_match or pts_match or pts_covered)
    located = bool(source_matches and covered)
    return {
        "sink_dir": str(sink_dir),
        "metadata_path": str(metadata_path),
        "video_path": str(video_path),
        "metadata_exists": True,
        "video_exists": True,
        "mode": mode,
        "source_id_matches": bool(source_matches),
        "metadata_sources": sources,
        "event_source_id": event_source,
        "event_frame_pts": event_pts,
        "event_frame_uuid": event_uuid or None,
        "metadata_frame_count": len(frames),
        "metadata_first_pts": min(pts_values) if pts_values else None,
        "metadata_last_pts": max(pts_values) if pts_values else None,
        "timestamp_or_frame_covered": covered,
        "event_frame_located": located,
        "frame_uuid_match": uuid_match,
        "frame_pts_match": pts_match,
        "join_failure_reason": None if located else join_failure_reason(source_matches=bool(source_matches), covered=covered, pts_values=pts_values),
    }


def join_failure_reason(*, source_matches: bool, covered: bool, pts_values: list[int]) -> str:
    if not source_matches:
        return "no_sink_coverage"
    if not pts_values or not covered:
        return "timestamp_mapping_gap"
    return "sidecar_join_gap"


def build_event_evidence_bundle(
    *,
    input_dir: Path,
    event: dict[str, Any],
    output_dir: Path,
    join_report: dict[str, Any],
    capture_mode: str,
    replay_request: dict[str, Any] | None,
) -> dict[str, Any]:
    video_path = first_existing(input_dir, VIDEO_CANDIDATES)
    metadata_path = first_existing(input_dir, METADATA_CANDIDATES)
    if video_path is None or metadata_path is None:
        return {"event_identity": event_identity(event), "status": "metadata_or_video_missing", "reason": "metadata_or_video_missing", "evidence_status": "fail"}
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_clip_path = output_dir / ("raw_clip.mp4" if video_path.suffix == ".mp4" else "raw_clip.mov")
    sink_metadata_path = output_dir / "sink_metadata.json"
    shutil.copy2(video_path, raw_clip_path)
    shutil.copy2(metadata_path, sink_metadata_path)
    sidecar_path = output_dir / SIDECAR_ANNOTATIONS_FILE
    sidecar_summary_path = output_dir / "summary.frame_cache.identity.json"
    sidecar = build_post_savant_annotation_sidecar(metadata_path=sink_metadata_path, output_jsonl_path=sidecar_path, summary_path=sidecar_summary_path)
    integrity = inspect_video_integrity(
        raw_clip_path,
        decode_log_path=output_dir / "video_integrity_decode_errors.log",
        sidecar_frame_count=len(sidecar.rows),
        time_domain_crop_applied=False,
        trim_occurred=False,
    )
    acceptance = evaluate_video_evidence_acceptance(
        raw_clip_exists=raw_clip_path.is_file() and raw_clip_path.stat().st_size > 0,
        metadata_exists=sink_metadata_path.is_file(),
        sidecar_exists=sidecar_path.is_file(),
        source_id_matches=bool(join_report.get("source_id_matches")),
        timestamp_or_frame_covered=bool(join_report.get("timestamp_or_frame_covered")),
        event_frame_located=bool(join_report.get("event_frame_located")),
        decoded_video_frame_count=to_int_or_none(integrity.get("decoded_frame_count")),
        sidecar_frame_count=len(sidecar.rows),
        video_integrity_pass=bool(integrity.get("production_gate_passed")),
        fallback_used=False,
        legacy_used_for_visual_binding=False,
        db_window_fallback_used=False,
        safe_time_domain_crop=False,
    )
    summary = {
        "schema_version": "1.0",
        "phase": "C2.13V",
        "evidence_capture_mode": capture_mode,
        "event_identity": event_identity(event),
        "event_type": event.get("event_type"),
        "source_id": event.get("source_id"),
        "camera_id": event.get("camera_id"),
        "event_frame_pts": event.get("frame_pts"),
        "event_ts_ms": event.get("event_ts_ms"),
        "source_event_id": event.get("source_event_id"),
        "event_id": event.get("event_id"),
        "track_id": event.get("track_id"),
        "person_id": event.get("person_id"),
        "external_person_id": event.get("external_person_id"),
        "roi_id": event.get("roi_id"),
        "rule_id": event.get("rule_id"),
        "raw_clip": str(raw_clip_path),
        "sink_metadata": str(sink_metadata_path),
        "sidecar": str(sidecar_path),
        "decoded_video_frame_count": integrity.get("decoded_frame_count"),
        "sidecar_frame_count": len(sidecar.rows),
        "video_integrity_status": integrity.get("integrity_status"),
        "video_integrity_pass": bool(integrity.get("production_gate_passed")),
        "event_frame_located": bool(join_report.get("event_frame_located")),
        "fallback_used": False,
        "legacy_used_for_visual_binding": False,
        "db_window_fallback_used": False,
        "replay_request_used": replay_request is not None,
        "replay_request": redact_runtime_value(replay_request),
        "acceptance": acceptance,
    }
    write_json(output_dir / "summary.json", summary)
    write_json(output_dir / "event.json", event)
    write_json(output_dir / "evidence_join_report.json", join_report)
    write_json(output_dir / "video_integrity_report.json", integrity)
    (output_dir / "operator_rtsp_event_evidence.html").write_text(render_event_evidence_html(summary, event, join_report, integrity), encoding="utf-8")
    return {
        "event_identity": event_identity(event),
        "priority": "A",
        "status": "pass" if acceptance["passed"] else "partial",
        "reason": None if acceptance["passed"] else first_or_none(acceptance["failure_reasons"]),
        "evidence_status": "pass" if acceptance["passed"] else "fail",
        "bundle_path": str(output_dir),
        "raw_clip": str(raw_clip_path),
        "summary": summary,
        "join_report": join_report,
        "video_integrity_report": integrity,
        "acceptance": acceptance,
    }


def evaluate_video_evidence_acceptance(
    *,
    raw_clip_exists: bool,
    metadata_exists: bool,
    sidecar_exists: bool,
    source_id_matches: bool,
    timestamp_or_frame_covered: bool,
    event_frame_located: bool,
    decoded_video_frame_count: int | None,
    sidecar_frame_count: int | None,
    video_integrity_pass: bool,
    fallback_used: bool,
    legacy_used_for_visual_binding: bool,
    db_window_fallback_used: bool,
    safe_time_domain_crop: bool,
) -> dict[str, Any]:
    failures = []
    if not raw_clip_exists:
        failures.append("raw_clip_missing")
    if not metadata_exists:
        failures.append("metadata_missing")
    if not sidecar_exists:
        failures.append("sidecar_missing")
    if not source_id_matches:
        failures.append("source_id_mismatch")
    if not timestamp_or_frame_covered:
        failures.append("timestamp_mapping_gap")
    if not event_frame_located:
        failures.append("event_frame_not_located")
    if not video_integrity_pass:
        failures.append("video_integrity_failed")
    if fallback_used:
        failures.append("fallback_used")
    if legacy_used_for_visual_binding:
        failures.append("legacy_used_for_visual_binding")
    if db_window_fallback_used:
        failures.append("db_window_fallback_used")
    if decoded_video_frame_count is None or decoded_video_frame_count <= 0:
        failures.append("decoded_video_frame_count_missing")
    if sidecar_frame_count is None or sidecar_frame_count <= 0:
        failures.append("sidecar_frame_count_missing")
    if decoded_video_frame_count is not None and sidecar_frame_count is not None and decoded_video_frame_count != sidecar_frame_count and not safe_time_domain_crop:
        failures.append("decoded_sidecar_frame_count_mismatch")
    return {
        "passed": not failures,
        "failure_reasons": failures,
        "fallback_used": fallback_used,
        "legacy_used_for_visual_binding": legacy_used_for_visual_binding,
        "db_window_fallback_used": db_window_fallback_used,
    }


def build_replay_job_payload(*, event: dict[str, Any], keyframe_uuid: str, replay_sink_url: str, resulting_stream_id: str) -> dict[str, Any]:
    frame_duration = replay_frame_duration(event)
    return {
        "sink": {"url": replay_sink_url, "options": reliable_sink_options()},
        "configuration": {
            "ts_sync": True,
            "skip_intermediary_eos": False,
            "send_eos": True,
            "stop_on_incorrect_ts": False,
            "stored_stream_id": str(event.get("source_id") or ""),
            "resulting_stream_id": resulting_stream_id,
            "routing_labels": "bypass",
            "max_idle_duration": {"secs": 10, "nanos": 0},
            "max_delivery_duration": {"secs": 45, "nanos": 0},
            "ts_discrepancy_fix_duration": frame_duration,
            "min_duration": frame_duration,
            "max_duration": frame_duration,
            "send_metadata_only": False,
            "labels": {
                "phase": "c2_13v",
                "event_id": event_identity(event),
                "event_type": str(event.get("event_type") or ""),
                "annotation_source_policy": "post_savant_sink_metadata_only",
            },
        },
        "stop_condition": {"ts_delta_sec": {"max_delta_sec": 10.0}},
        "anchor_keyframe": keyframe_uuid,
        "anchor_wait_duration": {"secs": 1, "nanos": 0},
        "offset": {"seconds": 0.0},
        "attributes": [],
    }


def replay_frame_duration(event: dict[str, Any]) -> dict[str, int]:
    raw_event = event.get("raw_event") if isinstance(event.get("raw_event"), dict) else {}
    payload = raw_event.get("payload") if isinstance(raw_event.get("payload"), dict) else {}
    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    duration_ns = to_int_or_none(first_non_empty(media.get("duration"), raw_event.get("duration")))
    if duration_ns is None or duration_ns <= 0:
        duration_ns = 41_666_667
    return {"secs": int(duration_ns // 1_000_000_000), "nanos": int(duration_ns % 1_000_000_000)}


def reliable_sink_options() -> dict[str, Any]:
    return {
        "send_timeout": {"secs": 5, "nanos": 0},
        "send_retries": 5,
        "receive_timeout": {"secs": 5, "nanos": 0},
        "receive_retries": 5,
        "send_hwm": 10000,
        "receive_hwm": 10000,
        "inflight_ops": 100,
    }


def fixed_frame_count_as_duration_proxy(payload: dict[str, Any], *, requested_duration_s: float) -> bool:
    stop = payload.get("stop_condition") if isinstance(payload, dict) else {}
    if not isinstance(stop, dict) or "frame_count" not in stop:
        return False
    measured_fps = (payload.get("configuration") or {}).get("measured_replay_fps") if isinstance(payload.get("configuration"), dict) else None
    return "ts_delta_sec" not in stop and measured_fps is None and int(stop["frame_count"]) == 240 and requested_duration_s == 10.0


def submit_replay_job(replay_api_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        replay_api_url.rstrip("/") + "/api/v1/job",
        data=json.dumps(payload).encode("utf-8"),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            return {"accepted": 200 <= int(response.status) < 300, "status_code": response.status, "body": json_loads_or_text(body)}
    except urllib.error.HTTPError as exc:
        return {"accepted": False, "status_code": exc.code, "body": json_loads_or_text(exc.read(4096).decode("utf-8", errors="replace"))}
    except Exception as exc:
        return {"accepted": False, "status_code": None, "error": str(exc)}


def wait_for_replay_sink_output(*, sink_root: Path, resulting_stream_id: str, wait_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    latest = None
    last_size = -1
    stable = 0
    while time.monotonic() < deadline:
        candidates = sorted(sink_root.glob(f"{resulting_stream_id}%/*%"), key=lambda path: path.stat().st_mtime, reverse=True)
        if candidates:
            latest = candidates[0]
            metadata = first_existing(latest, METADATA_CANDIDATES)
            video = first_existing(latest, VIDEO_CANDIDATES)
            if metadata and video and metadata.stat().st_size > 0 and video.stat().st_size > 0:
                size = metadata.stat().st_size + video.stat().st_size
                stable = stable + 1 if size == last_size else 0
                last_size = size
                if stable >= 2:
                    return {"ready": True, "sink_dir": str(latest), "metadata_path": str(metadata), "video_path": str(video), "size_bytes": size}
        time.sleep(2.0)
    return {"ready": False, "sink_dir": str(latest) if latest else None, "reason": "replay_output_not_ready_before_timeout"}


def build_video_evidence_results(attempts: dict[str, Any]) -> dict[str, Any]:
    generated = attempts.get("generated_bundles") or []
    replay_status = REPLAY_NOT_ATTEMPTED
    integrity_status = "not_run"
    event_types = []
    for attempt in attempts.get("priority_a_sink_attempts", []) + attempts.get("priority_b_replay_attempts", []):
        if attempt.get("priority") == "B" and attempt.get("status") in {REPLAY_PASSED, REPLAY_FAILED}:
            replay_status = str(attempt.get("status"))
        if attempt.get("video_integrity_report", {}).get("integrity_status") and integrity_status == "not_run":
            integrity_status = attempt["video_integrity_report"]["integrity_status"]
        if attempt.get("evidence_status") == "pass":
            event_type = (attempt.get("summary") or {}).get("event_type")
            if event_type:
                event_types.append(event_type)
    return {
        "schema_version": "1.0",
        "evidence_generated_count": len(generated),
        "evidence_bundle_paths": generated,
        "replay_video_status": replay_status,
        "video_integrity_status": integrity_status,
        "event_types_with_evidence": sorted(set(event_types)),
        "visual_evidence_optional_for_algorithm_event_pass": True,
        "visual_evidence_required_for_evidence_pass": True,
    }


def decide_retention_policy_status(*, config_report: dict[str, Any], disk_growth_summary: dict[str, Any]) -> dict[str, Any]:
    replay_policy = config_report.get("replay_storage_policy") or {}
    sink_policy = config_report.get("video_file_sink_policy") or {}
    evidence_policy = config_report.get("evidence_storage_policy") or {}
    path_summaries = disk_growth_summary.get("paths") or []
    replay_path = str(replay_policy.get("storage_dir") or "")
    replay_runtime_bounded = any(
        str(item.get("path")) == replay_path and int(item.get("delta_bytes") or 0) < 0
        for item in path_summaries
    )
    sink_runtime_bounded = any(
        str(item.get("path")) == str(sink_policy.get("output_root") or "")
        and int(item.get("delta_bytes") or 0) < 0
        for item in path_summaries
    )
    evidence_runtime_bounded = any(
        str(item.get("path")) == str(evidence_policy.get("path") or "")
        and int(item.get("delta_bytes") or 0) < 0
        for item in path_summaries
    )
    runtime_verified = replay_runtime_bounded and sink_runtime_bounded and evidence_runtime_bounded
    all_bounded = all(policy.get("classification") == RETENTION_BOUNDED_CONFIG for policy in (replay_policy, sink_policy, evidence_policy))
    status = RETENTION_BOUNDED_RUNTIME if runtime_verified else RETENTION_BOUNDED_CONFIG if all_bounded else RETENTION_UNKNOWN
    return {
        "retention_policy_status": status,
        "bounded_config_found": status in {RETENTION_BOUNDED_CONFIG, RETENTION_BOUNDED_RUNTIME},
        "bounded_runtime_verified": status == RETENTION_BOUNDED_RUNTIME,
        "unknown_unbounded_risk": status == RETENTION_UNKNOWN,
        "replay_runtime_bounded_observed": replay_runtime_bounded,
        "video_file_sink_runtime_bounded_observed": sink_runtime_bounded,
        "evidence_runtime_bounded_observed": evidence_runtime_bounded,
        "replay_storage_policy": replay_policy,
        "video_file_sink_policy": sink_policy,
        "evidence_storage_policy": evidence_policy,
        "disk_growth_delta_bytes": disk_growth_summary.get("total_delta_bytes"),
        "automatic_removal_observed": disk_growth_summary.get("automatic_removal_observed"),
        "unbounded_growth_risk_reported": status == RETENTION_UNKNOWN,
    }


def decide_overall_marker(
    *,
    input_type: str,
    event_count: int,
    evidence_generated_count: int,
    retention_policy_status: str,
    unsafe_payload_passed: bool,
    runtime_errors: list[str],
) -> dict[str, str]:
    if not unsafe_payload_passed:
        return {"result_marker": RESULT_FAIL, "reason": "unsafe_payload_scan_failed"}
    if runtime_errors:
        return {"result_marker": RESULT_FAIL, "reason": "runtime_errors:" + ",".join(runtime_errors)}
    if input_type != "rtsp":
        return {"result_marker": RESULT_FAIL, "reason": "active_input_not_rtsp"}
    if event_count <= 0:
        return {"result_marker": RESULT_NO_EVENTS, "reason": "no_watchlist_or_intrusion_events_in_window"}
    if evidence_generated_count <= 0:
        return {"result_marker": RESULT_VIDEO_GAP, "reason": "events_ready_but_video_evidence_gap"}
    if retention_policy_status not in {RETENTION_BOUNDED_CONFIG, RETENTION_BOUNDED_RUNTIME}:
        return {"result_marker": RESULT_RETENTION_UNKNOWN, "reason": "retention_policy_unknown_unbounded_risk"}
    return {"result_marker": RESULT_PASS, "reason": "rtsp_event_video_evidence_and_bounded_retention_ready"}


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c2_13.strip_internal(c2_13.json_safe(value)), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_disk_samples_csv(path: Path, samples: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sample_index", "sampled_at", "path", "exists", "bytes", "file_count"])
        writer.writeheader()
        for sample in samples:
            writer.writerow({key: sample.get(key) for key in writer.fieldnames})


def first_existing(directory: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = directory / name
        if path.is_file():
            return path
    return None


def event_identity(event: dict[str, Any]) -> str:
    return str(event.get("event_id") or event.get("source_event_id") or event.get("redis_id") or "unknown")


def sanitize_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in value.lower()).strip("-")
    return cleaned or "unknown"


def run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def first_or_none(values: list[str]) -> str | None:
    return values[0] if values else None


def to_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def nested(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def redact_runtime_value(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, nested_value in value.items():
            lowered = str(key).lower()
            if "password" in lowered or "secret" in lowered or "token" in lowered:
                out[key] = "***"
            elif "url" in lowered or "uri" in lowered:
                out[key] = c2_13.redact_url(str(nested_value)) if nested_value is not None else None
            else:
                out[key] = redact_runtime_value(nested_value)
        return out
    if isinstance(value, list):
        return [redact_runtime_value(item) for item in value]
    if isinstance(value, str) and value.startswith("rtsp://"):
        return c2_13.redact_url(value)
    return value


def json_loads_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return text[:1000]


def render_operator_report(
    *,
    summary: dict[str, Any],
    architecture: dict[str, Any],
    retention: dict[str, Any],
    disk_summary: dict[str, Any],
    captured: dict[str, Any],
    evidence_results: dict[str, Any],
    attempts: dict[str, Any],
) -> str:
    rows = [
        ("Result marker", summary.get("result_marker")),
        ("Input type", summary.get("input_type")),
        ("Source ID", summary.get("source_id")),
        ("Camera ID", summary.get("camera_id")),
        ("Retention policy", retention.get("retention_policy_status")),
        ("Disk growth delta bytes", disk_summary.get("total_delta_bytes")),
        ("Watchlist events", captured.get("summary", {}).get("watchlist_count")),
        ("Intrusion events", captured.get("summary", {}).get("intrusion_count")),
        ("Evidence bundles", evidence_results.get("evidence_generated_count")),
        ("Replay status", evidence_results.get("replay_video_status")),
        ("Video integrity", evidence_results.get("video_integrity_status")),
        ("Unsafe payload scan", summary.get("unsafe_payload_scan_passed")),
        ("Output dir", summary.get("output_dir")),
    ]
    row_html = "\n".join(f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows)
    growth = "\n".join(
        f"<li>{html.escape(str(item.get('path')))}: {html.escape(str(item.get('delta_bytes')))} bytes</li>"
        for item in (disk_summary.get("largest_growing_directories") or [])[:6]
    )
    no_visual = attempts.get("priority_c_no_visual_evidence") or []
    reason = no_visual[0].get("reason") if no_visual else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>C2.13V RTSP Video Evidence Retention Audit</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; line-height: 1.45; }}
    table {{ border-collapse: collapse; min-width: 820px; }}
    th, td {{ border: 1px solid #bbb; padding: 8px 10px; text-align: left; }}
    th {{ background: #f3f3f3; width: 280px; }}
  </style>
</head>
<body>
  <h1>C2.13V RTSP Video Evidence Retention Audit</h1>
  <table>{row_html}</table>
  <h2>Top Growing Directories</h2>
  <ul>{growth}</ul>
  <p>Video evidence gap reason: {html.escape(str(reason))}</p>
  <p>Replay storage: {html.escape(str(nested(architecture, 'architecture_answers', 'replay_storage_policy', 'classification')))}</p>
  <p>Video-file-sink storage: {html.escape(str(nested(architecture, 'architecture_answers', 'video_file_sink_policy', 'classification')))}</p>
</body>
</html>
"""


def render_event_evidence_html(summary: dict[str, Any], event: dict[str, Any], join_report: dict[str, Any], integrity: dict[str, Any]) -> str:
    rows = [
        ("Event type", event.get("event_type")),
        ("Source event ID", event.get("source_event_id")),
        ("Source ID", event.get("source_id")),
        ("Track ID", event.get("track_id")),
        ("Frame PTS", event.get("frame_pts")),
        ("Frame UUID", event.get("frame_uuid")),
        ("Event frame located", join_report.get("event_frame_located")),
        ("Video integrity", integrity.get("integrity_status")),
        ("Acceptance passed", summary.get("acceptance", {}).get("passed")),
        ("Raw clip", summary.get("raw_clip")),
    ]
    row_html = "\n".join(f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows)
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>C2.13V RTSP Event Evidence</title></head>
<body>
  <h1>C2.13V RTSP Event Evidence</h1>
  <table>{row_html}</table>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--redis-url", default=os.getenv("C2_13V_REDIS_URL", DEFAULT_REDIS_URL))
    parser.add_argument("--replay-api-url", default=os.getenv("C2_13V_REPLAY_API_URL", DEFAULT_REPLAY_API_URL))
    parser.add_argument("--replay-sink-url", default=os.getenv("C2_13V_REPLAY_SINK_URL", DEFAULT_REPLAY_SINK_URL))
    parser.add_argument("--disk-sample-seconds", type=float, default=float(os.getenv("C2_13V_DISK_SAMPLE_SECONDS", DEFAULT_DISK_SAMPLE_SECONDS)))
    parser.add_argument("--disk-sample-interval-seconds", type=float, default=float(os.getenv("C2_13V_DISK_SAMPLE_INTERVAL_SECONDS", DEFAULT_DISK_SAMPLE_INTERVAL_SECONDS)))
    parser.add_argument("--event-capture-seconds", type=float, default=float(os.getenv("C2_13V_EVENT_CAPTURE_SECONDS", DEFAULT_EVENT_CAPTURE_SECONDS)))
    parser.add_argument("--max-messages", type=int, default=int(os.getenv("C2_13V_MAX_MESSAGES", DEFAULT_MAX_MESSAGES)))
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--camera-config", type=Path, default=DEFAULT_CAMERA_CONFIG)
    parser.add_argument("--replay-config", type=Path, default=DEFAULT_REPLAY_CONFIG)
    parser.add_argument("--media-root", type=Path, default=DEFAULT_MEDIA_ROOT)
    parser.add_argument("--evidence-audit-root", type=Path, default=DEFAULT_EVIDENCE_AUDIT_ROOT)
    parser.add_argument("--try-replay", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--replay-wait-seconds", type=float, default=float(os.getenv("C2_13V_REPLAY_WAIT_SECONDS", DEFAULT_REPLAY_WAIT_SECONDS)))
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = args.run_id or f"c2_13v_rtsp_video_retention_audit_{run_stamp()}"
    output_dir = args.output_dir or args.evidence_root / run_id
    summary = run_audit(
        output_dir=output_dir,
        database_url=args.database_url,
        redis_url=args.redis_url,
        replay_api_url=args.replay_api_url,
        replay_sink_url=args.replay_sink_url,
        disk_sample_seconds=args.disk_sample_seconds,
        disk_sample_interval_seconds=args.disk_sample_interval_seconds,
        event_capture_seconds=args.event_capture_seconds,
        max_messages=args.max_messages,
        compose_file=args.compose_file,
        env_file=args.env_file,
        camera_config_path=args.camera_config,
        replay_config_path=args.replay_config,
        media_root=args.media_root,
        evidence_audit_root=args.evidence_audit_root,
        try_replay=args.try_replay,
        replay_wait_seconds=args.replay_wait_seconds,
        overwrite=args.overwrite,
    )
    print(
        json.dumps(
            {
                "result_marker": summary["result_marker"],
                "input_type": summary.get("input_type"),
                "source_id": summary.get("source_id"),
                "retention_policy_status": summary.get("retention_policy_status"),
                "disk_growth_delta": summary.get("disk_growth_delta"),
                "events_captured": summary.get("events_captured"),
                "evidence_generated_count": summary.get("evidence_generated_count"),
                "replay_video_status": summary.get("replay_video_status"),
                "output_dir": str(output_dir),
                "summary": str(output_dir / "decision_summary.json"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if summary["result_marker"] != RESULT_FAIL else 2


if __name__ == "__main__":
    raise SystemExit(main())
