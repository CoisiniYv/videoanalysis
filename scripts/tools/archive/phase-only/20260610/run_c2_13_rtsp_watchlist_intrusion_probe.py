#!/usr/bin/env python3
"""Bounded C2.13 RTSP watchlist + intrusion dual-algorithm probe.

This probe is read-only with respect to Redis, PostgreSQL, and the running
containers. It does not emit synthetic security events. A PASS can only come
from real runtime events already produced by the RTSP pipeline and persisted or
visible in Redis. If the RTSP stream is running but a worker/rule/config gap
prevents events, the tool writes a PARTIAL report instead of faking success.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import redis
import yaml
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6395/0"
DEFAULT_EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")
DEFAULT_COMPOSE_FILE = ROOT / "infra" / "docker-compose.c2-post-savant-replay-poc.yml"
DEFAULT_ENV_FILE = ROOT / "infra" / "env" / "c2-post-savant-replay-poc.env"
DEFAULT_CAMERA_CONFIG = ROOT / "modules" / "savant_security" / "config" / "cameras.c1e_replay.yml"
DEFAULT_RUNTIME_SECONDS = 180.0
DEFAULT_MAX_MESSAGES = 200
DEFAULT_TOP_K = 20
DEFAULT_THRESHOLD = 0.65
DEFAULT_WATCHLIST_RULE_ID = "c2_13_rtsp_reese_finch_watchlist_rule"
DEFAULT_INTRUSION_RULE_ID = "c2_13_rtsp_intrusion_rule"

FACE_STREAM = "security.face_observations"
PERSON_STREAM = "security.person_observations"
EVENT_STREAM = "security.events"

RESULT_PASS = "PASS_C2_13_RTSP_WATCHLIST_INTRUSION_READY"
RESULT_WATCHLIST_READY_INTRUSION_NO_TRIGGER = (
    "PARTIAL_C2_13_RTSP_WATCHLIST_READY_INTRUSION_NO_TRIGGER"
)
RESULT_INTRUSION_READY_WATCHLIST_NO_MATCH = (
    "PARTIAL_C2_13_RTSP_INTRUSION_READY_WATCHLIST_NO_MATCH"
)
RESULT_ALGORITHMS_NO_TRIGGER_OR_MATCH = (
    "PARTIAL_C2_13_RTSP_ALGORITHMS_NO_TRIGGER_OR_MATCH"
)
RESULT_NOT_REAL_RTSP = "PARTIAL_C2_13_NOT_REAL_RTSP_INPUT"
RESULT_RTSP_CONFIG_MISSING = "PARTIAL_C2_13_RTSP_CONFIG_MISSING"
RESULT_FAIL = "FAIL_C2_13_RTSP_DUAL_ALGORITHM_BLOCKED"

WATCHLIST_PASS = "PASS"
WATCHLIST_NO_MATCH = "PARTIAL_NO_MATCH"
WATCHLIST_NO_FACE_OBSERVATIONS = "PARTIAL_NO_FACE_OBSERVATIONS"
WATCHLIST_EVENT_PIPELINE_GAP = "PARTIAL_EVENT_PIPELINE_GAP"
WATCHLIST_FAIL = "FAIL"

INTRUSION_PASS = "PASS"
INTRUSION_NO_TRIGGER = "PARTIAL_NO_TRIGGER"
INTRUSION_ROI_CONFIG_GAP = "PARTIAL_C2_13_INTRUSION_ROI_CONFIG_GAP"
INTRUSION_NO_POSE_PERSON = "PARTIAL_NO_POSE_PERSON"
INTRUSION_FAIL = "FAIL"

TARGETS = {
    "reese": {
        "person_id": 5,
        "external_person_id": "demo:f4_3:reese",
        "gallery_embedding_id": 4,
        "name": "Reese",
    },
    "finch": {
        "person_id": 6,
        "external_person_id": "demo:f4_3:finch",
        "gallery_embedding_id": 5,
        "name": "Finch",
    },
}

FORBIDDEN_KEYS = {
    "embedding",
    "embedding_vector",
    "embedding_values",
    "image_bytes",
    "crop",
    "crop_bytes",
    "face_crop_bytes",
    "base64",
    "image_base64",
    "crop_base64",
    "face_crop_base64",
    "base64_image",
}
ALLOWED_NUMERIC_ARRAY_PATH_TOKENS = {
    "bbox",
    "face_bbox",
    "person_bbox",
    "landmarks",
    "keypoints",
    "xyxy",
    "values",
    "roi",
    "polygon",
    "points",
}


@dataclass(frozen=True)
class ProbeResult:
    result_marker: str
    output_dir: Path
    summary_path: Path
    summary: dict[str, Any]


def run_c2_13_probe(
    *,
    output_dir: Path,
    database_url: str,
    redis_url: str,
    runtime_seconds: float,
    max_messages: int,
    threshold: float,
    top_k: int,
    compose_file: Path,
    env_file: Path,
    camera_config_path: Path,
    api_base_url: str | None,
    overwrite: bool = False,
) -> ProbeResult:
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(f"threshold must be in [0.0, 1.0], got {threshold}")
    if runtime_seconds < 0:
        raise ValueError("runtime_seconds must be >= 0")
    if max_messages < 1:
        raise ValueError("max_messages must be >= 1")
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        _clear_dir(output_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    start_wall = datetime.now(timezone.utc)
    containers_before = inspect_runtime_containers()
    source_config = inspect_rtsp_source_config(
        containers=containers_before,
        compose_file=compose_file,
        env_file=env_file,
        camera_config_path=camera_config_path,
    )
    input_type = source_config.get("input_type") or "unknown"

    redis_client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    redis_before = inspect_redis(redis_client)
    db_before = inspect_database(database_url, source_config.get("source_id"))
    api_before = query_api(api_base_url, source_config.get("source_id"))

    face_window_entries: list[dict[str, Any]] = []
    person_window_entries: list[dict[str, Any]] = []
    event_window_entries: list[dict[str, Any]] = []
    if input_type == "rtsp" and runtime_seconds > 0:
        time.sleep(runtime_seconds)
        face_window_entries = read_stream_after(
            redis_client,
            FACE_STREAM,
            str(redis_before["streams"][FACE_STREAM].get("last_id") or "0-0"),
            max_messages=max_messages,
        )
        person_window_entries = read_stream_after(
            redis_client,
            PERSON_STREAM,
            str(redis_before["streams"][PERSON_STREAM].get("last_id") or "0-0"),
            max_messages=max_messages,
        )
        event_window_entries = read_stream_after(
            redis_client,
            EVENT_STREAM,
            str(redis_before["streams"][EVENT_STREAM].get("last_id") or "0-0"),
            max_messages=max_messages,
        )

    redis_after = inspect_redis(redis_client)
    db_after = inspect_database(database_url, source_config.get("source_id"))
    api_after = query_api(api_base_url, source_config.get("source_id"))
    containers_after = inspect_runtime_containers()
    end_wall = datetime.now(timezone.utc)

    face_window_samples = parse_face_observations(face_window_entries)
    face_sample_mode = "new_window"
    face_samples = list(face_window_samples)
    if not face_samples and int(redis_after["streams"][FACE_STREAM].get("length") or 0) > 0:
        face_samples = parse_face_observations(
            read_stream_backlog(redis_client, FACE_STREAM, max_messages=max_messages)
        )
        face_sample_mode = "redis_backlog_diagnostic"
    person_samples = parse_person_observations(person_window_entries)
    event_samples = parse_security_events(event_window_entries)

    galleries = fetch_target_gallery(database_url)
    top_matches = search_gallery_against_observations(
        galleries=galleries,
        observations=face_samples,
        top_k=top_k,
    )
    redis_events_recent = parse_security_events(
        read_stream_backlog(redis_client, EVENT_STREAM, max_messages=max_messages)
    )
    event_rows = fetch_recent_event_rows(
        database_url=database_url,
        source_id=source_config.get("source_id"),
        start_time=start_wall,
        end_time=end_wall,
    )
    watchlist_metrics = build_watchlist_metrics(
        source_config=source_config,
        redis_before=redis_before,
        redis_after=redis_after,
        db_before=db_before,
        db_after=db_after,
        face_samples=face_samples,
        face_window_sample_count=len(face_window_samples),
        face_sample_mode=face_sample_mode,
        top_matches=top_matches,
        redis_events=redis_events_recent + event_samples,
        event_rows=event_rows,
        threshold=threshold,
    )
    intrusion_metrics = build_intrusion_metrics(
        source_config=source_config,
        redis_before=redis_before,
        redis_after=redis_after,
        person_samples=person_samples,
        redis_events=redis_events_recent + event_samples,
        event_rows=event_rows,
    )
    event_worker_metrics = build_event_worker_metrics(
        containers=containers_after,
        redis_before=redis_before,
        redis_after=redis_after,
        db_before=db_before,
        db_after=db_after,
    )
    api_results = {
        "api_base_url": api_base_url,
        "before": api_before,
        "after": api_after,
        "status": "not_configured" if not api_base_url else api_after.get("status"),
    }
    runtime_status = {
        "schema_version": "1.0",
        "started_at": start_wall.isoformat(),
        "ended_at": end_wall.isoformat(),
        "runtime_seconds_requested": runtime_seconds,
        "runtime_seconds_observed": (end_wall - start_wall).total_seconds(),
        "containers_before": containers_before,
        "containers_after": containers_after,
        "redis_before": redis_before,
        "redis_after": redis_after,
        "database_before": db_before,
        "database_after": db_after,
        "services_started": [],
        "containers_restarted": [],
    }
    decision = decide_overall(
        input_type=input_type,
        rtsp_url=source_config.get("rtsp_url"),
        watchlist_result=watchlist_metrics["watchlist_result"],
        intrusion_result=intrusion_metrics["intrusion_result"],
    )
    visual_evidence = {
        "visual_evidence_status": "not_generated",
        "reason": (
            "C2.13 prioritizes RTSP algorithm/event verification; "
            "event-style Replay still not passed"
        ),
        "event_style_replay_job_passed": False,
    }
    output_payload = {
        "runtime_status": runtime_status,
        "rtsp_source_config": source_config,
        "watchlist_metrics": watchlist_metrics,
        "intrusion_metrics": intrusion_metrics,
        "event_worker_metrics": event_worker_metrics,
        "api_query_results": api_results,
        "visual_evidence": visual_evidence,
        "decision_summary": decision,
    }
    unsafe_scan = scan_for_unsafe_payload(output_payload)
    if not unsafe_scan["passed"]:
        decision = {
            "result_marker": RESULT_FAIL,
            "reason": "unsafe_payload_scan_failed",
            "watchlist_result": watchlist_metrics["watchlist_result"],
            "intrusion_result": intrusion_metrics["intrusion_result"],
        }

    summary = {
        "schema_version": "1.0",
        "result_marker": decision["result_marker"],
        "decision_reason": decision.get("reason"),
        "input_type": input_type,
        "source_id": source_config.get("source_id"),
        "camera_id": source_config.get("camera_id"),
        "rtsp_url_redacted": source_config.get("rtsp_url_redacted"),
        "runtime_window": {
            "started_at": start_wall.isoformat(),
            "ended_at": end_wall.isoformat(),
            "runtime_seconds_requested": runtime_seconds,
            "runtime_seconds_observed": (end_wall - start_wall).total_seconds(),
        },
        "watchlist_result": watchlist_metrics["watchlist_result"],
        "intrusion_result": intrusion_metrics["intrusion_result"],
        "watchlist_hit_count": watchlist_metrics["watchlist_hit_count"],
        "intrusion_event_count": intrusion_metrics["intrusion_event_count"],
        "payload_has_embedding": unsafe_scan.get("payload_has_embedding"),
        "payload_has_image_bytes": unsafe_scan.get("payload_has_image_bytes"),
        "unsafe_payload_scan_passed": unsafe_scan.get("passed"),
        "event_style_replay_job_passed": False,
        "event_style_replay_claimed": False,
        "visual_evidence_status": visual_evidence["visual_evidence_status"],
        "output_dir": str(output_dir),
    }

    _write_json(output_dir / "runtime_status.json", runtime_status)
    _write_json(output_dir / "rtsp_source_config.json", source_config)
    _write_json(output_dir / "watchlist_metrics.json", watchlist_metrics)
    _write_json(output_dir / "intrusion_metrics.json", intrusion_metrics)
    _write_json(output_dir / "event_worker_metrics.json", event_worker_metrics)
    _write_json(output_dir / "api_query_results.json", api_results)
    _write_json(output_dir / "unsafe_payload_scan.json", unsafe_scan)
    _write_json(output_dir / "decision_summary.json", {**decision, **summary})
    if event_rows:
        _write_json(output_dir / "persisted_event_rows.json", event_rows)
    watchlist_event = first_event_of_type(redis_events_recent + event_samples, "watchlist_hit")
    if watchlist_event:
        _write_json(output_dir / "watchlist_event.json", strip_unsafe_payload(watchlist_event))
    intrusion_event = first_event_of_type(redis_events_recent + event_samples, "intrusion")
    if intrusion_event:
        _write_json(output_dir / "intrusion_event.json", strip_unsafe_payload(intrusion_event))
    (output_dir / "operator_rtsp_dual_algorithm_report.html").write_text(
        render_operator_report(
            summary=summary,
            source_config=source_config,
            watchlist_metrics=watchlist_metrics,
            intrusion_metrics=intrusion_metrics,
            event_worker_metrics=event_worker_metrics,
            visual_evidence=visual_evidence,
        ),
        encoding="utf-8",
    )
    summary_path = output_dir / "decision_summary.json"
    return ProbeResult(
        result_marker=str(summary["result_marker"]),
        output_dir=output_dir,
        summary_path=summary_path,
        summary=summary,
    )


def inspect_runtime_containers() -> dict[str, Any]:
    names = [
        "c2-poc-source-adapter",
        "c2-poc-savant",
        "c2-poc-redis",
        "phase0-postgres",
        "c2-poc-video-file-sink",
        "c2-poc-evidence-viewer",
        "c2-poc-face-worker",
        "c2-poc-event-worker",
        "face-worker",
        "event-worker",
        "api",
        "c2-poc-api",
    ]
    inspected: dict[str, Any] = {}
    for name in names:
        info = docker_inspect(name)
        if info is None:
            inspected[name] = {"running": False, "exists": False}
            continue
        state = info.get("State") or {}
        inspected[name] = {
            "exists": True,
            "running": bool(state.get("Running")),
            "status": state.get("Status"),
            "health": _dict(state.get("Health")).get("Status"),
            "image": _dict(info.get("Config")).get("Image"),
            "env": redact_env(parse_env_list(_dict(info.get("Config")).get("Env") or [])),
        }
    return inspected


def docker_inspect(name: str) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            ["docker", "inspect", name],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not data:
        return None
    return data[0]


def inspect_rtsp_source_config(
    *,
    containers: dict[str, Any],
    compose_file: Path,
    env_file: Path,
    camera_config_path: Path,
) -> dict[str, Any]:
    source_env = _dict(_dict(containers.get("c2-poc-source-adapter")).get("env"))
    savant_env = _dict(_dict(containers.get("c2-poc-savant")).get("env"))
    env_defaults = read_env_file(env_file)
    compose_hint = parse_compose_source_hints(compose_file)
    raw_location = (
        source_env.get("LOCATION")
        or source_env.get("RTSP_URI")
        or compose_hint.get("LOCATION")
        or compose_hint.get("RTSP_URI")
        or env_defaults.get("RTSP_URI")
        or env_defaults.get("LOCATION")
        or ""
    )
    source_id = (
        source_env.get("SOURCE_ID")
        or savant_env.get("SOURCE_ID")
        or env_defaults.get("SOURCE_ID")
        or compose_hint.get("SOURCE_ID")
        or ""
    )
    camera_config_container_path = (
        savant_env.get("CAMERAS_CONFIG_PATH")
        or compose_hint.get("CAMERAS_CONFIG_PATH")
        or str(camera_config_path)
    )
    resolved_camera_config = resolve_container_module_path(
        camera_config_container_path,
        fallback=camera_config_path,
    )
    camera_config = load_camera_config_summary(resolved_camera_config, source_id)
    camera_id = camera_config.get("matching_camera_id") or source_id
    input_type = classify_input_type(raw_location)
    rtsp_url = raw_location if input_type == "rtsp" else ""
    return {
        "schema_version": "1.0",
        "input_type": input_type,
        "rtsp_url_configured": bool(rtsp_url),
        "rtsp_url_redacted": redact_url(rtsp_url) if rtsp_url else None,
        "rtsp_url": redact_url(rtsp_url) if rtsp_url else None,
        "raw_location_redacted": redact_url(raw_location),
        "source_id": source_id,
        "camera_id": camera_id,
        "source_adapter_running": bool(
            _dict(containers.get("c2-poc-source-adapter")).get("running")
        ),
        "savant_running": bool(_dict(containers.get("c2-poc-savant")).get("running")),
        "savant_health": _dict(containers.get("c2-poc-savant")).get("health"),
        "compose_file": str(compose_file),
        "env_file": str(env_file),
        "camera_config_container_path": camera_config_container_path,
        "camera_config_path": str(resolved_camera_config),
        "camera_config": camera_config,
        "intrusion_rule": camera_config.get("intrusion_rule"),
        "watchlist_rule_id": DEFAULT_WATCHLIST_RULE_ID,
        "source_adapter_entrypoint": compose_hint.get("source_adapter_entrypoint"),
        "services_inspected": [
            key for key, value in containers.items()
            if _dict(value).get("exists") or key.startswith("c2-poc")
        ],
    }


def classify_input_type(uri: str | None) -> str:
    value = (uri or "").strip().lower()
    if not value:
        return "missing"
    if value.startswith("rtsp://"):
        return "rtsp"
    if value.startswith("file://") or value.startswith("/") or value.startswith("./"):
        return "file"
    if value.startswith("http://") or value.startswith("https://"):
        return "http"
    return "unknown"


def redact_url(uri: str | None) -> str | None:
    if uri is None:
        return None
    value = str(uri)
    if not value:
        return value
    try:
        parts = urlsplit(value)
    except Exception:
        return re.sub(r"://([^/@:]+):([^/@]+)@", r"://***:***@", value)
    if not parts.scheme:
        return value
    hostname = parts.hostname or ""
    netloc = parts.netloc
    if "@" in netloc:
        host_part = hostname
        if parts.port:
            host_part = f"{host_part}:{parts.port}"
        netloc = f"***:***@{host_part}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def parse_compose_source_hints(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    services = _dict(data.get("services"))
    source_adapter = _dict(services.get("source-adapter"))
    savant = _dict(services.get("savant-security"))
    source_env = normalize_compose_environment(source_adapter.get("environment"))
    savant_env = normalize_compose_environment(savant.get("environment"))
    out = {**savant_env, **source_env}
    entrypoint = source_adapter.get("entrypoint")
    if entrypoint:
        out["source_adapter_entrypoint"] = entrypoint
    return out


def normalize_compose_environment(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, list):
        return parse_env_list(value)
    return {}


def parse_env_list(items: list[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        text = str(item)
        if "=" in text:
            key, value = text.split("=", 1)
            out[key] = value
    return out


def redact_env(env: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in env.items():
        lowered = key.lower()
        if "password" in lowered or "secret" in lowered or "token" in lowered:
            out[key] = "***"
        elif "url" in lowered or "uri" in lowered or key in {"LOCATION", "RTSP_URI"}:
            out[key] = redact_url(value) or ""
        else:
            out[key] = value
    return out


def resolve_container_module_path(path_text: str, *, fallback: Path) -> Path:
    if path_text.startswith("/opt/savant/src/module/"):
        suffix = path_text.removeprefix("/opt/savant/src/module/")
        candidate = ROOT / "modules" / "savant_security" / suffix
        if candidate.exists():
            return candidate
    candidate = Path(path_text)
    if candidate.exists():
        return candidate
    return fallback


def load_camera_config_summary(path: Path, runtime_source_id: str) -> dict[str, Any]:
    if not path.is_file():
        return {
            "status": "missing",
            "path": str(path),
            "runtime_source_id": runtime_source_id,
            "source_id_matches_runtime": False,
        }
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {
            "status": "parse_error",
            "path": str(path),
            "error": str(exc),
            "source_id_matches_runtime": False,
        }
    cameras = _dict(raw.get("cameras"))
    camera_summaries = []
    matching_camera = None
    for camera_id, cfg in cameras.items():
        cfg = _dict(cfg)
        zones = _dict(cfg.get("zones"))
        rules = _dict(cfg.get("rules"))
        intrusion = _dict(rules.get("intrusion"))
        summary = {
            "camera_id": str(camera_id),
            "source_id": cfg.get("source_id"),
            "enabled": cfg.get("enabled", True),
            "rtsp_url_redacted": redact_url(cfg.get("rtsp_url")),
            "zones": {
                zone_name: {
                    "type": _dict(zone).get("type"),
                    "points": _dict(zone).get("points"),
                }
                for zone_name, zone in zones.items()
            },
            "intrusion_rule": sanitize_intrusion_rule(intrusion),
        }
        camera_summaries.append(summary)
        if cfg.get("source_id") == runtime_source_id:
            matching_camera = summary
    intrusion_rule = None
    if matching_camera:
        intrusion_rule = matching_camera.get("intrusion_rule")
    return {
        "status": "loaded",
        "path": str(path),
        "runtime_source_id": runtime_source_id,
        "configured_sources": [item.get("source_id") for item in camera_summaries],
        "source_id_matches_runtime": matching_camera is not None,
        "matching_camera_id": matching_camera.get("camera_id") if matching_camera else None,
        "cameras": camera_summaries,
        "intrusion_rule": intrusion_rule,
    }


def sanitize_intrusion_rule(rule: dict[str, Any]) -> dict[str, Any] | None:
    if not rule:
        return None
    return {
        "algorithm_id": "behavior.intrusion",
        "rule_id": rule.get("rule_id") or DEFAULT_INTRUSION_RULE_ID,
        "enabled": bool(rule.get("enabled", True)),
        "zone": rule.get("zone"),
        "severity": rule.get("severity"),
        "cooldown_s": rule.get("cooldown_s"),
        "min_inside_ms": rule.get("min_inside_ms"),
        "min_person_confidence": rule.get("min_person_confidence"),
        "min_person_width": rule.get("min_person_width"),
        "min_person_height": rule.get("min_person_height"),
        "min_visible_keypoints": rule.get("min_visible_keypoints"),
    }


def inspect_redis(client: redis.Redis) -> dict[str, Any]:
    streams = {}
    for stream in (FACE_STREAM, PERSON_STREAM, EVENT_STREAM):
        streams[stream] = inspect_stream(client, stream)
    return {"streams": streams}


def inspect_stream(client: redis.Redis, stream: str) -> dict[str, Any]:
    try:
        info = client.xinfo_stream(stream)
    except Exception:
        return {"exists": False, "length": 0, "last_id": None, "groups": 0}
    return {
        "exists": True,
        "length": int(info.get("length") or 0),
        "last_id": info.get("last-generated-id"),
        "first_entry_id": info.get("first-entry", [None])[0] if info.get("first-entry") else None,
        "groups": int(info.get("groups") or 0),
    }


def read_stream_after(
    client: redis.Redis,
    stream: str,
    last_id: str,
    *,
    max_messages: int,
) -> list[dict[str, Any]]:
    try:
        result = client.xread({stream: last_id}, count=max_messages, block=100)
    except Exception:
        return []
    entries: list[dict[str, Any]] = []
    for _stream_name, messages in result:
        for msg_id, fields in messages:
            entries.append({"redis_id": msg_id, "fields": dict(fields)})
    return entries


def read_stream_backlog(
    client: redis.Redis,
    stream: str,
    *,
    max_messages: int,
) -> list[dict[str, Any]]:
    try:
        result = client.xrevrange(stream, count=max_messages)
    except Exception:
        return []
    entries = [
        {"redis_id": msg_id, "fields": dict(fields)}
        for msg_id, fields in reversed(result)
    ]
    return entries


def parse_face_observations(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    observations = []
    for entry in entries:
        data_raw = _dict(entry.get("fields")).get("data")
        if not data_raw:
            continue
        try:
            obs = json.loads(data_raw)
        except Exception:
            continue
        embedding = obs.get("embedding")
        safe = strip_unsafe_payload(obs)
        safe["redis_id"] = entry.get("redis_id")
        safe["embedding_present"] = isinstance(embedding, list)
        safe["_embedding_internal"] = embedding if isinstance(embedding, list) else None
        observations.append(safe)
    return observations


def parse_person_observations(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    observations = []
    for entry in entries:
        data_raw = _dict(entry.get("fields")).get("data")
        if not data_raw:
            continue
        try:
            obs = json.loads(data_raw)
        except Exception:
            continue
        safe = strip_unsafe_payload(obs)
        safe["redis_id"] = entry.get("redis_id")
        observations.append(safe)
    return observations


def parse_security_events(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for entry in entries:
        fields = _dict(entry.get("fields"))
        data_raw = fields.get("data")
        if data_raw:
            try:
                event = json.loads(data_raw)
            except Exception:
                event = dict(fields)
        else:
            event = dict(fields)
        event["redis_id"] = entry.get("redis_id")
        events.append(strip_unsafe_payload(event))
    return events


def fetch_target_gallery(database_url: str) -> dict[str, Any]:
    external_ids = [target["external_person_id"] for target in TARGETS.values()]
    rows: list[dict[str, Any]] = []
    try:
        with psycopg.connect(database_url, row_factory=dict_row) as conn:
            register_vector(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.id AS person_id, p.name, p.external_person_id,
                           pge.id AS gallery_embedding_id, pge.embedding,
                           pge.embedding_dim, pge.embedding_norm,
                           pge.embedding_model, pge.model_version,
                           pge.is_active AS gallery_active,
                           p.is_active AS person_active
                    FROM persons p
                    JOIN person_gallery_embeddings pge ON pge.person_id = p.id
                    WHERE p.external_person_id = ANY(%(external_ids)s)
                    ORDER BY p.external_person_id, pge.id
                    """,
                    {"external_ids": external_ids},
                )
                rows = [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        return {"status": "error", "error": str(exc), "targets": {}}
    targets: dict[str, Any] = {}
    for key, target in TARGETS.items():
        matched = [
            row for row in rows
            if row.get("external_person_id") == target["external_person_id"]
            and row.get("gallery_active") is True
            and row.get("person_active") is True
        ]
        targets[key] = {
            "person_id": target["person_id"],
            "external_person_id": target["external_person_id"],
            "configured_gallery_embedding_id": target["gallery_embedding_id"],
            "active_gallery_count": len(matched),
            "gallery": [
                {
                    "person_id": row.get("person_id"),
                    "external_person_id": row.get("external_person_id"),
                    "gallery_embedding_id": row.get("gallery_embedding_id"),
                    "embedding_dim": row.get("embedding_dim"),
                    "embedding_norm": row.get("embedding_norm"),
                    "embedding_model": row.get("embedding_model"),
                    "model_version": row.get("model_version"),
                    "_embedding_internal": vector_to_list(row.get("embedding")),
                }
                for row in matched
            ],
        }
    return {"status": "ok", "targets": targets}


def search_gallery_against_observations(
    *,
    galleries: dict[str, Any],
    observations: list[dict[str, Any]],
    top_k: int,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"reese": [], "finch": []}
    for identity_key, target in _dict(galleries.get("targets")).items():
        scored: list[dict[str, Any]] = []
        for gallery in _dict(target).get("gallery", []):
            gallery_embedding = gallery.get("_embedding_internal")
            if not isinstance(gallery_embedding, list):
                continue
            for obs in observations:
                obs_embedding = obs.get("_embedding_internal")
                if not isinstance(obs_embedding, list):
                    continue
                similarity = cosine_similarity(gallery_embedding, obs_embedding)
                if similarity is None:
                    continue
                scored.append(
                    {
                        "identity_key": identity_key,
                        "query_person_id": target.get("person_id"),
                        "query_external_person_id": target.get("external_person_id"),
                        "query_gallery_embedding_id": gallery.get("gallery_embedding_id"),
                        "similarity": similarity,
                        "source_observation_id": obs.get("source_observation_id"),
                        "camera_id": obs.get("camera_id"),
                        "source_id": obs.get("source_id"),
                        "track_id": obs.get("track_id"),
                        "timestamp_ms": obs.get("timestamp_ms"),
                        "frame_num": obs.get("frame_num"),
                        "face_bbox": obs.get("face_bbox"),
                        "face_confidence": obs.get("face_confidence"),
                        "quality": obs.get("quality"),
                        "embedding_dim": obs.get("embedding_dim"),
                        "embedding_norm": obs.get("embedding_norm"),
                        "redis_id": obs.get("redis_id"),
                        "match_source": "rtsp_face_observation",
                        "fake_match_used": False,
                        "gallery_self_match_used": False,
                    }
                )
        scored.sort(key=lambda item: float(item.get("similarity") or -999), reverse=True)
        out[identity_key] = scored[:top_k]
    return out


def cosine_similarity(a: list[Any], b: list[Any]) -> float | None:
    if len(a) != len(b) or not a:
        return None
    try:
        av = [float(x) for x in a]
        bv = [float(x) for x in b]
    except Exception:
        return None
    dot = sum(x * y for x, y in zip(av, bv))
    an = math.sqrt(sum(x * x for x in av))
    bn = math.sqrt(sum(y * y for y in bv))
    if an <= 0 or bn <= 0:
        return None
    return dot / (an * bn)


def vector_to_list(value: Any) -> list[float] | None:
    if value is None:
        return None
    if hasattr(value, "tolist"):
        return [float(x) for x in value.tolist()]
    if isinstance(value, (list, tuple)):
        return [float(x) for x in value]
    return None


def inspect_database(database_url: str, source_id: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "unknown",
        "face_observations": {},
        "events": {},
    }
    try:
        with psycopg.connect(database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS total FROM face_observations")
                out["face_observations"]["total"] = int(cur.fetchone()["total"])
                cur.execute(
                    """
                    SELECT COALESCE(source_id, '<null>') AS source_id,
                           COUNT(*) AS row_count,
                           COUNT(embedding) AS embedding_count,
                           MAX(created_at) AS max_created_at
                    FROM face_observations
                    GROUP BY source_id
                    ORDER BY row_count DESC
                    """
                )
                out["face_observations"]["by_source"] = [
                    json_safe(dict(row)) for row in cur.fetchall()
                ]
                if source_id:
                    cur.execute(
                        """
                        SELECT COUNT(*) AS row_count,
                               COUNT(embedding) AS embedding_count,
                               MAX(created_at) AS max_created_at
                        FROM face_observations
                        WHERE source_id = %(source_id)s
                        """,
                        {"source_id": source_id},
                    )
                    out["face_observations"]["runtime_source"] = json_safe(dict(cur.fetchone()))
                cur.execute("SELECT COUNT(*) AS total FROM events")
                out["events"]["total"] = int(cur.fetchone()["total"])
                cur.execute(
                    """
                    SELECT event_type, COALESCE(source_id, '<null>') AS source_id,
                           COUNT(*) AS row_count,
                           MAX(created_at) AS max_created_at
                    FROM events
                    GROUP BY event_type, source_id
                    ORDER BY row_count DESC
                    LIMIT 50
                    """
                )
                out["events"]["by_type_source"] = [
                    json_safe(dict(row)) for row in cur.fetchall()
                ]
        out["status"] = "ok"
    except Exception as exc:
        out["status"] = "error"
        out["error"] = str(exc)
    return out


def fetch_recent_event_rows(
    *,
    database_url: str,
    source_id: str | None,
    start_time: datetime,
    end_time: datetime,
) -> list[dict[str, Any]]:
    try:
        with psycopg.connect(database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id::text AS id, source_event_id, event_type,
                           camera_id, source_id, track_id, person_id,
                           confidence, severity, rule_name, created_at,
                           payload
                    FROM events
                    WHERE created_at >= %(start_time)s
                      AND created_at <= %(end_time)s
                      AND (%(source_id)s::text IS NULL OR source_id = %(source_id)s)
                    ORDER BY created_at DESC
                    LIMIT 100
                    """,
                    {
                        "start_time": start_time,
                        "end_time": end_time,
                        "source_id": source_id,
                    },
                )
                rows = [strip_unsafe_payload(json_safe(dict(row))) for row in cur.fetchall()]
    except Exception:
        return []
    return rows


def build_watchlist_metrics(
    *,
    source_config: dict[str, Any],
    redis_before: dict[str, Any],
    redis_after: dict[str, Any],
    db_before: dict[str, Any],
    db_after: dict[str, Any],
    face_samples: list[dict[str, Any]],
    face_window_sample_count: int,
    face_sample_mode: str,
    top_matches: dict[str, list[dict[str, Any]]],
    redis_events: list[dict[str, Any]],
    event_rows: list[dict[str, Any]],
    threshold: float,
) -> dict[str, Any]:
    source_id = source_config.get("source_id")
    watchlist_events = [
        event for event in redis_events + event_rows
        if event.get("event_type") == "watchlist_hit"
        and (not source_id or event.get("source_id") == source_id)
    ]
    best_reese = first_or_none(top_matches.get("reese", []))
    best_finch = first_or_none(top_matches.get("finch", []))
    best_match = best_of_matches([best_reese, best_finch])
    hit_count = len(watchlist_events)
    result = decide_watchlist_result(
        input_type=source_config.get("input_type"),
        face_observation_count=len(face_samples),
        best_match=best_match,
        threshold=threshold,
        watchlist_hit_count=hit_count,
    )
    return {
        "schema_version": "1.0",
        "watchlist_result": result["result"],
        "reason": result["reason"],
        "threshold": threshold,
        "watchlist_rule_id": DEFAULT_WATCHLIST_RULE_ID,
        "targets": TARGETS,
        "redis_face_observations_before": redis_before["streams"][FACE_STREAM],
        "redis_face_observations_after": redis_after["streams"][FACE_STREAM],
        "face_observation_sample_count": len(face_samples),
        "face_observation_new_window_count": face_window_sample_count,
        "face_observation_sample_mode": face_sample_mode,
        "db_face_observations_before": db_before.get("face_observations", {}),
        "db_face_observations_after": db_after.get("face_observations", {}),
        "top_reese_match": strip_internal(best_reese),
        "top_finch_match": strip_internal(best_finch),
        "best_match": strip_internal(best_match),
        "watchlist_hit_count": hit_count,
        "watchlist_hit_event_ids": [
            event.get("id") or event.get("source_event_id") or event.get("redis_id")
            for event in watchlist_events
        ],
        "face_worker_running": bool(
            source_config.get("face_worker_running")
        ),
        "fake_match_used": False,
        "gallery_self_match_used": False,
    }


def decide_watchlist_result(
    *,
    input_type: str | None,
    face_observation_count: int,
    best_match: dict[str, Any] | None,
    threshold: float,
    watchlist_hit_count: int,
) -> dict[str, str]:
    if input_type != "rtsp":
        return {"result": WATCHLIST_NO_FACE_OBSERVATIONS, "reason": "input_not_rtsp"}
    if watchlist_hit_count > 0 and is_valid_watchlist_pass_match(best_match, threshold=threshold):
        return {"result": WATCHLIST_PASS, "reason": "watchlist_hit_event_observed"}
    if face_observation_count <= 0:
        return {
            "result": WATCHLIST_NO_FACE_OBSERVATIONS,
            "reason": "no_rtsp_face_observations_in_window",
        }
    if best_match and float(best_match.get("similarity") or 0.0) >= threshold:
        return {
            "result": WATCHLIST_EVENT_PIPELINE_GAP,
            "reason": "reese_or_finch_match_above_threshold_but_no_watchlist_hit_event",
        }
    return {"result": WATCHLIST_NO_MATCH, "reason": "no_reese_finch_match_above_threshold"}


def is_valid_watchlist_pass_match(match: dict[str, Any] | None, *, threshold: float) -> bool:
    if not isinstance(match, dict):
        return False
    if match.get("query_external_person_id") not in {
        "demo:f4_3:reese",
        "demo:f4_3:finch",
    }:
        return False
    if not match.get("source_observation_id"):
        return False
    if float(match.get("similarity") or 0.0) < threshold:
        return False
    if match.get("fake_match_used") is True or match.get("gallery_self_match_used") is True:
        return False
    return True


def build_intrusion_metrics(
    *,
    source_config: dict[str, Any],
    redis_before: dict[str, Any],
    redis_after: dict[str, Any],
    person_samples: list[dict[str, Any]],
    redis_events: list[dict[str, Any]],
    event_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    source_id = source_config.get("source_id")
    intrusion_events = [
        event for event in redis_events + event_rows
        if event.get("event_type") == "intrusion"
        and (not source_id or event.get("source_id") == source_id)
    ]
    roi_config = _dict(_dict(source_config.get("camera_config")).get("intrusion_rule"))
    source_match = bool(_dict(source_config.get("camera_config")).get("source_id_matches_runtime"))
    result = decide_intrusion_result(
        input_type=source_config.get("input_type"),
        roi_configured=bool(roi_config) and source_match,
        person_pose_count=len(person_samples),
        intrusion_event_count=len(intrusion_events),
    )
    return {
        "schema_version": "1.0",
        "intrusion_result": result["result"],
        "reason": result["reason"],
        "algorithm_id": "behavior.intrusion",
        "rule_id": DEFAULT_INTRUSION_RULE_ID,
        "roi_configured_for_runtime_source": bool(roi_config) and source_match,
        "roi_source_match": source_match,
        "roi_rule": roi_config or None,
        "configured_sources": _dict(source_config.get("camera_config")).get("configured_sources"),
        "runtime_source_id": source_id,
        "redis_person_observations_before": redis_before["streams"][PERSON_STREAM],
        "redis_person_observations_after": redis_after["streams"][PERSON_STREAM],
        "person_pose_observation_count": len(person_samples),
        "intrusion_event_count": len(intrusion_events),
        "intrusion_event_ids": [
            event.get("id") or event.get("source_event_id") or event.get("redis_id")
            for event in intrusion_events
        ],
        "fake_intrusion_event_used": False,
    }


def decide_intrusion_result(
    *,
    input_type: str | None,
    roi_configured: bool,
    person_pose_count: int,
    intrusion_event_count: int,
) -> dict[str, str]:
    if input_type != "rtsp":
        return {"result": INTRUSION_NO_POSE_PERSON, "reason": "input_not_rtsp"}
    if intrusion_event_count > 0:
        return {"result": INTRUSION_PASS, "reason": "intrusion_event_observed"}
    if not roi_configured:
        return {
            "result": INTRUSION_ROI_CONFIG_GAP,
            "reason": "intrusion_roi_or_rule_not_bound_to_runtime_source",
        }
    if person_pose_count > 0:
        return {
            "result": INTRUSION_NO_TRIGGER,
            "reason": "person_pose_observed_but_intrusion_condition_not_triggered",
        }
    return {"result": INTRUSION_NO_POSE_PERSON, "reason": "no_person_pose_observations_in_window"}


def is_valid_intrusion_event(event: dict[str, Any] | None) -> bool:
    return isinstance(event, dict) and event.get("event_type") == "intrusion" and bool(event.get("source_event_id") or event.get("id"))


def build_event_worker_metrics(
    *,
    containers: dict[str, Any],
    redis_before: dict[str, Any],
    redis_after: dict[str, Any],
    db_before: dict[str, Any],
    db_after: dict[str, Any],
) -> dict[str, Any]:
    event_worker_running = any(
        _dict(containers.get(name)).get("running")
        for name in ("c2-poc-event-worker", "event-worker")
    )
    face_worker_running = any(
        _dict(containers.get(name)).get("running")
        for name in ("c2-poc-face-worker", "face-worker")
    )
    return {
        "schema_version": "1.0",
        "face_worker_running": face_worker_running,
        "event_worker_running": event_worker_running,
        "redis_events_before": redis_before["streams"][EVENT_STREAM],
        "redis_events_after": redis_after["streams"][EVENT_STREAM],
        "db_events_before": db_before.get("events", {}),
        "db_events_after": db_after.get("events", {}),
        "event_worker_consumed_count": None,
        "db_events_inserted_in_window": None,
        "note": "worker metrics are inferred from container presence and DB/Redis deltas",
    }


def decide_overall(
    *,
    input_type: str | None,
    rtsp_url: str | None,
    watchlist_result: str,
    intrusion_result: str,
) -> dict[str, Any]:
    if not rtsp_url:
        return {
            "result_marker": RESULT_RTSP_CONFIG_MISSING,
            "reason": "rtsp_url_not_configured",
            "watchlist_result": watchlist_result,
            "intrusion_result": intrusion_result,
        }
    if input_type != "rtsp":
        return {
            "result_marker": RESULT_NOT_REAL_RTSP,
            "reason": "active_input_is_not_rtsp",
            "watchlist_result": watchlist_result,
            "intrusion_result": intrusion_result,
        }
    if watchlist_result == WATCHLIST_PASS and intrusion_result == INTRUSION_PASS:
        marker = RESULT_PASS
        reason = "watchlist_and_intrusion_events_observed"
    elif watchlist_result == WATCHLIST_PASS and intrusion_result in {
        INTRUSION_NO_TRIGGER,
        INTRUSION_ROI_CONFIG_GAP,
        INTRUSION_NO_POSE_PERSON,
    }:
        marker = RESULT_WATCHLIST_READY_INTRUSION_NO_TRIGGER
        reason = "watchlist_ready_intrusion_not_triggered_or_not_configured"
    elif intrusion_result == INTRUSION_PASS and watchlist_result in {
        WATCHLIST_NO_MATCH,
        WATCHLIST_NO_FACE_OBSERVATIONS,
        WATCHLIST_EVENT_PIPELINE_GAP,
    }:
        marker = RESULT_INTRUSION_READY_WATCHLIST_NO_MATCH
        reason = "intrusion_ready_watchlist_no_match_or_pipeline_gap"
    else:
        marker = RESULT_ALGORITHMS_NO_TRIGGER_OR_MATCH
        reason = "rtsp_pipeline_running_but_required_watchlist_intrusion_events_not_both_observed"
    return {
        "result_marker": marker,
        "reason": reason,
        "watchlist_result": watchlist_result,
        "intrusion_result": intrusion_result,
    }


def query_api(api_base_url: str | None, source_id: str | None) -> dict[str, Any]:
    if not api_base_url:
        return {"status": "not_configured"}
    base = api_base_url.rstrip("/")
    urls = [
        f"{base}/health",
        f"{base}/api/v1/events?limit=10",
    ]
    if source_id:
        urls.append(f"{base}/api/v1/events?source_id={source_id}&limit=10")
    out = {"status": "unknown", "requests": []}
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = resp.read(2048).decode("utf-8", errors="replace")
                out["requests"].append(
                    {"url": url, "status_code": resp.status, "body_prefix": body[:500]}
                )
        except urllib.error.HTTPError as exc:
            out["requests"].append({"url": url, "status_code": exc.code, "error": str(exc)})
        except Exception as exc:
            out["requests"].append({"url": url, "status_code": None, "error": str(exc)})
    out["status"] = "queried"
    return out


def first_event_of_type(events: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
    for event in events:
        if event.get("event_type") == event_type:
            return event
    return None


def first_or_none(items: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if items:
        return items[0]
    return None


def best_of_matches(matches: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    valid = [item for item in matches if isinstance(item, dict)]
    if not valid:
        return None
    return max(valid, key=lambda item: float(item.get("similarity") or -999))


def strip_internal(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_internal(nested)
            for key, nested in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [strip_internal(item) for item in value]
    return value


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if hasattr(value, "tolist"):
        return json_safe(value.tolist())
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def scan_for_unsafe_payload(value: Any) -> dict[str, Any]:
    hits = sorted(set(_find_unsafe(value, "$")))
    return {
        "passed": not hits,
        "payload_has_embedding": any("embedding" in hit for hit in hits),
        "payload_has_image_bytes": any(
            token in hit
            for hit in hits
            for token in ("image", "image_bytes", "base64", "crop", "crop_bytes", "face_crop_bytes")
        ),
        "forbidden_key_paths": hits,
    }


def _find_unsafe(value: Any, path: str) -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            next_path = f"{path}.{key}"
            if lowered in FORBIDDEN_KEYS and nested not in (None, "", False, [], {}):
                hits.append(next_path)
            hits.extend(_find_unsafe(nested, next_path))
    elif isinstance(value, list):
        if _looks_like_forbidden_vector(value, path):
            hits.append(path)
        for index, nested in enumerate(value):
            hits.extend(_find_unsafe(nested, f"{path}.{index}"))
    elif isinstance(value, str):
        lowered = value.lower()
        if "data:image" in lowered or ";base64," in lowered:
            hits.append(path)
    return hits


def _looks_like_forbidden_vector(value: list[Any], path: str) -> bool:
    if len(value) < 64:
        return False
    lowered_path = path.lower()
    if any(token in lowered_path for token in ALLOWED_NUMERIC_ARRAY_PATH_TOKENS):
        return False
    numeric_count = sum(
        1
        for item in value
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    )
    return numeric_count >= 64 and numeric_count / max(len(value), 1) >= 0.9


def strip_unsafe_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_unsafe_payload(nested)
            for key, nested in value.items()
            if str(key).lower() not in FORBIDDEN_KEYS
        }
    if isinstance(value, list):
        if _looks_like_forbidden_vector(value, "$"):
            return []
        return [strip_unsafe_payload(item) for item in value]
    return value


def render_operator_report(
    *,
    summary: dict[str, Any],
    source_config: dict[str, Any],
    watchlist_metrics: dict[str, Any],
    intrusion_metrics: dict[str, Any],
    event_worker_metrics: dict[str, Any],
    visual_evidence: dict[str, Any],
) -> str:
    rows = [
        ("Result marker", summary.get("result_marker")),
        ("Input type", source_config.get("input_type")),
        ("RTSP URL", source_config.get("rtsp_url_redacted")),
        ("Source ID", source_config.get("source_id")),
        ("Camera ID", source_config.get("camera_id")),
        ("Watchlist result", watchlist_metrics.get("watchlist_result")),
        ("Top Reese similarity", _dict(watchlist_metrics.get("top_reese_match")).get("similarity")),
        ("Top Finch similarity", _dict(watchlist_metrics.get("top_finch_match")).get("similarity")),
        ("Watchlist hit count", watchlist_metrics.get("watchlist_hit_count")),
        ("Intrusion result", intrusion_metrics.get("intrusion_result")),
        ("Intrusion event count", intrusion_metrics.get("intrusion_event_count")),
        ("Face worker running", event_worker_metrics.get("face_worker_running")),
        ("Event worker running", event_worker_metrics.get("event_worker_running")),
        ("Visual evidence", visual_evidence.get("visual_evidence_status")),
        ("Event-style Replay passed", summary.get("event_style_replay_job_passed")),
    ]
    row_html = "\n".join(
        f"<tr><th>{html.escape(str(label))}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in rows
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>C2.13 RTSP Watchlist Intrusion Smoke</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; line-height: 1.45; }}
    table {{ border-collapse: collapse; min-width: 760px; }}
    th, td {{ border: 1px solid #bbb; padding: 8px 10px; text-align: left; }}
    th {{ background: #f3f3f3; width: 260px; }}
    code {{ background: #f6f6f6; padding: 1px 4px; }}
  </style>
</head>
<body>
  <h1>C2.13 RTSP Watchlist + Intrusion Smoke</h1>
  <table>{row_html}</table>
  <p>Visual evidence is optional in C2.13. Event-style Replay is not repaired or claimed passed.</p>
</body>
</html>
"""


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(strip_internal(json_safe(value)), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _clear_dir(path: Path) -> None:
    import shutil

    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--redis-url", default=os.getenv("C2_13_REDIS_URL", DEFAULT_REDIS_URL))
    parser.add_argument("--runtime-seconds", type=float, default=float(os.getenv("C2_13_RUNTIME_SECONDS", DEFAULT_RUNTIME_SECONDS)))
    parser.add_argument("--max-messages", type=int, default=int(os.getenv("C2_13_MAX_MESSAGES", DEFAULT_MAX_MESSAGES)))
    parser.add_argument("--threshold", type=float, default=float(os.getenv("C2_13_WATCHLIST_THRESHOLD", DEFAULT_THRESHOLD)))
    parser.add_argument("--top-k", type=int, default=int(os.getenv("C2_13_TOP_K", DEFAULT_TOP_K)))
    parser.add_argument("--api-base-url", default=os.getenv("C2_13_API_BASE_URL") or None)
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--camera-config-path", type=Path, default=DEFAULT_CAMERA_CONFIG)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = args.run_id or f"c2_13_rtsp_watchlist_intrusion_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    output_dir = args.output_dir or args.evidence_root / run_id
    result = run_c2_13_probe(
        output_dir=output_dir,
        database_url=args.database_url,
        redis_url=args.redis_url,
        runtime_seconds=args.runtime_seconds,
        max_messages=args.max_messages,
        threshold=args.threshold,
        top_k=args.top_k,
        compose_file=args.compose_file,
        env_file=args.env_file,
        camera_config_path=args.camera_config_path,
        api_base_url=args.api_base_url,
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
                "rtsp_url_redacted": result.summary.get("rtsp_url_redacted"),
                "watchlist_result": result.summary.get("watchlist_result"),
                "intrusion_result": result.summary.get("intrusion_result"),
                "watchlist_hit_count": result.summary.get("watchlist_hit_count"),
                "intrusion_event_count": result.summary.get("intrusion_event_count"),
                "visual_evidence_status": result.summary.get("visual_evidence_status"),
                "payload_has_embedding": result.summary.get("payload_has_embedding"),
                "payload_has_image_bytes": result.summary.get("payload_has_image_bytes"),
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    accepted = {
        RESULT_PASS,
        RESULT_WATCHLIST_READY_INTRUSION_NO_TRIGGER,
        RESULT_INTRUSION_READY_WATCHLIST_NO_MATCH,
        RESULT_ALGORITHMS_NO_TRIGGER_OR_MATCH,
        RESULT_NOT_REAL_RTSP,
        RESULT_RTSP_CONFIG_MISSING,
    }
    return 0 if result.result_marker in accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
