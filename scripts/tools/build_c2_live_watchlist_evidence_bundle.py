#!/usr/bin/env python3
"""Build C2.6 evidence from the face-worker pgvector watchlist match path.

This tool uses the face-worker vector-store/gallery matching code against an
existing face_observation, records a real match_results row, builds a
watchlist_hit event, and patches an existing C2 identity evidence bundle with
live-watchlist semantics. It does not call Replay and does not require the live
Redis consumer loop.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = ROOT / "scripts" / "tools"
FACE_WORKER_ROOT = ROOT / "services" / "face-worker"
for path in (TOOLS_ROOT, FACE_WORKER_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import psycopg  # noqa: E402
from pgvector.psycopg import register_vector  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import build_c2_watchlist_evidence_bundle as c25  # noqa: E402
from app.face_match_event_service import build_watchlist_hit_event  # noqa: E402
from app.match_repository import MatchResultRepository  # noqa: E402
from app.vector_store import FaceVectorStore  # noqa: E402


RESULT_HARNESS_ONLY = "PARTIAL_C2_6_FACE_WORKER_HARNESS_ONLY"
RESULT_RULE_GAP = "PARTIAL_C2_6_WATCHLIST_RULE_INFRA_GAP"
RESULT_FAIL = "FAIL_C2_6_LIVE_WATCHLIST_BLOCKED"
EVENT_TYPE = "watchlist_hit"
LIVE_EVENT_FILE = "live_watchlist_event.json"
C2_6_SUMMARY_FILE = "c2_6_live_watchlist_summary.json"
DEFAULT_DATABASE_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_SOURCE_ID = "c2_post_savant_fps_probe"
DEFAULT_SOURCE_OBSERVATION_ID = "face:c2_post_savant_fps_probe:4:17854:1"
DEFAULT_EXTERNAL_PERSON_ID = "test:c2_4:person"
DEFAULT_WATCHLIST_RULE_ID = "c2_6_test_watchlist_rule"
DEFAULT_WATCHLIST_RULE_NAME = "C2.6 Test Watchlist Rule"
DEFAULT_SEARCH_REQUEST_ID = "c2600000-0000-4000-8000-000000000001"
DEFAULT_THRESHOLD = 0.99

FORBIDDEN_EVENT_KEYS = c25.FORBIDDEN_EVENT_KEYS | {
    "crop",
    "crop_image",
    "image",
    "embedding_list",
}


@dataclass(frozen=True)
class FaceWorkerMatch:
    observation: dict[str, Any]
    gallery_match: dict[str, Any]
    match_result_id: int
    search_request_id: str
    threshold: float
    watchlist_rule_id: str
    watchlist_rule_name: str
    watchlist_rule_source: str


@dataclass(frozen=True)
class BuildResult:
    result_marker: str
    input_bundle: Path
    output_bundle: Path
    live_event_path: Path
    watchlist_event_path: Path
    c2_6_summary_path: Path
    summary_path: Path
    sidecar_path: Path
    live_event: dict[str, Any]
    c2_6_summary: dict[str, Any]
    summary: dict[str, Any]


def build_live_watchlist_evidence_bundle(
    *,
    input_bundle: Path,
    output_dir: Path,
    database_url: str,
    source_observation_id: str,
    external_person_id: str,
    watchlist_rule_id: str,
    watchlist_rule_name: str,
    threshold: float,
    search_request_id: str,
    top_k: int = 1,
    source_id: str = DEFAULT_SOURCE_ID,
    overwrite: bool = False,
) -> BuildResult:
    input_bundle = input_bundle.resolve(strict=False)
    output_dir = output_dir.resolve(strict=False)
    c25._require_input_bundle(input_bundle)

    sidecar_rows = c25._read_jsonl(input_bundle / c25.SIDECAR_FILE)
    input_summary = c25._read_json(input_bundle / c25.SUMMARY_FILE)
    identity_patches = c25._read_jsonl(input_bundle / c25.IDENTITY_PATCHES_FILE)
    c25._validate_input_summary(input_summary)

    with psycopg.connect(database_url, autocommit=True) as conn:
        register_vector(conn)
        match = run_face_worker_match(
            conn=conn,
            source_observation_id=source_observation_id,
            external_person_id=external_person_id,
            watchlist_rule_id=watchlist_rule_id,
            watchlist_rule_name=watchlist_rule_name,
            threshold=threshold,
            search_request_id=search_request_id,
            top_k=top_k,
            source_id=source_id,
        )

    person_id = int(match.gallery_match["person_id"])
    gallery_embedding_id = int(match.gallery_match["id"])
    binding = c25.find_known_face_binding(
        rows=sidecar_rows,
        identity_patches=identity_patches,
        person_id=person_id,
        external_person_id=external_person_id,
        source_observation_id=source_observation_id,
        threshold=threshold,
    )
    if binding is None:
        raise RuntimeError("input_sidecar_known_face_binding_missing")

    live_event = build_c2_6_live_event(
        match=match,
        input_bundle=input_bundle,
        output_bundle=output_dir,
        binding=binding,
    )
    assert_no_forbidden_live_event_payload(live_event)

    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        if output_dir.is_dir():
            shutil.rmtree(output_dir)
        else:
            output_dir.unlink()
    shutil.copytree(input_bundle, output_dir)

    patched_rows = patch_c2_6_sidecar_fields(
        sidecar_rows,
        binding=binding,
        live_event=live_event,
        watchlist_rule_id=watchlist_rule_id,
        watchlist_rule_name=watchlist_rule_name,
        severity=str(live_event.get("severity") or "high"),
    )
    summary = patch_c2_6_summary(
        input_summary,
        rows=patched_rows,
        binding=binding,
        live_event=live_event,
        match=match,
    )
    c2_6_summary = build_c2_6_summary(
        input_bundle=input_bundle,
        output_bundle=output_dir,
        summary=summary,
        binding=binding,
        live_event=live_event,
        match=match,
    )
    validate_c2_6_output(summary=summary, live_event=live_event, c2_6_summary=c2_6_summary)

    sidecar_path = output_dir / c25.SIDECAR_FILE
    summary_path = output_dir / c25.SUMMARY_FILE
    sidecar_summary_path = output_dir / c25.SIDECAR_SUMMARY_FILE
    watchlist_event_path = output_dir / c25.WATCHLIST_EVENT_FILE
    live_event_path = output_dir / LIVE_EVENT_FILE
    c2_6_summary_path = output_dir / C2_6_SUMMARY_FILE
    watchlist_summary_path = output_dir / c25.WATCHLIST_SUMMARY_FILE

    c25._write_jsonl(sidecar_path, patched_rows)
    c25._write_json(summary_path, summary)
    c25._write_json(sidecar_summary_path, summary)
    c25._write_json(watchlist_event_path, live_event)
    c25._write_json(live_event_path, live_event)
    c25._write_json(c2_6_summary_path, c2_6_summary)
    c25._write_json(watchlist_summary_path, c2_6_summary)

    return BuildResult(
        result_marker=RESULT_HARNESS_ONLY,
        input_bundle=input_bundle,
        output_bundle=output_dir,
        live_event_path=live_event_path,
        watchlist_event_path=watchlist_event_path,
        c2_6_summary_path=c2_6_summary_path,
        summary_path=summary_path,
        sidecar_path=sidecar_path,
        live_event=live_event,
        c2_6_summary=c2_6_summary,
        summary=summary,
    )


def run_face_worker_match(
    *,
    conn: psycopg.Connection,
    source_observation_id: str,
    external_person_id: str,
    watchlist_rule_id: str,
    watchlist_rule_name: str,
    threshold: float,
    search_request_id: str,
    top_k: int,
    source_id: str,
) -> FaceWorkerMatch:
    observation = fetch_face_observation(conn, source_observation_id)
    if observation is None:
        raise RuntimeError(f"face_observation_missing:{source_observation_id}")
    if observation.get("source_id") != source_id:
        raise RuntimeError(f"face_observation_source_id_mismatch:{observation.get('source_id')}")
    embedding = observation_to_embedding(observation)
    person = fetch_person_by_external_id(conn, external_person_id)
    if person is None:
        raise RuntimeError(f"person_missing:{external_person_id}")
    person_id = int(person["id"])

    store = FaceVectorStore(conn)
    results = store.search_gallery(
        embedding,
        top_k=max(top_k, 1),
        min_similarity=threshold,
        person_ids=[person_id],
    )
    if not results:
        raise RuntimeError("face_worker_pgvector_match_missing")
    best = dict(results[0])
    if int(best["person_id"]) != person_id:
        raise RuntimeError("face_worker_match_person_mismatch")
    if str(best.get("external_person_id") or "") != external_person_id:
        raise RuntimeError("face_worker_match_external_person_mismatch")
    if float(best["similarity"]) < threshold:
        raise RuntimeError("face_worker_match_below_threshold")

    match_result_id = upsert_match_result(
        conn=conn,
        observation=observation,
        gallery_match=best,
        threshold=threshold,
        search_request_id=search_request_id,
    )
    return FaceWorkerMatch(
        observation=observation,
        gallery_match=best,
        match_result_id=match_result_id,
        search_request_id=search_request_id,
        threshold=threshold,
        watchlist_rule_id=watchlist_rule_id,
        watchlist_rule_name=watchlist_rule_name,
        watchlist_rule_source="c2_6_synthetic_file_rule",
    )


def fetch_face_observation(conn: psycopg.Connection, source_observation_id: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, source_observation_id, camera_id, source_id, track_id,
                   timestamp_ms, face_bbox, landmarks, face_confidence, quality,
                   person_bbox, snapshot_path, crop_path, embedding,
                   embedding_model, embedding_dim, embedding_norm, payload
            FROM face_observations
            WHERE source_observation_id = %(source_observation_id)s
            """,
            {"source_observation_id": source_observation_id},
        )
        row = cur.fetchone()
    return dict(row) if row else None


def fetch_person_by_external_id(conn: psycopg.Connection, external_person_id: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, name, external_person_id, is_active
            FROM persons
            WHERE external_person_id = %(external_person_id)s
              AND is_active = true
            """,
            {"external_person_id": external_person_id},
        )
        row = cur.fetchone()
    return dict(row) if row else None


def observation_to_embedding(observation: dict[str, Any]) -> list[float]:
    embedding = observation.get("embedding")
    if embedding is None:
        raise RuntimeError("face_observation_embedding_missing")
    if hasattr(embedding, "tolist"):
        return [float(value) for value in embedding.tolist()]
    return [float(value) for value in embedding]


def upsert_match_result(
    *,
    conn: psycopg.Connection,
    observation: dict[str, Any],
    gallery_match: dict[str, Any],
    threshold: float,
    search_request_id: str,
) -> int:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    repo = MatchResultRepository(conn)
    row = {
        "search_request_id": search_request_id,
        "search_mode": "c2_6_watchlist_match",
        "query_observation_id": observation["id"],
        "query_source_observation_id": observation["source_observation_id"],
        "query_person_id": gallery_match.get("person_id"),
        "query_gallery_embedding_id": gallery_match["id"],
        "query_embedding_model": gallery_match.get("embedding_model", "adaface"),
        "similarity_threshold": threshold,
        "rank": 1,
        "similarity": gallery_match["similarity"],
        "face_confidence": observation.get("face_confidence"),
        "quality": observation.get("quality"),
        "expires_at": expires_at,
        "payload": {
            "phase": "C2.6",
            "identity_source": "face_worker_pgvector_match",
            "watchlist_match_source": "face_worker_shared_matching_logic",
        },
    }
    inserted_id = repo.insert_gallery_match_result(row)
    if inserted_id is not None:
        return int(inserted_id)
    rows = repo.get_by_search_request(search_request_id)
    for existing in rows:
        if int(existing.get("query_gallery_embedding_id") or 0) == int(gallery_match["id"]):
            return int(existing["id"])
    raise RuntimeError("match_result_insert_or_lookup_failed")


def build_c2_6_live_event(
    *,
    match: FaceWorkerMatch,
    input_bundle: Path,
    output_bundle: Path,
    binding: c25.KnownFaceBinding,
) -> dict[str, Any]:
    event = build_watchlist_hit_event(
        observation=match.observation,
        gallery_match=match.gallery_match,
        threshold=match.threshold,
        severity="high",
    )
    source_observation_id = str(match.observation["source_observation_id"])
    person_id = int(match.gallery_match["person_id"])
    gallery_embedding_id = int(match.gallery_match["id"])
    event_ts_ms = c25._event_ts_ms(binding) or int(match.observation.get("timestamp_ms") or 0)
    frame_pts = binding.frame_pts or _media_value(match.observation, "frame_pts")
    frame_num = binding.frame_index if binding.frame_index is not None else _media_value(match.observation, "frame_num")
    event.update(
        {
            "schema_version": "1.0",
            "event_type": EVENT_TYPE,
            "source_event_id": f"c2_6:watchlist_hit:{source_observation_id}:{person_id}",
            "producer": "c2_6_face_worker_harness",
            "source_observation_id": source_observation_id,
            "person_id": person_id,
            "external_person_id": match.gallery_match.get("external_person_id"),
            "person_name": match.gallery_match.get("person_name"),
            "gallery_embedding_id": gallery_embedding_id,
            "match_result_id": match.match_result_id,
            "search_request_id": match.search_request_id,
            "similarity": float(match.gallery_match["similarity"]),
            "threshold": float(match.threshold),
            "watchlist_rule_id": match.watchlist_rule_id,
            "watchlist_rule_name": match.watchlist_rule_name,
            "watchlist_rule_source": match.watchlist_rule_source,
            "event_ts_ms": event_ts_ms,
            "start_ts_ms": event_ts_ms,
            "end_ts_ms": event_ts_ms,
            "frame_pts": frame_pts,
            "frame_num": frame_num,
            "frame_id": frame_num,
            "track_id": str(binding.track_id or match.observation.get("track_id") or ""),
            "evidence_bundle": str(output_bundle),
            "input_identity_bundle": str(input_bundle),
            "face_worker_execution_mode": "shared_matching_harness",
            "face_worker_consumer_loop_exercised": False,
            "redis_event_published": False,
        }
    )
    payload = c25._dict(event.get("payload"))
    payload.update(
        {
            "identity_source": "face_worker_pgvector_match",
            "watchlist_match_source": "face_worker_shared_matching_logic",
            "identity_binding_status": "matched",
            "source_observation_id": source_observation_id,
            "gallery_embedding_id": gallery_embedding_id,
            "match_result_id": match.match_result_id,
            "similarity": float(match.gallery_match["similarity"]),
            "threshold": float(match.threshold),
            "watchlist_rule_id": match.watchlist_rule_id,
            "watchlist_rule_source": match.watchlist_rule_source,
            "embedding_included": False,
            "image_bytes_included": False,
            "crop_bytes_included": False,
            "evidence_capture_mode": "stable_post_savant_sink_time_crop",
            "workaround_used": True,
            "event_style_replay_job_passed": False,
            "geometry_source": "post_savant_sidecar",
            "geometry_modified": False,
            "join_method": binding.join_method,
            "event_sink": "file_only",
        }
    )
    event["payload"] = payload
    return event


def patch_c2_6_summary(
    summary: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    binding: c25.KnownFaceBinding,
    live_event: dict[str, Any],
    match: FaceWorkerMatch,
) -> dict[str, Any]:
    patched = c25.patch_summary(
        summary,
        rows=rows,
        binding=binding,
        watchlist_event=live_event,
        watchlist_rule_id=match.watchlist_rule_id,
        watchlist_rule_name=match.watchlist_rule_name,
        severity=str(live_event.get("severity") or "high"),
    )
    patched.update(
        {
            "source_event_id": live_event["source_event_id"],
            "live_watchlist_from_face_worker": True,
            "face_worker_match_verified": True,
            "face_worker_execution_mode": "shared_matching_harness",
            "face_worker_consumer_loop_exercised": False,
            "redis_event_published": False,
            "event_producer": live_event["producer"],
            "watchlist_rule_source": match.watchlist_rule_source,
            "watchlist_rule_infra_status": "synthetic_file_rule",
            "identity_source": "face_worker_pgvector_match",
            "identity_patch_source": "match_results",
            "match_result_id": match.match_result_id,
            "search_request_id": match.search_request_id,
            "live_watchlist_event_file": LIVE_EVENT_FILE,
            "c2_6_live_watchlist_summary_file": C2_6_SUMMARY_FILE,
        }
    )
    limitations = list(patched.get("limitations") or [])
    for item in (
        "face_worker_shared_matching_harness_only",
        "redis_consumer_loop_not_exercised",
        "synthetic_c2_6_watchlist_rule_contract",
        "event_style_replay_not_production_ready_stable_sink_workaround",
    ):
        if item not in limitations:
            limitations.append(item)
    patched["limitations"] = limitations
    return patched


def patch_c2_6_sidecar_fields(
    rows: list[dict[str, Any]],
    *,
    binding: c25.KnownFaceBinding,
    live_event: dict[str, Any],
    watchlist_rule_id: str,
    watchlist_rule_name: str,
    severity: str,
) -> list[dict[str, Any]]:
    patched = c25.patch_sidecar_watchlist_fields(
        rows,
        binding=binding,
        watchlist_event=live_event,
        watchlist_rule_id=watchlist_rule_id,
        watchlist_rule_name=watchlist_rule_name,
        severity=severity,
    )
    obj = patched[binding.row_index]["objects"][binding.object_index]
    original_geometry = c25._geometry_snapshot(obj)
    identity = c25._dict(obj.get("identity"))
    identity.update(
        {
            "identity_source": "face_worker_pgvector_match",
            "watchlist_match_source": "face_worker_shared_matching_logic",
            "match_result_id": live_event["match_result_id"],
            "gallery_embedding_id": live_event["gallery_embedding_id"],
            "similarity": live_event["similarity"],
            "threshold": live_event["threshold"],
            "search_request_id": live_event.get("search_request_id"),
            "source_event_id": live_event["source_event_id"],
            "watchlist_rule_id": watchlist_rule_id,
            "watchlist_rule_name": watchlist_rule_name,
            "watchlist_hit_status": "matched",
            "visual_evidence_status": "live_watchlist_hit_identity_bound",
        }
    )
    obj["identity"] = identity
    label = c25._dict(obj.get("label"))
    label.update(
        {
            "match_result_id": live_event["match_result_id"],
            "gallery_embedding_id": live_event["gallery_embedding_id"],
            "similarity": live_event["similarity"],
            "threshold": live_event["threshold"],
            "source_event_id": live_event["source_event_id"],
            "watchlist_rule_id": watchlist_rule_id,
            "watchlist_rule_name": watchlist_rule_name,
        }
    )
    obj["label"] = label
    if c25._geometry_snapshot(obj) != original_geometry:
        raise RuntimeError("c2_6_sidecar_patch_modified_geometry")
    return patched


def build_c2_6_summary(
    *,
    input_bundle: Path,
    output_bundle: Path,
    summary: dict[str, Any],
    binding: c25.KnownFaceBinding,
    live_event: dict[str, Any],
    match: FaceWorkerMatch,
) -> dict[str, Any]:
    return {
        "result_marker": RESULT_HARNESS_ONLY,
        "input_bundle": str(input_bundle),
        "output_bundle": str(output_bundle),
        "event_type": EVENT_TYPE,
        "source_event_id": live_event.get("source_event_id"),
        "producer": live_event.get("producer"),
        "source_observation_id": live_event.get("source_observation_id"),
        "camera_id": live_event.get("camera_id"),
        "source_id": live_event.get("source_id"),
        "track_id": live_event.get("track_id"),
        "frame_num": live_event.get("frame_num"),
        "frame_pts": live_event.get("frame_pts"),
        "person_id": live_event.get("person_id"),
        "external_person_id": live_event.get("external_person_id"),
        "gallery_embedding_id": live_event.get("gallery_embedding_id"),
        "match_result_id": live_event.get("match_result_id"),
        "search_request_id": match.search_request_id,
        "similarity": live_event.get("similarity"),
        "threshold": live_event.get("threshold"),
        "watchlist_rule_id": live_event.get("watchlist_rule_id"),
        "watchlist_rule_name": live_event.get("watchlist_rule_name"),
        "watchlist_rule_source": match.watchlist_rule_source,
        "producer_path": "services/face-worker/app/face_match_event_service.py + services/face-worker/app/vector_store.py",
        "face_worker_execution_mode": summary.get("face_worker_execution_mode"),
        "face_worker_match_verified": summary.get("face_worker_match_verified"),
        "live_watchlist_from_face_worker": summary.get("live_watchlist_from_face_worker"),
        "face_worker_consumer_loop_exercised": summary.get("face_worker_consumer_loop_exercised"),
        "redis_event_published": summary.get("redis_event_published"),
        "join_method": binding.join_method,
        "known_face_count": summary.get("known_face_count"),
        "watchlist_hit_count": summary.get("watchlist_hit_count"),
        "unknown_face_count": summary.get("unknown_face_count"),
        "production_ready": summary.get("production_ready"),
        "video_integrity": summary.get("video_integrity"),
        "fallback_used": summary.get("fallback_used"),
        "legacy_used_for_visual_binding": summary.get("legacy_used_for_visual_binding"),
        "allow_db_annotation_fallback": summary.get("allow_db_annotation_fallback"),
        "allow_legacy_annotation_fallback": summary.get("allow_legacy_annotation_fallback"),
        "evidence_capture_mode": summary.get("evidence_capture_mode"),
        "workaround_used": summary.get("workaround_used"),
        "event_style_replay_job_passed": summary.get("event_style_replay_job_passed"),
        "limitations": summary.get("limitations") or [],
    }


def validate_c2_6_output(
    *,
    summary: dict[str, Any],
    live_event: dict[str, Any],
    c2_6_summary: dict[str, Any],
) -> None:
    assert_no_forbidden_live_event_payload(live_event)
    if live_event.get("event_type") != EVENT_TYPE:
        raise RuntimeError("live_event_type_invalid")
    if live_event.get("producer") not in {"face-worker", "c2_6_face_worker_harness"}:
        raise RuntimeError("live_event_producer_invalid")
    for key in (
        "source_observation_id",
        "person_id",
        "gallery_embedding_id",
        "match_result_id",
        "similarity",
        "threshold",
        "watchlist_rule_id",
    ):
        if live_event.get(key) in (None, ""):
            raise RuntimeError(f"live_event_missing_{key}")
    for key in ("person_id", "gallery_embedding_id", "match_result_id"):
        if int(live_event.get(key) or 0) <= 0:
            raise RuntimeError(f"live_event_invalid_{key}")
    payload = c25._dict(live_event.get("payload"))
    if payload.get("identity_source") != "face_worker_pgvector_match":
        raise RuntimeError("live_event_identity_source_invalid")
    if payload.get("watchlist_match_source") != "face_worker_shared_matching_logic":
        raise RuntimeError("live_event_watchlist_match_source_invalid")
    if payload.get("embedding_included") is not False:
        raise RuntimeError("live_event_embedding_included")
    if payload.get("image_bytes_included") is not False:
        raise RuntimeError("live_event_image_bytes_included")
    if payload.get("event_style_replay_job_passed") is not False:
        raise RuntimeError("live_event_replay_status_invalid")
    if int(summary.get("known_face_count") or 0) <= 0:
        raise RuntimeError("known_face_count_positive_required")
    if int(summary.get("watchlist_hit_count") or 0) != 1:
        raise RuntimeError("watchlist_hit_count_must_be_1")
    if summary.get("live_watchlist_from_face_worker") is not True:
        raise RuntimeError("summary_live_watchlist_from_face_worker_required")
    if summary.get("face_worker_match_verified") is not True:
        raise RuntimeError("summary_face_worker_match_verified_required")
    if summary.get("evidence_capture_mode") != "stable_post_savant_sink_time_crop":
        raise RuntimeError("summary_capture_mode_invalid")
    if summary.get("event_style_replay_job_passed") is not False:
        raise RuntimeError("summary_replay_status_invalid")
    if summary.get("fallback_used") is not False:
        raise RuntimeError("summary_fallback_used")
    if summary.get("legacy_used_for_visual_binding") is not False:
        raise RuntimeError("summary_legacy_used")
    if summary.get("production_ready") is not True:
        raise RuntimeError("summary_production_ready_required")
    if c25._dict(summary.get("video_integrity")).get("production_gate_passed") is not True:
        raise RuntimeError("summary_video_integrity_gate_required")
    if c2_6_summary.get("result_marker") != RESULT_HARNESS_ONLY:
        raise RuntimeError("c2_6_summary_marker_invalid")


def assert_no_forbidden_live_event_payload(event: dict[str, Any]) -> None:
    hits = sorted(set(_find_forbidden_keys(event)))
    if hits:
        raise RuntimeError(f"live_watchlist_event_contains_forbidden_payload:{','.join(hits)}")


def _find_forbidden_keys(value: Any, *, parent_key: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_EVENT_KEYS:
                yield parent_key + str(key)
            yield from _find_forbidden_keys(nested, parent_key=parent_key + str(key) + ".")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _find_forbidden_keys(nested, parent_key=f"{parent_key}{index}.")


def _media_value(observation: dict[str, Any], key: str) -> Any:
    payload = c25._dict(observation.get("payload"))
    media = c25._dict(payload.get("media"))
    return media.get(key)


def _result_payload(result: BuildResult) -> dict[str, Any]:
    return {
        "result_marker": result.result_marker,
        "input_bundle": str(result.input_bundle),
        "output_bundle": str(result.output_bundle),
        "live_watchlist_event_path": str(result.live_event_path),
        "watchlist_event_path": str(result.watchlist_event_path),
        "c2_6_live_watchlist_summary_path": str(result.c2_6_summary_path),
        "summary_path": str(result.summary_path),
        "sidecar_path": str(result.sidecar_path),
        "source_event_id": result.live_event.get("source_event_id"),
        "producer": result.live_event.get("producer"),
        "source_observation_id": result.live_event.get("source_observation_id"),
        "camera_id": result.live_event.get("camera_id"),
        "source_id": result.live_event.get("source_id"),
        "track_id": result.live_event.get("track_id"),
        "frame_num": result.live_event.get("frame_num"),
        "frame_pts": result.live_event.get("frame_pts"),
        "person_id": result.live_event.get("person_id"),
        "external_person_id": result.live_event.get("external_person_id"),
        "gallery_embedding_id": result.live_event.get("gallery_embedding_id"),
        "match_result_id": result.live_event.get("match_result_id"),
        "similarity": result.live_event.get("similarity"),
        "threshold": result.live_event.get("threshold"),
        "watchlist_rule_id": result.live_event.get("watchlist_rule_id"),
        "known_face_count": result.summary.get("known_face_count"),
        "watchlist_hit_count": result.summary.get("watchlist_hit_count"),
        "live_watchlist_from_face_worker": result.summary.get("live_watchlist_from_face_worker"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-bundle", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--source-observation-id", default=DEFAULT_SOURCE_OBSERVATION_ID)
    parser.add_argument("--external-person-id", default=DEFAULT_EXTERNAL_PERSON_ID)
    parser.add_argument("--watchlist-rule-id", default=DEFAULT_WATCHLIST_RULE_ID)
    parser.add_argument("--watchlist-rule-name", default=DEFAULT_WATCHLIST_RULE_NAME)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--search-request-id", default=DEFAULT_SEARCH_REQUEST_ID)
    parser.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
    parser.add_argument("--top-k", type=int, default=1)
    parser.add_argument("--overwrite", action="store_true", default=False)
    args = parser.parse_args(argv)

    try:
        result = build_live_watchlist_evidence_bundle(
            input_bundle=args.input_bundle,
            output_dir=args.output_dir,
            database_url=args.database_url,
            source_observation_id=args.source_observation_id,
            external_person_id=args.external_person_id,
            watchlist_rule_id=args.watchlist_rule_id,
            watchlist_rule_name=args.watchlist_rule_name,
            threshold=args.threshold,
            search_request_id=args.search_request_id,
            top_k=args.top_k,
            source_id=args.source_id,
            overwrite=args.overwrite,
        )
    except Exception as exc:
        payload = {
            "result_marker": RESULT_FAIL,
            "reason": f"{type(exc).__name__}:{exc}",
            "input_bundle": str(args.input_bundle),
            "output_bundle": str(args.output_dir),
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2

    print(json.dumps(_result_payload(result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
