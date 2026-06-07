#!/usr/bin/env python3
"""Bounded runtime Reese / Finch capture from the current C2 movie loop.

The probe follows route A: current Savant/runtime pipeline, current
``security.face_observations`` Redis stream, current PostgreSQL, and the
C2.12A Reese / Finch gallery rows. It reads Redis non-destructively, writes only
namespaced C2.12C face_observation rows, and never writes embeddings or image
bytes to JSON/report payloads.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import redis
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[2]
FACE_WORKER_ROOT = ROOT / "services" / "face-worker"
if str(FACE_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(FACE_WORKER_ROOT))

from app.repository import FaceObservationRepository  # noqa: E402


DEFAULT_DATABASE_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6395/0"
DEFAULT_REDIS_STREAM = "security.face_observations"
DEFAULT_EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")
DEFAULT_EVIDENCE_SEARCH_ROOT = Path("/data/video-analytics/media/evidence")
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_REDIS_MESSAGES = 100
DEFAULT_BACKLOG_MESSAGES = 200
DEFAULT_TOP_K = 20
DEFAULT_THRESHOLD = 0.65
DEFAULT_WATCHLIST_RULE_ID = "c2_12c_reese_finch_runtime_watchlist_rule"

RESULT_PASS = "PASS_C2_12C_RUNTIME_REESE_FINCH_WATCHLIST_READY"
RESULT_NO_MATCH = "PARTIAL_C2_12C_RUNTIME_CAPTURE_NO_MATCH_IN_WINDOW"
RESULT_CAPTURE_GAP = "PARTIAL_C2_12C_RUNTIME_OBSERVATION_CAPTURE_GAP"
RESULT_STREAM_EMPTY = "PARTIAL_C2_12C_FACE_OBSERVATION_STREAM_EMPTY"
RESULT_NOT_CONSUMING = "PARTIAL_C2_12C_FACE_WORKER_NOT_CONSUMING"
RESULT_JOIN_GAP = "PARTIAL_C2_12C_MATCH_FOUND_EVIDENCE_JOIN_GAP"
RESULT_FAIL = "FAIL_C2_12C_RUNTIME_CAPTURE_BLOCKED"

TARGETS = {
    "reese": {
        "name": "Reese",
        "person_id": 5,
        "external_person_id": "demo:f4_3:reese",
        "gallery_embedding_id": 4,
    },
    "finch": {
        "name": "Finch",
        "person_id": 6,
        "external_person_id": "demo:f4_3:finch",
        "gallery_embedding_id": 5,
    },
}

FORBIDDEN_KEYS = {
    "embedding",
    "embedding_vector",
    "embedding_values",
    "image",
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
}


@dataclass(frozen=True)
class ProbeResult:
    result_marker: str
    output_dir: Path
    summary_path: Path
    summary: dict[str, Any]


def run_c2_12c_probe(
    *,
    output_dir: Path,
    database_url: str,
    redis_url: str,
    redis_stream: str,
    timeout_seconds: float,
    max_redis_messages: int,
    backlog_messages: int,
    threshold: float,
    top_k: int,
    watchlist_rule_id: str,
    evidence_search_root: Path,
    overwrite: bool = False,
) -> ProbeResult:
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(f"threshold must be in [0.0, 1.0], got {threshold}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")
    if max_redis_messages < 1:
        raise ValueError("max_redis_messages must be >= 1")
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    output_dir = output_dir.resolve(strict=False)
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        _clear_dir(output_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    redis_client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    runtime_before = inspect_runtime_status(
        database_url=database_url,
        redis_client=redis_client,
        redis_stream=redis_stream,
        phase="before_capture",
    )
    db_before = runtime_before["postgres"]["face_observations"]
    redis_before = runtime_before["redis"]["streams"][redis_stream]
    start_id = str(redis_before.get("last_id") or "$")

    captured = capture_runtime_redis_observations(
        redis_client=redis_client,
        redis_stream=redis_stream,
        start_id=start_id,
        timeout_seconds=timeout_seconds,
        max_messages=max_redis_messages,
    )
    capture_mode = "xread_new_messages_non_destructive"
    if not captured and backlog_messages > 0:
        captured = capture_recent_redis_backlog_observations(
            redis_client=redis_client,
            redis_stream=redis_stream,
            max_messages=backlog_messages,
        )
        capture_mode = "xrevrange_non_destructive_backlog_sample"
    prepared = [
        prepare_observation_for_c2_12c(entry, index, capture_namespace=output_dir.name)
        for index, entry in enumerate(captured, start=1)
    ]
    valid_prepared = [item for item in prepared if item.get("valid") is True]
    write_result = insert_prepared_observations(database_url, valid_prepared)

    runtime_after = inspect_runtime_status(
        database_url=database_url,
        redis_client=redis_client,
        redis_stream=redis_stream,
        phase="after_capture",
    )
    captured_sids = [
        item["observation"]["source_observation_id"]
        for item in valid_prepared
        if item.get("observation", {}).get("source_observation_id")
    ]
    db_rows = fetch_captured_db_rows(database_url, captured_sids)
    matches = search_targets_against_captured_observations(
        database_url=database_url,
        captured_source_observation_ids=captured_sids,
        threshold=threshold,
        top_k=top_k,
        evidence_search_root=evidence_search_root,
    )
    distribution = build_similarity_distribution(matches, threshold=threshold)
    decision = decide_result(
        captured_rows=db_rows,
        matches_by_identity=matches,
        threshold=threshold,
        runtime_before=runtime_before,
        runtime_after=runtime_after,
    )
    event = None
    if decision["result_marker"] in {RESULT_PASS, RESULT_JOIN_GAP}:
        event = build_watchlist_event(
            match=decision["best_match"],
            threshold=threshold,
            watchlist_rule_id=watchlist_rule_id,
            output_dir=output_dir,
        )

    unsafe_scan = scan_for_unsafe_payload(
        {
            "runtime_before": runtime_before,
            "runtime_after": runtime_after,
            "captured_observations": build_captured_observations_summary(
                captured_entries=captured,
                prepared_entries=valid_prepared,
                db_rows=db_rows,
                write_result=write_result,
            ),
            "matches": matches,
            "event": event,
            "decision": decision,
        }
    )
    if not unsafe_scan["passed"]:
        decision = {
            "result_marker": RESULT_FAIL,
            "reason": "unsafe_payload_scan_failed",
            "best_match": decision.get("best_match"),
        }

    summary = build_summary(
        output_dir=output_dir,
        runtime_before=runtime_before,
        runtime_after=runtime_after,
        captured_entries=captured,
        prepared_entries=valid_prepared,
        db_rows=db_rows,
        write_result=write_result,
        matches_by_identity=matches,
        distribution=distribution,
        decision=decision,
        event=event,
        unsafe_scan=unsafe_scan,
        threshold=threshold,
        timeout_seconds=timeout_seconds,
        max_redis_messages=max_redis_messages,
        backlog_messages=backlog_messages,
        capture_mode=capture_mode,
        watchlist_rule_id=watchlist_rule_id,
    )

    _write_json(output_dir / "runtime_pipeline_status.json", {
        "before": runtime_before,
        "after": runtime_after,
    })
    _write_json(
        output_dir / "captured_observations_summary.json",
        summary["captured_observations_summary"],
    )
    _write_json(output_dir / "reese_top_matches.json", matches.get("reese", []))
    _write_json(output_dir / "finch_top_matches.json", matches.get("finch", []))
    _write_json(output_dir / "similarity_distribution.json", distribution)
    _write_json(output_dir / "unsafe_payload_scan.json", unsafe_scan)
    if event is not None:
        _write_json(output_dir / "watchlist_event.json", event)
        _write_json(output_dir / "match_report.json", {
            "decision": decision,
            "event": event,
            "sidecar_join": decision.get("best_match", {}).get("sidecar_join"),
        })
        _write_jsonl(output_dir / "identity_patches.jsonl", build_identity_patches(event, decision))
        (output_dir / "operator_runtime_reese_finch_report.html").write_text(
            render_operator_report(summary),
            encoding="utf-8",
        )

    summary_path = output_dir / "summary.json"
    _write_json(summary_path, summary)
    _write_decision_report(output_dir / "decision_report.md", summary)
    return ProbeResult(
        result_marker=str(summary["result_marker"]),
        output_dir=output_dir,
        summary_path=summary_path,
        summary=summary,
    )


def inspect_runtime_status(
    *,
    database_url: str,
    redis_client: redis.Redis,
    redis_stream: str,
    phase: str,
) -> dict[str, Any]:
    containers = inspect_containers()
    redis_streams = inspect_redis_streams(redis_client, [
        redis_stream,
        "security.events",
        "c2_12c.security.events.test",
        "c2_12c.face_observations.test",
    ])
    postgres = inspect_postgres(database_url)
    savant_log_summary = inspect_savant_log_summary()
    source_id = infer_current_source_id(redis_streams.get(redis_stream, {}), postgres)
    return {
        "phase": phase,
        "inspected_at": datetime.now(timezone.utc).isoformat(),
        "compose_file_inferred": "infra/docker-compose.c2-post-savant-replay-poc.yml",
        "containers": containers,
        "redis": {"url": redact_redis_url(redis_client), "streams": redis_streams},
        "postgres": postgres,
        "savant_log_summary": savant_log_summary,
        "current_source_id": source_id,
        "pipeline_running": any(item["name"] == "c2-poc-savant" and "Up" in item["status"] for item in containers),
        "face_worker_container_running": any("face-worker" in item["name"] and "Up" in item["status"] for item in containers),
        "face_observation_stream_has_messages": bool(redis_streams.get(redis_stream, {}).get("xlen", 0)),
    }


def inspect_containers() -> list[dict[str, str]]:
    proc = _run_command(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"])
    if proc["returncode"] != 0:
        return [{"name": "<docker_unavailable>", "status": proc["stderr"], "ports": ""}]
    containers = []
    for line in proc["stdout"].splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 1:
            parts += ["", ""]
        elif len(parts) == 2:
            parts += [""]
        containers.append({"name": parts[0], "status": parts[1], "ports": parts[2]})
    return containers


def inspect_redis_streams(redis_client: redis.Redis, streams: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for stream in streams:
        info: dict[str, Any] = {"exists": False, "xlen": 0, "last_id": None}
        try:
            info["exists"] = bool(redis_client.exists(stream))
            info["xlen"] = int(redis_client.xlen(stream))
            if info["xlen"]:
                last = redis_client.xrevrange(stream, count=1)
                if last:
                    info["last_id"] = last[0][0]
                    fields = last[0][1]
                    info["last_fields"] = sorted(k for k in fields.keys() if k != "data")
                    info["last_source_id"] = fields.get("source_id")
                    info["last_source_observation_id"] = fields.get("source_observation_id")
                try:
                    info["groups"] = redis_client.xinfo_groups(stream)
                except Exception as exc:  # no groups is not an error for C2.12C
                    info["groups_error"] = str(exc)
        except Exception as exc:
            info["error"] = str(exc)
        out[stream] = info
    return out


def inspect_postgres(database_url: str) -> dict[str, Any]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count, MAX(created_at) AS max_created_at FROM face_observations")
            meta = dict(cur.fetchone())
            cur.execute(
                """
                SELECT COALESCE(source_id, '<null>') AS source_id,
                       COALESCE(camera_id, '<null>') AS camera_id,
                       COUNT(*) AS count,
                       COUNT(embedding) AS with_embedding,
                       MAX(created_at) AS max_created_at
                FROM face_observations
                GROUP BY source_id, camera_id
                ORDER BY count DESC, source_id, camera_id
                LIMIT 20
                """
            )
            distribution = [_sanitize_db_row(dict(row)) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT id::text, source_observation_id, camera_id, source_id,
                       track_id, frame_num, timestamp_ms, embedding_dim,
                       embedding_norm, quality, face_confidence, created_at
                FROM face_observations
                ORDER BY created_at DESC
                LIMIT 20
                """
            )
            recent = [_sanitize_db_row(dict(row)) for row in cur.fetchall()]
    return {"face_observations": {"meta": meta, "distribution": distribution, "recent": recent}}


def inspect_savant_log_summary() -> dict[str, Any]:
    proc = _run_command(["docker", "logs", "--tail", "160", "c2-poc-savant"])
    lines = (proc["stdout"] + "\n" + proc["stderr"]).splitlines()
    interesting = [
        line for line in lines
        if any(token in line for token in ("face_obs_export", "face_reid_gate_summary", "face_assoc", "face_embedding"))
    ]
    return {
        "available": proc["returncode"] == 0,
        "last_lines": interesting[-20:],
        "face_obs_export_seen": any("face_obs_export" in line for line in interesting),
        "face_reid_gate_summary_seen": any("face_reid_gate_summary" in line for line in interesting),
    }


def infer_current_source_id(stream_info: dict[str, Any], postgres: dict[str, Any]) -> str | None:
    if stream_info.get("last_source_id"):
        return str(stream_info["last_source_id"])
    distribution = postgres.get("face_observations", {}).get("distribution", [])
    if distribution:
        return distribution[0].get("source_id")
    return None


def capture_runtime_redis_observations(
    *,
    redis_client: redis.Redis,
    redis_stream: str,
    start_id: str,
    timeout_seconds: float,
    max_messages: int,
) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    last_id = start_id
    deadline = time.monotonic() + timeout_seconds
    while len(captured) < max_messages and time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        block_ms = max(1, min(1000, int(remaining * 1000)))
        result = redis_client.xread(
            {redis_stream: last_id},
            count=max_messages - len(captured),
            block=block_ms,
        )
        if not result:
            continue
        for _stream, entries in result:
            for msg_id, fields in entries:
                last_id = msg_id
                parsed = parse_redis_observation_fields(msg_id, fields)
                if parsed.get("valid"):
                    captured.append(parsed)
                if len(captured) >= max_messages:
                    break
    return captured


def capture_recent_redis_backlog_observations(
    *,
    redis_client: redis.Redis,
    redis_stream: str,
    max_messages: int,
) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    entries = redis_client.xrevrange(redis_stream, count=max_messages)
    for msg_id, fields in reversed(entries):
        parsed = parse_redis_observation_fields(msg_id, fields)
        if parsed.get("valid"):
            captured.append(parsed)
    return captured


def parse_redis_observation_fields(msg_id: str, fields: dict[str, str]) -> dict[str, Any]:
    data_raw = fields.get("data")
    if not data_raw:
        return {"redis_id": msg_id, "valid": False, "reason": "data_field_missing"}
    try:
        obs = json.loads(data_raw)
    except json.JSONDecodeError as exc:
        return {"redis_id": msg_id, "valid": False, "reason": f"json_parse_failed:{exc}"}
    if obs.get("message_type") not in (None, "face_observation"):
        return {"redis_id": msg_id, "valid": False, "reason": "not_face_observation"}
    if not obs.get("source_observation_id"):
        return {"redis_id": msg_id, "valid": False, "reason": "source_observation_id_missing"}
    embedding = obs.get("embedding")
    if not isinstance(embedding, list) or len(embedding) != 512:
        return {"redis_id": msg_id, "valid": False, "reason": "embedding_missing_or_wrong_dim"}
    if not all(_is_finite_number(value) for value in embedding):
        return {"redis_id": msg_id, "valid": False, "reason": "embedding_contains_non_finite"}
    return {"redis_id": msg_id, "valid": True, "observation": obs}


def prepare_observation_for_c2_12c(
    entry: dict[str, Any],
    index: int,
    *,
    capture_namespace: str = "manual",
) -> dict[str, Any]:
    if entry.get("valid") is not True:
        return entry
    original = copy.deepcopy(entry["observation"])
    namespaced = copy.deepcopy(original)
    original_sid = str(original.get("source_observation_id") or "")
    namespaced_sid = build_c2_12c_source_observation_id(
        original,
        entry["redis_id"],
        index,
        capture_namespace=capture_namespace,
    )
    payload = strip_unsafe_payload(copy.deepcopy(original.get("payload") or {}))
    payload.update(
        {
            "c2_12c_runtime_capture": True,
            "original_source_observation_id": original_sid,
            "redis_stream_id": entry["redis_id"],
            "source_observation_id_namespace": "c2_12c_runtime",
            "capture_namespace": capture_namespace,
            "primary_identity_join_key": "source_observation_id",
            "track_id_only_identity_join_used": False,
        }
    )
    namespaced["source_observation_id"] = namespaced_sid
    namespaced["payload"] = payload
    namespaced["snapshot_path"] = None
    namespaced["crop_path"] = None
    return {
        "redis_id": entry["redis_id"],
        "valid": True,
        "original_source_observation_id": original_sid,
        "observation": namespaced,
        "safe_observation": sanitize_observation_for_report(namespaced),
    }


def build_c2_12c_source_observation_id(
    obs: dict[str, Any],
    redis_id: str,
    index: int,
    *,
    capture_namespace: str = "manual",
) -> str:
    namespace = safe_id_part(capture_namespace)
    source_id = safe_id_part(str(obs.get("source_id") or obs.get("camera_id") or "unknown_source"))
    track_id = safe_id_part(str(obs.get("track_id") or "unknown_track"))
    media = obs.get("payload", {}).get("media", {}) if isinstance(obs.get("payload"), dict) else {}
    frame_pts = media.get("frame_pts") or obs.get("timestamp_ms") or index
    original_sid = str(obs.get("source_observation_id") or "")
    face_index = infer_face_index(original_sid, redis_id=redis_id, fallback=index)
    return f"face:c2_12c_runtime:{namespace}:{source_id}:{track_id}:{safe_id_part(str(frame_pts))}:{face_index}"


def infer_face_index(original_source_observation_id: str, *, redis_id: str, fallback: int) -> str:
    parts = original_source_observation_id.split(":")
    if len(parts) >= 5 and parts[-1].isdigit():
        return safe_id_part(parts[-1])
    redis_part = safe_id_part(redis_id)
    return f"{fallback}_{redis_part}"


def insert_prepared_observations(database_url: str, prepared_entries: list[dict[str, Any]]) -> dict[str, Any]:
    inserted: list[str] = []
    duplicates: list[str] = []
    failed: list[dict[str, str]] = []
    if not prepared_entries:
        return {"inserted_count": 0, "duplicate_count": 0, "failed_count": 0, "inserted": [], "duplicates": [], "failed": []}
    with psycopg.connect(database_url, autocommit=True) as conn:
        repo = FaceObservationRepository(conn)
        for item in prepared_entries:
            obs = item["observation"]
            sid = str(obs.get("source_observation_id") or "")
            try:
                row_id = repo.insert_observation(obs)
            except Exception as exc:  # fail closed, but do not drop diagnostics
                failed.append({"source_observation_id": sid, "error": str(exc)})
                continue
            if row_id:
                inserted.append(sid)
            else:
                duplicates.append(sid)
    return {
        "inserted_count": len(inserted),
        "duplicate_count": len(duplicates),
        "failed_count": len(failed),
        "inserted": inserted,
        "duplicates": duplicates,
        "failed": failed,
    }


def fetch_captured_db_rows(database_url: str, source_observation_ids: list[str]) -> list[dict[str, Any]]:
    if not source_observation_ids:
        return []
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text, source_observation_id, camera_id, source_id,
                       track_id, timestamp_ms, frame_num, person_bbox, face_bbox,
                       landmarks, face_confidence, quality, detector_model,
                       embedding_model, model_version, embedding_dim,
                       embedding IS NOT NULL AS embedding_present,
                       embedding_norm, association_score, association_method,
                       snapshot_path, crop_path, payload, created_at
                FROM face_observations
                WHERE source_observation_id = ANY(%(source_observation_ids)s::text[])
                ORDER BY created_at DESC
                """,
                {"source_observation_ids": source_observation_ids},
            )
            rows = [_sanitize_db_row(dict(row)) for row in cur.fetchall()]
    return rows


def search_targets_against_captured_observations(
    *,
    database_url: str,
    captured_source_observation_ids: list[str],
    threshold: float,
    top_k: int,
    evidence_search_root: Path,
) -> dict[str, list[dict[str, Any]]]:
    if not captured_source_observation_ids:
        return {"reese": [], "finch": []}
    matches: dict[str, list[dict[str, Any]]] = {}
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            for identity_key, target in TARGETS.items():
                cur.execute(
                    """
                    SELECT p.id AS query_person_id,
                           p.external_person_id AS query_external_person_id,
                           p.name AS query_person_name,
                           pge.id AS query_gallery_embedding_id,
                           fo.id::text AS id,
                           fo.source_observation_id,
                           fo.camera_id,
                           fo.source_id,
                           fo.track_id,
                           fo.timestamp_ms,
                           fo.frame_num,
                           fo.face_bbox,
                           fo.landmarks,
                           fo.face_confidence,
                           fo.quality,
                           fo.embedding_model,
                           fo.model_version,
                           fo.embedding_dim,
                           fo.embedding_norm,
                           fo.payload,
                           fo.created_at,
                           1 - (fo.embedding <=> pge.embedding) AS similarity,
                           fo.embedding <=> pge.embedding AS distance
                    FROM face_observations fo
                    JOIN person_gallery_embeddings pge ON pge.id = %(gallery_embedding_id)s
                    JOIN persons p ON p.id = pge.person_id
                    WHERE fo.source_observation_id = ANY(%(source_observation_ids)s::text[])
                      AND fo.embedding IS NOT NULL
                    ORDER BY fo.embedding <=> pge.embedding
                    LIMIT %(top_k)s
                    """,
                    {
                        "gallery_embedding_id": target["gallery_embedding_id"],
                        "source_observation_ids": captured_source_observation_ids,
                        "top_k": top_k,
                    },
                )
                rows = []
                for rank, row in enumerate(cur.fetchall(), start=1):
                    item = _sanitize_db_row(dict(row))
                    payload = _dict(item.get("payload"))
                    original_sid = payload.get("original_source_observation_id")
                    item.update(
                        {
                            "rank": rank,
                            "identity_key": identity_key,
                            "threshold": threshold,
                            "threshold_passed": float(item.get("similarity") or -999.0) >= threshold,
                            "captured_runtime_observation": True,
                            "fake_match_used": False,
                            "gallery_self_match_used": False,
                            "identity_join_key": "source_observation_id",
                            "track_id_only_identity_join_used": False,
                            "original_source_observation_id": original_sid,
                            "sidecar_join": check_sidecar_join(
                                evidence_search_root,
                                source_observation_id=str(item.get("source_observation_id") or ""),
                                original_source_observation_id=str(original_sid or ""),
                            ),
                        }
                    )
                    rows.append(item)
                matches[identity_key] = rows
    return matches


def decide_result(
    *,
    captured_rows: list[dict[str, Any]],
    matches_by_identity: dict[str, list[dict[str, Any]]],
    threshold: float,
    runtime_before: dict[str, Any],
    runtime_after: dict[str, Any],
) -> dict[str, Any]:
    stream_info = runtime_before.get("redis", {}).get("streams", {}).get(DEFAULT_REDIS_STREAM, {})
    if not stream_info.get("xlen"):
        return {
            "result_marker": RESULT_STREAM_EMPTY,
            "reason": "face_observation_stream_empty",
            "best_match": None,
        }
    if not captured_rows:
        face_worker_running = runtime_after.get("face_worker_container_running") is True
        return {
            "result_marker": RESULT_CAPTURE_GAP if face_worker_running else RESULT_NOT_CONSUMING,
            "reason": "no_new_runtime_observations_captured_in_bounded_window",
            "best_match": None,
        }
    all_matches = [item for rows in matches_by_identity.values() for item in rows]
    best = max(all_matches, key=lambda item: float(item.get("similarity") or -999.0), default=None)
    eligible = [item for item in all_matches if is_valid_pass_match(item, threshold=threshold)]
    if not eligible:
        return {
            "result_marker": RESULT_NO_MATCH,
            "reason": "runtime_capture_completed_no_reese_finch_match_above_threshold",
            "best_match": best,
        }
    best_eligible = max(eligible, key=lambda item: float(item.get("similarity") or -999.0))
    if not best_eligible.get("sidecar_join", {}).get("joinable"):
        return {
            "result_marker": RESULT_JOIN_GAP,
            "reason": "reese_finch_match_found_but_no_sidecar_visual_join",
            "best_match": best_eligible,
        }
    return {
        "result_marker": RESULT_PASS,
        "reason": "reese_finch_match_found_and_sidecar_joinable",
        "best_match": best_eligible,
    }


def is_valid_pass_match(match: dict[str, Any], *, threshold: float) -> bool:
    if match.get("captured_runtime_observation") is not True:
        return False
    if match.get("fake_match_used") is True:
        return False
    if match.get("gallery_self_match_used") is True:
        return False
    if match.get("query_external_person_id") not in {
        TARGETS["reese"]["external_person_id"],
        TARGETS["finch"]["external_person_id"],
    }:
        return False
    if not match.get("source_observation_id"):
        return False
    if match.get("identity_join_key") != "source_observation_id":
        return False
    if match.get("track_id_only_identity_join_used") is True:
        return False
    return float(match.get("similarity") or -999.0) >= threshold


def build_watchlist_event(
    *,
    match: dict[str, Any],
    threshold: float,
    watchlist_rule_id: str,
    output_dir: Path,
) -> dict[str, Any]:
    if not is_valid_pass_match(match, threshold=threshold):
        raise ValueError("invalid_runtime_reese_finch_match")
    external_person_id = str(match["query_external_person_id"])
    event = {
        "schema_version": "1.0",
        "event_type": "watchlist_hit",
        "source_event_id": (
            f"c2_12c:watchlist_hit:{match['source_observation_id']}:"
            f"{external_person_id}:{match['query_gallery_embedding_id']}"
        ),
        "producer": "c2_12c_runtime_reese_finch_capture",
        "camera_id": match.get("camera_id"),
        "source_id": match.get("source_id"),
        "track_id": match.get("track_id"),
        "source_observation_id": match.get("source_observation_id"),
        "original_source_observation_id": match.get("original_source_observation_id"),
        "person_id": match.get("query_person_id"),
        "external_person_id": external_person_id,
        "gallery_embedding_id": match.get("query_gallery_embedding_id"),
        "similarity": match.get("similarity"),
        "threshold": threshold,
        "watchlist_rule_id": watchlist_rule_id,
        "event_ts_ms": match.get("timestamp_ms"),
        "frame_num": match.get("frame_num"),
        "severity": "high",
        "evidence": {
            "bundle_path": str(output_dir) if match.get("sidecar_join", {}).get("joinable") else None,
            "capture_mode": "runtime_redis_face_observation_capture",
            "visual_evidence_joined": bool(match.get("sidecar_join", {}).get("joinable")),
            "event_style_replay_job_passed": False,
        },
        "payload": {
            "identity_source": "external_gallery_to_runtime_video_observation",
            "watchlist_match_source": "runtime_redis_face_observation_pgvector_search",
            "embedding_included": False,
            "image_bytes_included": False,
            "fake_match_used": False,
            "gallery_self_match_used": False,
            "primary_identity_join_key": "source_observation_id",
            "track_id_only_identity_join_used": False,
            "event_style_replay_job_passed": False,
        },
    }
    assert_no_unsafe_payload(event)
    return event


def build_captured_observations_summary(
    *,
    captured_entries: list[dict[str, Any]],
    prepared_entries: list[dict[str, Any]],
    db_rows: list[dict[str, Any]],
    write_result: dict[str, Any],
) -> dict[str, Any]:
    source_ids = sorted({
        item.get("safe_observation", {}).get("source_id")
        for item in prepared_entries
        if item.get("safe_observation", {}).get("source_id")
    })
    samples = [item.get("safe_observation") for item in prepared_entries[:20]]
    return {
        "redis_messages_captured": len(captured_entries),
        "valid_observations_prepared": len(prepared_entries),
        "db_rows_available": len(db_rows),
        "db_write": write_result,
        "source_ids": source_ids,
        "sampled_source_observation_ids": [
            item.get("source_observation_id") for item in samples if item
        ],
        "samples": samples,
    }


def build_similarity_distribution(matches_by_identity: dict[str, list[dict[str, Any]]], *, threshold: float) -> dict[str, Any]:
    out: dict[str, Any] = {"threshold": threshold, "targets": {}}
    for identity_key, rows in matches_by_identity.items():
        sims = [float(row["similarity"]) for row in rows if row.get("similarity") is not None]
        out["targets"][identity_key] = {
            "count": len(sims),
            "best_similarity": max(sims) if sims else None,
            "min_similarity": min(sims) if sims else None,
            "mean_similarity": sum(sims) / len(sims) if sims else None,
            "above_threshold_count": sum(1 for sim in sims if sim >= threshold),
        }
    return out


def build_summary(
    *,
    output_dir: Path,
    runtime_before: dict[str, Any],
    runtime_after: dict[str, Any],
    captured_entries: list[dict[str, Any]],
    prepared_entries: list[dict[str, Any]],
    db_rows: list[dict[str, Any]],
    write_result: dict[str, Any],
    matches_by_identity: dict[str, list[dict[str, Any]]],
    distribution: dict[str, Any],
    decision: dict[str, Any],
    event: dict[str, Any] | None,
    unsafe_scan: dict[str, Any],
    threshold: float,
    timeout_seconds: float,
    max_redis_messages: int,
    backlog_messages: int,
    capture_mode: str,
    watchlist_rule_id: str,
) -> dict[str, Any]:
    db_before = runtime_before["postgres"]["face_observations"]["meta"]
    db_after = runtime_after["postgres"]["face_observations"]["meta"]
    return {
        "schema_version": "1.0",
        "result_marker": decision["result_marker"],
        "output_dir": str(output_dir),
        "runtime_route": "current_savant_runtime_redis_face_observation_stream",
        "runtime_source_status": {
            "source_id": runtime_after.get("current_source_id"),
            "containers_inspected": [item["name"] for item in runtime_after.get("containers", [])],
            "pipeline_running": runtime_after.get("pipeline_running"),
            "face_worker_container_running": runtime_after.get("face_worker_container_running"),
            "redis_stream": DEFAULT_REDIS_STREAM,
            "redis_before": runtime_before.get("redis", {}).get("streams", {}).get(DEFAULT_REDIS_STREAM),
            "redis_after": runtime_after.get("redis", {}).get("streams", {}).get(DEFAULT_REDIS_STREAM),
            "db_before_count": db_before.get("count"),
            "db_after_count": db_after.get("count"),
            "db_before_max_created_at": db_before.get("max_created_at"),
            "db_after_max_created_at": db_after.get("max_created_at"),
        },
        "capture": {
            "duration_seconds": timeout_seconds,
            "max_redis_messages": max_redis_messages,
            "backlog_messages": backlog_messages,
            "redis_read_mode": capture_mode,
            "redis_destructive_consume": False,
            "redis_ack_used": False,
            "redis_delete_used": False,
            "db_write_namespace": "face:c2_12c_runtime",
        },
        "captured_observations_summary": build_captured_observations_summary(
            captured_entries=captured_entries,
            prepared_entries=prepared_entries,
            db_rows=db_rows,
            write_result=write_result,
        ),
        "threshold": threshold,
        "threshold_enforced": True,
        "watchlist_rule_id": watchlist_rule_id,
        "reese_top_matches": matches_by_identity.get("reese", []),
        "finch_top_matches": matches_by_identity.get("finch", []),
        "similarity_distribution": distribution,
        "decision": decision,
        "watchlist_event_generated": event is not None,
        "watchlist_event": event,
        "payload_has_embedding": unsafe_scan.get("payload_has_embedding"),
        "payload_has_image_bytes": unsafe_scan.get("payload_has_image_bytes"),
        "unsafe_payload_scan_passed": unsafe_scan.get("passed"),
        "fake_match_used": False,
        "gallery_self_match_used": False,
        "test_c2_4_person_used": False,
        "event_style_replay_job_passed": False,
        "event_style_replay_claimed": False,
        "db_window_fallback_used": False,
        "legacy_annotation_fallback_used": False,
        "limitations": [
            "bounded_window_may_miss_target_scene",
            "not_broad_accuracy_test",
            "event_style_replay_not_passed",
            "visual_evidence_requires_sidecar_join",
        ],
    }


def check_sidecar_join(
    evidence_search_root: Path,
    *,
    source_observation_id: str,
    original_source_observation_id: str,
) -> dict[str, Any]:
    needles = [value for value in {source_observation_id, original_source_observation_id} if value]
    result = {
        "joinable": False,
        "matched_sidecar_path": None,
        "matched_source_observation_id": None,
        "match_mode": "direct_source_observation_id_string",
        "db_window_fallback_used": False,
        "legacy_annotation_fallback_used": False,
    }
    if not needles or not evidence_search_root.is_dir():
        return result
    sidecars = sorted(evidence_search_root.rglob("annotations.frame_cache.identity.jsonl"))
    for sidecar in sidecars[:500]:
        try:
            text = sidecar.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for needle in needles:
            if needle in text:
                result.update(
                    {
                        "joinable": True,
                        "matched_sidecar_path": str(sidecar),
                        "matched_source_observation_id": needle,
                    }
                )
                return result
    return result


def build_identity_patches(event: dict[str, Any], decision: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "patch_type": "identity_only",
            "source_observation_id": event["source_observation_id"],
            "external_person_id": event["external_person_id"],
            "person_id": event["person_id"],
            "similarity": event["similarity"],
            "threshold": event["threshold"],
            "geometry_changed": False,
            "visual_evidence_joined": bool(decision.get("best_match", {}).get("sidecar_join", {}).get("joinable")),
        }
    ]


def render_operator_report(summary: dict[str, Any]) -> str:
    event = summary.get("watchlist_event") or {}
    decision = summary.get("decision") or {}
    rows = [
        ("Result marker", summary.get("result_marker")),
        ("Decision", decision.get("reason")),
        ("Event type", event.get("event_type")),
        ("External person id", event.get("external_person_id")),
        ("Person id", event.get("person_id")),
        ("Source observation id", event.get("source_observation_id")),
        ("Original source observation id", event.get("original_source_observation_id")),
        ("Similarity", event.get("similarity")),
        ("Threshold", event.get("threshold")),
        ("Watchlist rule", event.get("watchlist_rule_id")),
        ("Runtime source", summary.get("runtime_source_status", {}).get("source_id")),
        ("Visual evidence joined", event.get("evidence", {}).get("visual_evidence_joined")),
        ("Event-style Replay passed", event.get("evidence", {}).get("event_style_replay_job_passed")),
    ]
    body = "\n".join(
        f"<tr><th>{html.escape(str(label))}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in rows
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>C2.12C Runtime Reese Finch Watchlist Probe</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #17202a; }}
    table {{ border-collapse: collapse; min-width: 760px; }}
    th, td {{ border: 1px solid #cbd3df; padding: 0.55rem 0.75rem; text-align: left; }}
    th {{ background: #eef3f8; width: 260px; }}
    .warning {{ color: #7a4100; font-weight: 700; }}
  </style>
</head>
<body>
  <h1>C2.12C Runtime Reese / Finch Watchlist Probe</h1>
  <table>{body}</table>
  <p class="warning">Event-style Replay is not passed. Visual evidence is only valid when a direct sidecar join is available.</p>
</body>
</html>
"""


def sanitize_observation_for_report(obs: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_observation_id": obs.get("source_observation_id"),
        "original_source_observation_id": _dict(obs.get("payload")).get("original_source_observation_id"),
        "camera_id": obs.get("camera_id"),
        "source_id": obs.get("source_id"),
        "track_id": obs.get("track_id"),
        "timestamp_ms": obs.get("timestamp_ms"),
        "frame_num": obs.get("frame_num"),
        "face_bbox": obs.get("face_bbox"),
        "landmarks": obs.get("landmarks"),
        "face_confidence": obs.get("face_confidence"),
        "quality": obs.get("quality"),
        "embedding_model": obs.get("embedding_model"),
        "embedding_dim": obs.get("embedding_dim"),
        "embedding_norm": obs.get("embedding_norm"),
        "payload": strip_unsafe_payload(obs.get("payload") or {}),
    }


def strip_unsafe_payload(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                continue
            out[key] = strip_unsafe_payload(nested)
        return out
    if isinstance(value, list):
        return [strip_unsafe_payload(item) for item in value]
    return value


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


def assert_no_unsafe_payload(value: Any) -> None:
    scan = scan_for_unsafe_payload(value)
    if not scan["passed"]:
        raise RuntimeError(f"unsafe_payload:{scan['forbidden_key_paths']}")


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


def _sanitize_db_row(row: dict[str, Any]) -> dict[str, Any]:
    clean = {}
    for key, value in row.items():
        if key.lower() == "embedding":
            continue
        if isinstance(value, bytes):
            clean[key] = f"<{len(value)} bytes omitted>"
        elif hasattr(value, "tolist"):
            clean[key] = "<vector omitted>"
        else:
            clean[key] = strip_unsafe_payload(value)
    return clean


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def safe_id_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "unknown"


def redact_redis_url(redis_client: redis.Redis) -> str:
    try:
        kwargs = redis_client.connection_pool.connection_kwargs
        host = kwargs.get("host", "unknown")
        port = kwargs.get("port", "unknown")
        db = kwargs.get("db", 0)
        return f"redis://{host}:{port}/{db}"
    except Exception:
        return "redis://<unknown>"


def _run_command(cmd: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=10)
    except Exception as exc:
        return {"returncode": 127, "stdout": "", "stderr": str(exc)}
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, default=str, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, default=str, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_decision_report(path: Path, summary: dict[str, Any]) -> None:
    decision = summary.get("decision") or {}
    best = decision.get("best_match") or {}
    distribution = summary.get("similarity_distribution", {}).get("targets", {})
    lines = [
        "# C2.12C Runtime Reese / Finch Capture Decision",
        "",
        f"Result marker: `{summary['result_marker']}`",
        f"Decision reason: `{decision.get('reason')}`",
        f"Runtime source_id: `{summary.get('runtime_source_status', {}).get('source_id')}`",
        f"Capture duration seconds: `{summary.get('capture', {}).get('duration_seconds')}`",
        f"New observations available in DB: `{summary.get('captured_observations_summary', {}).get('db_rows_available')}`",
        f"Threshold: `{summary.get('threshold')}`",
        "",
        "## Top Similarities",
        "",
        f"- Reese best: `{distribution.get('reese', {}).get('best_similarity')}`",
        f"- Finch best: `{distribution.get('finch', {}).get('best_similarity')}`",
        "",
        "## Best Match",
        "",
        f"- external_person_id: `{best.get('query_external_person_id')}`",
        f"- source_observation_id: `{best.get('source_observation_id')}`",
        f"- original_source_observation_id: `{best.get('original_source_observation_id')}`",
        f"- similarity: `{best.get('similarity')}`",
        f"- sidecar_join: `{(best.get('sidecar_join') or {}).get('joinable')}`",
        "",
        "## Recommendation",
        "",
    ]
    if summary["result_marker"] == RESULT_NO_MATCH:
        lines.extend(
            [
                "- Capture longer or seek the looped movie to a Reese/Finch scene.",
                "- Verify face quality and whether the current loop segment includes target faces.",
            ]
        )
    elif summary["result_marker"] == RESULT_JOIN_GAP:
        lines.extend(
            [
                "- A runtime Reese/Finch match was found, but no direct sidecar visual join exists.",
                "- Fix current runtime sidecar/evidence capture for this source_observation_id before claiming visual evidence.",
            ]
        )
    elif summary["result_marker"] in {RESULT_CAPTURE_GAP, RESULT_NOT_CONSUMING, RESULT_STREAM_EMPTY}:
        lines.extend(
            [
                "- Runtime capture did not produce usable new observations in the bounded window.",
                "- Inspect source-adapter/Savant face_obs_export and face-worker/DB consumer state.",
            ]
        )
    else:
        lines.append("- Package the runtime Reese/Finch evidence for C2.13.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clear_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", DEFAULT_REDIS_URL))
    parser.add_argument("--redis-stream", default=DEFAULT_REDIS_STREAM)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-redis-messages", type=int, default=DEFAULT_MAX_REDIS_MESSAGES)
    parser.add_argument("--backlog-messages", type=int, default=DEFAULT_BACKLOG_MESSAGES)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--watchlist-rule-id", default=DEFAULT_WATCHLIST_RULE_ID)
    parser.add_argument("--evidence-search-root", type=Path, default=DEFAULT_EVIDENCE_SEARCH_ROOT)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.run_id:
        run_id = args.run_id
    else:
        run_id = f"c2_12c_runtime_capture_search_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    output_dir = args.output_dir or args.evidence_root / run_id
    result = run_c2_12c_probe(
        output_dir=output_dir,
        database_url=args.database_url,
        redis_url=args.redis_url,
        redis_stream=args.redis_stream,
        timeout_seconds=args.timeout_seconds,
        max_redis_messages=args.max_redis_messages,
        backlog_messages=args.backlog_messages,
        threshold=args.threshold,
        top_k=args.top_k,
        watchlist_rule_id=args.watchlist_rule_id,
        evidence_search_root=args.evidence_search_root,
        overwrite=args.overwrite,
    )
    print(
        json.dumps(
            {
                "result_marker": result.result_marker,
                "output_dir": str(result.output_dir),
                "summary": str(result.summary_path),
                "source_id": result.summary.get("runtime_source_status", {}).get("source_id"),
                "db_before_count": result.summary.get("runtime_source_status", {}).get("db_before_count"),
                "db_after_count": result.summary.get("runtime_source_status", {}).get("db_after_count"),
                "new_observations": result.summary.get("captured_observations_summary", {}).get("db_rows_available"),
                "threshold": result.summary.get("threshold"),
                "decision": result.summary.get("decision"),
                "payload_has_embedding": result.summary.get("payload_has_embedding"),
                "payload_has_image_bytes": result.summary.get("payload_has_image_bytes"),
            },
            indent=2,
            default=str,
            sort_keys=True,
        )
    )
    accepted = {
        RESULT_PASS,
        RESULT_NO_MATCH,
        RESULT_CAPTURE_GAP,
        RESULT_STREAM_EMPTY,
        RESULT_NOT_CONSUMING,
        RESULT_JOIN_GAP,
    }
    return 0 if result.result_marker in accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
