#!/usr/bin/env python3
"""C2.13P RTSP pose/person observation repair probe for intrusion.

This probe keeps the RTSP path as the source of truth. It diagnoses the active
Savant pose/person path, verifies whether BehaviorRulesPyFunc can see the
active RTSP source configuration, optionally syncs the already-committed camera
config into the mounted C2 runtime module copy, and runs a bounded smoke for
``security.person_observations`` plus real intrusion events.

The tool never creates synthetic person observations or intrusion events. The
only Redis writes expected during PASS are produced by the Savant runtime. A
bounded local event-worker consumer may persist real runtime events from
``security.events`` into PostgreSQL.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import redis
import yaml
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[2]
SERVICES_EVENT_WORKER = ROOT / "services" / "event-worker"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.tools import run_c2_13_rtsp_watchlist_intrusion_probe as c2_13  # noqa: E402

_EVENT_REPOSITORY_SPEC = importlib.util.spec_from_file_location(
    "c2_13p_event_worker_repository",
    SERVICES_EVENT_WORKER / "app" / "repository.py",
)
assert _EVENT_REPOSITORY_SPEC is not None
_EVENT_REPOSITORY_MODULE = importlib.util.module_from_spec(_EVENT_REPOSITORY_SPEC)
assert _EVENT_REPOSITORY_SPEC.loader is not None
_EVENT_REPOSITORY_SPEC.loader.exec_module(_EVENT_REPOSITORY_MODULE)
EventRepository = _EVENT_REPOSITORY_MODULE.EventRepository


DEFAULT_DATABASE_URL = c2_13.DEFAULT_DATABASE_URL
DEFAULT_REDIS_URL = c2_13.DEFAULT_REDIS_URL
DEFAULT_EVIDENCE_ROOT = c2_13.DEFAULT_EVIDENCE_ROOT
DEFAULT_COMPOSE_FILE = c2_13.DEFAULT_COMPOSE_FILE
DEFAULT_ENV_FILE = c2_13.DEFAULT_ENV_FILE
DEFAULT_CAMERA_CONFIG = c2_13.DEFAULT_CAMERA_CONFIG
DEFAULT_RUNTIME_SECONDS = 120.0
DEFAULT_MAX_RUNTIME_SECONDS = 300.0
DEFAULT_MAX_MESSAGES = 300
DEFAULT_INTRUSION_RULE_ID = "c2_13p_rtsp_intrusion_rule"
DEFAULT_RUNTIME_MODULE_CONTAINER_PATH = "/opt/savant/src/module"

RESULT_PASS = "PASS_C2_13P_RTSP_INTRUSION_READY"
RESULT_EXPORT_GAP = "PARTIAL_C2_13P_POSE_METADATA_PRESENT_EXPORT_GAP"
RESULT_POSE_MODEL_NOT_ACTIVE = "PARTIAL_C2_13P_POSE_MODEL_NOT_ACTIVE"
RESULT_NO_PERSON_IN_SCENE = "PARTIAL_C2_13P_NO_PERSON_IN_SCENE"
RESULT_EVENT_WORKER_GAP = "PARTIAL_C2_13P_EVENT_WORKER_INTRUSION_PERSISTENCE_GAP"
RESULT_FAIL = "FAIL_C2_13P_RTSP_INTRUSION_BLOCKED"

INTRUSION_PASS = "PASS"
INTRUSION_EXPORT_GAP = RESULT_EXPORT_GAP
INTRUSION_POSE_MODEL_NOT_ACTIVE = RESULT_POSE_MODEL_NOT_ACTIVE
INTRUSION_NO_PERSON_IN_SCENE = RESULT_NO_PERSON_IN_SCENE
INTRUSION_EVENT_WORKER_GAP = RESULT_EVENT_WORKER_GAP


def run_c2_13p_probe(
    *,
    output_dir: Path,
    database_url: str,
    redis_url: str,
    runtime_seconds: float,
    max_messages: int,
    camera_config_path: Path,
    compose_file: Path,
    env_file: Path,
    api_base_url: str | None,
    sync_runtime_config: bool,
    restart_runtime_if_needed: bool,
    overwrite: bool = False,
) -> c2_13.ProbeResult:
    if runtime_seconds < 0:
        raise ValueError("runtime_seconds must be >= 0")
    if runtime_seconds > DEFAULT_MAX_RUNTIME_SECONDS:
        raise ValueError(f"runtime_seconds must be <= {DEFAULT_MAX_RUNTIME_SECONDS}")
    if max_messages < 1:
        raise ValueError("max_messages must be >= 1")
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        c2_13._clear_dir(output_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    start_wall = datetime.now(timezone.utc)
    containers_before = c2_13.inspect_runtime_containers()
    source_status_before = c2_13.inspect_rtsp_source_config(
        containers=containers_before,
        compose_file=compose_file,
        env_file=env_file,
        camera_config_path=camera_config_path,
    )
    input_type = source_status_before.get("input_type")
    source_id = str(source_status_before.get("source_id") or "")
    camera_id = str(source_status_before.get("camera_id") or source_id)

    module_probe = inspect_savant_pose_module(ROOT / "modules" / "savant_security" / "module.yml")
    runtime_config_probe = inspect_runtime_camera_config(
        container_name="c2-poc-savant",
        container_config_path=str(
            source_status_before.get("camera_config_container_path")
            or "/opt/savant/src/module/config/cameras.c1e_replay.yml"
        ),
        repo_config_path=camera_config_path,
        source_id=source_id,
    )
    sync_report = {
        "sync_requested": sync_runtime_config,
        "sync_performed": False,
        "reason": "not_needed",
        "source": None,
        "destination": None,
        "backup_path": None,
    }
    if sync_runtime_config and should_sync_runtime_config(runtime_config_probe):
        sync_report = sync_runtime_camera_config(runtime_config_probe)

    restart_report = {
        "restart_requested": False,
        "restart_performed": False,
        "services": [],
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "reason": "",
    }
    if restart_runtime_if_needed and sync_report.get("sync_performed"):
        restart_report = restart_c2_savant_runtime(reason="c2_13p_runtime_camera_config_synced")

    containers_after_sync = c2_13.inspect_runtime_containers()
    source_status_after = c2_13.inspect_rtsp_source_config(
        containers=containers_after_sync,
        compose_file=compose_file,
        env_file=env_file,
        camera_config_path=camera_config_path,
    )
    runtime_config_probe_after = inspect_runtime_camera_config(
        container_name="c2-poc-savant",
        container_config_path=str(
            source_status_after.get("camera_config_container_path")
            or "/opt/savant/src/module/config/cameras.c1e_replay.yml"
        ),
        repo_config_path=camera_config_path,
        source_id=source_id,
    )

    redis_client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    redis_before = c2_13.inspect_redis(redis_client)
    db_before = c2_13.inspect_database(database_url, source_id)
    api_before = c2_13.query_api(api_base_url, source_id)
    event_group = f"c2_13p_event_worker_{sanitize_group_id(output_dir.name)}"
    event_consumer = "c2_13p_event_worker_1"
    event_start_id = str(redis_before["streams"][c2_13.EVENT_STREAM].get("last_id") or "0-0")
    ensure_stream_group(redis_client, c2_13.EVENT_STREAM, event_group, event_start_id)

    runtime_started_at = datetime.now(timezone.utc)
    if input_type == "rtsp" and runtime_seconds > 0:
        time.sleep(runtime_seconds)
    runtime_ended_at = datetime.now(timezone.utc)

    person_entries = c2_13.read_stream_after(
        redis_client,
        c2_13.PERSON_STREAM,
        str(redis_before["streams"][c2_13.PERSON_STREAM].get("last_id") or "0-0"),
        max_messages=max_messages,
    )
    event_entries = c2_13.read_stream_after(
        redis_client,
        c2_13.EVENT_STREAM,
        event_start_id,
        max_messages=max_messages,
    )
    person_samples = c2_13.parse_person_observations(person_entries)
    redis_events = c2_13.parse_security_events(event_entries)
    event_worker = run_bounded_event_worker(
        database_url=database_url,
        redis_client=redis_client,
        group=event_group,
        consumer=event_consumer,
        max_messages=max_messages,
    )
    destroy_stream_group(redis_client, c2_13.EVENT_STREAM, event_group)

    redis_after = c2_13.inspect_redis(redis_client)
    db_after = c2_13.inspect_database(database_url, source_id)
    api_after = c2_13.query_api(api_base_url, source_id)
    event_rows = c2_13.fetch_recent_event_rows(
        database_url=database_url,
        source_id=source_id,
        start_time=start_wall,
        end_time=datetime.now(timezone.utc),
    )
    log_probe = inspect_savant_runtime_logs(
        source_id=source_id,
        seconds=max(int(runtime_seconds) + 180, 600),
    )
    pose_metadata_probe = build_pose_metadata_probe(
        module_probe=module_probe,
        log_probe=log_probe,
        source_id=source_id,
    )
    person_stream_probe = build_person_stream_probe(
        redis_before=redis_before,
        redis_after=redis_after,
        person_samples=person_samples,
    )
    intrusion_rule_probe = build_intrusion_rule_input_probe(
        source_status=source_status_after,
        runtime_config_probe=runtime_config_probe_after,
        source_id=source_id,
        camera_id=camera_id,
    )
    intrusion_metrics = build_intrusion_metrics(
        input_type=input_type,
        pose_metadata_probe=pose_metadata_probe,
        person_stream_probe=person_stream_probe,
        intrusion_rule_probe=intrusion_rule_probe,
        redis_events=redis_events,
        event_worker=event_worker,
        event_rows=event_rows,
    )
    event_worker_metrics = build_event_worker_metrics(
        event_worker=event_worker,
        db_before=db_before,
        db_after=db_after,
        api_before=api_before,
        api_after=api_after,
    )
    runtime_status = {
        "schema_version": "1.0",
        "started_at": start_wall.isoformat(),
        "runtime_started_at": runtime_started_at.isoformat(),
        "runtime_ended_at": runtime_ended_at.isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "runtime_seconds_requested": runtime_seconds,
        "runtime_seconds_observed": (runtime_ended_at - runtime_started_at).total_seconds(),
        "containers_before": containers_before,
        "containers_after": c2_13.inspect_runtime_containers(),
        "redis_before": redis_before,
        "redis_after": redis_after,
        "database_before": db_before,
        "database_after": db_after,
        "services_started": [],
        "containers_restarted": restart_report.get("services", []),
        "restart_report": restart_report,
        "event_style_replay_job_passed": False,
        "event_style_replay_claimed": False,
    }
    decision = decide_overall(
        input_type=input_type,
        pose_model_present=bool(pose_metadata_probe.get("pose_model_present")),
        person_objects_in_metadata=bool(pose_metadata_probe.get("person_objects_in_metadata")),
        person_observation_count=int(person_stream_probe.get("person_observation_new_count") or 0),
        intrusion_event_count=int(intrusion_metrics.get("intrusion_event_count") or 0),
        intrusion_event_emitted_count=int(intrusion_metrics.get("intrusion_event_emitted_count") or 0),
        event_worker_ok=not bool(event_worker.get("fatal_error")),
        rule_bound=bool(intrusion_rule_probe.get("roi_bound_to_active_source")),
    )
    output_payload = {
        "runtime_status": runtime_status,
        "savant_pose_metadata_probe": pose_metadata_probe,
        "person_observation_stream_probe": person_stream_probe,
        "intrusion_rule_input_probe": intrusion_rule_probe,
        "intrusion_metrics": intrusion_metrics,
        "event_worker_metrics": event_worker_metrics,
        "runtime_config_probe": runtime_config_probe_after,
        "sync_report": sync_report,
        "decision": decision,
    }
    unsafe_scan = c2_13.scan_for_unsafe_payload(output_payload)
    if not unsafe_scan["passed"]:
        decision = {
            "result_marker": RESULT_FAIL,
            "reason": "unsafe_payload_scan_failed",
            "intrusion_result": intrusion_metrics.get("intrusion_result"),
        }

    visual_evidence = {
        "visual_evidence_status": "not_generated",
        "reason": "C2.13P focuses RTSP pose/person intrusion; event-style Replay still not passed",
        "event_style_replay_job_passed": False,
    }
    summary = {
        "schema_version": "1.0",
        "result_marker": decision["result_marker"],
        "decision_reason": decision.get("reason"),
        "input_type": input_type,
        "source_id": source_id,
        "camera_id": camera_id,
        "rtsp_url_redacted": source_status_after.get("rtsp_url_redacted"),
        "pose_model_present": pose_metadata_probe.get("pose_model_present"),
        "person_objects_in_metadata": pose_metadata_probe.get("person_objects_in_metadata"),
        "security_person_observations_before": redis_before["streams"][c2_13.PERSON_STREAM],
        "security_person_observations_after": redis_after["streams"][c2_13.PERSON_STREAM],
        "person_observation_new_count": person_stream_probe.get("person_observation_new_count"),
        "intrusion_result": intrusion_metrics.get("intrusion_result"),
        "intrusion_event_count": intrusion_metrics.get("intrusion_event_count"),
        "intrusion_event_emitted_count": intrusion_metrics.get("intrusion_event_emitted_count"),
        "roi_bound_to_active_source": intrusion_rule_probe.get("roi_bound_to_active_source"),
        "runtime_config_synced": sync_report.get("sync_performed"),
        "payload_has_embedding": unsafe_scan.get("payload_has_embedding"),
        "payload_has_image_bytes": unsafe_scan.get("payload_has_image_bytes"),
        "unsafe_payload_scan_passed": unsafe_scan.get("passed"),
        "event_style_replay_job_passed": False,
        "event_style_replay_claimed": False,
        "visual_evidence_status": visual_evidence["visual_evidence_status"],
        "runtime_window": {
            "started_at": runtime_started_at.isoformat(),
            "ended_at": runtime_ended_at.isoformat(),
            "runtime_seconds_requested": runtime_seconds,
            "runtime_seconds_observed": (runtime_ended_at - runtime_started_at).total_seconds(),
        },
        "output_dir": str(output_dir),
    }

    c2_13._write_json(output_dir / "runtime_status.json", runtime_status)
    c2_13._write_json(output_dir / "savant_pose_metadata_probe.json", pose_metadata_probe)
    c2_13._write_json(output_dir / "person_observation_stream_probe.json", person_stream_probe)
    c2_13._write_json(output_dir / "intrusion_rule_input_probe.json", intrusion_rule_probe)
    c2_13._write_json(output_dir / "intrusion_metrics.json", intrusion_metrics)
    c2_13._write_json(output_dir / "event_worker_metrics.json", event_worker_metrics)
    c2_13._write_json(output_dir / "runtime_config_probe.json", runtime_config_probe_after)
    c2_13._write_json(output_dir / "sync_report.json", sync_report)
    c2_13._write_json(output_dir / "api_query_results.json", {"before": api_before, "after": api_after})
    c2_13._write_json(output_dir / "unsafe_payload_scan.json", unsafe_scan)
    c2_13._write_json(output_dir / "decision_summary.json", {**decision, **summary})
    intrusion_event = c2_13.first_event_of_type(
        intrusion_metrics.get("intrusion_events", []),
        "intrusion",
    )
    if intrusion_event:
        c2_13._write_json(output_dir / "intrusion_event.json", intrusion_event)
    persisted_intrusion = c2_13.first_event_of_type(
        event_worker.get("persisted_events", []) + event_rows,
        "intrusion",
    )
    if persisted_intrusion:
        c2_13._write_json(output_dir / "persisted_intrusion_event_row.json", persisted_intrusion)
    (output_dir / "operator_rtsp_intrusion_report.html").write_text(
        render_operator_report(
            summary=summary,
            pose_metadata_probe=pose_metadata_probe,
            person_stream_probe=person_stream_probe,
            intrusion_rule_probe=intrusion_rule_probe,
            intrusion_metrics=intrusion_metrics,
            sync_report=sync_report,
        ),
        encoding="utf-8",
    )
    return c2_13.ProbeResult(
        result_marker=str(summary["result_marker"]),
        output_dir=output_dir,
        summary_path=output_dir / "decision_summary.json",
        summary=summary,
    )


def inspect_savant_pose_module(module_path: Path) -> dict[str, Any]:
    if not module_path.is_file():
        return {
            "status": "missing",
            "module_path": str(module_path),
            "pose_model_present": False,
            "behavior_rules_active": False,
            "person_observation_exporter_expected": False,
        }
    try:
        raw = yaml.safe_load(module_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {
            "status": "parse_error",
            "module_path": str(module_path),
            "error": str(exc),
            "pose_model_present": False,
            "behavior_rules_active": False,
            "person_observation_exporter_expected": False,
        }
    elements = list(c2_13._dict(raw.get("pipeline")).get("elements") or [])
    pose_elements = []
    behavior_rules = []
    for element in elements:
        item = c2_13._dict(element)
        name = str(item.get("name") or "")
        model = c2_13._dict(item.get("model"))
        output = c2_13._dict(model.get("output"))
        converter = c2_13._dict(output.get("converter"))
        objects = output.get("objects") or []
        attributes = output.get("attributes") or []
        has_person_object = any(c2_13._dict(obj).get("label") == "person" for obj in objects)
        has_keypoints = any(c2_13._dict(attr).get("name") == "keypoints" for attr in attributes)
        if name == "yolo26_pose" or converter.get("module") == "custom.converters.yolo26_pose":
            pose_elements.append(
                {
                    "name": name,
                    "element": item.get("element"),
                    "model_file": model.get("model_file"),
                    "converter": converter,
                    "emits_person_objects": has_person_object,
                    "attaches_keypoints": has_keypoints,
                }
            )
        if item.get("module") == "custom.pyfuncs.behavior_rules":
            behavior_rules.append(
                {
                    "name": name,
                    "class_name": item.get("class_name"),
                    "cameras_config_path": c2_13._dict(item.get("kwargs")).get("cameras_config_path"),
                }
            )
    behavior_file = ROOT / "modules" / "savant_security" / "custom" / "pyfuncs" / "behavior_rules.py"
    behavior_text = behavior_file.read_text(encoding="utf-8", errors="ignore") if behavior_file.is_file() else ""
    return {
        "schema_version": "1.0",
        "status": "loaded",
        "module_path": str(module_path),
        "pose_model_present": bool(pose_elements),
        "pose_elements": pose_elements,
        "person_objects_declared": any(item.get("emits_person_objects") for item in pose_elements),
        "keypoints_declared": any(item.get("attaches_keypoints") for item in pose_elements),
        "behavior_rules_active": bool(behavior_rules),
        "behavior_rules": behavior_rules,
        "behavior_rules_exports_person_observations": "create_person_observation_exporter" in behavior_text
        and "_export_person_observations" in behavior_text,
        "behavior_rules_exports_intrusion_events": "create_event_exporter" in behavior_text
        and "_enrich_and_export" in behavior_text,
    }


def inspect_runtime_camera_config(
    *,
    container_name: str,
    container_config_path: str,
    repo_config_path: Path,
    source_id: str,
) -> dict[str, Any]:
    mounts = docker_mounts(container_name)
    runtime_host_path = resolve_runtime_host_path(
        container_path=container_config_path,
        mounts=mounts,
    )
    repo_summary = c2_13.load_camera_config_summary(repo_config_path, source_id)
    runtime_summary = (
        c2_13.load_camera_config_summary(runtime_host_path, source_id)
        if runtime_host_path is not None
        else {"status": "unresolved", "source_id_matches_runtime": False}
    )
    return {
        "schema_version": "1.0",
        "container_name": container_name,
        "container_config_path": container_config_path,
        "mounts": mounts,
        "repo_config_path": str(repo_config_path),
        "repo_config": repo_summary,
        "runtime_host_config_path": str(runtime_host_path) if runtime_host_path else None,
        "runtime_config": runtime_summary,
        "repo_has_active_source": bool(repo_summary.get("source_id_matches_runtime")),
        "runtime_has_active_source": bool(runtime_summary.get("source_id_matches_runtime")),
        "runtime_config_gap": bool(repo_summary.get("source_id_matches_runtime"))
        and not bool(runtime_summary.get("source_id_matches_runtime")),
    }


def docker_mounts(container_name: str) -> list[dict[str, str]]:
    info = c2_13.docker_inspect(container_name)
    if not info:
        return []
    mounts = []
    for mount in info.get("Mounts") or []:
        mounts.append(
            {
                "source": str(mount.get("Source") or ""),
                "destination": str(mount.get("Destination") or ""),
                "mode": str(mount.get("Mode") or ""),
                "rw": str(mount.get("RW")),
            }
        )
    return mounts


def resolve_runtime_host_path(
    *,
    container_path: str,
    mounts: list[dict[str, str]],
) -> Path | None:
    cpath = Path(container_path)
    best: tuple[int, Path] | None = None
    for mount in mounts:
        dest = mount.get("destination") or ""
        src = mount.get("source") or ""
        if not dest or not src:
            continue
        if container_path == dest or container_path.startswith(dest.rstrip("/") + "/"):
            suffix = Path(container_path).relative_to(Path(dest))
            candidate = Path(src) / suffix
            score = len(dest)
            if best is None or score > best[0]:
                best = (score, candidate)
    if best is not None:
        return best[1]
    if cpath.exists():
        return cpath
    return None


def should_sync_runtime_config(runtime_config_probe: dict[str, Any]) -> bool:
    return bool(runtime_config_probe.get("runtime_config_gap")) and bool(
        runtime_config_probe.get("runtime_host_config_path")
    )


def sync_runtime_camera_config(runtime_config_probe: dict[str, Any]) -> dict[str, Any]:
    source = Path(str(runtime_config_probe.get("repo_config_path") or ""))
    destination_text = runtime_config_probe.get("runtime_host_config_path")
    destination = Path(str(destination_text)) if destination_text else None
    if not source.is_file() or destination is None:
        return {
            "sync_requested": True,
            "sync_performed": False,
            "reason": "source_or_destination_missing",
            "source": str(source),
            "destination": str(destination) if destination else None,
            "backup_path": None,
        }
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if destination.exists():
        backup_path = destination.with_suffix(
            destination.suffix + f".c2_13p_backup_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
        )
        shutil.copy2(destination, backup_path)
    shutil.copy2(source, destination)
    return {
        "sync_requested": True,
        "sync_performed": True,
        "reason": "runtime_config_missing_active_source",
        "source": str(source),
        "destination": str(destination),
        "backup_path": str(backup_path) if backup_path else None,
    }


def restart_c2_savant_runtime(*, reason: str) -> dict[str, Any]:
    services = ["c2-poc-savant", "c2-poc-source-adapter"]
    report = {
        "restart_requested": True,
        "restart_performed": False,
        "services": services,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "reason": reason,
    }
    try:
        proc1 = subprocess.run(
            ["docker", "restart", "c2-poc-savant"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
        )
        wait_for_container_running("c2-poc-savant", timeout_seconds=90)
        proc2 = subprocess.run(
            ["docker", "restart", "c2-poc-source-adapter"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        report["returncode"] = 0 if proc1.returncode == 0 and proc2.returncode == 0 else 1
        report["stdout"] = (proc1.stdout + proc2.stdout)[-4000:]
        report["stderr"] = (proc1.stderr + proc2.stderr)[-4000:]
        report["restart_performed"] = report["returncode"] == 0
    except Exception as exc:
        report["returncode"] = -1
        report["stderr"] = str(exc)
    wait_for_container_running("c2-poc-savant", timeout_seconds=90)
    wait_for_container_running("c2-poc-source-adapter", timeout_seconds=30)
    return report


def wait_for_container_running(container_name: str, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        info = c2_13.docker_inspect(container_name)
        if info:
            state = c2_13._dict(info.get("State"))
            health = c2_13._dict(state.get("Health")).get("Status")
            if state.get("Running") and health in ("healthy", None):
                return True
        time.sleep(1)
    return False


def inspect_savant_runtime_logs(*, source_id: str, seconds: int) -> dict[str, Any]:
    text = docker_logs("c2-poc-savant", since=f"{seconds}s")
    return parse_savant_logs(text, source_id=source_id)


def docker_logs(container_name: str, *, since: str) -> str:
    try:
        proc = subprocess.run(
            ["docker", "logs", container_name, "--since", since],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
    except Exception as exc:
        return f"docker_logs_error={exc}"
    return proc.stdout[-200000:]


def parse_savant_logs(text: str, *, source_id: str) -> dict[str, Any]:
    face_assoc_samples = []
    behavior_ticks = []
    behavior_inits = []
    unknown_sources = []
    person_exporter_lines = []
    intrusion_event_lines = []
    for line in text.splitlines():
        if "stage=savant_security_behavior_rules_init" in line:
            behavior_inits.append(line[-1000:])
        if "stage=savant_security_behavior_rules_unknown_source" in line:
            unknown_sources.append(line[-1000:])
        if "stage=savant_security_person_observation" in line or "person_obs_exporter" in line:
            person_exporter_lines.append(line[-1000:])
        if "event_type=intrusion" in line or "source_event_id=" in line and "intrusion" in line:
            intrusion_event_lines.append(line[-1000:])
        match = re.search(
            r"\[face_assoc\]\s+frame=(?P<frame>\d+)\s+source=(?P<source>\S+)\s+"
            r"persons=(?P<persons>\d+)\s+faces=(?P<faces>\d+)\s+associated=(?P<associated>\d+)",
            line,
        )
        if match and match.group("source") == source_id:
            face_assoc_samples.append(
                {
                    "frame": int(match.group("frame")),
                    "source_id": match.group("source"),
                    "persons": int(match.group("persons")),
                    "faces": int(match.group("faces")),
                    "associated": int(match.group("associated")),
                }
            )
        if "stage=savant_security_behavior_rules_tick" in line and f"source_id={source_id}" in line:
            behavior_ticks.append(parse_behavior_tick(line))
    max_persons = max((sample["persons"] for sample in face_assoc_samples), default=0)
    total_persons = sum(sample["persons"] for sample in face_assoc_samples)
    max_raw = max((int(tick.get("raw_observation_count") or 0) for tick in behavior_ticks), default=0)
    max_accepted = max((int(tick.get("observation_count") or 0) for tick in behavior_ticks), default=0)
    exported_total = sum(int(tick.get("person_observations_exported") or 0) for tick in behavior_ticks)
    events_total = sum(int(tick.get("events_exported") or 0) for tick in behavior_ticks)
    return {
        "schema_version": "1.0",
        "source_id": source_id,
        "line_count": len(text.splitlines()),
        "behavior_rules_init_lines": behavior_inits[-5:],
        "behavior_rules_unknown_source_lines": unknown_sources[-10:],
        "person_observation_exporter_lines": person_exporter_lines[-10:],
        "intrusion_event_log_lines": intrusion_event_lines[-10:],
        "face_assoc_sample_count": len(face_assoc_samples),
        "face_assoc_samples": face_assoc_samples[-50:],
        "face_assoc_person_frame_count": sum(1 for sample in face_assoc_samples if sample["persons"] > 0),
        "face_assoc_total_person_count": total_persons,
        "face_assoc_max_persons_per_frame": max_persons,
        "behavior_tick_count": len(behavior_ticks),
        "behavior_tick_samples": behavior_ticks[-50:],
        "behavior_raw_person_max": max_raw,
        "behavior_accepted_person_max": max_accepted,
        "behavior_person_observations_exported_sum": exported_total,
        "behavior_intrusion_events_exported_sum": events_total,
    }


def parse_behavior_tick(line: str) -> dict[str, Any]:
    out: dict[str, Any] = {"raw_line": line[-1000:]}
    for key in (
        "frame",
        "source_id",
        "raw_observation_count",
        "observation_count",
        "tracked_count",
        "track_count",
        "events_exported",
        "person_observations_exported",
        "person_observations_skipped",
        "total_person_observations_exported",
    ):
        match = re.search(rf"{re.escape(key)}=([^ ]+)", line)
        if not match:
            continue
        value = match.group(1)
        if value.isdigit():
            out[key] = int(value)
        else:
            out[key] = value
    return out


def build_pose_metadata_probe(
    *,
    module_probe: dict[str, Any],
    log_probe: dict[str, Any],
    source_id: str,
) -> dict[str, Any]:
    person_objects_in_metadata = (
        int(log_probe.get("face_assoc_max_persons_per_frame") or 0) > 0
        or int(log_probe.get("behavior_raw_person_max") or 0) > 0
    )
    return {
        "schema_version": "1.0",
        "source_id": source_id,
        "pose_model_present": bool(module_probe.get("pose_model_present")),
        "person_objects_declared": bool(module_probe.get("person_objects_declared")),
        "keypoints_present": bool(module_probe.get("keypoints_declared")),
        "behavior_rules_active": bool(module_probe.get("behavior_rules_active")),
        "behavior_rules_publishes_person_observations": bool(
            module_probe.get("behavior_rules_exports_person_observations")
        ),
        "behavior_rules_directly_publishes_intrusion_events": bool(
            module_probe.get("behavior_rules_exports_intrusion_events")
        ),
        "person_objects_in_metadata": person_objects_in_metadata,
        "metadata_source_id": source_id,
        "sampled_frame_count": int(log_probe.get("face_assoc_sample_count") or 0),
        "sampled_person_frame_count": int(log_probe.get("face_assoc_person_frame_count") or 0),
        "sampled_person_count_total": int(log_probe.get("face_assoc_total_person_count") or 0),
        "sampled_person_count_max_per_frame": int(log_probe.get("face_assoc_max_persons_per_frame") or 0),
        "behavior_rules_tick_count": int(log_probe.get("behavior_tick_count") or 0),
        "behavior_raw_person_max": int(log_probe.get("behavior_raw_person_max") or 0),
        "behavior_accepted_person_max": int(log_probe.get("behavior_accepted_person_max") or 0),
        "behavior_person_observations_exported_sum": int(
            log_probe.get("behavior_person_observations_exported_sum") or 0
        ),
        "behavior_intrusion_events_exported_sum": int(
            log_probe.get("behavior_intrusion_events_exported_sum") or 0
        ),
        "keypoint_count": None,
        "keypoint_count_note": (
            "YOLO26 pose declares keypoints; security.person_observations currently "
            "exports bbox metadata without serialized keypoint arrays."
        ),
        "log_probe": log_probe,
        "module_probe": module_probe,
    }


def build_person_stream_probe(
    *,
    redis_before: dict[str, Any],
    redis_after: dict[str, Any],
    person_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    before = redis_before["streams"][c2_13.PERSON_STREAM]
    after = redis_after["streams"][c2_13.PERSON_STREAM]
    before_len = int(before.get("length") or 0)
    after_len = int(after.get("length") or 0)
    return {
        "schema_version": "1.0",
        "stream": c2_13.PERSON_STREAM,
        "before": before,
        "after": after,
        "person_observation_new_count": max(after_len - before_len, 0),
        "person_observation_sample_count": len(person_samples),
        "sample_source_observation_ids": [
            sample.get("source_observation_id") for sample in person_samples[:20]
        ],
        "sample_observations": person_samples[:20],
        "redis_ack_used": False,
        "redis_delete_used": False,
        "fake_person_observation_used": False,
    }


def build_intrusion_rule_input_probe(
    *,
    source_status: dict[str, Any],
    runtime_config_probe: dict[str, Any],
    source_id: str,
    camera_id: str,
) -> dict[str, Any]:
    runtime_config = c2_13._dict(runtime_config_probe.get("runtime_config"))
    intrusion_rule = c2_13._dict(runtime_config.get("intrusion_rule"))
    bound = bool(runtime_config.get("source_id_matches_runtime"))
    return {
        "schema_version": "1.0",
        "algorithm_id": "behavior.intrusion",
        "rule_id": intrusion_rule.get("rule_id") or DEFAULT_INTRUSION_RULE_ID,
        "source_id": source_id,
        "camera_id": camera_id,
        "input_type": source_status.get("input_type"),
        "roi_bound_to_active_source": bound,
        "runtime_config_path": runtime_config_probe.get("runtime_host_config_path"),
        "runtime_configured_sources": runtime_config.get("configured_sources"),
        "stale_c1e_source_present": "c1e_rtsp_replay" in (runtime_config.get("configured_sources") or []),
        "stale_c1e_source_satisfies_active_source": False,
        "roi_rule": intrusion_rule or None,
        "coordinate_space": "pixel",
    }


def build_intrusion_metrics(
    *,
    input_type: str | None,
    pose_metadata_probe: dict[str, Any],
    person_stream_probe: dict[str, Any],
    intrusion_rule_probe: dict[str, Any],
    redis_events: list[dict[str, Any]],
    event_worker: dict[str, Any],
    event_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    emitted_intrusion_events = [
        event for event in redis_events
        if event.get("event_type") == "intrusion"
    ]
    persisted_intrusion_events = [
        event for event in event_worker.get("persisted_events", []) + event_rows
        if event.get("event_type") == "intrusion"
    ]
    result = decide_intrusion_result(
        input_type=input_type,
        pose_model_present=bool(pose_metadata_probe.get("pose_model_present")),
        person_objects_in_metadata=bool(pose_metadata_probe.get("person_objects_in_metadata")),
        person_observation_count=int(person_stream_probe.get("person_observation_new_count") or 0),
        rule_bound=bool(intrusion_rule_probe.get("roi_bound_to_active_source")),
        intrusion_event_emitted_count=len(emitted_intrusion_events),
        intrusion_event_persisted_count=len(persisted_intrusion_events),
        event_worker_ok=not bool(event_worker.get("fatal_error")),
    )
    return {
        "schema_version": "1.0",
        "intrusion_result": result["result"],
        "reason": result["reason"],
        "algorithm_id": "behavior.intrusion",
        "rule_id": intrusion_rule_probe.get("rule_id"),
        "roi_bound_to_active_source": intrusion_rule_probe.get("roi_bound_to_active_source"),
        "person_objects_in_metadata": pose_metadata_probe.get("person_objects_in_metadata"),
        "person_pose_observation_count": person_stream_probe.get("person_observation_new_count"),
        "intrusion_event_emitted_count": len(emitted_intrusion_events),
        "intrusion_event_persisted_count": len(persisted_intrusion_events),
        "intrusion_event_count": len(persisted_intrusion_events),
        "intrusion_event_ids": [
            event.get("event_id") or event.get("id") or event.get("source_event_id") or event.get("redis_id")
            for event in persisted_intrusion_events
        ],
        "intrusion_events": [
            c2_13.strip_unsafe_payload(event)
            for event in (persisted_intrusion_events or emitted_intrusion_events)
        ],
        "fake_intrusion_event_used": False,
    }


def decide_intrusion_result(
    *,
    input_type: str | None,
    pose_model_present: bool,
    person_objects_in_metadata: bool,
    person_observation_count: int,
    rule_bound: bool,
    intrusion_event_emitted_count: int,
    intrusion_event_persisted_count: int,
    event_worker_ok: bool,
) -> dict[str, str]:
    if input_type != "rtsp":
        return {"result": RESULT_FAIL, "reason": "input_not_rtsp"}
    if not pose_model_present:
        return {"result": INTRUSION_POSE_MODEL_NOT_ACTIVE, "reason": "pose_model_not_active"}
    if intrusion_event_persisted_count > 0:
        return {"result": INTRUSION_PASS, "reason": "intrusion_event_emitted_and_persisted"}
    if intrusion_event_emitted_count > 0 and (not event_worker_ok or intrusion_event_persisted_count <= 0):
        return {
            "result": INTRUSION_EVENT_WORKER_GAP,
            "reason": "intrusion_event_emitted_but_not_persisted",
        }
    if not person_objects_in_metadata and person_observation_count <= 0:
        return {
            "result": INTRUSION_NO_PERSON_IN_SCENE,
            "reason": "no_person_detected_in_bounded_rtsp_window",
        }
    if not rule_bound or person_observation_count <= 0:
        return {
            "result": INTRUSION_EXPORT_GAP,
            "reason": "pose_metadata_present_but_not_exported_or_not_consumable_by_intrusion",
        }
    return {
        "result": INTRUSION_EXPORT_GAP,
        "reason": "person_observations_emitted_but_intrusion_event_not_observed",
    }


def decide_overall(
    *,
    input_type: str | None,
    pose_model_present: bool,
    person_objects_in_metadata: bool,
    person_observation_count: int,
    intrusion_event_count: int,
    intrusion_event_emitted_count: int,
    event_worker_ok: bool,
    rule_bound: bool,
) -> dict[str, Any]:
    result = decide_intrusion_result(
        input_type=input_type,
        pose_model_present=pose_model_present,
        person_objects_in_metadata=person_objects_in_metadata,
        person_observation_count=person_observation_count,
        rule_bound=rule_bound,
        intrusion_event_emitted_count=intrusion_event_emitted_count,
        intrusion_event_persisted_count=intrusion_event_count,
        event_worker_ok=event_worker_ok,
    )
    intrusion_result = result["result"]
    if intrusion_result == INTRUSION_PASS:
        marker = RESULT_PASS
    elif intrusion_result in {
        INTRUSION_EXPORT_GAP,
        INTRUSION_POSE_MODEL_NOT_ACTIVE,
        INTRUSION_NO_PERSON_IN_SCENE,
        INTRUSION_EVENT_WORKER_GAP,
    }:
        marker = intrusion_result
    else:
        marker = RESULT_FAIL
    return {
        "result_marker": marker,
        "reason": result["reason"],
        "intrusion_result": intrusion_result,
    }


def build_event_worker_metrics(
    *,
    event_worker: dict[str, Any],
    db_before: dict[str, Any],
    db_after: dict[str, Any],
    api_before: dict[str, Any],
    api_after: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "mode": "bounded_local_consumer",
        "messages_read": event_worker.get("messages_read"),
        "events_inserted": event_worker.get("events_inserted"),
        "duplicates": event_worker.get("duplicates"),
        "failed": event_worker.get("failed"),
        "fatal_error": event_worker.get("fatal_error"),
        "persisted_intrusion_count": len(
            [event for event in event_worker.get("persisted_events", []) if event.get("event_type") == "intrusion"]
        ),
        "db_events_before": db_before.get("events", {}),
        "db_events_after": db_after.get("events", {}),
        "api_before": api_before,
        "api_after": api_after,
        "redis_ack_used_for_local_group": True,
        "redis_delete_used": False,
    }


def run_bounded_event_worker(
    *,
    database_url: str,
    redis_client: redis.Redis,
    group: str,
    consumer: str,
    max_messages: int,
) -> dict[str, Any]:
    result = {
        "consumer_group": group,
        "consumer_name": consumer,
        "stream": c2_13.EVENT_STREAM,
        "messages_read": 0,
        "events_inserted": 0,
        "duplicates": 0,
        "failed": 0,
        "errors": [],
        "persisted_events": [],
        "fatal_error": None,
    }
    try:
        messages = redis_client.xreadgroup(
            group,
            consumer,
            {c2_13.EVENT_STREAM: ">"},
            count=max_messages,
            block=1000,
        )
    except Exception as exc:
        result["fatal_error"] = f"redis_xreadgroup_failed:{exc}"
        return result
    entries = []
    for _stream_name, stream_entries in messages:
        entries.extend(stream_entries)
    result["messages_read"] = len(entries)
    try:
        with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
            repo = EventRepository(conn)
            for msg_id, fields in entries:
                msg_id_s = str(msg_id)
                data_raw = fields.get("data") if isinstance(fields, dict) else None
                try:
                    event = json.loads(data_raw or "{}")
                    event_id = repo.insert_event(event)
                    if event_id:
                        repo.create_evidence_task(event, event_id)
                        result["events_inserted"] += 1
                        persisted = c2_13.strip_unsafe_payload(dict(event))
                        persisted["event_id"] = str(event_id)
                        result["persisted_events"].append(persisted)
                    else:
                        result["duplicates"] += 1
                    redis_client.xack(c2_13.EVENT_STREAM, group, msg_id_s)
                except Exception as exc:
                    result["failed"] += 1
                    if len(result["errors"]) < 10:
                        result["errors"].append(
                            {
                                "redis_id": msg_id_s,
                                "source_event_id": safe_source_event_id(data_raw),
                                "error": str(exc),
                            }
                        )
    except Exception as exc:
        result["fatal_error"] = f"event_worker_db_failed:{exc}"
    return result


def ensure_stream_group(
    redis_client: redis.Redis,
    stream: str,
    group: str,
    start_id: str,
) -> None:
    try:
        redis_client.xgroup_create(stream, group, id=start_id, mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def destroy_stream_group(redis_client: redis.Redis, stream: str, group: str) -> None:
    try:
        redis_client.xgroup_destroy(stream, group)
    except Exception:
        pass


def safe_source_event_id(data_raw: Any) -> str | None:
    try:
        parsed = json.loads(data_raw or "{}")
    except Exception:
        return None
    value = parsed.get("source_event_id") if isinstance(parsed, dict) else None
    return str(value) if value is not None else None


def sanitize_group_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[-80:] or "run"


def render_operator_report(
    *,
    summary: dict[str, Any],
    pose_metadata_probe: dict[str, Any],
    person_stream_probe: dict[str, Any],
    intrusion_rule_probe: dict[str, Any],
    intrusion_metrics: dict[str, Any],
    sync_report: dict[str, Any],
) -> str:
    rows = [
        ("Result marker", summary.get("result_marker")),
        ("Input type", summary.get("input_type")),
        ("Source ID", summary.get("source_id")),
        ("Camera ID", summary.get("camera_id")),
        ("RTSP URL", summary.get("rtsp_url_redacted")),
        ("Pose model present", pose_metadata_probe.get("pose_model_present")),
        ("Person objects in metadata", pose_metadata_probe.get("person_objects_in_metadata")),
        ("Runtime config synced", sync_report.get("sync_performed")),
        ("ROI bound to active source", intrusion_rule_probe.get("roi_bound_to_active_source")),
        ("Person observations", person_stream_probe.get("person_observation_new_count")),
        ("Intrusion result", intrusion_metrics.get("intrusion_result")),
        ("Intrusion events", intrusion_metrics.get("intrusion_event_count")),
        ("Event-style Replay passed", summary.get("event_style_replay_job_passed")),
    ]
    row_html = "\n".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in rows
    )
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>C2.13P RTSP Intrusion</title></head>
<body>
<h1>C2.13P RTSP Pose Intrusion Probe</h1>
<table>{row_html}</table>
<p>This report verifies RTSP pose/person observation export and intrusion events. Event-style Replay is not passed.</p>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--redis-url", default=os.getenv("C2_13P_REDIS_URL", DEFAULT_REDIS_URL))
    parser.add_argument(
        "--runtime-seconds",
        type=float,
        default=float(os.getenv("C2_13P_RUNTIME_SECONDS", DEFAULT_RUNTIME_SECONDS)),
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=int(os.getenv("C2_13P_MAX_MESSAGES", DEFAULT_MAX_MESSAGES)),
    )
    parser.add_argument("--camera-config-path", type=Path, default=DEFAULT_CAMERA_CONFIG)
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--api-base-url", default=os.getenv("C2_13P_API_BASE_URL") or None)
    parser.add_argument(
        "--no-sync-runtime-config",
        action="store_true",
        help="Do not copy the repo camera config into the mounted runtime module copy.",
    )
    parser.add_argument(
        "--no-runtime-restart",
        action="store_true",
        help="Do not restart Savant/source-adapter after syncing runtime camera config.",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = args.run_id or f"c2_13p_rtsp_pose_intrusion_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    output_dir = args.output_dir or args.evidence_root / run_id
    result = run_c2_13p_probe(
        output_dir=output_dir,
        database_url=args.database_url,
        redis_url=args.redis_url,
        runtime_seconds=args.runtime_seconds,
        max_messages=args.max_messages,
        camera_config_path=args.camera_config_path,
        compose_file=args.compose_file,
        env_file=args.env_file,
        api_base_url=args.api_base_url,
        sync_runtime_config=not args.no_sync_runtime_config,
        restart_runtime_if_needed=not args.no_runtime_restart,
        overwrite=args.overwrite,
    )
    print(
        json.dumps(
            {
                "result_marker": result.result_marker,
                "output_dir": str(result.output_dir),
                "summary": str(result.summary_path),
                "input_type": result.summary.get("input_type"),
                "source_id": result.summary.get("source_id"),
                "camera_id": result.summary.get("camera_id"),
                "pose_model_present": result.summary.get("pose_model_present"),
                "person_objects_in_metadata": result.summary.get("person_objects_in_metadata"),
                "person_observation_new_count": result.summary.get("person_observation_new_count"),
                "intrusion_result": result.summary.get("intrusion_result"),
                "intrusion_event_count": result.summary.get("intrusion_event_count"),
                "payload_has_embedding": result.summary.get("payload_has_embedding"),
                "payload_has_image_bytes": result.summary.get("payload_has_image_bytes"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    accepted = {
        RESULT_PASS,
        RESULT_EXPORT_GAP,
        RESULT_POSE_MODEL_NOT_ACTIVE,
        RESULT_NO_PERSON_IN_SCENE,
        RESULT_EVENT_WORKER_GAP,
    }
    return 0 if result.result_marker in accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
