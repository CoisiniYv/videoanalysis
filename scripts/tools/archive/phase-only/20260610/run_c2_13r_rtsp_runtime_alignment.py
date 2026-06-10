#!/usr/bin/env python3
"""C2.13R RTSP source alignment for watchlist + intrusion runtime smoke.

The tool aligns the mounted camera config with the active RTSP source and then
runs a bounded, local worker path against the current Redis streams:

* face observations are consumed from ``security.face_observations`` using a
  C2.13R consumer group, inserted idempotently into PostgreSQL, and searched
  against Reese/Finch gallery.
* watchlist_hit events are emitted only if a real RTSP face observation matches
  Reese/Finch above threshold.
* events are consumed from ``security.events`` using a C2.13R consumer group
  and persisted through the event-worker repository.

No fake events are created. Intrusion still depends on Savant emitting person
observations/events for the active source after config alignment.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import math
import os
import shutil
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import redis
import yaml
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[2]
SERVICES_FACE_WORKER = ROOT / "services" / "face-worker"
SERVICES_EVENT_WORKER = ROOT / "services" / "event-worker"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SERVICES_FACE_WORKER) not in sys.path:
    sys.path.insert(0, str(SERVICES_FACE_WORKER))

from scripts.tools import run_c2_13_rtsp_watchlist_intrusion_probe as c2_13  # noqa: E402
from app.repository import FaceObservationRepository  # type: ignore  # noqa: E402
from app.face_match_event_service import (  # type: ignore  # noqa: E402
    build_watchlist_hit_event,
    publish_security_event,
)
from app.vector_store import FaceVectorStore  # type: ignore  # noqa: E402

_EVENT_REPOSITORY_SPEC = importlib.util.spec_from_file_location(
    "c2_13r_event_worker_repository",
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
DEFAULT_CAMERA_CONFIG = c2_13.DEFAULT_CAMERA_CONFIG
DEFAULT_COMPOSE_FILE = c2_13.DEFAULT_COMPOSE_FILE
DEFAULT_ENV_FILE = c2_13.DEFAULT_ENV_FILE
DEFAULT_RUNTIME_SECONDS = 180.0
DEFAULT_MAX_MESSAGES = 100
DEFAULT_THRESHOLD = 0.65
DEFAULT_WATCHLIST_RULE_ID = "c2_13r_rtsp_reese_finch_watchlist_rule"
DEFAULT_INTRUSION_RULE_ID = "c2_13r_rtsp_intrusion_rule"
FACE_CONSUMER_GROUP = "c2_13r_face_worker"
FACE_CONSUMER_NAME = "c2_13r_face_worker_1"
EVENT_CONSUMER_GROUP = "c2_13r_event_worker"
EVENT_CONSUMER_NAME = "c2_13r_event_worker_1"

RESULT_PASS = "PASS_C2_13R_RTSP_WATCHLIST_INTRUSION_RUNTIME_READY"
RESULT_WATCHLIST_READY_INTRUSION_NO_TRIGGER = "PARTIAL_C2_13R_WATCHLIST_READY_INTRUSION_NO_TRIGGER"
RESULT_INTRUSION_READY_WATCHLIST_NO_MATCH = "PARTIAL_C2_13R_INTRUSION_READY_WATCHLIST_NO_MATCH"
RESULT_ALIGNED_NO_MATCH_OR_TRIGGER = "PARTIAL_C2_13R_RUNTIME_ALIGNED_NO_MATCH_OR_TRIGGER"
RESULT_INTRUSION_CONFIG_GAP = "PARTIAL_C2_13R_INTRUSION_CONFIG_GAP"
RESULT_WORKER_RUNTIME_GAP = "PARTIAL_C2_13R_WORKER_RUNTIME_GAP"
RESULT_FAIL = "FAIL_C2_13R_RTSP_RUNTIME_ALIGNMENT_BLOCKED"

WATCHLIST_PASS = "PASS"
WATCHLIST_NO_MATCH = "PARTIAL_C2_13R_WATCHLIST_NO_MATCH_IN_WINDOW"
WATCHLIST_NO_FACE_OBSERVATIONS = "PARTIAL_C2_13R_WATCHLIST_NO_FACE_OBSERVATIONS"
WATCHLIST_WORKER_GAP = "PARTIAL_C2_13R_WATCHLIST_WORKER_RUNTIME_GAP"

INTRUSION_PASS = "PASS"
INTRUSION_NO_TRIGGER = "PARTIAL_C2_13R_INTRUSION_NO_TRIGGER_IN_WINDOW"
INTRUSION_NO_POSE_PERSON = "PARTIAL_C2_13R_INTRUSION_NO_POSE_PERSON"
INTRUSION_CONFIG_GAP = "PARTIAL_C2_13R_INTRUSION_CONFIG_GAP"

TARGET_EXTERNAL_IDS = ["demo:f4_3:reese", "demo:f4_3:finch"]


def run_c2_13r_alignment(
    *,
    output_dir: Path,
    database_url: str,
    redis_url: str,
    runtime_seconds: float,
    max_messages: int,
    threshold: float,
    camera_config_path: Path,
    compose_file: Path,
    env_file: Path,
    api_base_url: str | None,
    restart_runtime_if_needed: bool,
    overwrite: bool = False,
) -> c2_13.ProbeResult:
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    containers_before = c2_13.inspect_runtime_containers()
    source_config_before = c2_13.inspect_rtsp_source_config(
        containers=containers_before,
        compose_file=compose_file,
        env_file=env_file,
        camera_config_path=camera_config_path,
    )
    if source_config_before.get("input_type") != "rtsp":
        summary = build_blocked_summary(
            output_dir=output_dir,
            source_config=source_config_before,
            result_marker=RESULT_FAIL,
            reason="active_input_not_rtsp",
        )
        return write_outputs_and_result(output_dir, summary)

    alignment = align_camera_config_to_active_source(
        camera_config_path=camera_config_path,
        source_config=source_config_before,
    )
    restart_report = {
        "restart_requested": False,
        "services": [],
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "reason": "",
    }
    if restart_runtime_if_needed and alignment.get("config_changed"):
        restart_report = restart_c2_runtime_for_config_reload(
            compose_file=compose_file,
            reason="camera_config_changed_for_active_rtsp_source",
        )
    containers_after_align = c2_13.inspect_runtime_containers()
    source_config_after = c2_13.inspect_rtsp_source_config(
        containers=containers_after_align,
        compose_file=compose_file,
        env_file=env_file,
        camera_config_path=camera_config_path,
    )
    source_config_after["watchlist_rule_id"] = DEFAULT_WATCHLIST_RULE_ID

    redis_client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    ensure_stream_group(redis_client, c2_13.FACE_STREAM, FACE_CONSUMER_GROUP, "$")
    ensure_stream_group(redis_client, c2_13.EVENT_STREAM, EVENT_CONSUMER_GROUP, "$")
    start_wall = datetime.now(timezone.utc)
    redis_before = c2_13.inspect_redis(redis_client)
    db_before = c2_13.inspect_database(database_url, source_config_after.get("source_id"))

    if runtime_seconds > 0:
        time.sleep(runtime_seconds)

    face_worker = run_bounded_face_worker(
        database_url=database_url,
        redis_client=redis_client,
        threshold=threshold,
        max_messages=max_messages,
    )
    event_worker = run_bounded_event_worker(
        database_url=database_url,
        redis_client=redis_client,
        max_messages=max_messages,
    )

    redis_after = c2_13.inspect_redis(redis_client)
    db_after = c2_13.inspect_database(database_url, source_config_after.get("source_id"))
    end_wall = datetime.now(timezone.utc)
    event_rows = c2_13.fetch_recent_event_rows(
        database_url=database_url,
        source_id=source_config_after.get("source_id"),
        start_time=start_wall,
        end_time=end_wall,
    )
    api_results = c2_13.query_api(api_base_url, source_config_after.get("source_id"))
    watchlist_metrics = build_watchlist_metrics(
        face_worker=face_worker,
        event_worker=event_worker,
        event_rows=event_rows,
        threshold=threshold,
    )
    intrusion_metrics = build_intrusion_metrics(
        source_config=source_config_after,
        redis_before=redis_before,
        redis_after=redis_after,
        event_worker=event_worker,
        event_rows=event_rows,
    )
    worker_status = {
        "face_worker": {
            "mode": "bounded_local_consumer",
            "consumer_group": FACE_CONSUMER_GROUP,
            "consumer_name": FACE_CONSUMER_NAME,
            "stream": c2_13.FACE_STREAM,
            "messages_read": face_worker["messages_read"],
            "inserted": face_worker["inserted"],
            "duplicates": face_worker["duplicates"],
            "failed": face_worker["failed"],
            "watchlist_events_emitted": face_worker["watchlist_events_emitted"],
            "status": "verified" if not face_worker["fatal_error"] else "error",
        },
        "event_worker": {
            "mode": "bounded_local_consumer",
            "consumer_group": EVENT_CONSUMER_GROUP,
            "consumer_name": EVENT_CONSUMER_NAME,
            "stream": c2_13.EVENT_STREAM,
            "messages_read": event_worker["messages_read"],
            "events_inserted": event_worker["events_inserted"],
            "duplicates": event_worker["duplicates"],
            "failed": event_worker["failed"],
            "status": "verified" if not event_worker["fatal_error"] else "error",
        },
        "services_started": [],
        "services_already_running": [],
        "containers_restarted": restart_report.get("services", []),
        "restart_report": restart_report,
    }
    decision = decide_overall(
        input_type=source_config_after.get("input_type"),
        intrusion_config_aligned=bool(alignment["active_source_bound"]),
        face_worker_ok=not face_worker["fatal_error"],
        event_worker_ok=not event_worker["fatal_error"],
        watchlist_result=watchlist_metrics["watchlist_result"],
        intrusion_result=intrusion_metrics["intrusion_result"],
    )
    runtime_status = {
        "schema_version": "1.0",
        "started_at": start_wall.isoformat(),
        "ended_at": end_wall.isoformat(),
        "runtime_seconds_requested": runtime_seconds,
        "runtime_seconds_observed": (end_wall - start_wall).total_seconds(),
        "redis_before": redis_before,
        "redis_after": redis_after,
        "database_before": db_before,
        "database_after": db_after,
        "containers_before": containers_before,
        "containers_after": c2_13.inspect_runtime_containers(),
        "services_started": [],
        "containers_restarted": [],
        "restart_report": restart_report,
    }
    visual_evidence = {
        "visual_evidence_status": "not_generated",
        "reason": (
            "C2.13R focuses RTSP algorithm/event alignment; "
            "event-style Replay still not passed"
        ),
        "event_style_replay_job_passed": False,
    }
    payload = {
        "runtime_config_alignment": alignment,
        "rtsp_source_status": source_config_after,
        "roi_rule_config": alignment.get("roi_rule_config"),
        "worker_runtime_status": worker_status,
        "watchlist_metrics": watchlist_metrics,
        "intrusion_metrics": intrusion_metrics,
        "event_worker_metrics": event_worker,
        "api_query_results": api_results,
        "visual_evidence": visual_evidence,
        "decision": decision,
    }
    unsafe = c2_13.scan_for_unsafe_payload(payload)
    if not unsafe["passed"]:
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
        "input_type": source_config_after.get("input_type"),
        "source_id": source_config_after.get("source_id"),
        "camera_id": source_config_after.get("camera_id"),
        "rtsp_url_redacted": source_config_after.get("rtsp_url_redacted"),
        "runtime_window": {
            "started_at": start_wall.isoformat(),
            "ended_at": end_wall.isoformat(),
            "runtime_seconds_requested": runtime_seconds,
            "runtime_seconds_observed": (end_wall - start_wall).total_seconds(),
        },
        "config_alignment": {
            "camera_config_path": str(camera_config_path),
            "old_configured_sources": alignment.get("old_configured_sources"),
            "new_configured_sources": alignment.get("new_configured_sources"),
            "active_source_bound": alignment.get("active_source_bound"),
            "config_changed": alignment.get("config_changed"),
            "runtime_restart_performed": bool(restart_report.get("services")),
        },
        "watchlist_result": watchlist_metrics["watchlist_result"],
        "intrusion_result": intrusion_metrics["intrusion_result"],
        "watchlist_hit_count": watchlist_metrics["watchlist_hit_count"],
        "intrusion_event_count": intrusion_metrics["intrusion_event_count"],
        "payload_has_embedding": unsafe["payload_has_embedding"],
        "payload_has_image_bytes": unsafe["payload_has_image_bytes"],
        "unsafe_payload_scan_passed": unsafe["passed"],
        "event_style_replay_job_passed": False,
        "event_style_replay_claimed": False,
        "visual_evidence_status": visual_evidence["visual_evidence_status"],
        "output_dir": str(output_dir),
    }

    c2_13._write_json(output_dir / "runtime_config_alignment.json", alignment)
    c2_13._write_json(output_dir / "rtsp_source_status.json", source_config_after)
    c2_13._write_json(output_dir / "roi_rule_config.json", alignment.get("roi_rule_config"))
    c2_13._write_json(output_dir / "worker_runtime_status.json", worker_status)
    c2_13._write_json(output_dir / "watchlist_metrics.json", watchlist_metrics)
    c2_13._write_json(output_dir / "intrusion_metrics.json", intrusion_metrics)
    c2_13._write_json(output_dir / "event_worker_metrics.json", event_worker)
    c2_13._write_json(output_dir / "api_query_results.json", api_results)
    c2_13._write_json(output_dir / "unsafe_payload_scan.json", unsafe)
    c2_13._write_json(output_dir / "decision_summary.json", {**decision, **summary})
    c2_13._write_json(output_dir / "runtime_status.json", runtime_status)
    if event_rows:
        c2_13._write_json(output_dir / "persisted_event_rows.json", event_rows)
    if watchlist_metrics.get("watchlist_events"):
        c2_13._write_json(output_dir / "watchlist_event.json", watchlist_metrics["watchlist_events"][0])
    if intrusion_metrics.get("intrusion_events"):
        c2_13._write_json(output_dir / "intrusion_event.json", intrusion_metrics["intrusion_events"][0])
    (output_dir / "operator_rtsp_watchlist_intrusion_alignment_report.html").write_text(
        render_operator_report(summary, alignment, worker_status, watchlist_metrics, intrusion_metrics),
        encoding="utf-8",
    )

    return c2_13.ProbeResult(
        result_marker=str(summary["result_marker"]),
        output_dir=output_dir,
        summary_path=output_dir / "decision_summary.json",
        summary=summary,
    )


def align_camera_config_to_active_source(
    *,
    camera_config_path: Path,
    source_config: dict[str, Any],
) -> dict[str, Any]:
    source_id = str(source_config.get("source_id") or "")
    camera_id = str(source_config.get("camera_id") or source_id)
    rtsp_url = str(source_config.get("rtsp_url_redacted") or "")
    old_raw = yaml.safe_load(camera_config_path.read_text(encoding="utf-8")) or {}
    old_sources = sorted(
        str(cfg.get("source_id"))
        for cfg in (old_raw.get("cameras") or {}).values()
        if isinstance(cfg, dict) and cfg.get("source_id")
    )
    raw = json.loads(json.dumps(old_raw))
    cameras = raw.setdefault("cameras", {})
    roi_points = [[0.0, 0.0], [1920.0, 0.0], [1920.0, 1080.0], [0.0, 1080.0]]
    camera_entry = {
        "enabled": True,
        "source_id": source_id,
        "name": "C2.13R RTSP Runtime Alignment",
        "rtsp_url": rtsp_url,
        "gpu_id": 0,
        "zones": {
            "c2_13r_rtsp_full_frame": {
                "type": "polygon",
                "points": roi_points,
            }
        },
        "rules": {
            "intrusion": {
                "enabled": True,
                "rule_id": DEFAULT_INTRUSION_RULE_ID,
                "zone": "c2_13r_rtsp_full_frame",
                "severity": "medium",
                "cooldown_s": 30,
                "clip_required": False,
                "snapshot_required": False,
                "min_inside_ms": 1,
                "min_person_confidence": 0.25,
                "min_person_width": 20,
                "min_person_height": 40,
                "min_visible_keypoints": 0,
                "max_bbox_area_ratio": 0.9,
            }
        },
    }
    config_changed = cameras.get(camera_id) != camera_entry
    cameras[camera_id] = camera_entry
    if config_changed:
        camera_config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    new_sources = sorted(
        str(cfg.get("source_id"))
        for cfg in cameras.values()
        if isinstance(cfg, dict) and cfg.get("source_id")
    )
    return {
        "schema_version": "1.0",
        "camera_config_path": str(camera_config_path),
        "active_source_id": source_id,
        "active_camera_id": camera_id,
        "old_configured_sources": old_sources,
        "new_configured_sources": new_sources,
        "stale_intrusion_source": "c1e_rtsp_replay" if "c1e_rtsp_replay" in old_sources else None,
        "active_source_bound": source_id in new_sources,
        "config_changed": config_changed,
        "roi_rule_config": {
            "algorithm_id": "behavior.intrusion",
            "rule_id": DEFAULT_INTRUSION_RULE_ID,
            "camera_id": camera_id,
            "source_id": source_id,
            "coordinate_space": "pixel",
            "roi_polygon": roi_points,
            "min_inside_ms": 1,
            "cooldown_s": 30,
            "min_person_confidence": 0.25,
            "min_person_width": 20,
            "min_person_height": 40,
            "min_visible_keypoints": 0,
        },
        "runtime_restart_required_for_savant_to_load_new_config": config_changed,
    }


def restart_c2_runtime_for_config_reload(*, compose_file: Path, reason: str) -> dict[str, Any]:
    services = ["savant-security", "source-adapter"]
    report = {
        "restart_requested": True,
        "services": services,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "reason": reason,
    }
    try:
        proc = c2_13.subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "restart", *services],
            check=False,
            text=True,
            stdout=c2_13.subprocess.PIPE,
            stderr=c2_13.subprocess.PIPE,
            timeout=60,
        )
        report["returncode"] = proc.returncode
        report["stdout"] = proc.stdout[-4000:]
        report["stderr"] = proc.stderr[-4000:]
    except Exception as exc:
        report["returncode"] = -1
        report["stderr"] = str(exc)
    wait_for_savant_after_restart()
    return report


def wait_for_savant_after_restart(timeout_seconds: float = 45.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        info = c2_13.docker_inspect("c2-poc-savant")
        if info:
            state = c2_13._dict(info.get("State"))
            health = c2_13._dict(state.get("Health")).get("Status")
            if state.get("Running") and health in ("healthy", None):
                return
        time.sleep(1)


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


def run_bounded_face_worker(
    *,
    database_url: str,
    redis_client: redis.Redis,
    threshold: float,
    max_messages: int,
) -> dict[str, Any]:
    result = {
        "messages_read": 0,
        "inserted": 0,
        "duplicates": 0,
        "failed": 0,
        "errors": [],
        "watchlist_events_emitted": 0,
        "top_reese_match": None,
        "top_finch_match": None,
        "best_match": None,
        "watchlist_events": [],
        "fatal_error": None,
    }
    try:
        messages = redis_client.xreadgroup(
            FACE_CONSUMER_GROUP,
            FACE_CONSUMER_NAME,
            {c2_13.FACE_STREAM: ">"},
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
            register_vector(conn)
            repo = FaceObservationRepository(conn)
            store = FaceVectorStore(conn)
            for msg_id, fields in entries:
                msg_id_s = msg_id if isinstance(msg_id, str) else msg_id.decode()
                data_raw = fields.get("data") if isinstance(fields, dict) else None
                if isinstance(data_raw, bytes):
                    data_raw = data_raw.decode()
                try:
                    obs = json.loads(data_raw or "{}")
                    validate_face_observation_for_worker(obs)
                    inserted_id = repo.insert_observation(obs)
                    if inserted_id:
                        result["inserted"] += 1
                    else:
                        result["duplicates"] += 1
                    redis_client.xack(c2_13.FACE_STREAM, FACE_CONSUMER_GROUP, msg_id_s)
                    gallery_results = store.search_gallery(
                        [float(x) for x in obs["embedding"]],
                        top_k=10,
                        min_similarity=None,
                        person_ids=[5, 6],
                    )
                    for match in gallery_results:
                        external_id = match.get("external_person_id")
                        if external_id not in TARGET_EXTERNAL_IDS:
                            continue
                        candidate = {
                            "query_external_person_id": external_id,
                            "query_person_id": int(match["person_id"]),
                            "query_gallery_embedding_id": int(match["id"]),
                            "similarity": float(match["similarity"]),
                            "threshold": threshold,
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
                            "fake_match_used": False,
                            "gallery_self_match_used": False,
                        }
                        update_top_match(result, candidate)
                        if float(match["similarity"]) >= threshold:
                            event = build_watchlist_hit_event(
                                observation=obs,
                                gallery_match=match,
                                threshold=threshold,
                            )
                            event["rule_name"] = DEFAULT_WATCHLIST_RULE_ID
                            event["payload"]["watchlist_rule_id"] = DEFAULT_WATCHLIST_RULE_ID
                            event["payload"]["fake_match_used"] = False
                            event["payload"]["gallery_self_match_used"] = False
                            publish_security_event(redis_client, event, stream=c2_13.EVENT_STREAM)
                            result["watchlist_events_emitted"] += 1
                            result["watchlist_events"].append(c2_13.strip_unsafe_payload(event))
                except Exception as exc:
                    result["failed"] += 1
                    if len(result["errors"]) < 10:
                        result["errors"].append(
                            {
                                "redis_id": msg_id_s,
                                "source_observation_id": safe_source_observation_id(data_raw),
                                "error": str(exc),
                            }
                        )
            result["best_match"] = best_match(result.get("top_reese_match"), result.get("top_finch_match"))
    except Exception as exc:
        result["fatal_error"] = f"face_worker_db_or_vector_search_failed:{exc}"
    return result


def validate_face_observation_for_worker(obs: dict[str, Any]) -> None:
    if not obs.get("source_observation_id"):
        raise ValueError("source_observation_id_missing")
    embedding = obs.get("embedding")
    if not isinstance(embedding, list) or len(embedding) != 512:
        raise ValueError("embedding_invalid")
    for value in embedding:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("embedding_value_invalid")


def safe_source_observation_id(data_raw: Any) -> str | None:
    try:
        parsed = json.loads(data_raw or "{}")
    except Exception:
        return None
    value = parsed.get("source_observation_id") if isinstance(parsed, dict) else None
    return str(value) if value is not None else None


def update_top_match(result: dict[str, Any], candidate: dict[str, Any]) -> None:
    key = "top_reese_match" if candidate["query_external_person_id"].endswith(":reese") else "top_finch_match"
    current = result.get(key)
    if current is None or float(candidate["similarity"]) > float(current.get("similarity", -999)):
        result[key] = candidate


def best_match(*matches: dict[str, Any] | None) -> dict[str, Any] | None:
    valid = [m for m in matches if isinstance(m, dict)]
    if not valid:
        return None
    return max(valid, key=lambda m: float(m.get("similarity") or -999))


def run_bounded_event_worker(
    *,
    database_url: str,
    redis_client: redis.Redis,
    max_messages: int,
) -> dict[str, Any]:
    result = {
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
            EVENT_CONSUMER_GROUP,
            EVENT_CONSUMER_NAME,
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
                msg_id_s = msg_id if isinstance(msg_id, str) else msg_id.decode()
                data_raw = fields.get("data") if isinstance(fields, dict) else None
                if isinstance(data_raw, bytes):
                    data_raw = data_raw.decode()
                try:
                    event = json.loads(data_raw or "{}")
                    event_id = repo.insert_event(event)
                    if event_id:
                        repo.create_evidence_task(event, event_id)
                        result["events_inserted"] += 1
                        persisted = c2_13.strip_unsafe_payload(dict(event))
                        persisted["event_id"] = event_id
                        result["persisted_events"].append(persisted)
                    else:
                        result["duplicates"] += 1
                    redis_client.xack(c2_13.EVENT_STREAM, EVENT_CONSUMER_GROUP, msg_id_s)
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


def safe_source_event_id(data_raw: Any) -> str | None:
    try:
        parsed = json.loads(data_raw or "{}")
    except Exception:
        return None
    value = parsed.get("source_event_id") if isinstance(parsed, dict) else None
    return str(value) if value is not None else None


def build_watchlist_metrics(
    *,
    face_worker: dict[str, Any],
    event_worker: dict[str, Any],
    event_rows: list[dict[str, Any]],
    threshold: float,
) -> dict[str, Any]:
    events = [
        e for e in event_worker.get("persisted_events", [])
        if e.get("event_type") == "watchlist_hit"
    ] + [e for e in event_rows if e.get("event_type") == "watchlist_hit"]
    best = face_worker.get("best_match")
    if events and is_valid_watchlist_pass_match(best, threshold):
        result = WATCHLIST_PASS
        reason = "watchlist_hit_emitted_and_persisted"
    elif face_worker.get("fatal_error") or event_worker.get("fatal_error"):
        result = WATCHLIST_WORKER_GAP
        reason = "bounded_worker_error"
    elif face_worker.get("messages_read", 0) <= 0:
        result = WATCHLIST_NO_FACE_OBSERVATIONS
        reason = "no_new_face_observation_messages_for_bounded_worker"
    else:
        result = WATCHLIST_NO_MATCH
        reason = "no_reese_finch_match_above_threshold"
    return {
        "schema_version": "1.0",
        "watchlist_result": result,
        "reason": reason,
        "threshold": threshold,
        "watchlist_rule_id": DEFAULT_WATCHLIST_RULE_ID,
        "observations_count": face_worker.get("messages_read", 0),
        "db_inserted_count": face_worker.get("inserted", 0),
        "db_duplicate_count": face_worker.get("duplicates", 0),
        "top_reese_match": face_worker.get("top_reese_match"),
        "top_finch_match": face_worker.get("top_finch_match"),
        "best_match": best,
        "watchlist_hit_count": len(events),
        "watchlist_event_ids": [
            e.get("event_id") or e.get("id") or e.get("source_event_id")
            for e in events
        ],
        "watchlist_events": events,
        "fake_match_used": False,
        "gallery_self_match_used": False,
    }


def is_valid_watchlist_pass_match(match: dict[str, Any] | None, threshold: float) -> bool:
    if not isinstance(match, dict):
        return False
    if match.get("query_external_person_id") not in TARGET_EXTERNAL_IDS:
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
    event_worker: dict[str, Any],
    event_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    events = [
        e for e in event_worker.get("persisted_events", [])
        if e.get("event_type") == "intrusion"
    ] + [e for e in event_rows if e.get("event_type") == "intrusion"]
    person_before = int(redis_before["streams"][c2_13.PERSON_STREAM].get("length") or 0)
    person_after = int(redis_after["streams"][c2_13.PERSON_STREAM].get("length") or 0)
    person_count = max(person_after - person_before, 0)
    bound = bool(c2_13._dict(source_config.get("camera_config")).get("source_id_matches_runtime"))
    if events:
        result = INTRUSION_PASS
        reason = "intrusion_event_persisted"
    elif not bound:
        result = INTRUSION_CONFIG_GAP
        reason = "intrusion_config_not_bound_to_active_source"
    elif person_count <= 0:
        result = INTRUSION_NO_POSE_PERSON
        reason = "no_person_pose_observations_in_window_after_alignment"
    else:
        result = INTRUSION_NO_TRIGGER
        reason = "person_pose_observed_but_intrusion_not_triggered"
    return {
        "schema_version": "1.0",
        "intrusion_result": result,
        "reason": reason,
        "algorithm_id": "behavior.intrusion",
        "rule_id": DEFAULT_INTRUSION_RULE_ID,
        "roi_configured_for_runtime_source": bound,
        "person_pose_observation_count": person_count,
        "intrusion_event_count": len(events),
        "intrusion_event_ids": [
            e.get("event_id") or e.get("id") or e.get("source_event_id")
            for e in events
        ],
        "intrusion_events": events,
        "fake_intrusion_event_used": False,
    }


def decide_overall(
    *,
    input_type: str | None,
    intrusion_config_aligned: bool,
    face_worker_ok: bool,
    event_worker_ok: bool,
    watchlist_result: str,
    intrusion_result: str,
) -> dict[str, Any]:
    if input_type != "rtsp":
        return {"result_marker": RESULT_FAIL, "reason": "input_not_rtsp"}
    if not intrusion_config_aligned:
        return {
            "result_marker": RESULT_INTRUSION_CONFIG_GAP,
            "reason": "intrusion_config_not_aligned",
            "watchlist_result": watchlist_result,
            "intrusion_result": intrusion_result,
        }
    if not face_worker_ok or not event_worker_ok:
        return {
            "result_marker": RESULT_WORKER_RUNTIME_GAP,
            "reason": "bounded_worker_runtime_error",
            "watchlist_result": watchlist_result,
            "intrusion_result": intrusion_result,
        }
    if watchlist_result == WATCHLIST_PASS and intrusion_result == INTRUSION_PASS:
        marker = RESULT_PASS
        reason = "watchlist_and_intrusion_ready"
    elif watchlist_result == WATCHLIST_PASS:
        marker = RESULT_WATCHLIST_READY_INTRUSION_NO_TRIGGER
        reason = "watchlist_ready_intrusion_no_trigger"
    elif intrusion_result == INTRUSION_PASS:
        marker = RESULT_INTRUSION_READY_WATCHLIST_NO_MATCH
        reason = "intrusion_ready_watchlist_no_match"
    else:
        marker = RESULT_ALIGNED_NO_MATCH_OR_TRIGGER
        reason = "runtime_aligned_but_no_watchlist_match_or_intrusion_trigger"
    return {
        "result_marker": marker,
        "reason": reason,
        "watchlist_result": watchlist_result,
        "intrusion_result": intrusion_result,
    }


def build_blocked_summary(
    *,
    output_dir: Path,
    source_config: dict[str, Any],
    result_marker: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "result_marker": result_marker,
        "decision_reason": reason,
        "input_type": source_config.get("input_type"),
        "source_id": source_config.get("source_id"),
        "camera_id": source_config.get("camera_id"),
        "output_dir": str(output_dir),
        "payload_has_embedding": False,
        "payload_has_image_bytes": False,
        "unsafe_payload_scan_passed": True,
        "event_style_replay_job_passed": False,
        "event_style_replay_claimed": False,
    }


def write_outputs_and_result(output_dir: Path, summary: dict[str, Any]) -> c2_13.ProbeResult:
    c2_13._write_json(output_dir / "decision_summary.json", summary)
    return c2_13.ProbeResult(
        result_marker=str(summary["result_marker"]),
        output_dir=output_dir,
        summary_path=output_dir / "decision_summary.json",
        summary=summary,
    )


def render_operator_report(
    summary: dict[str, Any],
    alignment: dict[str, Any],
    worker_status: dict[str, Any],
    watchlist_metrics: dict[str, Any],
    intrusion_metrics: dict[str, Any],
) -> str:
    rows = [
        ("Result marker", summary.get("result_marker")),
        ("Input type", summary.get("input_type")),
        ("Source ID", summary.get("source_id")),
        ("Camera ID", summary.get("camera_id")),
        ("RTSP URL", summary.get("rtsp_url_redacted")),
        ("Config changed", alignment.get("config_changed")),
        ("Active source bound", alignment.get("active_source_bound")),
        ("Face worker mode", c2_13._dict(worker_status.get("face_worker")).get("mode")),
        ("Event worker mode", c2_13._dict(worker_status.get("event_worker")).get("mode")),
        ("Watchlist result", watchlist_metrics.get("watchlist_result")),
        ("Watchlist hits", watchlist_metrics.get("watchlist_hit_count")),
        ("Intrusion result", intrusion_metrics.get("intrusion_result")),
        ("Intrusion events", intrusion_metrics.get("intrusion_event_count")),
        ("Event-style Replay passed", summary.get("event_style_replay_job_passed")),
    ]
    row_html = "\n".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in rows
    )
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>C2.13R RTSP Runtime Alignment</title></head>
<body>
<h1>C2.13R RTSP Runtime Alignment</h1>
<table>{row_html}</table>
<p>Visual evidence is optional in C2.13R. Event-style Replay is not passed.</p>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--redis-url", default=os.getenv("C2_13R_REDIS_URL", DEFAULT_REDIS_URL))
    parser.add_argument("--runtime-seconds", type=float, default=float(os.getenv("C2_13R_RUNTIME_SECONDS", DEFAULT_RUNTIME_SECONDS)))
    parser.add_argument("--max-messages", type=int, default=int(os.getenv("C2_13R_MAX_MESSAGES", DEFAULT_MAX_MESSAGES)))
    parser.add_argument("--threshold", type=float, default=float(os.getenv("C2_13R_WATCHLIST_THRESHOLD", DEFAULT_THRESHOLD)))
    parser.add_argument("--camera-config-path", type=Path, default=DEFAULT_CAMERA_CONFIG)
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--api-base-url", default=os.getenv("C2_13R_API_BASE_URL") or None)
    parser.add_argument(
        "--no-runtime-restart",
        action="store_true",
        help="Do not restart C2 Savant/source-adapter after camera config changes.",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = args.run_id or f"c2_13r_rtsp_runtime_alignment_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    output_dir = args.output_dir or args.evidence_root / run_id
    result = run_c2_13r_alignment(
        output_dir=output_dir,
        database_url=args.database_url,
        redis_url=args.redis_url,
        runtime_seconds=args.runtime_seconds,
        max_messages=args.max_messages,
        threshold=args.threshold,
        camera_config_path=args.camera_config_path,
        compose_file=args.compose_file,
        env_file=args.env_file,
        api_base_url=args.api_base_url,
        restart_runtime_if_needed=not args.no_runtime_restart,
        overwrite=args.overwrite,
    )
    print(json.dumps({
        "result_marker": result.result_marker,
        "output_dir": str(result.output_dir),
        "summary": str(result.summary_path),
        "input_type": result.summary.get("input_type"),
        "source_id": result.summary.get("source_id"),
        "camera_id": result.summary.get("camera_id"),
        "watchlist_result": result.summary.get("watchlist_result"),
        "intrusion_result": result.summary.get("intrusion_result"),
        "watchlist_hit_count": result.summary.get("watchlist_hit_count"),
        "intrusion_event_count": result.summary.get("intrusion_event_count"),
        "payload_has_embedding": result.summary.get("payload_has_embedding"),
        "payload_has_image_bytes": result.summary.get("payload_has_image_bytes"),
    }, indent=2, sort_keys=True))
    accepted = {
        RESULT_PASS,
        RESULT_WATCHLIST_READY_INTRUSION_NO_TRIGGER,
        RESULT_INTRUSION_READY_WATCHLIST_NO_MATCH,
        RESULT_ALIGNED_NO_MATCH_OR_TRIGGER,
        RESULT_INTRUSION_CONFIG_GAP,
        RESULT_WORKER_RUNTIME_GAP,
    }
    return 0 if result.result_marker in accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
